"""Class mode: Mika teaches a real course on stream, with or without viewers.

    VR_CLASS_MODE=1               turn it on (the build-anything mode is off then)
    VR_CLASS_COURSE=python-basics where to start (see class_course.COURSES)
    VR_CLASS_TEACHER=mika         who teaches; the other character is the
                                  curious sidekick who asks the questions
    VR_CLASS_QUIZ_SECONDS=20      how long chat has to answer a quiz

How a lesson happens:

1. The LLM writes the whole lesson as JSON (slides, spoken lines, notebook
   cells, an intentional mistake, a quiz) from a short outline.
2. Every cell is run in a scratch notebook first. Anything that breaks when
   it should not goes back to the LLM once; what is still broken is dropped,
   so the class never shows a surprise traceback.
3. The lesson is taught step by step: the slide changes, the line is spoken,
   the cell types itself into the notebook and really runs.
4. Between steps chat is answered (one short line, sometimes with a tiny cell
   that shows the answer), quiz answers are counted, and the next lesson is
   already being written in the background.

Progress is kept in data/class/progress.json, so a restart continues where
the class stopped. Every lesson is also saved as a Jupyter notebook in
data/class/notebooks/ for the after-stream materials.
"""

from __future__ import annotations

import asyncio
import json
import keyword
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from .class_course import course, lesson_at
from .notebook_kernel import NotebookKernel

DATA_DIR = Path("data/class")
MAX_SAY = 280
LETTERS = "ABCD"
BUILD_WORDS = re.compile(
    r"\b(build|make|create|code|program|develop)\b.{0,40}\b(app|game|website|site|backend|"
    r"server|bot|database|api|login|platform)\b",
    re.IGNORECASE,
)


def class_mode_enabled() -> bool:
    return os.environ.get("VR_CLASS_MODE", "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def speech_chunks(text: str, limit: int = MAX_SAY) -> list[str]:
    """Split a long line at sentence ends so each piece fits one TTS call."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return [text] if text else []
    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for part in parts:
        while len(part) > limit:  # one giant sentence: cut at a comma or space
            cut = max(part.rfind(", ", 0, limit), part.rfind(" ", 0, limit))
            cut = cut if cut > 40 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(part[:cut].strip())
            part = part[cut:].strip()
        if current and len(current) + 1 + len(part) > limit:
            chunks.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def variable_name(author: str) -> str:
    """A viewer's name as a friendly Python variable name ("@Math Fan!" -> math_fan)."""
    name = re.sub(r"[^a-z0-9]+", "_", str(author or "").lstrip("@").lower()).strip("_")[:20]
    if not name:
        return ""
    if name[0].isdigit():
        name = "v_" + name
    if keyword.iskeyword(name) or name in {"print", "list", "dict", "str", "int", "len", "sum"}:
        name += "_"
    return name


def parse_json_object(text: str) -> Optional[dict[str, Any]]:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_lesson(raw: dict[str, Any], title: str) -> Optional[dict[str, Any]]:
    """Keep only what the stage and the teacher understand."""
    steps_in = raw.get("steps") if isinstance(raw, dict) else None
    if not isinstance(steps_in, list):
        return None
    steps: list[dict[str, Any]] = []
    for item in steps_in[:32]:
        if not isinstance(item, dict):
            continue
        step: dict[str, Any] = {}
        step["who"] = "sidekick" if str(item.get("who", "")).lower() == "sidekick" else "teacher"
        step["say"] = _clip(item.get("say"), 600)
        step["mood"] = _clip(item.get("mood"), 20).lower()
        slide = item.get("slide")
        if isinstance(slide, dict) and (slide.get("title") or slide.get("bullets")):
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            step["slide"] = {
                "title": _clip(slide.get("title"), 80),
                "bullets": [_clip(b, 120) for b in bullets[:4] if _clip(b, 120)],
                "visual": _visual(slide.get("visual")),
            }
        cell = str(item.get("cell") or "").strip("\n")
        if cell.strip():
            step["cell"] = cell[:1500]
            step["expect_error"] = bool(item.get("expect_error"))
        step["after"] = _clip(item.get("after"), 600)
        quiz = item.get("quiz")
        if isinstance(quiz, dict):
            choices = [_clip(c, 80) for c in (quiz.get("choices") or [])[:4] if _clip(c, 80)]
            answer = str(quiz.get("answer", "")).strip().upper()[:1]
            if len(choices) >= 2 and answer in LETTERS[: len(choices)] and quiz.get("question"):
                step["quiz"] = {
                    "question": _clip(quiz.get("question"), 200),
                    "choices": choices,
                    "answer": LETTERS.index(answer),
                    "code": str(quiz.get("code") or "").strip("\n")[:800],
                    "explain": _clip(quiz.get("explain"), 400),
                }
        if step["say"] or step.get("cell") or step.get("quiz") or step.get("slide"):
            steps.append(step)
    if len(steps) < 4:
        return None
    return {
        "title": _clip(raw.get("title") or title, 80),
        "steps": steps,
        "summary": _clip(raw.get("summary"), 400),
    }


def _visual(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or "").lower()
    if kind == "list":
        items = [_clip(v, 24) for v in (value.get("items") or [])[:10]]
        highlight = [int(i) for i in (value.get("highlight") or []) if isinstance(i, int)][:10]
        return {"type": "list", "name": _clip(value.get("name"), 24), "items": items, "highlight": highlight}
    if kind == "vars":
        pairs = value.get("items") or {}
        if isinstance(pairs, dict):
            pairs = list(pairs.items())
        items = [[_clip(k, 24), _clip(v, 40)] for k, v in (p for p in pairs if isinstance(p, (list, tuple)) and len(p) == 2)][:6]
        return {"type": "vars", "items": items}
    if kind == "math":
        return {"type": "math", "latex": str(value.get("latex") or "")[:600]}
    if kind == "table":
        rows = [[_clip(c, 24) for c in r[:6]] for r in (value.get("rows") or [])[:7] if isinstance(r, list)]
        return {"type": "table", "rows": rows}
    return None


WRITER_PROMPT = """You write one lesson for a LIVE coding class on YouTube, taught by two anime VTubers.

TEACHER: {teacher} ({teacher_persona})
SIDEKICK: {sidekick} ({sidekick_persona}). The sidekick is the curious student: asks the questions a beginner would ask, makes wrong guesses, jokes, gets excited when things work.

Course: {course_title}, for {audience}.
This lesson ({number} of {total}): "{title}". Cover: {goals}.
{previous}
{viewers}

Return ONLY a JSON object:
{{"title": "...", "summary": "one sentence recap", "steps": [ ...16 to 24 steps... ]}}

A step is an object with any of these fields:
  "who": "teacher" or "sidekick"
  "say": what that character says out loud (1 or 2 short spoken sentences, max 240 characters). Spoken words only: no markdown, no code symbols, no emojis. Say "x equals five", not "x = 5".
  "mood": optional, one of joy, smirk, surprise, thinking, sadness
  "slide": optional new slide {{"title": "max 6 words", "bullets": ["max 3 bullets, max 9 words each"], "visual": optional}}
      visual is one of:
        {{"type": "list", "name": "fruits", "items": ["apple", "kiwi"], "highlight": [1]}}   (boxes with index numbers under them)
        {{"type": "vars", "items": [["score", "10"], ["name", "'Mika'"]]}}               (labelled boxes)
        {{"type": "math", "latex": "\\\\frac{{a}}{{b}}"}}                                      (one formula)
        {{"type": "table", "rows": [["name", "score"], ["Luna", "90"]]}}
  "cell": optional Python typed into the notebook and run right after the line is spoken. 1 to 8 short lines a beginner can read. Show results with print() or by ending the cell with an expression (like Jupyter).
  "expect_error": true when the cell is MEANT to fail (the intentional mistake)
  "after": optional line the same character says after the cell output appears (react to the real output)
  "quiz": optional {{"question": "...", "choices": ["...", "...", "..."], "answer": "B", "code": "optional cell that proves the answer", "explain": "one spoken sentence why"}}

Rules:
- Start with a slide titled with the lesson name and a warm, funny hook. End with a recap slide and a teaser for the next lesson.
- Teach by doing: most steps have a cell. Each cell builds on the earlier cells (one shared notebook, variables carry over).
- Exactly one intentional mistake (expect_error true) that the sidekick or teacher makes on purpose, with a funny "after" that explains the error message.
- One or two quizzes. Chat answers them by typing A, B or C.
- Change the slide whenever the idea changes (about every 3 steps). Slides are short; the talking explains.
- Plain Python only. Available: math, random{libraries}. Nothing else can be imported. No input(), no files, no internet, no time.sleep.
- Plots: use matplotlib, do not call plt.show(); the figure is shown automatically.
- Playful and kind, never boring. Short sentences. Real examples (snacks, games, anime, pets).
"""

CHAT_PROMPT = """You are {name} in a LIVE class on YouTube. {role}
Personality: {persona}
Right now the class is on "{lesson}"{slide}.
A viewer wrote in chat. Answer them out loud in ONE or TWO short spoken sentences (max 45 words total), in character, warm and playful. Use their name.
- A question about the lesson or Python: answer it simply. If a tiny piece of code would show the answer, put it in "cell" (max 5 lines, plain Python, ends with an expression or print).
- They ask you to build an app, game, website, backend or anything big: joke about it, for example "I can't build it because of constraints, and you can't build it because YOU CAN'T... yet! Stay in class and you will!", then pull them back to the lesson.
- Small talk: reply briefly and invite them to follow along.
No emojis, no markdown, no code inside "say".
Return ONLY JSON: {{"say": "...", "cell": ""}}"""


# ---------------------------------------------------------------------------
class ClassEngine:
    def __init__(self, runtimes: Any, session: Any, course_id: str = "") -> None:
        self.runtimes = runtimes
        self.session = session
        cast = [c.id for c in session.room.characters]
        teacher = os.environ.get("VR_CLASS_TEACHER", "").strip().lower() or "mika"
        self.teacher = teacher if teacher in cast else (cast[0] if cast else "mika")
        self.sidekick = next((c for c in cast if c != self.teacher), self.teacher)
        self.course_id = course_id or os.environ.get("VR_CLASS_COURSE", "").strip() or "python-basics"
        self.quiz_seconds = max(8, min(60, int(os.environ.get("VR_CLASS_QUIZ_SECONDS", "20") or 20)))
        self.data_dir = DATA_DIR
        self.kernel = NotebookKernel()
        self.scratch = NotebookKernel()
        self.inbox: deque[dict[str, str]] = deque(maxlen=12)
        self.viewers: deque[str] = deque(maxlen=8)
        self.last_chat_at = 0.0
        self.quiz_votes: Optional[dict[str, int]] = None
        self.task: Optional[asyncio.Task] = None
        self._prefetch: dict[tuple[str, int], asyncio.Task] = {}
        self._cell_number = 0
        self._lesson_cells: list[str] = []
        self.lessons_taught = 0
        self.view: dict[str, Any] = {"active": False}
        # Called at each lesson start (the stream title follows the lesson).
        self.on_lesson = None

    # ------------------------------------------------------------ public
    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if self.active:
            return
        self.task = asyncio.create_task(self._run(), name="vr-class")
        logger.info(f"Class mode on: {course(self.course_id)['title']}, taught by {self.teacher}")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except BaseException:
                pass
        for task in self._prefetch.values():
            task.cancel()
        await self.kernel.close()
        await self.scratch.close()
        self.view = {"active": False}
        await self._push({"kind": "stop"})

    def enqueue(self, author: str, text: str) -> None:
        """A chat message: a quiz answer while a quiz is open, else a question."""
        author = _clip(author, 40).lstrip("@") or "viewer"
        text = _clip(text, 300)
        if not text:
            return
        self.last_chat_at = time.time()
        if author not in self.viewers:
            self.viewers.append(author)
        if self.quiz_votes is not None:
            match = re.fullmatch(r"\s*\(?([abcd])\)?[.!]?\s*", text, re.IGNORECASE)
            if match:
                self.quiz_votes[author] = LETTERS.index(match.group(1).upper())
                return
        self.inbox.append({"author": author, "text": text})

    def snapshot(self) -> dict[str, Any]:
        return self.view

    # ------------------------------------------------------------ plumbing
    async def _push(self, op: dict[str, Any]) -> None:
        try:
            await self.session.push([{"op": "class", **op}])
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug(f"Class op failed: {exc}")

    def _name(self, character_id: str) -> str:
        profile = self.session.room.get(character_id)
        return profile.name if profile else character_id.title()

    def _persona(self, character_id: str) -> str:
        profile = self.session.room.get(character_id)
        return _clip(getattr(profile, "persona", ""), 500)

    def _llm(self, for_writing: bool = False):
        if for_writing and getattr(self.runtimes, "code_model", None) is not None:
            return self.runtimes.code_model
        agent = self.runtimes.agent(self.teacher)
        return getattr(agent, "_llm", None)

    async def _ask(self, messages: list[dict[str, Any]], timeout: float, writing: bool = False) -> str:
        llm = self._llm(for_writing=writing)
        if llm is None:
            return ""
        try:
            return await asyncio.wait_for(self.runtimes._complete(llm, messages), timeout=timeout)
        except Exception as exc:
            logger.warning(f"Class: LLM call failed: {exc}")
            if writing and llm is not self._llm():
                return await self._ask(messages, timeout, writing=False)
            return ""

    async def _wait_ready(self) -> None:
        from ..vr_agent.state import runtime

        while not self.session.speech_target() or runtime.paused:
            await asyncio.sleep(1.0)

    async def _say(self, who: str, text: str, mood: str = "") -> None:
        character = self.teacher if who != "sidekick" else self.sidekick
        if mood:
            await self._mood(character, mood)
        for chunk in speech_chunks(text):
            await self._wait_ready()
            self.view["caption"] = {"who": character, "text": chunk}
            try:
                spoken = await self.session.speech.say(character, chunk)
            except Exception as exc:
                logger.warning(f"Class: speech failed: {exc}")
                spoken = False
            if not spoken:  # no voice: the room still shows the line, give time to read it
                await asyncio.sleep(min(8.0, 1.5 + len(chunk) * 0.05))

    async def _mood(self, character: str, mood: str) -> None:
        profile = self.session.room.get(character)
        if not profile:
            return
        action = (profile.emotions or {}).get(mood) or next(
            iter((profile.reactions or {}).get(mood) or ()), None
        )
        if action:
            try:
                await self.session.push(self.session.play(character, action))
            except Exception:
                pass

    # ------------------------------------------------------------ progress
    def _load_progress(self) -> dict[str, Any]:
        try:
            data = json.loads((self.data_dir / "progress.json").read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"course": self.course_id, "lesson": 0}

    def _save_progress(self, course_id: str, index: int) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "progress.json").write_text(
                json.dumps({"course": course_id, "lesson": index, "saved_at": time.time()})
            )
        except Exception as exc:
            logger.warning(f"Class: progress not saved: {exc}")

    # ------------------------------------------------------------ the class
    async def _run(self) -> None:
        try:
            await self._wait_ready()
            progress = self._load_progress()
            course_id = str(progress.get("course") or self.course_id)
            if os.environ.get("VR_CLASS_COURSE") and course_id != self.course_id:
                course_id, progress["lesson"] = self.course_id, 0  # the setting wins
            index = int(progress.get("lesson") or 0)
            course_id, index, title, _goals = lesson_at(course_id, index)
            self._lesson_task(course_id, index)  # written while the intro is spoken
            await self._open(course_id, title)
            while True:
                course_id, index, title, _goals = lesson_at(course_id, index)
                task = self._lesson_task(course_id, index)
                lesson = await self._await_lesson(task, title)
                self._prefetch.pop((course_id, index), None)
                if lesson is None:
                    await asyncio.sleep(20)
                    continue
                next_course, next_index, _t, _g = lesson_at(course_id, index + 1)
                self._lesson_task(next_course, next_index)  # write the next one meanwhile
                await self._teach(course_id, index, lesson)
                self.lessons_taught += 1
                self._save_progress(next_course, next_index)
                course_id, index = next_course, next_index
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Class mode stopped: {exc}")

    async def _open(self, course_id: str, title: str) -> None:
        info = course(course_id)
        await self._push_start(course_id, -1, "Class starts in a moment")
        await self._slide(
            {"title": f"Today: {info['title']}!", "bullets": [f"First up: {title}", "Ask anything in chat", "Quizzes: answer with A, B or C"]}
        )
        await self._say(
            "teacher",
            f"Welcome to class everyone! Today we are learning {info['title']}. "
            "Stop scrolling, it's time to learn!",
            "joy",
        )
        await self._say(
            "sidekick",
            "I brought snacks and zero knowledge. Ask your questions in chat, we answer them as we go!",
            "smirk",
        )

    async def _push_start(self, course_id: str, index: int, title: str) -> None:
        info = course(course_id)
        self.view = {
            "active": True,
            "course": info["title"],
            "lesson": title,
            "number": index + 1,
            "total": len(info["lessons"]),
            "teacher": self.teacher,
            "sidekick": self.sidekick,
            "slide": None,
            "cells": [],
        }
        await self._push({"kind": "start", **{k: v for k, v in self.view.items() if k not in ("cells", "slide")}})

    async def _slide(self, slide: dict[str, Any]) -> None:
        self.view["slide"] = slide
        await self._push({"kind": "slide", "slide": slide})

    # ------------------------------------------------------------ writing
    def _lesson_task(self, course_id: str, index: int) -> asyncio.Task:
        key = (course_id, index)
        task = self._prefetch.get(key)
        stale = task is not None and task.done() and (
            task.cancelled() or task.exception() is not None or task.result() is None
        )
        if task is None or stale:
            task = asyncio.create_task(self._write_lesson(course_id, index))
            self._prefetch[key] = task
        return task

    async def _await_lesson(self, task: asyncio.Task, title: str) -> Optional[dict[str, Any]]:
        """Wait for the lesson; keep chat company while it is still being written."""
        started = time.time()
        while not task.done():
            await asyncio.sleep(0.5)
            if self.inbox:
                await self._chat_break(title)
            if time.time() - started > 240:
                task.cancel()
                return None
        try:
            return task.result()
        except BaseException as exc:
            logger.warning(f"Class: lesson could not be written: {exc}")
            return None

    async def _write_lesson(self, course_id: str, index: int) -> Optional[dict[str, Any]]:
        info = course(course_id)
        _c, _i, title, goals = lesson_at(course_id, index)
        previous = ""
        if index > 0:
            done = ", ".join(t for t, _ in info["lessons"][max(0, index - 4) : index])
            previous = f"Earlier lessons: {done}. A quick callback to them is welcome."
        names = [variable_name(v) for v in self.viewers if variable_name(v)]
        viewers = (
            f"Viewers in chat today: {', '.join(names[-5:])}. Use one or two of these as "
            "variable names or in examples (it makes them smile), and shout them out."
            if names
            else ""
        )
        if not self.scratch.available:
            try:
                await self.scratch.reset()
            except Exception:
                pass
        libraries = "".join(f", {m}" for m in self.scratch.available)
        missing = [m for m in ("sympy", "numpy", "matplotlib") if m not in self.scratch.available]
        if missing and not getattr(self, "_warned_missing", False):
            self._warned_missing = True
            logger.warning(
                f"Class notebook is missing {', '.join(missing)}: lessons avoid them. "
                f"Install with: uv pip install {' '.join(missing)}"
            )
        prompt = WRITER_PROMPT.format(
            libraries=libraries,
            teacher=self._name(self.teacher),
            teacher_persona=self._persona(self.teacher)[:300],
            sidekick=self._name(self.sidekick),
            sidekick_persona=self._persona(self.sidekick)[:300],
            course_title=info["title"],
            audience=info["audience"],
            number=index + 1,
            total=len(info["lessons"]),
            title=title,
            goals=goals,
            previous=previous,
            viewers=viewers,
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f'Write the lesson "{title}" now as JSON.'},
        ]
        started = time.time()
        reply = await self._ask(messages, timeout=150, writing=True)
        lesson = normalize_lesson(parse_json_object(reply) or {}, title)
        if lesson is None:
            logger.warning(f"Class: lesson '{title}' came back unreadable")
            return None
        problems = await self._check(lesson)
        if problems:
            logger.info(f"Class: lesson '{title}' has {len(problems)} broken cells, asking for a fix")
            fix = messages + [
                {"role": "assistant", "content": json.dumps(lesson)},
                {
                    "role": "user",
                    "content": "These cells did not behave as planned when run in order:\n"
                    + "\n".join(problems[:8])
                    + "\nReturn the whole corrected lesson JSON.",
                },
            ]
            fixed = normalize_lesson(parse_json_object(await self._ask(fix, timeout=150, writing=True)) or {}, title)
            if fixed is not None:
                lesson = fixed
            problems = await self._check(lesson, drop=True)
        logger.info(
            f"Class: lesson '{lesson['title']}' ready in {time.time() - started:.0f}s "
            f"({len(lesson['steps'])} steps)"
        )
        return lesson

    async def _check(self, lesson: dict[str, Any], drop: bool = False) -> list[str]:
        """Run every cell in order in a scratch notebook. With drop, remove the
        cells that still misbehave (the spoken lines stay)."""
        problems: list[str] = []
        try:
            await self.scratch.reset()
        except Exception as exc:
            logger.warning(f"Class: scratch notebook unavailable, cells are not checked: {exc}")
            return []
        for number, step in enumerate(lesson["steps"]):
            for field in ("cell", "quiz"):
                code = step.get("cell") if field == "cell" else (step.get("quiz") or {}).get("code")
                if not code:
                    continue
                reply = await self.scratch.execute(code, timeout=20)
                error = reply.get("error")
                expect = field == "cell" and step.get("expect_error")
                if expect and not error:
                    problems.append(f"step {number}: expected an error but it ran fine")
                    if drop:
                        step.pop("cell", None)
                        step["after"] = ""
                elif error and not expect:
                    problems.append(f"step {number}: {error.get('trace') or error.get('value')}")
                    if drop:
                        if field == "cell":
                            step.pop("cell", None)
                            step["after"] = ""
                        else:
                            step["quiz"]["code"] = ""
        return problems

    # ------------------------------------------------------------ teaching
    async def _teach(self, course_id: str, index: int, lesson: dict[str, Any]) -> None:
        await self._push_start(course_id, index, lesson["title"])
        if self.on_lesson is not None:
            info = course(course_id)
            goals = lesson_at(course_id, index)[3]
            asyncio.create_task(
                self.on_lesson(info["title"], index + 1, len(info["lessons"]), lesson["title"], goals)
            )
        try:
            await self.kernel.reset()
        except Exception as exc:
            logger.warning(f"Class: notebook unavailable: {exc}")
        self._cell_number = 0
        self._lesson_cells = []
        record: list[dict[str, Any]] = []
        for step in lesson["steps"]:
            if step.get("slide"):
                await self._slide(step["slide"])
                record.append({"slide": step["slide"]})
            if step.get("quiz"):
                await self._quiz(step["who"], step["say"], step["quiz"], record)
            else:
                await self._step(step, record)
            await asyncio.sleep(0.4)
            if self.inbox:
                await self._chat_break(lesson["title"], record)
        await self._push({"kind": "end", "title": lesson["title"], "summary": lesson.get("summary", "")})
        self._save_notebook(course_id, index, lesson, record)
        await asyncio.sleep(2.0)

    async def _step(self, step: dict[str, Any], record: list) -> None:
        code = step.get("cell")
        typing_ms = 0
        if code:
            self._cell_number += 1
            typing_ms = min(7000, 500 + len(code) * 30)
            cell = {"n": self._cell_number, "code": code, "output": None}
            self.view.setdefault("cells", []).append(cell)
            del self.view["cells"][:-6]
            await self._push({"kind": "cell", "n": self._cell_number, "code": code, "typing_ms": typing_ms})
        typed_at = time.time() + typing_ms / 1000
        if step.get("say"):
            await self._say(step["who"], step["say"], step.get("mood", ""))
        if not code:
            return
        await asyncio.sleep(max(0.0, typed_at - time.time()))
        output = await self._run_cell(self._cell_number, code)
        record.append({"code": code, "output": output})
        error = output.get("error")
        if error and not step.get("expect_error"):
            await self._say(
                "sidekick",
                "Wait, that one broke for real! Even teachers get bugs. Let's keep going.",
                "surprise",
            )
            return
        if step.get("after"):
            await asyncio.sleep(0.5)
            await self._say(step["who"], step["after"], "surprise" if error else "")

    async def _run_cell(self, number: int, code: str) -> dict[str, Any]:
        try:
            output = await self.kernel.execute(code, timeout=25)
        except Exception as exc:
            output = {"stdout": "", "result": None, "images": [], "error": {"name": "Kernel", "value": str(exc)[:200], "line": None, "trace": str(exc)[:200]}}
        if output.get("restarted"):
            await self._replay()
        shown = {
            "stdout": str(output.get("stdout") or "")[-3000:],
            "result": output.get("result"),
            "images": list(output.get("images") or [])[:2],
            "error": output.get("error"),
        }
        for cell in self.view.get("cells") or []:
            if cell["n"] == number:
                cell["output"] = shown
        for cell in (self.view.get("cells") or [])[:-2]:  # keep the snapshot small
            if cell.get("output") and cell["output"].get("images"):
                cell["output"] = {**cell["output"], "images": []}
        await self._push({"kind": "output", "n": number, **shown})
        if not shown["error"]:
            self._lesson_cells.append(code)
        return shown

    async def _replay(self) -> None:
        """The kernel restarted: quietly rebuild the variables of this lesson."""
        for code in self._lesson_cells:
            try:
                await self.kernel.execute(code, timeout=20)
            except Exception:
                break

    async def _quiz(self, who: str, say: str, quiz: dict[str, Any], record: list) -> None:
        busy_chat = time.time() - self.last_chat_at < 300
        seconds = self.quiz_seconds if busy_chat else 8
        self.quiz_votes = {}
        self.view["quiz"] = {"question": quiz["question"], "choices": quiz["choices"]}
        await self._push({"kind": "quiz", "question": quiz["question"], "choices": quiz["choices"], "seconds": seconds})
        intro = say or quiz["question"]
        letters = ", ".join(LETTERS[: len(quiz["choices"])])
        await self._say(who, f"{intro} Type {letters} in chat!", "smirk")
        deadline = time.time() + seconds
        while time.time() < deadline:
            await asyncio.sleep(0.5)
        votes, self.quiz_votes = self.quiz_votes or {}, None
        answer = quiz["answer"]
        counts = [0] * len(quiz["choices"])
        for choice in votes.values():
            if choice < len(counts):
                counts[choice] += 1
        winners = [name for name, choice in votes.items() if choice == answer][:5]
        await self._push({"kind": "quiz_result", "answer": answer, "counts": counts, "winners": winners})
        self.view.pop("quiz", None)
        letter = LETTERS[answer]
        if winners:
            names = ", ".join(winners[:3])
            line = f"The answer is {letter}! Nice one {names}!"
        elif votes:
            line = f"Ooh, tricky one! The answer is {letter}."
        else:
            guess = (answer + (1 if time.time() % 2 > 1 else 0)) % len(quiz["choices"])
            await self._say("sidekick", f"Nobody answered? Then I say {LETTERS[guess]}!", "smirk")
            line = f"The answer is {letter}!" + (" You got it!" if guess == answer else " Nice try though!")
        await self._say("teacher", f"{line} {quiz.get('explain', '')}".strip(), "joy")
        if quiz.get("code"):
            self._cell_number += 1
            code = quiz["code"]
            self.view.setdefault("cells", []).append({"n": self._cell_number, "code": code, "output": None})
            del self.view["cells"][:-6]
            await self._push({"kind": "cell", "n": self._cell_number, "code": code, "typing_ms": min(5000, 400 + len(code) * 30)})
            await asyncio.sleep(min(5.0, 0.4 + len(code) * 0.03))
            record.append({"code": code, "output": await self._run_cell(self._cell_number, code)})

    # ------------------------------------------------------------ chat
    async def _chat_break(self, lesson_title: str, record: Optional[list] = None) -> None:
        """Answer up to two chat messages, newest first."""
        for _ in range(2):
            if not self.inbox:
                return
            message = self.inbox.pop()
            await self._answer(message, lesson_title, record)
        if len(self.inbox) > 4:  # a flood: the oldest ones are skipped
            for _ in range(len(self.inbox) - 2):
                self.inbox.popleft()

    async def _answer(self, message: dict[str, str], lesson_title: str, record: Optional[list]) -> None:
        author, text = message["author"], message["text"]
        addressed_teacher = re.search(rf"\b{re.escape(self._name(self.teacher))}\b", text, re.IGNORECASE)
        who = "teacher" if addressed_teacher else "sidekick"
        character = self.teacher if who == "teacher" else self.sidekick
        slide = (self.view.get("slide") or {}).get("title")
        prompt = CHAT_PROMPT.format(
            name=self._name(character),
            role="You are the teacher." if who == "teacher" else f"You are the sidekick student; {self._name(self.teacher)} is teaching.",
            persona=self._persona(character)[:400],
            lesson=lesson_title,
            slide=f', slide "{slide}"' if slide else "",
        )
        await self._push({"kind": "chat", "author": author, "text": text})
        reply = await self._ask(
            [{"role": "system", "content": prompt}, {"role": "user", "content": f"{author}: {text}"}],
            timeout=12,
        )
        data = parse_json_object(reply) or {"say": reply if reply and "{" not in reply else ""}
        say = _clip(data.get("say"), 400)
        if not say:
            if BUILD_WORDS.search(text):
                say = (
                    f"{author}, I can't build it because of constraints, and you can't build it "
                    "because YOU CAN'T... yet! Stay in class and you will!"
                )
            else:
                return
        cell = str(data.get("cell") or "").strip()
        if cell and len(cell) < 400 and cell.count("\n") < 6:
            self._cell_number += 1
            self.view.setdefault("cells", []).append({"n": self._cell_number, "code": cell, "output": None})
            del self.view["cells"][:-6]
            await self._push({"kind": "cell", "n": self._cell_number, "code": cell, "typing_ms": min(4000, 400 + len(cell) * 30), "for": author})
            await self._say(who, say)
            output = await self._run_cell(self._cell_number, cell)
            if record is not None:
                record.append({"code": cell, "output": output, "for": author})
        else:
            await self._say(who, say)
        await self._push({"kind": "chat_done"})

    # ------------------------------------------------------------ saving
    def _save_notebook(self, course_id: str, index: int, lesson: dict[str, Any], record: list) -> None:
        cells: list[dict[str, Any]] = [
            {"cell_type": "markdown", "metadata": {}, "source": [f"# {lesson['title']}\n", f"{course(course_id)['title']}, lesson {index + 1}\n"]}
        ]
        count = 0
        for item in record:
            if "slide" in item:
                slide = item["slide"]
                text = f"## {slide.get('title', '')}\n" + "".join(f"- {b}\n" for b in slide.get("bullets", []))
                cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)})
                continue
            count += 1
            output = item.get("output") or {}
            outputs: list[dict[str, Any]] = []
            if output.get("stdout"):
                outputs.append({"output_type": "stream", "name": "stdout", "text": output["stdout"].splitlines(True)})
            if output.get("result"):
                outputs.append({"output_type": "execute_result", "execution_count": count, "metadata": {}, "data": {"text/plain": [output["result"].get("text", "")]}})
            for image in output.get("images") or []:
                outputs.append({"output_type": "display_data", "metadata": {}, "data": {"image/png": image}})
            if output.get("error"):
                error = output["error"]
                outputs.append({"output_type": "error", "ename": error.get("name", ""), "evalue": error.get("value", ""), "traceback": [error.get("trace", "")]})
            source = item["code"]
            if item.get("for"):
                source = f"# asked in chat by {item['for']}\n{source}"
            cells.append({"cell_type": "code", "execution_count": count, "metadata": {}, "outputs": outputs, "source": source.splitlines(True)})
        notebook = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
            "cells": cells,
        }
        try:
            folder = self.data_dir / "notebooks"
            folder.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", lesson["title"].lower()).strip("-")[:40] or "lesson"
            path = folder / f"{course_id}-{index + 1:02d}-{slug}.ipynb"
            path.write_text(json.dumps(notebook, indent=1))
            logger.info(f"Class: notebook saved to {path}")
        except Exception as exc:
            logger.warning(f"Class: notebook not saved: {exc}")
