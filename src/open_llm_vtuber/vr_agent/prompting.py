"""Builds the per-message livestream prompt sent to the existing agent.

The capability list and the outcome of the viewer's action request are added
here, in the same single LLM request that produces the spoken reply. No extra
LLM call is made for actions.
"""

from __future__ import annotations

from typing import Optional

from .capabilities import CharacterCapabilities
from .intent import ActionIntent, intent_verb
from .text_safety import prompt_quote


def build_livestream_prompt(
    viewer_name: str,
    viewer_text: str,
    capabilities: Optional[CharacterCapabilities],
    intent: ActionIntent,
    is_system_prompt: bool = False,
) -> str:
    if is_system_prompt:
        # Internal nudges (quiet-chat banter) are not viewer messages.
        return (
            "[Livestream] You are live on YouTube right now. "
            f"{viewer_text} Keep it to one or two short sentences."
        )

    name = prompt_quote(viewer_name, 60) or "a viewer"
    text = prompt_quote(viewer_text, 280)
    lines = [
        "[Livestream] You are live on YouTube right now, reading your live chat out loud.",
        f'YouTube viewer {name} says: "{text}"',
    ]
    if capabilities is not None:
        lines.append(
            "Physical actions your avatar can really perform on stream: "
            f"{capabilities.prompt_summary()}. You cannot do anything else physically."
        )

    if intent.requested:
        asked = intent_verb(intent.requested)
        if intent.supported and intent.action:
            lines.append(
                f"The viewer asked you to {asked}. Your avatar is doing it right now "
                f"({intent.action.description}) while you talk, so you can acknowledge it naturally."
            )
        elif intent.action:
            lines.append(
                f"The viewer asked you to {asked}, which your avatar cannot do. "
                f"Say so honestly and lightly. Instead your avatar is doing this right now: "
                f"{intent.action.description}."
            )
        else:
            lines.append(
                f"The viewer asked you to {asked}, which your avatar cannot do. "
                "Say so honestly and lightly; never claim you did it."
            )

    lines.append(
        "Reply naturally as the configured character. Keep it stream-friendly and concise. "
        "Do not write stage directions or describe actions in asterisks. "
        "Acknowledge the viewer when it feels natural, but do not say the username every time."
    )
    return "\n".join(lines)
