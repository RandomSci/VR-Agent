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

import itertools
import random
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from ..vr_agent.intent import NO_INTENT, ActionIntent, resolve_intent
from . import awareness
from ..vr_agent.text_safety import prompt_quote
from . import events as ev
from .live_message import LiveMessage
from .prompting import turn_prompt, viewer_quote
from .routing import RoutingDecision, _strip_names, route_message

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
    performed: Any = None  # a stage request carried out for this message

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

        performed = self.session.take_performed(message.message_id)
        if performed is not None:
            # The stage already acted on this message: the one who acted
            # answers first, and her prompt says exactly what is happening.
            plan.performed = performed
            if (
                performed.character in available
                and plan.turns[0].speaker != performed.character
            ):
                plan.turns[0].speaker = performed.character
                for turn in plan.turns[1:]:
                    if turn.speaker == performed.character:
                        turn.speaker = first
                    if turn.addressee == performed.character:
                        turn.addressee = None
        elif decision.intent:
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
        session = self.session
        message = plan.message
        self.interactions += 1
        self.last_plan = plan
        session.state.active_interaction = plan.id
        session.state.current_topic = message.clean_text[:120]
        session.state.add_line(
            f"@{message.display_name.lstrip('@')}", message.clean_text
        )
        session.trace("director_decision", interaction=plan.id, **plan.describe())
        targets = plan.decision.addressed or (
            ["all"]
            if plan.decision.mode in ("group", "compare")
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
            session.record_success(turn.speaker)
            session.state.add_line(turn.speaker, text)
            session.show.note_text(text)
            await session.push(self._listener_reactions(plan, turn))
            follow = self._continuation(plan, index)
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
            world_note=awareness.build(self.session, turn.speaker),
            performed=plan.performed if index == 0 else None,
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
