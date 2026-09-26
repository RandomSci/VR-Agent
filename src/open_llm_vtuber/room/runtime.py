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

import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from loguru import logger

from . import events as ev
from .prompting import strip_wrapping_quotes, system_prompt

if TYPE_CHECKING:  # pragma: no cover
    from .director import InteractionPlan, Turn
    from .session import RoomSession


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
        self.errors: dict[str, str] = {}

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
