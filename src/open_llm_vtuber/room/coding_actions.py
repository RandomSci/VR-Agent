"""The one semantic decision behind Code in Public.

A viewer comment becomes exactly one structured action. Nothing in here
matches English phrases, keywords or regexes against viewer text, so it works
the same in Tagalog, Taglish, Japanese or anything else chat types.

    chat             talk; the source is not touched
    start_coding     begin coding in public on something
    create           write a new program
    create_and_run   write it and execute it
    modify           change the existing program
    modify_and_run   change it and execute it
    run              execute the existing source, byte for byte
    control          a session control (pause, resume, quit, slower, ...)
    switch_teacher   hand the keyboard to the other character

Two deliberate properties:

* A direct request is already authorization. "Make the heart green" and "Can
  you make the heart green?" both mean modify, immediately. The host never
  waits for a follow-up "okay" or "do it".
* Deciding is cheap and always happens; WRITING code is expensive and happens
  only for create/modify. Plain chat therefore never pays for code generation,
  which is what made ordinary conversation slow.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .capabilities import KINDS, kind_for, kinds_for_decision
from .stage_design import live_rules

# Every action the host can carry out.
ACTIONS = (
    "chat",
    "start_coding",
    "create",
    "create_and_run",
    "modify",
    "modify_and_run",
    "run",
    "control",
    "switch_teacher",
)
WRITES_CODE = frozenset({"create", "create_and_run", "modify", "modify_and_run"})
RUNS_CODE = frozenset({"create_and_run", "modify_and_run", "run"})
CONTROLS = (
    "pause",
    "resume",
    "quit",
    "slow_down",
    "speed_up",
    "simplify",
    "explain_more",
    "go_back",
    "skip",
)
LANGUAGES = ("python", "web")

# What the runtime can actually execute. Anything else is redirected rather
# than attempted, because the stream runs headless.
UNSUPPORTED = {
    "manim": "an animation rendered in the browser with HTML, CSS and JavaScript",
    "turtle": "the same drawing in the browser with HTML canvas and JavaScript",
    "tkinter": "a small web page with HTML, CSS and JavaScript",
    "pygame": "a small web game with HTML canvas and JavaScript",
    "desktop_gui": "a small web page with HTML, CSS and JavaScript",
}


# How each action looks to the ConversationDirector, which thinks in
# (intent, artifact_action). Keeping the mapping here means the director
# never has to know about the wording of the decision.
DIRECTOR_INTENT: dict[str, tuple[str, str]] = {
    "chat": ("answer", "none"),
    "start_coding": ("start_lesson", "none"),
    "create": ("write_code", "create"),
    "create_and_run": ("write_and_run", "create"),
    "modify": ("write_code", "modify"),
    "modify_and_run": ("write_and_run", "modify"),
    "run": ("run_code", "run"),
    "control": ("answer", "none"),
    "switch_teacher": ("switch_teacher", "none"),
}


@dataclass
class CodingDecision:
    """What the host will actually do about one viewer comment."""

    action: str = "chat"
    language: str = ""  # python | web, when code is involved
    control: str = ""  # when action == control
    teacher: str = ""  # when action == switch_teacher
    subject: str = ""  # short human description of the request
    unsupported: str = ""  # a named technology the runtime cannot run
    # True when the request is a different program than the one on screen
    # (a new topic, a new kind of program), so it is written from scratch
    # instead of being edited into the old one.
    fresh: bool = False
    # What to build or change, resolved from recent chat so it stands alone.
    # "yeah update it" becomes "Python list basics: ...".
    brief: str = ""
    # Which capability to build with (capabilities.KINDS); decides the
    # library, guide and starting template the code writer receives.
    kind: str = ""
    # The id of an earlier published game to go back to and change (from
    # situation.published_games). Empty for the program on screen.
    reopen: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def writes_code(self) -> bool:
        return self.action in WRITES_CODE

    @property
    def runs_code(self) -> bool:
        return self.action in RUNS_CODE

    @property
    def touches_source(self) -> bool:
        return self.writes_code

    def describe(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "language": self.language,
            "control": self.control,
            "teacher": self.teacher,
            "subject": self.subject[:120],
            "unsupported": self.unsupported,
            "fresh": self.fresh,
            "brief": self.brief[:160],
            "kind": self.kind,
            "reopen": self.reopen,
        }


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

DECIDE_SYSTEM = """You route one live-chat comment for two AI VTubers who code in public.

Reply with ONE JSON object and nothing else:
{"action": "...", "language": "...", "control": "...", "teacher": "...",
 "subject": "...", "unsupported": "...", "fresh": false, "brief": "...",
 "kind": "...", "reopen": ""}

action is exactly one of:
  chat            talking, reacting, greeting, asking about them, unclear text
  start_coding    asking them to start coding/building something
  create          asking for a new program when there is no suitable one yet
  create_and_run  the same, and also asking to see it run
  modify          asking to change the existing program
  modify_and_run  the same, and also asking to see it run
  run             asking to execute the existing program, unchanged
  control         pause, resume, stop, slower, faster, simpler, more detail,
                  go back, skip
  switch_teacher  asking the other character to take over the coding

Rules:
- Judge ONLY the latest comment, in whatever language it is written.
- A direct request IS the authorization. "Make the heart green" and
  "Can you make the heart green?" are both modify. Never answer chat just
  because the comment was phrased politely or as a question.
- Short agreement with no request of its own ("ok", "nice", "sure", "go on")
  is chat, not an action.
- run means execute what already exists. Never choose run when the comment
  also asks for a change; choose modify_and_run.
- If there is no existing program yet, a change request is create.
- Asking about or discussing code without requesting work is chat.
- If you are unsure, choose chat.
- Change or new? If a request to build could mean either, it is a CHANGE to
  the program on screen (modify, fresh false). Only treat it as a new program
  (create, fresh true) when the viewer says new, another or different, or
  clearly asks for a different kind of thing than what is on screen.

kind: for create, create_and_run, modify and modify_and_run, the ONE kind
of program to build, from this list (it also decides the language):
__KINDS__
Pick by what the viewer wants to SEE, without asking them technical
questions. Learning Python basics is python_lesson (never a chart). Anything
in Python that should move is python_animation. A shape, drawing or
art in Python (a heart, flower, spiral) is python_image, which animates.
A picture, drawing, painting, portrait or scene of something (animals,
places, people, Mika or Luna) is web_art, a real painting brought to life,
unless the viewer says Python. A game is a web_game kind.
For a change to the existing program keep its kind unless the change needs
another (making a still chart move becomes python_animation). Empty for
every other action.

language: "web" for anything that moves, animates, is interactive, is a game
or is a page; "python" for data, algorithms, maths and charts. Empty when no
code is involved. Prefer "web" whenever the result should move. When kind is
set, language must match it.

control: one of pause, resume, quit, slow_down, speed_up, simplify,
explain_more, go_back, skip. Empty unless action is control.

teacher: the character named to take over. Empty unless action is
switch_teacher.

subject: a short plain description of what was asked, for the log.

fresh: true when the comment asks for something DIFFERENT from the existing
program (a new topic, a new lesson, a different kind of program, or a new
idea that does not build on it), so it must be written from scratch. false
when it changes, extends or fixes the existing program. Look at
existing_program.preview and what_they_are_building to judge. Example: the
program is a starfield animation and the viewer now wants to learn Python
lists: fresh is true.

brief: for create, create_and_run, modify and modify_and_run, one or two
plain English sentences saying exactly what to build or change. Resolve short
references from recent_chat so the brief stands alone: if the viewer says
"yeah update it" right after the teacher offered a snacks-list example, the
brief is "A Python list lesson: make a snacks list, append, remove and index
items, and print each step." Never copy an old topic into a fresh brief.
Empty for every other action.

reopen: when the viewer asks to go back to, change, fix or remix one of the
games in situation.published_games that is NOT the program on screen, its
"id" from that list, with action modify (or modify_and_run) and fresh false.
Match by title or by who asked for it. Empty otherwise, and always empty for
the program currently on screen.

unsupported: name a technology ONLY if the comment explicitly asked for one of
manim, turtle, tkinter, pygame, or a desktop GUI app. Otherwise empty."""
DECIDE_SYSTEM = DECIDE_SYSTEM.replace("__KINDS__", kinds_for_decision())


def generate_system(language: str, supported: str, kind: str = "") -> str:
    """The system prompt for the expensive step that writes the code.

    Live-stream rules and the stream's look, then the guide for this kind of
    program. The request itself carries the template, libraries and the
    approved asset list (capabilities.writer_context).
    """
    chosen = kind_for(kind, language)
    if language == "web":
        shape = (
            "Reply with ONE complete, self-contained HTML document: <!doctype html>, "
            "inline <style> and inline <script>, no external files and no network "
            "requests. It must run by opening the file."
        )
    else:
        shape = (
            "Reply with ONE complete Python file that runs top to bottom with no "
            "input(). It must be headless: no windows and no GUI toolkits, never "
            "plt.show(). Pictures are saved as output.png and animations as "
            "output.gif in the current folder; the Stage shows them."
        )
    return (
        "You are writing code live on a stream, for an audience.\n\n"
        f"{shape}\n\n"
        f"{live_rules(language, chosen.name)}\n\n"
        f"WHAT TO BUILD ({chosen.name}):\n{chosen.guide}\n\n"
        "Rules:\n"
        "- Output ONLY the code. No explanation, no commentary, no markdown fences.\n"
        "- If a starting_template is given, build from it: keep its structure and "
        "everything that already works, and change what the request needs.\n"
        "- Use ONLY the libraries and approved_assets listed in the request, with "
        "those exact URLs. Never invent a file, a URL or a library. No other "
        "network access exists.\n"
        "- art_from_this_stream lists pictures painted earlier on this stream. "
        "When the viewer points at one (\"use that cat\", \"the dragon from "
        "before\"), load that exact URL (this.load.image or <img>), with a short "
        "comment where it is loaded so it can be explained on stream.\n"
        "- requested_by is the viewer who asked. \"My name\", \"me\" or \"for "
        "me\" means that name: write it out, never a placeholder like \"Your "
        "Name\".\n"
        "- A site or page is FOR THE VIEWER WHO ASKED: use their name, brand "
        "or topic. Never put the stream's name (Selwyn, Selwyn Builds) on it "
        "unless the request asks for it; only then is about_the_host given, "
        "so use those facts. Never invent facts about a real person or "
        "business (no made-up prices, clients or awards); where you lack a "
        "detail, write short copy that is clearly generic.\n"
        "- Keep it short enough to read on screen, and readable: clear names, "
        "a few helpful comments.\n"
        "- When changing existing code, keep everything the viewer did not ask to "
        "change, and return the WHOLE updated file.\n"
        f"- The runtime supports only {supported}.\n"
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.S)


def _json_object(text: str) -> dict[str, Any]:
    """The first JSON object in a model reply (tolerates fences and chatter)."""
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _one_of(value: Any, options, default: str = "") -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in options else default


def parse_decision(reply: str, cast: Optional[list[str]] = None) -> CodingDecision:
    """A model reply becomes a decision. Anything unclear becomes chat."""
    data = _json_object(reply)
    action = _one_of(data.get("action"), ACTIONS, "chat")
    control = _one_of(data.get("control"), CONTROLS)
    teacher = str(data.get("teacher") or "").strip().lower()
    if cast is not None and teacher not in cast:
        teacher = ""
    language = _one_of(data.get("language"), LANGUAGES)
    unsupported = _one_of(data.get("unsupported"), tuple(UNSUPPORTED))

    # Keep the action and its fields consistent, so the host never has to
    # second-guess the model.
    if action == "control" and not control:
        action = "chat"
    if action == "switch_teacher" and not teacher:
        action = "chat"
    # An omitted language is resolved by the host from the existing program
    # (a change keeps its language), never forced to Python here.
    if action == "run":
        language = language or ""
    kind = _one_of(data.get("kind"), tuple(KINDS))
    if action not in WRITES_CODE:
        kind = ""
    elif kind and not language:
        language = KINDS[kind].language
    fresh = data.get("fresh")
    fresh = fresh is True or str(fresh).strip().lower() == "true"
    brief = " ".join(str(data.get("brief") or "").split())[:600]
    if action not in WRITES_CODE:
        fresh, brief = False, ""
    reopen = re.sub(r"[^a-z0-9]", "", str(data.get("reopen") or "").lower())[:40]
    if action not in WRITES_CODE:
        reopen = ""
    if reopen:
        fresh = False
    return CodingDecision(
        action=action,
        language=language,
        control=control,
        teacher=teacher,
        subject=str(data.get("subject") or "")[:200],
        unsupported=unsupported,
        fresh=fresh,
        brief=brief,
        kind=kind,
        reopen=reopen,
        raw=data,
    )


def parse_code(reply: str) -> str:
    """Code out of a model reply, with any markdown fence removed."""
    text = str(reply or "").strip()
    match = _FENCE.match(text)
    if match:
        return match.group(1).strip()
    # A reply that opens with a fence but was cut off before closing it.
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        return body.rsplit("```", 1)[0].strip()
    return text


def redirect_note(technology: str) -> str:
    """What to offer instead of something the runtime cannot run."""
    instead = UNSUPPORTED.get(
        technology, "a small web page with HTML, CSS and JavaScript"
    )
    return (
        f"The stream cannot run {technology.replace('_', ' ')} here, because the "
        f"coding runtime is headless. Offer {instead} instead, in one friendly "
        "sentence, and ask if they want that."
    )
