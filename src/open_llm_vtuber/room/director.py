"""Conversation Director: plans who speaks for a viewer message, then runs it.

* Routing is deterministic (``routing.route_message``); no LLM decides who talks.
* A plan is a short list of turns with a hard budget (``director.max_turns``,
  never above 4), so character-to-character exchanges always end.
* Optional turns (follow-ups) are dropped the moment another viewer message is
  waiting. Viewer messages always win.
* The other character mostly reacts silently (looks, expressions), which is
  free. Only planned turns call the LLM, one at a time.
* The Director only ever runs for a real viewer message. It never starts on
  its own, so zero chat means zero LLM and TTS requests.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from ..vr_agent.intent import NO_INTENT, ActionIntent, resolve_intent
from ..vr_agent.text_safety import prompt_quote
from . import events as ev
from .coding_actions import redirect_note
from .coding_lesson import MAX_CODE_BYTES
from .live_message import LiveMessage
from .prompting import turn_prompt, viewer_quote
from .routing import RoutingDecision, _strip_names, route_message
from .acknowledge import acknowledgment
from .stage_design import SPEECH_LIVE_NOTE

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

HARD_TURN_LIMIT = 4
_ids = itertools.count(1)


def asks_character(text: str, profile: Any) -> bool:
    """True when ``text`` puts a question to that character by name
    ("What do you think, Luna?", "Mika, would you like to go first?")."""
    if not text or profile is None or "?" not in text:
        return False
    names = [n for n in getattr(profile, "names", []) if n]
    if not names:
        return False
    alternation = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    for sentence in re.findall(r"[^.!?]*\?", text):
        if re.search(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])", sentence.lower()):
            return True
    return False


@dataclass
class Turn:
    speaker: str
    kind: str  # answer | follow_up | ask | reply
    addressee: Optional[str] = None  # a character id; None means the viewer
    optional: bool = False
    intent: ActionIntent = NO_INTENT
    text: str = ""
    skipped: str = ""


@dataclass
class InteractionPlan:
    id: str
    message: LiveMessage
    decision: RoutingDecision
    turns: list[Turn] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.decision.mode,
            "reason": self.decision.reason,
            "turns": [
                {
                    "speaker": t.speaker,
                    "kind": t.kind,
                    "addressee": t.addressee,
                    "optional": t.optional,
                    "spoken": bool(t.text),
                    "skipped": t.skipped,
                }
                for t in self.turns
            ],
        }


# turn_runner(turn, prompt, plan) -> spoken text ("" when it failed)
TurnRunner = Callable[[Turn, str, InteractionPlan], Awaitable[str]]


class ConversationDirector:
    def __init__(
        self,
        session: "RoomSession",
        turn_runner: Optional[TurnRunner] = None,
        pending_probe: Callable[[], bool] = lambda: False,
        rng: Optional[random.Random] = None,
        clock=time.time,
    ):
        self.session = session
        self.turn_runner = turn_runner
        self.pending_probe = pending_probe
        self.rng = rng or random.Random()
        self.clock = clock

        # Wired by RoomRuntimes when the room client connects.
        # The classifier may propose meaning; TeachingDirector
        # still owns authorization and state.
        self.teaching_intent_classifier = None
        self.coding_action_runner = None

        self.last_plan: Optional[InteractionPlan] = None
        self.interactions = 0

    @property
    def ready(self) -> bool:
        return self.turn_runner is not None and bool(
            self.session.state.available_characters()
        )

    # ------------------------------------------------------------------
    # planning (pure, deterministic given the random source)
    # ------------------------------------------------------------------
    # Coding is routed by ONE semantic decision (room/coding_actions.py),
    # never by phrase lists. The phrase matchers that used to live here
    # could only ever work in the languages someone remembered to type.

    def plan(self, message: LiveMessage) -> InteractionPlan:
        room = self.session.room
        decision = route_message(message.clean_text, room, self.session.state, self.rng)
        plan = InteractionPlan(id=f"i{next(_ids)}", message=message, decision=decision)
        budget = max(1, min(HARD_TURN_LIMIT, room.director.max_turns))
        speakers = decision.speakers
        if not speakers:
            return plan
        first = speakers[0]
        second = speakers[1] if len(speakers) > 1 else None
        available = self.session.state.available_characters()

        if decision.mode == "pair" and second:
            plan.turns = [
                Turn(first, "ask", addressee=second),
                Turn(second, "reply", addressee=first),
            ]
        elif decision.mode in ("group", "compare") and second:
            plan.turns = [
                Turn(first, "answer"),
                Turn(
                    second,
                    "answer" if decision.mode == "group" else "follow_up",
                    addressee=None if decision.mode == "group" else first,
                ),
            ]
        else:
            plan.turns = [Turn(first, "answer")]
            others = [c for c in available if c != first]
            if others and self.rng.random() < room.director.follow_up_chance:
                plan.turns.append(
                    Turn(others[0], "follow_up", addressee=first, optional=True)
                )

        if decision.intent:
            profile = room.get(first)
            plan.turns[0].intent = resolve_intent(
                _strip_names(message.clean_text, room),
                profile.capabilities if profile else None,
            )
        plan.turns = plan.turns[:budget]
        return plan

    # ------------------------------------------------------------------
    # instructions for each turn
    # ------------------------------------------------------------------
    def _instruction(self, plan: InteractionPlan, index: int) -> str:
        turn = plan.turns[index]
        room = self.session.room
        message = plan.message
        name = lambda cid: (room.get(cid).name if room.get(cid) else cid)  # noqa: E731
        previous = next((t for t in reversed(plan.turns[:index]) if t.text), None)
        quote = viewer_quote(message.display_name, message.clean_text)

        teaching = self.session.teaching.session

        if index == 0 and turn.kind.startswith("teaching_control_"):
            control = turn.kind.removeprefix("teaching_control_")

            instructions = {
                "pause": (
                    "The deterministic lesson controller has paused "
                    "the lesson. Acknowledge naturally and briefly. "
                    "Do not continue teaching until the owner resumes."
                ),
                "resume": (
                    "The deterministic lesson controller has resumed "
                    "the lesson. Welcome them back briefly and continue "
                    "as their active teacher."
                ),
                "slow_down": (
                    "The deterministic lesson controller changed the "
                    "lesson pace to slow. Acknowledge briefly and use "
                    "smaller, slower steps from now on."
                ),
                "speed_up": (
                    "The deterministic lesson controller changed the "
                    "lesson pace to fast. Acknowledge briefly and become "
                    "more concise."
                ),
                "simplify": (
                    "The deterministic lesson controller changed the "
                    "lesson to beginner pacing. Acknowledge briefly and "
                    "explain things more simply from now on."
                ),
                "explain_more": (
                    "The deterministic lesson controller changed the "
                    "lesson to detailed pacing. Acknowledge briefly and "
                    "give more explanation."
                ),
                "go_back": (
                    "The deterministic lesson controller moved the "
                    "lesson step backward. Acknowledge briefly and return "
                    "to the previous coding concept."
                ),
                "skip": (
                    "The deterministic lesson controller advanced the "
                    "lesson step. Acknowledge briefly and move to the next "
                    "coding concept."
                ),
                "quit": (
                    "The deterministic lesson controller has ended the "
                    "lesson. Give one short natural sign-off. The normal "
                    "room will return after you finish speaking."
                ),
            }

            return f"{quote}\n\n" + instructions.get(
                control,
                "Acknowledge the lesson control briefly.",
            )

        if index == 0 and teaching.active and turn.kind == "teaching_switch":
            return (
                f"{quote}\n\n"
                "The lesson owner has just switched the active coding "
                f"teacher to you ({name(turn.speaker)}). "
                "The teaching interface has already switched to you. "
                "Acknowledge the handoff naturally in one short sentence "
                "and continue as the active teacher. "
                "Do not criticize, compete with, insult, or question the "
                "other teacher's ability. Do not ask the other teacher to "
                "join in."
            )

        if index == 0 and teaching.active and turn.kind == "teaching_answer":
            student_id = str(
                message.author_id or f"{message.platform}:{message.display_name}"
            )

            owner = self.session.teaching.owns(student_id)

            owner_note = (
                "This viewer owns the current lesson and may request teaching changes."
                if owner
                else "This viewer does not own the current lesson. They may "
                "talk to you, but they cannot change the active teacher "
                "or control the lesson."
            )

            action_result = teaching.last_action_result or {}
            run_result = action_result.get("run_result") or {}
            if run_result:
                action_note = (
                    f"The most recent script run exited with status {run_result.get('exit_code')}. "
                    f"stdout={str(run_result.get('stdout', ''))[-1200:]!r}; "
                    f"stderr={str(run_result.get('stderr', ''))[-1200:]!r}; "
                    f"error={str(run_result.get('error', ''))[:300]!r}. "
                    "Describe only this recorded result. If it failed, explain the error and propose one focused fix."
                )
            elif action_result.get("code"):
                action_note = "The current code is displayed in the editor but has not been run yet. Do not claim it works."
            elif action_result.get("error"):
                action_note = f"The coding action failed: {action_result.get('error')}. Do not claim it happened."
            else:
                action_note = (
                    "No code was changed or run for this comment. "
                    "Do NOT say or imply that you wrote, displayed, changed, "
                    "executed, showed, opened, or updated any code. "
                    "Do NOT refer to a line being on screen unless a verified "
                    "coding action for this comment actually occurred. "
                    "If the viewer is ready for the next coding demonstration, "
                    "the coding action system must perform it first."
                )

            action_note += _verification_note(action_result)
            if action_result.get("code") or run_result:
                action_note += " " + build_attitude(teaching.teacher, action_result)
            context = (
                self.session.teaching.coding_lesson.prompt_context()
                if self.session.teaching.coding_lesson
                else {}
            )
            return (
                f"{quote}\n\nYou are {name(teaching.teacher)}, teaching {teaching.goal!r}. "
                f"{owner_note} Lesson phase: {teaching.phase}. {SPEECH_LIVE_NOTE} "
                f"Session context: {context}. {action_note} "
                "Teach one small complete idea per comment. Keep the code and terminal result synchronized with the viewer's latest request. "
                "A viewer may ask follow-up questions, request a change, ask to run again, or change topics; use the session transcript and current artifact to resolve short references. "
                "Only the active lesson owner can change or run code. Other viewers can still ask questions. "
                "Never say code ran or succeeded unless the recorded process result confirms it. If paused, answer briefly without advancing. "
                "The code runner accepts Python or JavaScript scripts in an isolated, network-disabled workspace; it does not accept shell commands. "
                "IMPORTANT SPEECH AND VISUAL SEPARATION: anything that is source code belongs in the visible editor, NOT in your spoken response. "
                "Never read source code aloud. Never put code blocks, backticks, exact assignment expressions, brackets, braces, function-call syntax, "
                "or identifiers containing underscores into the words you speak. Do not pronounce underscores, punctuation, or programming symbols. "
                "If code is visible, refer to it naturally with phrases such as 'the list on screen', 'the variable on screen', 'this line', or "
                "'the example I just wrote'. Explain what the code means instead of reciting it. "
                "If a coding example was requested and the editor was updated, briefly explain what you just wrote while the student watches it appear. "
                "Keep the explanation beginner-friendly and focused on the current lesson. "
                "TEACHING CONVERSATION RULES: Do not end lessons with filler questions such as "
                "'Is that clear?', 'Does that make sense?', 'Are you ready?', 'Would you like me to continue?', "
                "'Do you want another example?', or similar confirmation questions. "
                "When the next useful step is obvious, simply continue teaching or perform it. "
                "CODING REACTION MODE: Treat genuine coding failures, bizarre results, obvious visual mistakes, "
                "or unexpectedly broken behavior as natural livestream moments. If something genuinely goes wrong, "
                "react briefly in character BEFORE calmly diagnosing and fixing it. A spectacular mistake may earn "
                "a stronger surprised, embarrassed, competitive, or comedic reaction, while a tiny typo should get "
                "only a small reaction. Keep reactions spontaneous and short rather than turning every error into a "
                "performance. Never deliberately introduce bugs just to create reactions. Never pretend something "
                "failed when the recorded execution says it succeeded. Never pretend you can see a visual defect "
                "unless the runtime, current code, or viewer feedback actually supports that conclusion. "
                "When appropriate, begin with one supported emotion tag such as [surprise], [sadness], [anger], "
                "[fear], [disgust], [joy], or [smirk], then react naturally in your own personality. After the "
                "reaction, immediately explain what actually went wrong and move toward a real fix. Avoid repeating "
                "the same reaction on every retry. "
                "Ask a question only when genuinely required because the viewer's request cannot be acted on "
                "without missing information. "
                "If the viewer explicitly says they are recording, wants everything at once, wants no interruptions, "
                "or asks you not to ask questions, explain the requested material continuously and completely without "
                "asking follow-up questions or waiting for confirmation. "
                "The live coding environment supports only Python and JavaScript. If another language is requested, "
                "briefly say that only Python and JavaScript are currently supported and continue with the closest "
                "useful supported example when appropriate; do not ask the viewer which one they want unless choosing "
                "between them is genuinely necessary. "
                "Never claim an unavailable library can be used. Prefer Python standard-library code and built-in "
                "JavaScript functionality."
            )

        if (
            index == 0
            and teaching.active
            and teaching.phase == "preparing"
            and teaching.student_id
            == str(message.author_id or f"{message.platform}:{message.display_name}")
            and turn.speaker == teaching.teacher
            and turn.kind == "teaching_start"
        ):
            return (
                f"{quote}\n\n"
                "The viewer is beginning a coding lesson. Acknowledge naturally, "
                "name the small goal you inferred, and keep the opening short. "
                "Do not claim code has been written or run yet."
            )
        if turn.kind == "answer":
            if (
                index == 0
                and plan.decision.mode in ("group", "compare")
                and len(plan.turns) > 1
            ):
                return (
                    f"{quote} They asked everyone. Give your own answer; {name(plan.turns[1].speaker)} "
                    f"answers right after you, so you may end by asking {name(plan.turns[1].speaker)} what she thinks."
                )
            if previous:
                return (
                    f'{quote} {name(previous.speaker)} already answered: "{prompt_quote(previous.text, 200)}". '
                    "Give your own answer, reacting to theirs if it fits."
                )
            return f"{quote} Answer them."
        if turn.kind == "follow_up" and previous:
            said = prompt_quote(previous.text, 200)
            if asks_character(previous.text, room.get(turn.speaker)):
                return (
                    f'{quote} {name(previous.speaker)} answered and then asked you: "{said}" '
                    f"Answer {name(previous.speaker)}'s question directly with your own opinion, talking to her, "
                    "in one or two short sentences."
                )
            return (
                f'{quote} {name(previous.speaker)} answered: "{said}" '
                f"Talk to {name(previous.speaker)} directly: agree, disagree or tease her, and add your own take, "
                "in one or two short sentences."
            )
        if turn.kind == "ask":
            return (
                f"{quote} Ask {name(turn.addressee)} one short question or say one short line to them, "
                "as the viewer requested."
            )
        if turn.kind == "reply" and previous:
            last = index + 1 >= self._budget()
            return (
                f'{name(previous.speaker)} just said to you: "{prompt_quote(previous.text, 200)}" '
                f"Answer {name(previous.speaker)} directly, talking to her, in one or two short sentences."
                + (" Do not ask another question back." if last else "")
            )
        return f"{quote} Answer them."

    def _budget(self) -> int:
        return max(1, min(HARD_TURN_LIMIT, self.session.room.director.max_turns))

    def _continuation(self, plan: InteractionPlan, index: int) -> Optional[Turn]:
        """When a character asks the other one a question, the other one answers.
        Bounded by the turn budget, and optional so a waiting viewer wins."""
        turn = plan.turns[index]
        if index != len(plan.turns) - 1 or len(plan.turns) >= self._budget():
            return None
        available = self.session.state.available_characters()
        for other in available:
            if other == turn.speaker:
                continue
            if asks_character(turn.text, self.session.room.get(other)):
                return Turn(other, "reply", addressee=turn.speaker, optional=True)
        return None

    GAME_TALK_RE = re.compile(
        r"\b(?:games?|play|playing|trivia|quiz|rules|start|compete|challenge|battle|round|score)\b",
        re.I,
    )

    def _game_note(self, plan: Optional[InteractionPlan] = None) -> Optional[str]:
        show = self.session.show
        engine = show.engine
        if engine.playing:
            return (
                f"You are in the middle of {engine.active.info.display_name}. Answer briefly, "
                "the game continues right after."
            )
        text = plan.message.clean_text if plan else ""
        if not (show.current_offer() or self.GAME_TALK_RE.search(text)):
            return None
        games = []
        for factory in show.registry.enabled():
            rules = factory.info.how_to_play or factory.info.description
            games.append(
                f"{factory.info.display_name} ({rules})"
                if rules
                else factory.info.display_name
            )
        if not games:
            return "No games are installed right now; say so honestly if asked."
        return (
            "Games the stream can run: " + "; ".join(games) + ". "
            "The stream starts and runs games on screen by itself when a viewer says "
            '"let\'s start" or names the game. Never invent other rules, never host a game '
            "in conversation and never ask quiz questions yourselves; just invite them to say let's start."
        )

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------
    async def run(self, plan: InteractionPlan) -> InteractionPlan:
        if not self.turn_runner:
            raise RuntimeError("no turn runner configured")
        # Lines spoken from inside this interaction (and tasks it starts) must
        # not wait for the interaction's own speech lock.
        from .speech import INSIDE_INTERACTION

        INSIDE_INTERACTION.set(True)
        session = self.session
        message = plan.message

        teaching_started = False

        # --------------------------------------------------
        # ACTIVE TEACHING ROUTING
        #
        # Once a lesson is active, the current teacher owns
        # the conversation.
        #
        # Only the lesson owner may mutate lesson state.
        # --------------------------------------------------
        teaching = session.teaching.session
        addressed_targets = {
            str(target).strip().lower()
            for target in (
                getattr(getattr(plan, "decision", None), "addressed", None) or []
            )
        }
        addressed_teacher = next(
            (
                character
                for character in session.teaching.cast
                if str(character).strip().lower() in addressed_targets
            ),
            "",
        )

        if teaching.active:
            viewer_id = str(
                message.author_id or f"{message.platform}:{message.display_name}"
            )
            session.teaching.record_comment(
                viewer_id,
                message.display_name,
                message.clean_text,
            )

        # --------------------------------------------------
        # SEMANTIC TEACHING INTENT
        #
        # During a lesson, natural viewer language is
        # interpreted by the LLM before deterministic
        # authorization. During a lesson, the classifier receives the
        # explicit session transcript rather than private agent memory.
        # --------------------------------------------------
        semantic: dict[str, Any] = {}
        semantic_intent = ""

        # Let the semantic classifier understand every real viewer turn.
        # Do not hide semantic understanding behind keyword/phrase detection.
        should_classify = bool(plan.turns)

        if should_classify and plan.turns and self.teaching_intent_classifier:
            classifier_teacher = (
                teaching.teacher
                if teaching.active
                else addressed_teacher or plan.turns[0].speaker
            )

            try:
                semantic = await self.teaching_intent_classifier(
                    classifier_teacher,
                    message.clean_text,
                    {
                        "active": teaching.active,
                        "phase": teaching.phase,
                        "goal": teaching.goal,
                        "teacher": teaching.teacher,
                        "last_intent": teaching.last_intent,
                        "coding_lesson": (
                            session.teaching.coding_lesson.prompt_context()
                            if session.teaching.coding_lesson
                            else {}
                        ),
                    },
                )
            except Exception as exc:
                logger.error(f"VR Room: teaching semantic intent failed: {exc}")
                semantic = {}

            semantic_intent = str(semantic.get("intent") or "").lower()

            session.trace(
                "teaching_semantic_intent",
                interaction=plan.id,
                intent=semantic_intent or "none",
                confidence=semantic.get("confidence"),
                artifact_action=str(semantic.get("artifact_action") or "none"),
                code_len=len(str(semantic.get("code") or "")),
                subject=str(semantic.get("subject") or "")[:120],
            )

        # --------------------------------------------------
        # A BUILD REQUEST FROM LIVE CHAT
        #
        # On stream nobody says "start a lesson" first: "make a flappy bird
        # game" must just work. A build request starts the coding session if
        # none is running, and a different viewer asking for a build becomes
        # its owner. Changing someone else's program makes it their remix
        # (a new published game), so nobody overwrites another viewer's game.
        # --------------------------------------------------
        build_request = plan.turns and (
            semantic_intent in {"write_code", "write_and_run"}
            or str(semantic.get("artifact_action") or "") in {"create", "modify"}
        )
        if build_request:
            viewer_id = str(
                message.author_id or f"{message.platform}:{message.display_name}"
            )
            if not session.teaching.session.active:
                wanted = str(semantic.get("teacher") or "").lower()
                teacher = (
                    addressed_teacher
                    or (wanted if wanted in session.teaching.cast else "")
                    or plan.turns[0].speaker
                )
                if teacher in session.teaching.cast:
                    session.teaching.start(
                        student_id=viewer_id,
                        student_name=message.display_name,
                        goal=str(semantic.get("subject") or message.clean_text),
                        teacher=teacher,
                    )
                    session.teaching.record_comment(
                        viewer_id, message.display_name, message.clean_text
                    )
                    teaching = session.teaching.session
                    session.trace(
                        "teaching_session_started",
                        student=message.display_name,
                        teacher=teacher,
                        goal=message.clean_text[:120],
                        auto=True,
                    )
            elif not session.teaching.owns(viewer_id):
                session.teaching.hand_over(viewer_id, message.display_name)
                lesson = session.teaching.coding_lesson
                if (
                    lesson is not None
                    and str(semantic.get("artifact_action") or "") == "modify"
                    and not semantic.get("reopen")
                ):
                    lesson.creation_job_id = ""  # their remix, a new game
                teaching = session.teaching.session
                session.trace(
                    "teaching_handed_over", student=message.display_name
                )

        if teaching.active:
            # A previous verified action must never be narrated
            # again as though it happened for this message.
            teaching.last_action_result = {}

            student_id = str(
                message.author_id or f"{message.platform}:{message.display_name}"
            )

            owns_lesson = session.teaching.owns(student_id)

            semantic_teacher = str(semantic.get("teacher") or "").lower()

            artifact_action = str(semantic.get("artifact_action") or "none").lower()

            if artifact_action not in {
                "none",
                "create",
                "modify",
                "run",
            }:
                artifact_action = "none"

            semantic_confidence = float(semantic.get("confidence") or 0.0)

            # Handing the live coding work to the other character is a real
            # request ("let Luna take over"), never a side effect of saying
            # someone's name. Naming a character addresses her; it does not
            # take the keyboard away from whoever is coding.
            #
            # Only the session owner may hand it over, and the new owner must
            # be named by the semantic switch itself, falling back to whoever
            # the viewer addressed in that same comment.
            if owns_lesson and semantic_intent == "switch_teacher":
                candidate = (
                    semantic_teacher
                    if semantic_teacher in session.teaching.cast
                    else addressed_teacher
                )
                requested_teacher = (
                    candidate if candidate in session.teaching.cast else None
                )

            else:
                requested_teacher = None

            control_intents = {
                "pause",
                "resume",
                "quit",
                "slow_down",
                "speed_up",
                "simplify",
                "explain_more",
                "go_back",
                "skip",
            }

            if semantic_intent:
                control = (
                    semantic_intent if semantic_intent in control_intents else None
                )
            else:
                # No decision means no action. Silence is chat, never a guess.
                control = None

            capability_action = ""

            # Structured artifact permission is authoritative.
            #
            # none:
            #   normal chat; code stays untouched.
            #
            # run:
            #   execute the existing artifact exactly as-is.
            #
            # create / modify:
            #   these are the ONLY states allowed to replace code.
            # artifact_action is the explicit mutation/execution permission.
            # Do not silently discard it because the model's optional
            # confidence field is low or omitted.
            unsupported = str(semantic.get("unsupported") or "").strip()
            if unsupported:
                # The runtime is headless, so these can never work here. Say
                # so and offer the web equivalent rather than writing code
                # that is guaranteed to fail.
                artifact_action = "none"
                teaching.last_action_result = {
                    "ok": False,
                    "action": "unsupported",
                    "error": redirect_note(unsupported),
                }

            if artifact_action == "run":
                capability_action = "run_code"

            elif artifact_action == "create":
                capability_action = (
                    "write_and_run"
                    if semantic_intent == "write_and_run"
                    else "write_code"
                )

            elif artifact_action == "modify":
                capability_action = (
                    "write_and_run"
                    if semantic_intent == "write_and_run"
                    else "write_code"
                )

            # Compatibility fallback only when semantic classification
            # itself was unavailable or failed completely.
            if not semantic and control == "run_code":
                capability_action = "run_code"

            teaching.last_intent = semantic_intent or control or "answer"

            # ------------------------------------------
            # Teacher switch has first priority.
            # ------------------------------------------
            if (
                requested_teacher
                and requested_teacher != teaching.teacher
                and owns_lesson
            ):
                previous_teacher = teaching.teacher

                session.teaching.switch_teacher(
                    student_id,
                    requested_teacher,
                )

                teaching = session.teaching.session

                session.trace(
                    "teaching_teacher_switched",
                    student=message.display_name,
                    frm=previous_teacher,
                    to=teaching.teacher,
                    interaction=plan.id,
                )

                await session.push(
                    [
                        {
                            "op": "teaching",
                            "active": True,
                            "teacher": teaching.teacher,
                        }
                    ]
                )

                plan.turns = [
                    Turn(
                        teaching.teacher,
                        "teaching_switch",
                    )
                ]

            # ------------------------------------------
            # Coding action: only the owner may edit or execute.
            #
            # Semantic intent may REQUEST this, but only
            # the lesson owner is allowed to execute it.
            # The teacher speaks only AFTER this returns.
            # ------------------------------------------
            elif capability_action and owns_lesson:
                requested_language = str(semantic.get("language") or "python").lower()

                requested_code = (
                    str(semantic.get("code") or "")[:MAX_CODE_BYTES]
                    if artifact_action
                    in {
                        "create",
                        "modify",
                    }
                    else ""
                )
                if (
                    capability_action in {"write_code", "write_and_run"}
                    and requested_code
                ):
                    await session.push(
                        [
                            {
                                "op": "coding",
                                "phase": "writing",
                                "language": requested_language,
                                "filename": (
                                    "index.html"
                                    if requested_language == "web"
                                    else "main.js"
                                    if requested_language == "javascript"
                                    else "main.py"
                                ),
                                "code": requested_code,
                                "display": "typewriter",
                            }
                        ]
                    )
                # Say something the moment a build starts, while the code
                # is written at the same time, and show it on the Stage.
                acknowledgment_task = None
                if capability_action in {"write_code", "write_and_run"}:
                    acknowledgment_task = await self._start_build_acknowledgment(
                        session,
                        teaching,
                        message,
                        semantic,
                        artifact_action,
                        requested_language,
                    )
                if self.coding_action_runner:
                    action_result = await self.coding_action_runner(
                        capability_action,
                        {
                            "phase": teaching.phase,
                            "goal": teaching.goal,
                            "teacher": teaching.teacher,
                            "student_id": student_id,
                            "language": requested_language,
                            "code": requested_code,
                            "instruction": str(
                                semantic.get("instruction") or message.clean_text
                            ),
                            "requirements": semantic.get("requirements") or [],
                            "forbidden": semantic.get("forbidden") or [],
                            "remove_requirements": (
                                semantic.get("remove_requirements") or []
                            ),
                            "remove_forbidden": (
                                semantic.get("remove_forbidden") or []
                            ),
                            "project_summary": str(
                                semantic.get("project_summary") or ""
                            ),
                            "fresh": bool(semantic.get("fresh")),
                            "kind": str(semantic.get("kind") or ""),
                            "reopen": str(semantic.get("reopen") or ""),
                            "subject": str(semantic.get("subject") or ""),
                            # Who asked: kept for publishing credit, never sent to the model.
                            "viewer": {
                                "platform": message.platform,
                                "message_id": message.message_id,
                                "display_name": message.display_name,
                                "author_id": message.author_id,
                                "author_type": message.author_type,
                                "text": message.clean_text[:500],
                            },
                        },
                    )
                else:
                    action_result = {
                        "ok": False,
                        "action": capability_action,
                        "error": "The isolated coding runner is not connected.",
                    }

                if acknowledgment_task is not None:
                    try:
                        await acknowledgment_task
                    except Exception as exc:  # never let a spoken line break a build
                        logger.warning(f"VR Room: build acknowledgment failed: {exc}")
                    if not (action_result or {}).get("ok") and not (
                        action_result or {}
                    ).get("code"):
                        await session.push([{"op": "coding", "phase": "build_failed"}])

                teaching.last_action_result = dict(action_result or {})

                if action_result.get("ok"):
                    teaching.phase = "teaching"
                    teaching.last_error = ""
                    new_goal = str(semantic.get("subject") or "").strip()
                    if semantic.get("fresh") and new_goal:
                        # The viewer moved on to something new; the lesson is
                        # now about that, not the previous program.
                        teaching.goal = new_goal[:200]
                        if session.teaching.coding_lesson is not None:
                            session.teaching.coding_lesson.goal = new_goal[:200]

                elif not action_result.get("ok"):
                    teaching.last_error = str(
                        action_result.get("error") or "coding action failed"
                    )[:1000]

                session.trace(
                    "coding_action_result",
                    interaction=plan.id,
                    student=message.display_name,
                    teacher=teaching.teacher,
                    action=capability_action,
                    ok=bool(action_result.get("ok")),
                    exit_code=(action_result.get("run_result") or {}).get("exit_code"),
                )

                # Keep code display and actual process output as ordered events.
                await session.push(
                    [
                        {
                            "op": "coding",
                            "phase": "run_result"
                            if action_result.get("run_result")
                            else "ready",
                            "language": action_result.get("language", "python"),
                            "filename": action_result.get("filename", "main.py"),
                            "result": action_result.get("run_result"),
                            "artifacts": action_result.get("artifacts", []),
                            "preview_html": action_result.get("preview_html", ""),
                            "preview_ready": bool(action_result.get("preview_ready")),
                            # The source too, so a Stage that joined mid-build
                            # (OBS switched or refreshed) shows the code.
                            "code": str(action_result.get("code") or "")[:32000],
                            "error": action_result.get("error", ""),
                        }
                    ]
                )

                plan.turns = [
                    Turn(
                        teaching.teacher,
                        "teaching_answer",
                    )
                ]

            # ------------------------------------------
            # Deterministic lesson controls.
            # Non-owners cannot execute these.
            # ------------------------------------------
            elif control and owns_lesson:
                control_teacher = teaching.teacher

                if control == "pause":
                    session.teaching.pause(student_id)

                elif control == "resume":
                    session.teaching.resume(student_id)

                elif control == "slow_down":
                    session.teaching.set_pace(
                        student_id,
                        "slow",
                    )

                elif control == "speed_up":
                    session.teaching.set_pace(
                        student_id,
                        "fast",
                    )

                elif control == "simplify":
                    session.teaching.set_pace(
                        student_id,
                        "beginner",
                    )

                elif control == "explain_more":
                    session.teaching.set_pace(
                        student_id,
                        "detailed",
                    )

                elif control == "go_back":
                    session.teaching.go_back(student_id)

                elif control == "skip":
                    session.teaching.skip(student_id)

                elif control == "quit":
                    # State ends now, but the UI stays in
                    # Teaching Mode until the teacher finishes
                    # the short spoken sign-off below.
                    session.teaching.quit(student_id)

                session.trace(
                    "teaching_control_applied",
                    student=message.display_name,
                    control=control,
                    interaction=plan.id,
                )

                plan.turns = [
                    Turn(
                        control_teacher,
                        f"teaching_control_{control}",
                    )
                ]

            else:
                # CHARACTER OWNERSHIP.
                #
                # A viewer who names a character is talking to THAT character,
                # and an active coding session never steals the message:
                #
                #     "Mika how are you?"  -> Mika, even while Luna is coding
                #
                # Only an unaddressed comment belongs to the session owner by
                # default, because that is who the room is currently watching.
                answering = addressed_teacher or teaching.teacher
                plan.turns = [
                    Turn(
                        answering,
                        "teaching_answer"
                        if answering == teaching.teacher
                        else "answer",
                    )
                ]

        if (
            semantic_intent == "start_lesson"
            and not session.teaching.session.active
            and plan.turns
        ):
            semantic_teacher = str(semantic.get("teacher") or "").lower()

            teacher = (
                addressed_teacher
                if addressed_teacher
                else semantic_teacher
                if semantic_teacher in session.teaching.cast
                else plan.turns[0].speaker
            )

            if teacher in session.teaching.cast:
                student_id = str(
                    message.author_id or f"{message.platform}:{message.display_name}"
                )

                session.teaching.start(
                    student_id=student_id,
                    student_name=message.display_name,
                    goal=str(semantic.get("goal") or message.clean_text),
                    teacher=teacher,
                )
                session.teaching.record_comment(
                    student_id,
                    message.display_name,
                    message.clean_text,
                )

                # The requested teacher immediately owns
                # the teaching interaction.
                plan.turns = [
                    Turn(
                        teacher,
                        "teaching_start",
                    )
                ]

                teaching_started = True

                session.trace(
                    "teaching_session_started",
                    student=message.display_name,
                    teacher=teacher,
                    goal=message.clean_text[:120],
                )

        self.interactions += 1
        self.last_plan = plan
        session.state.active_interaction = plan.id
        session.state.current_topic = message.clean_text[:120]
        session.state.add_line(
            f"@{message.display_name.lstrip('@')}", message.clean_text
        )
        session.trace("director_decision", interaction=plan.id, **plan.describe())
        if plan.turns and (
            session.teaching.session.active
            or plan.turns[0].kind.startswith("teaching_")
        ):
            targets = [plan.turns[0].speaker]
        else:
            targets = plan.decision.addressed or (
                ["all"]
                if plan.decision.mode
                in (
                    "group",
                    "compare",
                )
                else [plan.turns[0].speaker]
                if plan.turns
                else []
            )
        ops = session.emit(
            ev.INTERACTION_STARTED,
            interaction=plan.id,
            mode=plan.decision.mode,
            speakers=[t.speaker for t in plan.turns],
        )
        ops += session.emit(ev.VIEWER_ADDRESSED, targets=targets)
        ops += self._opening_reactions(plan)

        await session.push(ops)

        rerouted = False
        index = -1
        while index + 1 < len(plan.turns):
            index += 1
            turn = plan.turns[index]
            if turn.optional and self.pending_probe():
                turn.skipped = "viewer waiting"
                for rest in plan.turns[index:]:
                    if rest.optional and not rest.skipped:
                        rest.skipped = "viewer waiting"
                session.trace(
                    "turn_skipped",
                    interaction=plan.id,
                    character=turn.speaker,
                    reason=turn.skipped,
                )
                break
            if turn.speaker not in session.state.available_characters():
                replacement = (
                    self._replacement(turn.speaker)
                    if index == 0 and not rerouted
                    else None
                )
                if not replacement:
                    turn.skipped = "unavailable"
                    continue
                rerouted = True
                turn.speaker = replacement
            previous = next((t for t in reversed(plan.turns[:index]) if t.text), None)
            if (
                previous
                and turn.speaker == previous.speaker
                and turn.kind in ("follow_up", "reply", "answer")
            ):
                # After a reroute nobody reacts to themselves.
                others = [
                    c
                    for c in session.state.available_characters()
                    if c != previous.speaker
                ]
                if not others or turn.optional:
                    turn.skipped = "same speaker"
                    continue
                turn.speaker = others[0]
            prompt = self._prompt(plan, index)
            session.trace(
                "character_selected",
                interaction=plan.id,
                character=turn.speaker,
                kind=turn.kind,
            )
            try:
                text = await self.turn_runner(turn, prompt, plan)
            except Exception as exc:
                logger.error(f"VR Room: turn by {turn.speaker} failed: {exc}")
                text = ""
            if not text:
                session.record_failure(turn.speaker, "empty or failed turn")
                replacement = (
                    self._replacement(turn.speaker)
                    if index == 0 and not rerouted
                    else None
                )
                if replacement:
                    rerouted = True
                    session.trace(
                        "turn_rerouted",
                        interaction=plan.id,
                        frm=turn.speaker,
                        to=replacement,
                    )
                    turn.speaker = replacement
                    try:
                        text = await self.turn_runner(
                            turn, self._prompt(plan, index), plan
                        )
                    except Exception as exc:
                        logger.error(f"VR Room: rerouted turn failed: {exc}")
                        text = ""
                if not text:
                    turn.skipped = "failed"
                    continue
            turn.text = text
            if session.teaching.session.active:
                session.teaching.record_teacher_turn(text)
            session.record_success(turn.speaker)
            session.state.add_line(turn.speaker, text)
            session.show.note_text(text)
            if not turn.kind.startswith("teaching_"):
                await session.push(self._listener_reactions(plan, turn))

            # QUIT is delayed until after the teacher's spoken
            # sign-off so the presentation does not disappear
            # underneath their final sentence.
            if turn.kind == "teaching_control_quit":
                await session.push(
                    [
                        {
                            "op": "teaching",
                            "active": False,
                            "teacher": turn.speaker,
                        }
                    ]
                )

                session.trace(
                    "teaching_mode_exited",
                    interaction=plan.id,
                    teacher=turn.speaker,
                )

                break

            # A new lesson stays in PREPARING while the teacher gives the
            # acknowledgement in the normal room. turn_runner() does not
            # return until that spoken turn has completed, so this is the
            # deterministic NORMAL -> TEACHING transition point.
            if teaching_started and index == 0:
                teaching_session = session.teaching.session

                # If the original speaker failed and ConversationDirector
                # successfully rerouted the turn, the actual speaker becomes
                # the teacher.
                if turn.speaker != teaching_session.teacher:
                    session.teaching.switch_teacher(
                        teaching_session.student_id,
                        turn.speaker,
                    )

                session.teaching.set_phase("teaching")

                session.trace(
                    "teaching_mode_entered",
                    student=teaching_session.student_name,
                    teacher=session.teaching.session.teacher,
                    interaction=plan.id,
                )

                await session.push(
                    [
                        {
                            "op": "teaching",
                            "active": True,
                            "teacher": session.teaching.session.teacher,
                        }
                    ]
                )

                # The teacher has acknowledged the lesson and Teaching Mode
                # now owns the interaction. Do not allow ordinary room turns
                # or follow-up characters to continue after this transition.
                session.trace(
                    "normal_interaction_stopped_for_teaching",
                    interaction=plan.id,
                    teacher=session.teaching.session.teacher,
                )
                break

            follow = (
                None
                if turn.kind.startswith("teaching_")
                else self._continuation(plan, index)
            )
            if follow is not None:
                plan.turns.append(follow)
                session.trace(
                    "turn_added",
                    interaction=plan.id,
                    character=follow.speaker,
                    reason="asked by " + turn.speaker,
                )

        session.state.active_interaction = None
        spoken = sum(1 for t in plan.turns if t.text)
        session.trace("interaction_finished", interaction=plan.id, turns=spoken)
        await session.push(
            session.emit(ev.INTERACTION_FINISHED, interaction=plan.id, turns=spoken)
        )
        return plan

    async def _start_build_acknowledgment(
        self, session, teaching, message, semantic, artifact_action, language
    ):
        """Show "writing the code" on the Stage and speak one short line.

        Returns the speaking task so the explanation turn waits for it and the
        two lines never overlap. Speaking only happens when speech is allowed
        (a viewer is active), like every other line.
        """
        subject = str(semantic.get("subject") or "").strip()
        await session.push(
            [
                {
                    "op": "coding",
                    "phase": "building",
                    "teacher": teaching.teacher,
                    "language": language,
                    "subject": subject[:120],
                }
            ]
        )
        if not session.speech_allowed():
            return None
        line = acknowledgment(
            teaching.teacher,
            message.display_name,
            subject,
            fresh=bool(semantic.get("fresh")) or artifact_action == "create",
        )
        session.trace("build_acknowledgment", character=teaching.teacher, text=line)
        session.teaching.record_teacher_turn(line)
        return asyncio.create_task(session.speech.say(teaching.teacher, line))

    def _prompt(self, plan: InteractionPlan, index: int) -> str:
        turn = plan.turns[index]
        profile = self.session.room.get(turn.speaker)
        return turn_prompt(
            profile,
            self.session.room,
            self.session.state,
            self._instruction(plan, index),
            intent=turn.intent if turn.intent.requested else None,
            game_note=self._game_note(plan),
        )

    def _replacement(self, speaker: str) -> Optional[str]:
        others = [c for c in self.session.state.available_characters() if c != speaker]
        return others[0] if others else None

    # ------------------------------------------------------------------
    # silent reactions (free: looks and expressions, no LLM)
    # ------------------------------------------------------------------
    def _chance(self) -> bool:
        return self.rng.random() < self.session.room.director.silent_reaction_chance

    def _opening_reactions(self, plan: InteractionPlan) -> list[dict[str, Any]]:
        session = self.session
        decision = plan.decision
        ops: list[dict[str, Any]] = []
        subject = (
            decision.addressed[0] if decision.addressed else (decision.first or None)
        )
        if not subject:
            return ops
        if decision.compliment:
            if decision.mode in ("group", "compare") or not decision.addressed:
                for cid in session.state.available_characters():
                    ops += session.emit(ev.REACTION, character=cid, reaction="happy")
            else:
                ops += session.emit(ev.REACTION, character=subject, reaction="shy")
                for cid in session.state.available_characters():
                    if cid != subject and self._chance():
                        ops += session.emit(
                            ev.REACTION,
                            character=cid,
                            reaction="suspicious"
                            if self.rng.random() < 0.5
                            else "happy",
                        )
        elif decision.tease:
            ops += session.emit(ev.REACTION, character=subject, reaction="suspicious")
        return ops

    def _listener_reactions(
        self, plan: InteractionPlan, turn: Turn
    ) -> list[dict[str, Any]]:
        session = self.session
        ops: list[dict[str, Any]] = []
        upcoming = {t.speaker for t in plan.turns if not t.text and not t.skipped}
        for cid in session.state.available_characters():
            if cid == turn.speaker or cid in upcoming or not self._chance():
                continue
            reaction = "suspicious" if plan.decision.mode == "compare" else "agree"
            if plan.decision.tease:
                reaction = "surprised"
            ops += session.emit(ev.REACTION, character=cid, reaction=reaction)
        return ops

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "interactions": self.interactions,
            "last_plan": self.last_plan.describe() if self.last_plan else None,
        }


BUILD_STYLE = {
    "mika": (
        "Mika is loud and dramatic: groans, gasps, 'my code betrayed me!', "
        "then bounces right back."
    ),
    "luna": (
        "Luna is dry and deadpan: a calm sigh, a raised eyebrow, sarcasm "
        "delivered completely straight."
    ),
}


def build_attitude(teacher: str, action_result: dict) -> str:
    """Roast and build: a streamer with opinions, not an obedient assistant.

    One line of attitude that fits what really happened, then the facts.
    """
    style = BUILD_STYLE.get(str(teacher or "").lower(), BUILD_STYLE["mika"])
    check = action_result.get("check") or {}
    run = action_result.get("run_result") or {}
    broken = (check and not check.get("ok") and not check.get("skipped")) or (
        run and (run.get("exit_code") not in (0, None) or run.get("error"))
    )
    if broken:
        mood = (
            "It is still not working: be openly, playfully upset about it in "
            "your own style for one line (never blame the viewer), then say "
            "honestly what is wrong and what you will try next."
        )
    elif action_result.get("repaired"):
        mood = (
            "It broke and you fixed it: one line of relief or triumph, in "
            "your own style."
        )
    else:
        mood = (
            "If the request was very simple, silly or absurd (a neon pink "
            "potato shooter, make everything huge), playfully roast the design "
            "choice in one line while clearly happy to build it. Tease the "
            "idea, never the person. If it was a good idea, say so with "
            "real enthusiasm instead."
        )
    return (
        f"Attitude: you are a streamer with opinions, not an assistant. {style} {mood}"
    )


def _verification_note(action_result: dict) -> str:
    """What the browser check and repair actually established, for the turn.

    The character may only describe these facts: a check that passed, a bug
    that was caught and fixed, or problems that are still there.
    """
    if not action_result:
        return ""
    note = ""
    first = action_result.get("first_problems") or []
    if action_result.get("repaired") and first:
        note += (
            f" Before showing it you tested it in a browser, caught a problem "
            f"({str(first[0])[:160]}) and fixed it. You may mention catching that bug "
            "in one short, fun line."
        )
    elif action_result.get("repaired") and action_result.get("first_error"):
        note += (
            " The first run failed, you read the error, fixed the code and ran it "
            "again; the result above is from the second run. You may mention the "
            "fix in one short line."
        )
    check = action_result.get("check") or {}
    if check and not check.get("skipped"):
        if check.get("ok"):
            note += (
                " It was opened in a real browser: no errors, it draws and it moves. "
                "It is running in the preview now."
            )
        elif check.get("problems"):
            note += (
                " A browser test still shows a problem: "
                + "; ".join(str(p)[:140] for p in check["problems"][:2])
                + ". Say honestly that this part is not working yet and what you would "
                "change next. Do not claim it works."
            )
    return note
