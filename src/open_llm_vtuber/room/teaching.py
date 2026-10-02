from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .coding_lesson import CodingLesson


VALID_PHASES = {
    "idle",
    "preparing",
    "teaching",
    "paused",
    "adapting",
    "waiting",
    "testing",
    "finished",
    "abandoned",
    "error",
}


@dataclass
class TeachingRequest:
    student_id: str
    student_name: str
    goal: str
    teacher: str = "mika"


@dataclass
class TeachingSession:
    active: bool = False

    student_id: str = ""
    student_name: str = ""

    teacher: str = "mika"
    assistant_teacher: str = ""

    goal: str = ""

    phase: str = "idle"
    pace: str = "normal"
    current_step: int = 0

    last_error: str = ""

    # Semantic meaning proposed by the LLM.
    # This is NOT authority by itself.
    last_intent: str = ""

    # Grounded script and process result returned by the isolated runner.
    last_action_result: dict[str, Any] = field(default_factory=dict)
    coding_state: dict[str, Any] = field(default_factory=dict)

    queue: list[TeachingRequest] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "student_id": self.student_id,
            "student_name": self.student_name,
            "teacher": self.teacher,
            "assistant_teacher": self.assistant_teacher,
            "goal": self.goal,
            "phase": self.phase,
            "pace": self.pace,
            "current_step": self.current_step,
            "last_error": self.last_error,
            "last_intent": self.last_intent,
            "last_action_result": self.last_action_result.copy(),
            "coding": self.coding_state.copy(),
            "queue": [r.__dict__.copy() for r in self.queue],
        }


class TeachingDirector:
    """
    Deterministic owner of lesson state.

    Mika/Luna may reason about what should happen.
    This class owns what ACTUALLY happens.
    """

    def __init__(self, cast: list[str]):
        self.cast = tuple(cast)
        default_teacher = self.cast[0] if self.cast else "mika"
        self.session = TeachingSession(teacher=default_teacher)
        self.coding_lesson: CodingLesson | None = None

    def _teacher(self, teacher: str) -> str:
        value = str(teacher or "").lower()

        if value not in self.cast:
            raise ValueError(f"unknown teacher: {value}")

        return value

    def owns(self, student_id: str) -> bool:
        return (
            self.session.active
            and bool(student_id)
            and student_id == self.session.student_id
        )

    def start(
        self,
        student_id: str,
        student_name: str,
        goal: str,
        teacher: str,
    ) -> TeachingSession:

        if self.session.active:
            raise RuntimeError("a lesson is already active")

        self.session = TeachingSession(
            active=True,
            student_id=str(student_id),
            student_name=str(student_name),
            teacher=self._teacher(teacher),
            goal=str(goal).strip()[:500],
            phase="preparing",
        )
        self.coding_lesson = CodingLesson(
            student_id=self.session.student_id,
            student_name=self.session.student_name,
            teacher=self.session.teacher,
            goal=self.session.goal,
            phase="preparing",
        )
        self.sync_coding_state()

        return self.session

    def record_comment(self, student_id: str, student_name: str, text: str) -> bool:
        """Keep lesson comments in authoritative session state, not agent memory."""
        if not self.coding_lesson:
            return False
        owner = self.coding_lesson.add_comment(student_id, student_name, text)
        self.sync_coding_state()
        return owner

    def record_teacher_turn(self, text: str) -> None:
        if self.coding_lesson:
            self.coding_lesson.add_teacher_turn(text)
            self.sync_coding_state()

    def sync_coding_state(self) -> None:
        if self.coding_lesson:
            self.session.coding_state = self.coding_lesson.prompt_context()

    def switch_teacher(
        self,
        student_id: str,
        teacher: str,
    ) -> TeachingSession:

        self._require_owner(student_id)

        self.session.teacher = self._teacher(teacher)
        if self.coding_lesson:
            self.coding_lesson.teacher = self.session.teacher
            self.sync_coding_state()

        return self.session

    def pause(self, student_id: str) -> TeachingSession:
        self._require_owner(student_id)

        self.session.phase = "paused"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def resume(self, student_id: str) -> TeachingSession:
        self._require_owner(student_id)

        self.session.phase = "teaching"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def set_pace(
        self,
        student_id: str,
        pace: str,
    ) -> TeachingSession:

        self._require_owner(student_id)

        allowed = {
            "slow",
            "normal",
            "fast",
            "beginner",
            "detailed",
            "demo",
            "interactive",
        }

        if pace not in allowed:
            raise ValueError(f"unsupported pace: {pace}")

        self.session.pace = pace
        self.session.phase = "adapting"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def go_back(
        self,
        student_id: str,
    ) -> TeachingSession:
        self._require_owner(student_id)

        self.session.current_step = max(
            0,
            self.session.current_step - 1,
        )
        self.session.phase = "adapting"
        if self.coding_lesson:
            self.coding_lesson.step = self.session.current_step
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def skip(
        self,
        student_id: str,
    ) -> TeachingSession:
        self._require_owner(student_id)

        self.session.current_step += 1
        self.session.phase = "adapting"
        if self.coding_lesson:
            self.coding_lesson.step = self.session.current_step
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def set_phase(self, phase: str) -> TeachingSession:
        if phase not in VALID_PHASES:
            raise ValueError(f"unsupported teaching phase: {phase}")

        self.session.phase = phase
        if self.coding_lesson:
            self.coding_lesson.phase = phase
            self.sync_coding_state()

        return self.session

    def record_error(self, message: str) -> TeachingSession:
        self.session.last_error = str(message).strip()[:1000]
        self.session.phase = "error"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def clear_error(self) -> TeachingSession:
        self.session.last_error = ""

        if self.session.active:
            self.session.phase = "teaching"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def queue_request(
        self,
        student_id: str,
        student_name: str,
        goal: str,
        teacher: str = "",
    ) -> bool:

        if not self.session.active:
            return False

        if self.owns(student_id):
            return False

        if any(r.student_id == student_id for r in self.session.queue):
            return False

        chosen = (
            self._teacher(teacher)
            if teacher
            else (self.cast[0] if self.cast else "mika")
        )

        self.session.queue.append(
            TeachingRequest(
                student_id=str(student_id),
                student_name=str(student_name),
                goal=str(goal).strip()[:500],
                teacher=chosen,
            )
        )

        return True

    def quit(self, student_id: str) -> TeachingSession:
        self._require_owner(student_id)

        self.session.active = False
        self.session.phase = "abandoned"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def finish(self) -> TeachingSession:
        self.session.active = False
        self.session.phase = "finished"
        if self.coding_lesson:
            self.coding_lesson.phase = self.session.phase
            self.sync_coding_state()

        return self.session

    def _require_owner(self, student_id: str) -> None:
        if not self.owns(student_id):
            raise PermissionError(
                "only the current student can control this lesson"
            )
