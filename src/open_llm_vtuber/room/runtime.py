"""Character runtimes: one agent (persona, own LLM client) per room character.

A turn reuses the proven single-character pipeline unchanged
(``process_single_conversation``: streamed LLM, sentence splitting, parallel
TTS, ordered audio, wait for playback). Only the context differs: the
character's own agent, voice and emotion map, and every audio payload is
tagged with the character id so the right model lip syncs.

Agents are created lazily on a character's first turn. Creating an agent
makes no API call; only a turn does.
"""

from __future__ import annotations

import re

import asyncio
import contextlib
import json
import os
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from . import events as ev
from .coding_actions import (
    DECIDE_SYSTEM,
    DIRECTOR_INTENT,
    CodingDecision,
    generate_system,
    parse_code,
    parse_decision,
)
from .capabilities import KINDS, kind_for, writer_context
from .coding_lesson import MAX_CODE_BYTES, RestrictedScriptRunner
from .code_model import code_model_from_env
from .coding_mood import (
    FACE_REFRESH_SECONDS,
    FIDGETS,
    FOCUS_AFTER_SECONDS,
    TIRED_AFTER_SECONDS,
    LinePicker,
    face_for,
    motion_for,
)
from .coding_worker import CodingJob, CodingWorker
from .prompting import strip_wrapping_quotes, system_prompt
from .stage_design import stage_preview_html

if TYPE_CHECKING:  # pragma: no cover
    from .director import InteractionPlan, Turn
    from .session import RoomSession


def _program_preview(code: str, max_lines: int = 18, max_chars: int = 900) -> str:
    """The top of the program: titles, imports, the first data and comments."""
    lines = [line.rstrip() for line in str(code or "").splitlines() if line.strip()]
    return "\n".join(lines[:max_lines])[:max_chars]


def resolve_program_target(
    decision: CodingDecision, existing_language: str, existing_kind: str = ""
) -> tuple[str, bool]:
    """(language, fresh) for a coding decision, from structure only.

    * A change keeps the program's language unless the viewer named another.
    * Asking for a different language than the one on screen is always a new
      program, because one file cannot be turned into the other.
    * With nothing on screen and no language named, Python is the default.
    """
    fresh = bool(decision.fresh)
    language = (
        KINDS[decision.kind].language
        if decision.kind in KINDS
        else decision.language or existing_language or "python"
    )
    if decision.writes_code and existing_language and language != existing_language:
        fresh = True
    # "create" means a new program. Building it on top of the one on screen
    # turned "make a snake game" into a re-skinned space shooter.
    if existing_language and decision.action in ("create", "create_and_run"):
        fresh = True
    # A web program built from a different starting template is a different
    # kind of thing (shooter -> snake): it cannot be an edit of this one.
    if (
        decision.writes_code
        and existing_kind
        and decision.kind
        and decision.kind != existing_kind
        and KINDS.get(decision.kind)
        and KINDS.get(existing_kind)
        and KINDS[decision.kind].language == "web"
        and KINDS[decision.kind].template != KINDS[existing_kind].template
    ):
        fresh = True
    return language, fresh


LLM_ERROR_PREFIX = "Error calling the chat endpoint"


def is_llm_error_text(text: Any) -> bool:
    return isinstance(text, str) and text.strip().startswith(LLM_ERROR_PREFIX)


class RoomRuntimes:
    def __init__(
        self, session: "RoomSession", base_context: Any, client_uid: str, send
    ):
        self.session = session
        self.base = base_context
        self.client_uid = client_uid
        self.send = send
        self._agents: dict[str, Any] = {}
        self._models: dict[str, Any] = {}
        self._coding_runner = RestrictedScriptRunner()
        # Every change to the live source goes through this one worker, so
        # two comments can never write the file at the same time.
        self.coding_worker = CodingWorker(self._execute_job)
        self.last_decision: Optional[CodingDecision] = None
        self.last_decision_seconds = 0.0
        self.last_generation_seconds = 0.0
        # Optional real-browser check for web programs (browser_qa.BrowserQA
        # .check). None means web programs are shown without being checked.
        self.browser_check = None
        # Optional trusted publishing service (publishing.PublicationService).
        # None, or disabled by its own flags: nothing is ever published.
        self.publisher = None
        self.last_screenshot: Optional[bytes] = None
        # Repair passes after a failed check or run: one by default, never a loop.
        self.max_repairs = max(
            0, min(2, int(os.environ.get("VR_CODE_REPAIRS", "1") or 1))
        )
        self.errors: dict[str, str] = {}
        # Faces and short lines about the code (no LLM): see coding_mood.
        self.mood_lines = LinePicker()
        # Optional stronger model for writing code only (VR_CODE_MODEL).
        self.code_model = code_model_from_env()
        if self.code_model is not None:
            logger.info(f"VR Room: code is written by {self.code_model.model}")
        # Automatic publishing runs after the build; DEV shows its outcome.
        self.publish_task: Optional[asyncio.Task] = None
        self.publish_events: list[dict[str, Any]] = []
        self._mood_speech: Optional[asyncio.Task] = None
        self.mood_timing = (
            FOCUS_AFTER_SECONDS,
            TIRED_AFTER_SECONDS,
            FACE_REFRESH_SECONDS,
        )

    def retarget(self, client_uid: str, send) -> None:
        self.client_uid = client_uid
        self.send = send

    # ------------------------------------------------------------------
    def _live2d(self, character_id: str):
        if character_id in self._models:
            return self._models[character_id]
        from ..live2d_model import Live2dModel

        profile = self.session.room.get(character_id)
        model = Live2dModel(profile.model)
        # Emotion tags map to the character's profile emotions; the room page
        # plays them through its registry (emotion_mode "profile").
        model.emo_map = profile.emotion_index_map()
        model.emo_str = " ".join(f"[{k}]," for k in model.emo_map)
        self._models[character_id] = model
        return model

    def agent(self, character_id: str):
        if character_id in self._agents:
            return self._agents[character_id]
        from ..agent.agent_factory import AgentFactory

        profile = self.session.room.get(character_id)
        base_agent = self.base.character_config.agent_config
        settings = base_agent.agent_settings.model_dump()
        memory = dict(settings.get("basic_memory_agent") or {})
        memory["use_mcpp"] = False  # viewer text never drives tools in the room
        settings["basic_memory_agent"] = memory
        try:
            agent = AgentFactory.create_agent(
                conversation_agent_choice="basic_memory_agent",
                agent_settings=settings,
                llm_configs=base_agent.llm_configs.model_dump(),
                system_prompt=system_prompt(profile, self.session.room),
                live2d_model=self._live2d(character_id),
                tts_preprocessor_config=self.base.character_config.tts_preprocessor_config,
                character_avatar="",
                system_config=self.base.system_config.model_dump(),
                tool_manager=None,
                tool_executor=None,
                mcp_prompt_string="",
            )
        except Exception as exc:
            self.errors[character_id] = str(exc)[:200]
            logger.error(f"VR Room: agent for {character_id} unavailable: {exc}")
            agent = None
        self._agents[character_id] = agent
        return agent

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # The one semantic decision, and the code generation it may ask for.
    #
    # Deciding is cheap and happens for every comment. Writing code is
    # expensive and happens only when the decision actually asks for code,
    # so ordinary conversation never pays for code generation.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Chat while a build runs: one short spoken line, never a silent void.
    # ------------------------------------------------------------------
    @property
    def building(self) -> bool:
        return self.coding_worker.busy

    async def side_reply(self, viewer: str, text: str, wants_build: bool) -> bool:
        """Answer a chat message while the code is being written or fixed.

        The character who is NOT coding answers (Luna talks while Mika codes),
        in one short line, without touching the build. A new build request is
        told it is next in line; the message stays queued for after this one.
        """
        from .speech import INSIDE_INTERACTION

        teaching = self.session.teaching.session
        coder = teaching.teacher
        cast = [c.id for c in self.session.room.characters]
        speaker = next((c for c in cast if c != coder), coder)
        profile = self.session.room.get(speaker)
        coder_profile = self.session.room.get(coder)
        if profile is None:
            return False
        name = profile.name
        coder_name = coder_profile.name if coder_profile else coder.title()
        who = str(viewer or "chat").lstrip("@")[:40]
        what = (teaching.goal or "a program")[:120]
        for_whom = (teaching.student_name or "a viewer").lstrip("@")[:40]
        if speaker == coder:
            situation = f"You are busy coding {what} for {for_whom} right now."
        else:
            situation = f"{coder_name} is busy coding {what} for {for_whom} right now, so you keep chat company."
        rule = (
            "They want something built: say warmly that theirs is next in line "
            "right after this build."
            if wants_build
            else "Answer what they said directly."
        )
        system = (
            f"You are {name}, a VTuber on a live coding stream.\n"
            f"Your personality: {profile.persona[:700]}\n"
            f"{situation}\n"
            f"Reply to the viewer in ONE short spoken sentence, at most 22 words, "
            f"in character. {rule} No emojis, no code, no lists, no questions back "
            "unless they asked you one."
        )
        agent = self.agent(speaker)
        llm = getattr(agent, "_llm", None)
        if llm is None:
            return False
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{who} says: {str(text or '')[:300]}"},
        ]
        try:
            line = await asyncio.wait_for(self._complete(llm, messages), timeout=10)
        except Exception as exc:  # a slow or failed reply is skipped, never spoken as an error
            logger.debug(f"Side reply skipped: {exc}")
            return False
        line = " ".join(str(line or "").split())[:240]
        if not line or line.startswith(LLM_ERROR_PREFIX):
            return False
        # Inside the build's interaction: take only the voice, never the
        # interaction lock the build is holding.
        INSIDE_INTERACTION.set(True)
        self.session.trace("side_reply", character=speaker, user=who, queued=wants_build)
        return await self.session.speech.say(speaker, line, addressee=who)

    async def _complete(self, llm, messages: list[dict[str, Any]]) -> str:
        """One completion as plain text, whatever shape the client streams."""
        try:
            stream = llm.chat_completion(messages)
        except TypeError:
            stream = llm.chat_completion(messages=messages)
        if asyncio.iscoroutine(stream):
            stream = await stream
        if isinstance(stream, str):
            return stream
        chunks: list[str] = []

        def take(chunk: Any) -> None:
            if isinstance(chunk, str):
                chunks.append(chunk)
                return
            if not isinstance(chunk, dict):
                return
            value = chunk.get("text") or chunk.get("content")
            if isinstance(value, str):
                chunks.append(value)
                return
            for choice in chunk.get("choices") or []:
                try:
                    value = (choice.get("delta") or {}).get("content") or choice.get(
                        "text"
                    )
                except AttributeError:
                    value = None
                if isinstance(value, str):
                    chunks.append(value)

        if hasattr(stream, "__aiter__"):
            async for chunk in stream:
                take(chunk)
        elif hasattr(stream, "__iter__"):
            for chunk in stream:
                take(chunk)
        return "".join(chunks)

    def _coding_context(self, lesson_context: dict[str, Any]) -> dict[str, Any]:
        """The small, bounded picture the decision is made from."""
        lesson = (lesson_context or {}).get("coding_lesson") or {}
        artifact = lesson.get("artifact") or {}
        code = str(artifact.get("code") or lesson.get("code") or "")
        recent = []
        for item in (lesson.get("recent_comments") or [])[-6:]:
            text = item.get("text")
            if isinstance(text, str) and text:
                recent.append(
                    {"from": str(item.get("role") or "viewer"), "text": text[:300]}
                )
        return {
            "coding_session_active": bool((lesson_context or {}).get("active")),
            "what_they_are_building": str((lesson_context or {}).get("goal") or "")[
                :300
            ],
            "existing_program": {
                "exists": bool(code.strip()),
                "language": str(
                    artifact.get("language") or lesson.get("language") or ""
                ),
                "lines": len(code.splitlines()),
                # Enough of the source to tell what the program IS, so a new
                # topic is recognised as a new program instead of an edit.
                "preview": _program_preview(code),
            },
            "recent_chat": recent,
            # Earlier games a viewer may ask to go back to (newest first).
            "published_games": self._published_games(),
        }

    def _published_games(self, limit: int = 8) -> list[dict[str, str]]:
        if self.publisher is None:
            return []
        lesson = self.session.teaching.coding_lesson
        on_screen = getattr(lesson, "creation_job_id", "") if lesson else ""
        out = []
        try:
            for job in self.publisher.store.published()[:limit]:
                out.append(
                    {
                        "id": job.job_id,
                        "title": job.title[:80],
                        "requested_by": job.viewer_display_name[:40],
                        "on_screen": job.job_id == on_screen,
                    }
                )
        except Exception as exc:
            logger.debug(f"Published games unavailable: {exc}")
        return out

    async def decide_coding_action(
        self,
        character_id: str,
        viewer_text: str,
        lesson_context: dict[str, Any],
    ) -> CodingDecision:
        """What this one comment asks for. Cheap, and never writes code."""
        agent = self.agent(character_id)
        llm = getattr(agent, "_llm", None)
        if llm is None:
            return CodingDecision()
        messages = [
            {"role": "system", "content": DECIDE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "latest_comment": str(viewer_text or "")[:1000],
                        "situation": self._coding_context(lesson_context),
                        "characters": list(self.session.teaching.cast),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        started = time.perf_counter()
        try:
            reply = await self._complete(llm, messages)
        except Exception as exc:
            logger.error(f"VR Room: coding decision failed: {exc}")
            return CodingDecision()
        decision = parse_decision(reply, cast=list(self.session.teaching.cast))
        self.last_decision_seconds = time.perf_counter() - started
        logger.info(
            f"Coding decision: {decision.action} "
            f"({self.last_decision_seconds * 1000:.0f} ms) {decision.subject[:60]!r}"
        )
        return decision

    async def generate_code(
        self,
        character_id: str,
        language: str,
        instruction: str,
        existing: str = "",
        notes: Optional[dict[str, Any]] = None,
        kind: str = "",
    ) -> str:
        """Write or rewrite the program. The expensive step."""
        agent = self.agent(character_id)
        llm = getattr(agent, "_llm", None)
        if llm is None:
            return ""
        supported = (
            "a single self-contained HTML document"
            if language == "web"
            else "a single headless Python file (matplotlib for anything visual)"
        )
        request: dict[str, Any] = {"what_to_build": str(instruction or "")[:2000]}
        # "My name", "for me": the writer must know who asked.
        asker = str(self.session.teaching.session.student_name or "").lstrip("@")[:40]
        if asker:
            request["requested_by"] = asker
        if existing:
            request["current_program"] = existing[:MAX_CODE_BYTES]
            request["instruction"] = (
                "Change the current program as asked and return the whole updated file."
            )
        for key in (
            "requirements",
            "forbidden",
            "project_summary",
            "fix_these_problems",
        ):
            value = (notes or {}).get(key)
            if value:
                request[key] = value
        chosen = kind_for(kind, language)
        art_task = None
        art_url = ""
        if chosen.name == "web_art" and not (notes or {}).get("fix_these_problems"):
            from . import art as art_mod

            old_art = re.search(r"/stage-assets/generated/[A-Za-z0-9_.-]+", existing or "")
            if art_mod.art_enabled() and (
                not existing or not old_art or art_mod.needs_new_art(instruction)
            ):
                stem, art_url = art_mod.new_art_url()
                goal = self.session.teaching.session.goal if existing else ""
                art_task = asyncio.create_task(
                    art_mod.generate_art(
                        art_mod.art_prompt(str(instruction or ""), goal), stem
                    )
                )
                request["generated_art"] = art_url
                if existing:
                    request["instruction"] = (
                        "A new picture was painted for this change: set SETTINGS.art "
                        "to generated_art, update the title and effects to match, "
                        "and return the whole file."
                    )
            elif old_art:
                request["generated_art"] = old_art.group(0)
            else:
                request["generated_art"] = art_mod.fallback_art()
        # A game with a named character ("use that cat as a character"):
        # paint the character as a cut-out sprite while the code is written.
        sprite_url = ""
        if (
            chosen.name.startswith("web_game")
            and chosen.name != "web_game_quiz"
            and not (notes or {}).get("fix_these_problems")
        ):
            from . import art as art_mod

            if art_mod.art_enabled() and art_mod.GAME_CHARACTER.search(str(instruction or "")):
                stem, sprite_url = art_mod.new_art_url()
                sprite_url = sprite_url[: -len(".jpg")] + ".png"
                earlier = self.session.teaching.session.goal or ""
                art_task = asyncio.create_task(
                    art_mod.generate_sprite(
                        art_mod.sprite_prompt(str(instruction or ""), earlier), stem
                    )
                )
                art_url = sprite_url
                request["generated_sprite"] = sprite_url
                request["sprite_instruction"] = (
                    "generated_sprite is the requested main character (a transparent "
                    "PNG). Use it as the player's image: this.load.image(key, "
                    "generated_sprite), scaled to about 120px tall. Keep everything "
                    "else from the template."
                )
        # Libraries, approved assets and (for a new program) the template.
        request.update(
            writer_context(
                chosen,
                creating=not existing,
                request_text=f"{instruction} {(notes or {}).get('project_summary') or ''}",
            )
        )
        messages = [
            {
                "role": "system",
                "content": generate_system(language, supported, chosen.name),
            },
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ]
        self.last_generation_seconds = 0.0
        started = time.perf_counter()
        reply = ""
        if self.code_model is not None:
            try:
                reply = await self._complete(self.code_model, messages)
            except Exception as exc:
                logger.warning(
                    f"VR Room: code model {self.code_model.model} failed, "
                    f"using the chat model instead: {str(exc)[:200]}"
                )
                reply = ""
        if not reply.strip():
            try:
                reply = await self._complete(llm, messages)
            except Exception as exc:
                logger.error(f"VR Room: code generation failed: {exc}")
                return ""
        code = parse_code(reply)[:MAX_CODE_BYTES]
        if art_task is not None:
            # The writer and the painter work at the same time; the browser
            # check needs the picture, so wait for it here.
            try:
                made = await asyncio.wait_for(art_task, timeout=150)
            except Exception as exc:
                logger.warning(f"Art failed: {exc}")
                made = ""
            if not made:
                from . import art as art_mod

                fallback = (
                    "/stage-assets/characters/mika-head.png"
                    if art_url.endswith(".png")
                    else art_mod.fallback_art()
                )
                code = code.replace(art_url, fallback)
        self.last_generation_seconds = time.perf_counter() - started
        logger.info(
            f"Code generated: {language}, {len(code)} bytes, "
            f"{(time.perf_counter() - started) * 1000:.0f} ms"
        )
        return code

    async def classify_teaching_intent(
        self,
        character_id: str,
        viewer_text: str,
        lesson_context: dict[str, Any],
    ) -> dict[str, Any]:
        """The director's hook. One cheap decision, in the shape it expects.

        No code is generated here on purpose: the coding worker writes it, so
        the source is only ever written in one place, and plain chat costs one
        small call instead of a whole program.
        """
        decision = await self.decide_coding_action(
            character_id, viewer_text, lesson_context
        )
        # A picture of something is drawn in the browser (animated SVG), not
        # with Python's Pillow: Pillow scenes came out as a few coloured boxes.
        # Only when the viewer names Python does Python draw it.
        said = str(viewer_text or "")
        if (
            decision.writes_code
            and not re.search(r"\bpython\b", said, re.I)
            and (
                decision.kind in ("python_image", "web_illustration")
                or (
                    re.search(r"\b(draw\w*|image|picture|painting|illustration|scene|poster|portrait|wallpaper)\b", said, re.I)
                    and (decision.language == "python" or decision.kind.startswith("python"))
                    and not re.search(r"\b(chart|graph|plot|data)\b", said, re.I)
                )
            )
        ):
            from .art import art_enabled

            decision.kind = "web_art" if art_enabled() else "web_illustration"
            decision.language = "web"
        intent, artifact_action = DIRECTOR_INTENT.get(
            decision.action, ("answer", "none")
        )
        if decision.action == "control":
            intent = decision.control
        lesson = (lesson_context or {}).get("coding_lesson") or {}
        artifact = lesson.get("artifact") or {}
        has_code = bool(str(artifact.get("code") or "").strip())
        existing_language = str(artifact.get("language") or "") if has_code else ""
        language, fresh = resolve_program_target(
            decision,
            existing_language,
            str(artifact.get("kind") or "") if has_code else "",
        )
        decision.fresh = fresh
        self.last_decision = decision
        if decision.reopen and not self._can_reopen(decision.reopen):
            decision.reopen = ""
        if decision.writes_code:
            # A different program is written from scratch; a change to the
            # one on screen is an edit. Nothing else decides this.
            artifact_action = "create" if (fresh or not has_code) else "modify"
            if decision.reopen:
                # Going back to an earlier game: it is loaded, then edited.
                fresh, decision.fresh, artifact_action = False, False, "modify"
                language = "web"
        instruction = str(viewer_text or "")[:2000]
        if decision.brief:
            instruction = f"{decision.brief}\n(The viewer's words: {str(viewer_text or '')[:600]})"
        existing_kind = str(artifact.get("kind") or "") if has_code else ""
        kind = decision.kind or ("" if fresh else existing_kind)
        if decision.reopen:
            kind = ""  # the reopened game keeps its own kind
        if kind and KINDS.get(kind) and KINDS[kind].language != language:
            kind = ""
        # A Python picture is one still PNG. "Make it move" in Python must be
        # the GIF animation kind, or the viewer sees nothing move.
        if (
            decision.writes_code
            and language == "python"
            and kind != "python_animation"
            and re.search(r"\b(mov(e|es|ing)|animat\w*|wiggl\w*)\b", str(viewer_text or ""), re.I)
        ):
            kind = "python_animation"
        # Python shows nothing until it runs: writing it always runs it, so
        # nobody has to type "run it" to see the result.
        if decision.writes_code and language == "python" and intent == "write_code":
            intent = "write_and_run"
        return {
            "intent": intent,
            "artifact_action": artifact_action,
            "fresh": fresh,
            "kind": kind,
            "language": language,
            "teacher": decision.teacher,
            "subject": decision.subject,
            "goal": decision.subject,
            "unsupported": decision.unsupported,
            "reopen": decision.reopen,
            "instruction": instruction,
            "code": "",  # the worker writes it
        }

    # ------------------------------------------------------------------
    # Coding actions, all of them through the one worker.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Mood: real faces and pre-written lines while coding (coding_mood).
    # ------------------------------------------------------------------

    async def _mood(self, character_id: str, mood: str, speak: bool = False) -> None:
        session = self.session
        profile = session.room.get(character_id)
        if not profile:
            return
        caps = profile.capabilities or {}
        supports = lambda name: bool(caps.get(name))  # noqa: E731
        face = face_for(mood, supports)
        motion = motion_for(mood, supports)
        try:
            if face:
                await session.push(session.play(character_id, face))
            if motion:
                await session.push(
                    session.play(character_id, motion, delay_seconds=0.3)
                )
            if not speak or not session.speech_allowed():
                return
            if session.speech.talking or (
                self._mood_speech is not None and not self._mood_speech.done()
            ):
                return  # never talk over another line
            line = self.mood_lines.line(mood, character_id)
            if line:
                session.trace(
                    "coding_mood", character=character_id, mood=mood, text=line
                )
                self._mood_speech = asyncio.create_task(
                    session.speech.say(character_id, line)
                )
        except Exception as exc:  # a face must never break a build
            logger.debug(f"VR Room: mood {mood} for {character_id} skipped: {exc}")

    async def _fidget(self, character_id: str, turn: int) -> None:
        """A small motion now and then during a long build."""
        profile = self.session.room.get(character_id)
        caps = (profile.capabilities if profile else None) or {}
        options = [name for name in FIDGETS if caps.get(name)]
        if not options:
            return
        try:
            await self.session.push(
                self.session.play(character_id, options[turn % len(options)])
            )
        except Exception as exc:
            logger.debug(f"VR Room: fidget skipped: {exc}")

    async def _finish_mood_speech(self) -> None:
        """Let a reaction line end before the character explains the result."""
        task = self._mood_speech
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 12)
            except Exception:
                pass

    @contextlib.asynccontextmanager
    async def _waiting_faces(self, character_id: str):
        """Focused face after a couple of seconds, one tired grumble after ~15."""
        focus_after, tired_after, refresh = self.mood_timing

        async def watch():
            started = time.monotonic()
            await asyncio.sleep(focus_after)
            grumbled = False
            beat = 0
            while True:
                waited = time.monotonic() - started
                if not grumbled and waited >= tired_after:
                    grumbled = True
                    await self._mood(character_id, "tired", speak=True)
                else:
                    await self._mood(character_id, "focus")
                    beat += 1
                    if beat % 3 == 0:
                        await self._fidget(character_id, beat // 3)
                await asyncio.sleep(refresh)

        watcher = asyncio.create_task(watch()) if character_id else None
        try:
            yield
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher

    async def _execute_job(self, job: CodingJob) -> dict[str, Any]:
        """Generate (if asked), write (if asked), run (if asked). One job."""
        lesson = self.session.teaching.coding_lesson
        if lesson is None:
            return {
                "ok": False,
                "action": job.action,
                "error": "No active coding session.",
            }
        if not lesson.owns(job.owner_id):
            return {
                "ok": False,
                "action": job.action,
                "error": "Only the viewer who started this session can change or run the code.",
            }

        language = job.language or lesson.language or "python"
        filename = (
            "index.html"
            if language == "web"
            else "main.py"
            if language == "python"
            else "main.js"
        )

        try:
            if job.writes_code:
                kind = job.kind or (
                    lesson.kind if job.action.startswith("modify") else ""
                )
                speaker = job.speaker or self.session.teaching.session.teacher
                async with self._waiting_faces(speaker):
                    code = await self.generate_code(
                        speaker,
                        language,
                        job.instruction,
                        existing=lesson.code if job.action.startswith("modify") else "",
                        notes=job.notes,
                        kind=kind,
                    )
                if not code.strip():
                    return {
                        "ok": False,
                        "action": job.action,
                        "error": "The code could not be written this time.",
                    }
                # The page shows the source as it lands, before anything runs.
                await self.session.push(
                    [
                        {
                            "op": "coding",
                            "phase": "writing",
                            "language": language,
                            "filename": filename,
                            "code": code,
                            "display": "typewriter",
                        }
                    ]
                )
                lesson.set_artifact(job.owner_id, language, code)
                lesson.kind = kind_for(kind, language).name
            elif not lesson.code:
                # RUN with nothing written yet.
                return {
                    "ok": False,
                    "action": job.action,
                    "error": "There is no program on screen to run yet.",
                }
            # A plain run NEVER touches the source: whatever is on screen is
            # exactly what executes.

            response: dict[str, Any] = {
                "ok": True,
                "action": job.action,
                "language": lesson.language,
                "code": lesson.code,
                "filename": (
                    "index.html"
                    if lesson.language == "web"
                    else "main.py"
                    if lesson.language == "python"
                    else "main.js"
                ),
            }

            if (
                lesson.language == "web"
                and job.writes_code
                and self.browser_check is not None
            ):
                response.update(await self._check_web_program(job, kind))
                response["code"] = lesson.code

            if lesson.language == "web":
                # The sandboxed Stage preview runs a browser project; there is
                # nothing for Python or Node to execute.
                response["filename"] = "index.html"
                # The preview gets the stage autopilot; the source on screen stays as written.
                response["preview_html"] = stage_preview_html(lesson.code)
                response["preview_ready"] = True
            elif job.should_run:
                await self.session.push(
                    [
                        {
                            "op": "coding",
                            "phase": "running",
                            "language": lesson.language,
                            "filename": response["filename"],
                            "teacher": job.speaker,
                        }
                    ]
                )
                teacher = job.speaker or self.session.teaching.session.teacher
                async with self._waiting_faces(teacher):
                    result = await self._coding_runner.run(lesson, job.owner_id)
                failed = result.exit_code != 0 or bool(result.error)
                # One repair, only for code written in this job (a plain "run"
                # never edits the source), using the real error output.
                if failed and job.writes_code and self.max_repairs > 0:
                    problems = (result.stderr or result.error or result.stdout or "")[
                        -1500:
                    ]
                    await self._mood(teacher, "bug", speak=True)
                    fixed = await self._repair(job, kind, problems)
                    if fixed:
                        response["repaired"] = True
                        response["first_error"] = problems[-400:]
                        response["code"] = lesson.code
                        await self.session.push(
                            [
                                {
                                    "op": "coding",
                                    "phase": "running",
                                    "language": lesson.language,
                                    "filename": response["filename"],
                                    "teacher": job.speaker,
                                }
                            ]
                        )
                        async with self._waiting_faces(teacher):
                            result = await self._coding_runner.run(lesson, job.owner_id)
                    still_broken = result.exit_code != 0 or bool(result.error)
                    await self._mood(
                        teacher,
                        "failed" if still_broken or not fixed else "fixed",
                        speak=bool(fixed) and not still_broken,
                    )
                response["run_result"] = result.snapshot()
                response["artifacts"] = self._coding_runner.artifacts(lesson)
                # The truth about the run, so nobody can claim success over an
                # error or say it is still running after it finished.
                response["ok"] = result.exit_code == 0 and not result.error
                response["error"] = result.error or (
                    "" if response["ok"] else "The program exited with an error."
                )
            self.session.teaching.sync_coding_state()
            await self._finish_mood_speech()
            return response
        except (ValueError, PermissionError) as exc:
            return {"ok": False, "action": job.action, "error": str(exc)}

    async def _repair(self, job: CodingJob, kind: str, problems: str) -> str:
        """One repair pass: the same writer, the real problems, the whole file back.

        Shows "fixing" on the Stage, then types the repaired source. Returns
        the new source, or "" when the repair produced nothing usable.
        """
        lesson = self.session.teaching.coding_lesson
        await self.session.push(
            [
                {
                    "op": "coding",
                    "phase": "fixing",
                    "teacher": job.speaker,
                    "language": lesson.language,
                    "subject": "Found a problem, fixing it",
                }
            ]
        )
        notes = dict(job.notes or {})
        notes["fix_these_problems"] = problems[:2000]
        speaker = job.speaker or self.session.teaching.session.teacher
        async with self._waiting_faces(speaker):
            fixed = await self.generate_code(
                speaker,
                lesson.language,
                "Fix the problems listed in fix_these_problems. Keep everything else "
                "exactly as it is and return the whole file.",
                existing=lesson.code,
                notes=notes,
                kind=kind,
            )
        if not fixed.strip() or fixed.strip() == lesson.code.strip():
            return ""
        await self.session.push(
            [
                {
                    "op": "coding",
                    "phase": "writing",
                    "language": lesson.language,
                    "filename": "index.html" if lesson.language == "web" else "main.py",
                    "code": fixed,
                    "display": "typewriter",
                }
            ]
        )
        lesson.set_artifact(job.owner_id, lesson.language, fixed)
        return fixed

    def _record_creation(self, job: CodingJob, kind: str, lesson) -> None:
        """Who asked for this program and whether it passed. Never publishes."""
        if self.publisher is None:
            return
        try:
            if job.action.startswith("create") or not lesson.creation_job_id:
                viewer = dict((job.notes or {}).get("viewer") or {})
                creation = self.publisher.record_creation(
                    viewer=viewer,
                    request_text=str(viewer.get("text") or job.instruction),
                    title=str((job.notes or {}).get("subject") or "")
                    or job.instruction[:60],
                    kind=kind_for(kind, lesson.language).name,
                    language=lesson.language,
                    built_by=job.speaker or self.session.teaching.session.teacher,
                )
                lesson.creation_job_id = creation.job_id
            self.publisher.record_result(
                lesson.creation_job_id, lesson.code, lesson.last_check
            )
            settings = self.publisher.settings
            if (
                settings.enabled
                and settings.auto_publish
                and lesson.last_check.get("ok")
            ):
                self.publish_task = asyncio.get_running_loop().create_task(
                    self.publish_current(announce=True, speaker=job.speaker)
                )
            elif settings.enabled and settings.auto_publish and lesson.language == "web":
                # Never silent: the viewer is waiting for a link.
                problems = (lesson.last_check.get("problems") or ["it did not pass the check"])[:2]
                logger.warning(f"Not published (browser check failed): {problems}")
                who = str(lesson.student_name or "").lstrip("@") or "chat"
                self.publish_task = asyncio.get_running_loop().create_task(
                    self._say_later(
                        job.speaker or self.session.teaching.session.teacher,
                        f"{who}, this one still has a bug, so I didn't put it on the "
                        "games page yet. Say fix it and I'll try again!",
                    )
                )
        except Exception as exc:  # publishing must never break a build
            logger.warning(f"Could not record the creation: {exc}")

    async def _say_later(self, character_id: str, text: str) -> None:
        from .speech import INSIDE_INTERACTION

        INSIDE_INTERACTION.set(False)
        if self.session.speech_target():
            await self.session.speech.say(character_id, text)

    async def publish_current(
        self, announce: bool = True, speaker: str = ""
    ) -> dict[str, Any]:
        """Publish the program on screen through the trusted service.

        The only publishing entry point on Mika's side. It passes a job id and
        the checked source; the service decides whether that is allowed.
        """
        # The "your game is live" line must wait for the viewer interaction
        # that started this publish to finish, never talk over the next one.
        from .speech import INSIDE_INTERACTION

        INSIDE_INTERACTION.set(False)
        lesson = self.session.teaching.coding_lesson
        if self.publisher is None or lesson is None or not lesson.creation_job_id:
            return {"ok": False, "reason": "nothing to publish"}
        job_id = lesson.creation_job_id
        before = self.publisher.store.get(job_id)
        # Published before (it has a public page): this publish updates it.
        updated = bool(before and before.public_url)
        outcome = await asyncio.to_thread(
            self.publisher.publish_project, job_id, lesson.code, self.last_screenshot
        )
        outcome["updated"] = updated
        if outcome.get("ok"):
            try:
                outcome["preview"] = self.publisher.youtube_preview(job_id)
            except Exception as exc:  # the preview is only for the terminal
                logger.debug(f"YouTube preview unavailable: {exc}")
        self.publish_events.append(outcome)
        if outcome.get("ok") and announce:
            outcome["chat"] = await asyncio.to_thread(
                self.publisher.announce_published_project, job_id
            )
            outcome["description"] = await asyncio.to_thread(
                self.publisher.update_generated_games_section, job_id
            )
            preview = outcome.get("preview") or {}
            logger.info(
                "Published to GitHub: "
                + str(outcome.get("url") or outcome.get("public_url") or "")
                + "\n--- chat message ---\n"
                + str(preview.get("chat", ""))
                + "\n--- description section ---\n"
                + str(preview.get("description", ""))
                + f"\n(YouTube chat: {outcome['chat'].get('reason') or 'sent'};"
                + f" description: {outcome['description'].get('reason') or 'updated'})"
            )
            # Always tell the viewer, even when nobody chatted in the last
            # minute: they are waiting for this.
            if self.session.speech_target():
                record = self.publisher.store.get(job_id)
                who = (record.viewer_display_name if record else "").lstrip(
                    "@"
                ) or "chat"
                teacher = speaker or self.session.teaching.session.teacher
                where = (
                    "The link is in chat."
                    if outcome["chat"].get("ok")
                    else "Search your name on our games page."
                )
                line = (
                    f"{who}, your update is pushed to GitHub! Give it about five "
                    f"minutes to show up. {where}"
                    if updated
                    else f"{who}, your game is pushed to GitHub! Give it about five "
                    f"minutes, then {where[0].lower() + where[1:]}"
                )
                await self.session.speech.say(teacher, line)
        return outcome

    async def _browser_check(self, source: str, kind: str):
        """The real-browser check. A self-scrolling website may be tall."""
        allow_scroll = kind_for(kind, "web").name == "web_site"
        try:
            return await self.browser_check(source, allow_scroll=allow_scroll)
        except TypeError:
            return await self.browser_check(source)

    async def _check_web_program(self, job: CodingJob, kind: str) -> dict[str, Any]:
        """Open the program in a real browser; repair once if it is broken.

        Runs while the Stage is still typing the source, so it mostly hides
        inside time the viewer already spends watching.
        """
        lesson = self.session.teaching.coding_lesson
        await self.session.push(
            [{"op": "coding", "phase": "checking", "teacher": job.speaker}]
        )
        report = await self._browser_check(lesson.code, kind)
        result: dict[str, Any] = {}
        teacher = job.speaker or self.session.teaching.session.teacher
        if not report.ok and not report.skipped and self.max_repairs > 0:
            original = lesson.code
            first = report
            await self._mood(teacher, "bug", speak=True)
            fixed = await self._repair(job, kind, report.feedback())
            if fixed:
                second = await self._browser_check(fixed, kind)
                if len(second.problems()) <= len(first.problems()):
                    report = second
                    result["repaired"] = True
                    result["first_problems"] = first.problems()[:3]
                else:
                    # The repair made it worse: keep what was there.
                    lesson.set_artifact(job.owner_id, lesson.language, original)
                    await self.session.push(
                        [
                            {
                                "op": "coding",
                                "phase": "writing",
                                "language": "web",
                                "filename": "index.html",
                                "code": original,
                                "display": "instant",
                            }
                        ]
                    )
            await self._mood(
                teacher,
                "fixed" if report.ok else "failed",
                speak=report.ok,
            )
        lesson.last_check = report.summary()
        result["check"] = lesson.last_check
        self.last_screenshot = report.screenshot_png
        self._record_creation(job, kind, lesson)
        logger.info(
            f"Browser check: {'ok' if report.ok else 'problems'} in {report.seconds:.1f}s"
            + (" after one repair" if result.get("repaired") else "")
            + (f" {report.problems()[:2]}" if not report.ok else "")
        )
        return result

    def _can_reopen(self, job_id: str) -> bool:
        lesson = self.session.teaching.coding_lesson
        if self.publisher is None or not job_id:
            return False
        if lesson is not None and lesson.creation_job_id == job_id:
            return False  # already on screen: a normal change
        record = self.publisher.store.get(job_id)
        return bool(record and record.public_url and self.publisher.saved_code(job_id))

    async def _reopen(self, lesson, lesson_context: dict[str, Any]) -> dict[str, Any]:
        """Put an earlier published game back on screen before changing it.

        The host and the viewer who asked for the game change the original
        (same page, same link). Anyone else gets a remix: a new game that
        credits the original, so nobody can wreck someone else's game.
        """
        job_id = str(lesson_context.get("reopen") or "")
        if not job_id or lesson is None or not self._can_reopen(job_id):
            return {}
        record = self.publisher.store.get(job_id)
        code = self.publisher.saved_code(job_id)
        viewer = dict(lesson_context.get("viewer") or {})
        owner = str(lesson_context.get("student_id") or "")
        is_host = (
            viewer.get("platform") == "dev" or viewer.get("author_type") == "owner"
        )
        same_viewer = bool(viewer.get("author_id")) and (
            viewer.get("author_id") == record.viewer_channel_id
        )
        try:
            lesson.set_artifact(owner, "web", code)
        except (PermissionError, ValueError) as exc:
            logger.warning(f"VR Room: could not reopen {job_id}: {exc}")
            return {}
        lesson.kind = record.project_type or lesson.kind
        remix = not (is_host or same_viewer)
        lesson.creation_job_id = "" if remix else job_id
        lesson.project_requirements = []
        lesson.project_forbidden = []
        lesson.project_summary = ""
        await self.session.push(
            [
                {
                    "op": "coding",
                    "phase": "writing",
                    "language": "web",
                    "filename": "index.html",
                    "code": code,
                    "display": "instant",
                }
            ]
        )
        logger.info(
            f"VR Room: reopened {record.title!r} ({'remix' if remix else 'update'})"
        )
        out: dict[str, Any] = {"job_id": job_id, "title": record.title, "remix": remix}
        if remix:
            out["remix_of"] = record.title
            out["original_by"] = record.viewer_display_name
        return out

    async def run_coding_action(
        self,
        action: str,
        lesson_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit one coding action and wait for its verified result."""
        mapped = {
            "write_code": "modify",
            "write_and_run": "modify_and_run",
            "run_code": "run",
        }
        action = str(action or "").strip().lower()
        if action not in mapped:
            return {
                "ok": False,
                "action": action,
                "error": "Unsupported coding action.",
            }
        lesson = self.session.teaching.coding_lesson
        if lesson is None:
            return {"ok": False, "action": action, "error": "No active coding session."}

        job_action = mapped[action]
        reopened = await self._reopen(lesson, lesson_context)
        fresh = bool(lesson_context.get("fresh")) and not reopened
        if job_action.startswith("modify") and (fresh or not lesson.code):
            job_action = job_action.replace("modify", "create")
        if fresh:
            # A new program must not inherit the old one's rules ("make it
            # twinkle") or summary; they belong to the previous topic.
            lesson.project_requirements = []
            lesson.project_forbidden = []
            lesson.project_summary = ""

        lesson.update_project_state(
            requirements=lesson_context.get("requirements") or [],
            forbidden=lesson_context.get("forbidden") or [],
            remove_requirements=lesson_context.get("remove_requirements") or [],
            remove_forbidden=lesson_context.get("remove_forbidden") or [],
            project_summary=str(lesson_context.get("project_summary") or ""),
        )

        job = CodingJob(
            action=job_action,
            owner_id=str(lesson_context.get("student_id") or ""),
            language=str(lesson_context.get("language") or "").lower(),
            instruction=str(
                lesson_context.get("instruction") or lesson_context.get("goal") or ""
            ),
            speaker=str(lesson_context.get("teacher") or ""),
            kind=str(lesson_context.get("kind") or ""),
        )
        subject = str(lesson_context.get("subject") or "")
        if reopened.get("remix_of"):
            subject = f"{reopened['remix_of']} remix"
        job.notes = {
            "viewer": dict(lesson_context.get("viewer") or {}),
            "subject": subject,
            "reopened": reopened,
            "requirements": lesson_context.get("requirements") or [],
            "forbidden": lesson_context.get("forbidden") or [],
            "project_summary": lesson_context.get("project_summary") or "",
        }
        return await self.coding_worker.submit(job)

    async def run_turn(self, turn: "Turn", prompt: str, plan: "InteractionPlan") -> str:
        from ..conversations.single_conversation import process_single_conversation

        session = self.session
        cid = turn.speaker
        profile = session.room.get(cid)
        agent = self.agent(cid)
        engine = session.voices.engine(cid)
        if not profile or agent is None:
            return ""
        context = SimpleNamespace(
            agent_engine=agent,
            tts_engine=engine,
            live2d_model=self._live2d(cid),
            translate_engine=None,
            asr_engine=None,
            history_uid=None,
            character_config=SimpleNamespace(
                character_name=profile.name,
                avatar="",
                human_name="viewer",
                conf_uid=f"room-{cid}",
            ),
        )
        started = time.time()
        state = {"first": None, "audio": 0, "silent": 0}
        send = self.send
        character = session.state.characters.get(cid)

        async def tagged_send(payload: str) -> None:
            if payload.startswith('{"type": "audio"'):
                data = json.loads(payload)
                shown = data.get("display_text")
                if is_llm_error_text(
                    (shown or {}).get("text") if isinstance(shown, dict) else ""
                ):
                    # Open-LLM-VTuber speaks its own error messages. Never on
                    # stream: drop the line, log it, stay quiet this turn.
                    state["llm_error"] = True
                    logger.error(
                        f"VR Room: {cid}'s LLM call failed; the error was not "
                        "spoken. The real reason is in the 'LLM API' log line."
                    )
                    return
                if isinstance(shown, dict) and shown.get("text"):
                    shown["text"] = strip_wrapping_quotes(str(shown["text"]))
                data["character"] = cid
                data["emotion_mode"] = "profile"
                if data.get("audio"):
                    state["audio"] += 1
                else:
                    state["silent"] += 1
                if state["first"] is None:
                    state["first"] = time.time()
                    timing = getattr(plan, "timing", None)
                    if timing is not None and not timing.first_audio_at:
                        from ..vr_agent.state import VRAgentState, runtime

                        timing.first_audio_at = state["first"]
                        runtime.set(VRAgentState.SPEAKING, plan.id)
                    session.trace(
                        "character_audio_started",
                        interaction=plan.id,
                        character=cid,
                        after_s=round(state["first"] - started, 2),
                        chat_to_voice_s=round(
                            state["first"] - plan.message.timestamp, 2
                        )
                        if plan.message.timestamp
                        else None,
                    )
                    if character:
                        character.speaking = True
                    seconds = min(
                        20.0,
                        2.0
                        + len(str((data.get("display_text") or {}).get("text", "")))
                        * 0.07,
                    )
                    ops = session.emit(
                        ev.SPEECH_STARTED,
                        character=cid,
                        addressee=turn.addressee,
                        seconds=seconds,
                    )
                    if turn.intent.action:
                        ops += session.play(cid, turn.intent.action.name)
                    await session.push(ops)
                payload = json.dumps(data)
            await send(payload)

        metadata = {
            "source": "youtube_live",  # no "Thinking..." text on stream
            "from_name": "viewer",
            "skip_history": True,
            "skip_memory": True,
            "history_limit": -1,  # the prompt carries the short room context
        }
        session.trace(
            "character_llm_started", interaction=plan.id, character=cid, kind=turn.kind
        )
        try:
            text = await process_single_conversation(
                context=context,
                websocket_send=tagged_send,
                client_uid=self.client_uid,
                user_input=prompt,
                images=None,
                metadata=metadata,
            )
        finally:
            if character:
                character.speaking = False
                character.spoken_turns += 1
            await session.push(session.emit(ev.SPEECH_ENDED, character=cid))
        text = strip_wrapping_quotes(" ".join(str(text or "").split()))
        if state.get("llm_error") or is_llm_error_text(text):
            session.record_failure(cid, "llm call failed")
            text = ""
        if text and state["audio"] == 0 and engine is not None:
            session.record_failure(cid, "tts produced no audio")
        session.trace(
            "character_turn_finished",
            interaction=plan.id,
            character=cid,
            seconds=round(time.time() - started, 2),
            audio_chunks=state["audio"],
            spoken=bool(text),
        )
        if character and text:
            character.recent_dialogue.append(text[:200])
        return text

    def describe(self) -> dict[str, Any]:
        return {
            cid: {
                "agent": cid in self._agents and self._agents[cid] is not None,
                "error": self.errors.get(cid),
            }
            for cid in self.session.state.characters
        }
