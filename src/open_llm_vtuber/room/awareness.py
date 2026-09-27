"""Character awareness: a compact slice of the ONE authoritative room state.

When a viewer triggers a reply, the speaking character gets a short block of
facts read straight from RoomState, the Stage Director, the World Director,
the Game Engine and (when running) the Adventure Director. Nothing here is
generated or guessed, and no LLM is involved: state is perception.

Only what is relevant is included (no game block without a game, at most a
few visible objects and recent events), so the block stays small. The
character is told plainly that anything not listed is unknown to her, so she
does not invent authoritative-sounding facts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .world_catalog import side_of

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

MAX_OBJECTS = 6
MAX_EVENTS = 5
EVENT_WINDOW_SECONDS = 900


def _ago(seconds: float) -> str:
    if seconds < 45:
        return "just now"
    minutes = int(round(seconds / 60))
    return f"{minutes} min ago" if minutes > 1 else "a minute ago"


def _name(session: "RoomSession", side: Optional[str]) -> str:
    if side == "viewers":
        return "chat"
    profile = session.room.get(side) if side else None
    return profile.name if profile else (side or "nobody")


# ---------------------------------------------------------------------------
# games
# ---------------------------------------------------------------------------


def _scores(session: "RoomSession", game: Any) -> str:
    scores = getattr(game, "scores", None) or {}
    return ", ".join(f"{_name(session, k)} {v}" for k, v in scores.items())


def describe_game(session: "RoomSession", speaker: str) -> Optional[str]:
    engine = session.show.engine
    if not engine.playing or engine.active is None:
        return None
    game = engine.active
    gid = game.info.id
    phase = getattr(game, "phase", "")
    parts = [f"Game on the board right now: {game.info.display_name} (phase {phase})."]
    sides = list(getattr(game, "sides", []) or [])
    if gid == "tictactoe":
        marks = getattr(game, "marks", {}) or {}
        who = "; ".join(
            f"{_name(session, side)} plays {mark}" for side, mark in marks.items()
        )
        if who:
            parts.append(who + ".")
        cells = getattr(game, "cells", []) or []
        taken = [f"{m} in {i + 1}" for i, m in enumerate(cells) if m]
        parts.append(
            "Board (cells 1 to 9, 5 is the center): "
            + (", ".join(taken) if taken else "empty")
            + "."
        )
        turn = getattr(game, "turn", None)
        if turn and phase in ("vote", "think"):
            parts.append(f"It is {_name(session, turn)}'s turn.")
        last = getattr(game, "last_move", None)
        if last is not None and cells and cells[last]:
            parts.append(f"Last move: {cells[last]} in {last + 1}.")
    elif gid == "rps":
        parts.append(f"Round {getattr(game, 'round_no', 0)}.")
        if phase == "vote":
            counts = game.vote.counts() if getattr(game, "vote", None) else {}
            parts.append(
                "Chat is voting now"
                + (
                    f" ({', '.join(f'{k} {v}' for k, v in counts.items())})"
                    if counts
                    else ""
                )
                + "; hands stay hidden until the reveal."
            )
        throws = getattr(game, "throws", {}) or {}
        if phase in ("reveal", "between", "finished") and any(throws.values()):
            parts.append(
                "Revealed: "
                + ", ".join(f"{_name(session, s)} {t}" for s, t in throws.items() if t)
                + "."
            )
    elif gid == "trivia":
        parts.append(f"Round {getattr(game, 'round_no', 0)}.")
        question = getattr(game, "question", None) or {}
        if phase == "question":
            parts.append(
                "Chat's answer window is open. Never reveal or hint at the answer."
                + (
                    f" Question on screen: {question.get('question', '')}"
                    if question
                    else ""
                )
            )
        elif phase == "turn":
            parts.append(
                "It is a character's turn to answer. Never reveal the answer early."
            )
        elif phase in ("reveal", "between") and question:
            parts.append(f"Answer revealed: {question.get('correct_answer', '')}.")
        winner = getattr(game, "round_winner", None)
        if winner and phase in ("reveal", "between"):
            parts.append(f"{_name(session, winner)} won this round.")
    if sides and speaker in sides:
        parts.append(
            f"You are playing ({'against chat' if 'viewers' in sides else 'against ' + _name(session, next((s for s in sides if s != speaker), ''))})."
        )
    elif sides:
        parts.append("You are not playing this one; you are cheering and commenting.")
    score = _scores(session, game)
    if score:
        parts.append(f"Score: {score}.")
    parts.append(
        "The game itself decides moves, answers and scores; never contradict it."
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# abilities
# ---------------------------------------------------------------------------


def abilities(session: "RoomSession", speaker: str) -> str:
    profile = session.room.get(speaker)
    items = []
    lock = session.stage.locked_reason(speaker)
    zones = session.stage.zone_names()
    labels = [session.world.zone_label(z) for z in zones]
    if lock:
        items.append(f"moving around the stage is paused because {lock}")
    else:
        items.append(
            "move to a spot on stage ("
            + ", ".join(labels)
            + "), come closer, go back to your spot"
        )
    items.append(
        "look at the viewer, the other character or anything listed as visible"
    )
    items.append(
        "hop or jump in place" + ("" if not session.show.engine.playing else "")
    )
    if not session.show.engine.playing:
        items.append("a little dance in place")
    if profile and "magic" in profile.abilities:
        items.append(
            "magic on small creatures or glowing things that are visible (it makes creatures vanish and makes stones or lanterns glow)"
        )
    if profile and profile.capabilities:
        gestures = [a.label for a in profile.capabilities.viewer_actions()]
        if gestures:
            items.append("gestures and faces: " + ", ".join(gestures))
    return (
        "What you can really do on stream right now (the stream performs it; if a viewer asks for "
        "one of these, it is happening, so never say you cannot): "
        + "; ".join(items)
        + ". "
        "You cannot do anything else physically, and you have no real walking animation, only a short glide."
    )


# ---------------------------------------------------------------------------
# the block
# ---------------------------------------------------------------------------


def build(session: "RoomSession", speaker: str) -> str:
    state = session.state
    now = session.clock()
    lines = [
        "[What is true on stream right now. This comes from the stream itself; never contradict it.]"
    ]

    me = state.characters.get(speaker)
    if me:
        doing = me.activity or "chatting with viewers"
        lines.append(
            f"You ({_name(session, speaker)}) stand {side_of(me.x)} of the stage; doing: {doing}."
        )
    for cid, other in state.characters.items():
        if cid == speaker or not other.is_available():
            continue
        where = side_of(other.x)
        doing = f", {other.activity}" if other.activity else ""
        lines.append(f"{other.name} stands {where}{doing}.")

    scene = state.scene
    place = (
        scene.name
        if scene.id != "room"
        else "your cozy stream room at night (the city is outside the window)"
    )
    weather = (
        f", weather {scene.weather}"
        if scene.weather and scene.weather != "clear"
        else ""
    )
    lines.append(f"Place: {place} ({scene.time}{weather}).")

    objects = state.visible_objects()[:MAX_OBJECTS]
    if objects:
        lines.append(
            "Visible right now: "
            + "; ".join(
                f"{o.label} {side_of(o.x)}"
                + (
                    f" ({o.state})"
                    if o.state and o.state not in ("idle", "still")
                    else ""
                )
                for o in objects
            )
            + "."
        )
    else:
        lines.append("Nothing special is visible in the scene right now.")

    events = [e for e in state.history if now - e.at <= EVENT_WINDOW_SECONDS][
        -MAX_EVENTS:
    ]
    if events:
        lines.append(
            "Recent events, oldest first: "
            + "; ".join(f"{_ago(now - e.at)} {e.text}" for e in events)
            + "."
        )

    adventure = getattr(session, "adventure", None)
    if adventure is not None:
        note = adventure.context_note()
        if note:
            lines.append(note)

    game = describe_game(session, speaker)
    if game:
        lines.append(game)

    lines.append(abilities(session, speaker))
    lines.append(
        "If you are asked about something not listed here, you do not know it; say so lightly instead of inventing it."
    )
    return "\n".join(lines)
