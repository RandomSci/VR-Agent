"""Small, stateful coding lessons with a restricted local script runner.

This module deliberately has no LLM or UI dependencies. The room director can
attach one lesson session to the active viewer, pass ``prompt_context()`` to
its normal turn, publish ``code`` to the Teaching Stage, then call ``run()``
when the turn requests execution. The runner requires bubblewrap and fails
closed when that OS boundary is unavailable.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import os
import resource
import secrets
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


MAX_CODE_BYTES = 32_000
MAX_OUTPUT_BYTES = 16_000
MAX_ARTIFACT_BYTES = 30_000_000
MAX_ARTIFACT_TOTAL_BYTES = 50_000_000
MAX_LESSON_MESSAGES = 24
RUN_TIMEOUT_SECONDS = 20
ALLOWED_LANGUAGES = {
    "python": ("python3", "main.py"),
    "javascript": ("node", "main.js"),
    "web": ("", "index.html"),
}


@dataclass
class LessonMessage:
    role: str
    text: str
    at: float = field(default_factory=time.time)


@dataclass
class RunResult:
    language: str
    filename: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    error: str = ""

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodingLesson:
    """Authoritative state for a single owned lesson, independent of LLM memory."""

    student_id: str
    student_name: str
    teacher: str
    goal: str
    phase: str = "teaching"
    step: int = 0
    code: str = ""
    language: str = "python"
    kind: str = ""  # what kind of program is on screen (capabilities.KINDS)
    # The last real-browser check of a web program (browser_qa summary).
    last_check: dict[str, Any] = field(default_factory=dict)
    # The publishing record for the program on screen (publishing.JobStore id).
    creation_job_id: str = ""
    last_result: RunResult | None = None
    transcript: list[LessonMessage] = field(default_factory=list)
    summary: str = ""

    # Persistent project memory. This is separate from conversational
    # transcript memory so long teaching sessions do not lose project rules.
    project_summary: str = ""
    project_requirements: list[str] = field(default_factory=list)
    project_forbidden: list[str] = field(default_factory=list)
    workspace_id: str = field(default_factory=lambda: secrets.token_hex(16), repr=False)

    def _append(self, message: LessonMessage) -> None:
        self.transcript.append(message)
        overflow = len(self.transcript) - MAX_LESSON_MESSAGES
        if overflow > 0:
            removed = self.transcript[:overflow]
            self.transcript = self.transcript[overflow:]
            notes = " | ".join(f"{item.role}: {item.text}" for item in removed)
            self.summary = (self.summary + " " + notes)[-1600:]

    def update_project_state(
        self,
        *,
        requirements: list[str] | None = None,
        forbidden: list[str] | None = None,
        remove_requirements: list[str] | None = None,
        remove_forbidden: list[str] | None = None,
        project_summary: str = "",
    ) -> None:
        """
        Merge durable project constraints.

        New requirements are additive by default.
        A requirement disappears only when explicitly placed in a remove list.
        """

        def clean(items):
            out = []
            for item in items or []:
                item = " ".join(str(item or "").split()).strip()
                if not item:
                    continue
                item = item[:240]
                if item not in out:
                    out.append(item)
            return out

        def merge(existing, incoming, removed):
            removed_lower = {x.lower() for x in clean(removed)}
            result = [x for x in existing if x.lower() not in removed_lower]

            existing_lower = {x.lower() for x in result}

            for item in clean(incoming):
                if item.lower() not in existing_lower:
                    result.append(item)
                    existing_lower.add(item.lower())

            # Bound prompt size during long livestream sessions.
            return result[-32:]

        self.project_requirements = merge(
            self.project_requirements,
            requirements,
            remove_requirements,
        )

        self.project_forbidden = merge(
            self.project_forbidden,
            forbidden,
            remove_forbidden,
        )

        project_summary = " ".join(str(project_summary or "").split()).strip()

        if project_summary:
            self.project_summary = project_summary[-1600:]

    def owns(self, student_id: str) -> bool:
        return bool(student_id) and student_id == self.student_id

    def add_comment(self, student_id: str, name: str, text: str) -> bool:
        """Record every comment; return whether this commenter owns mutations."""
        text = str(text or "").strip()
        if not text:
            return False
        self._append(LessonMessage("viewer", f"{name}: {text}"[:1200]))
        return self.owns(student_id)

    def add_teacher_turn(self, text: str) -> None:
        text = str(text or "").strip()
        if text:
            self._append(LessonMessage("teacher", text[:1200]))

    def prompt_context(self) -> dict[str, Any]:
        """Bounded context for the next normal LLM turn; no classifier call needed."""
        last_run = self.last_result.snapshot() if self.last_result else None
        if last_run:
            last_run["stdout"] = last_run["stdout"][-1200:]
            last_run["stderr"] = last_run["stderr"][-1200:]
        return {
            "goal": self.goal,
            "teacher": self.teacher,
            "phase": self.phase,
            "step": self.step,
            "summary": self.summary,
            "project_state": {
                "summary": self.project_summary,
                "requirements": list(self.project_requirements),
                "forbidden": list(self.project_forbidden),
            },
            "recent_comments": [asdict(item) for item in self.transcript[-12:]],
            "artifact": {
                "language": self.language,
                "kind": self.kind,
                "code": self.code,
            },
            "last_run": last_run,
        }

    @staticmethod
    def _validate_python_runtime(code: str) -> None:
        """Reject Python modules that require unsupported visual runtimes."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return

        unsupported = {"manim", "turtle", "tkinter"}
        found: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".", 1)[0])

        blocked = sorted(found & unsupported)
        if blocked:
            names = ", ".join(blocked)
            raise ValueError(
                f"Unsupported Python module(s): {names}. "
                "Use self-contained HTML/CSS/JavaScript for animation or interactive visuals."
            )

    def set_artifact(self, student_id: str, language: str, code: str) -> None:
        if not self.owns(student_id):
            raise PermissionError("only the lesson owner can change the code")
        if language not in ALLOWED_LANGUAGES:
            raise ValueError("language must be python, javascript, or web")

        if language == "python":
            self._validate_python_runtime(code)

        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise ValueError(f"code exceeds {MAX_CODE_BYTES} bytes")
        self.language, self.code = language, code
        self.last_result = None


def sandbox_environment() -> dict[str, str]:
    """The only environment a generated program ever sees."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
        **SINGLE_THREAD_MATH,
    }


# numpy and scipy start one math thread per CPU core when imported. Under the
# sandbox's memory and process limits that fails on a many-core desktop
# ("OpenBLAS blas_thread_init: pthread_create failed") and the program dies.
# Lesson-sized programs gain nothing from those threads, so use one.
SINGLE_THREAD_MATH = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

# RLIMIT_NPROC counts every process and thread of this user on the whole
# machine, not just the sandbox. A fixed 128 is already used up by a normal
# desktop session (browser, OBS, the server itself), so the limit is set to
# what the user already runs plus room for the program.
SANDBOX_EXTRA_PROCESSES = 64


def sandbox_process_limit() -> int:
    uid = os.getuid()
    used = 0
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            text = status.read_text()
        except OSError:
            continue
        owner = threads = None
        for line in text.splitlines():
            if line.startswith("Uid:"):
                owner = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
        if owner == uid and threads:
            used += threads
    return max(128, used + SANDBOX_EXTRA_PROCESSES)


class RestrictedScriptRunner:
    """Execute only a session script inside a no-network bubblewrap namespace."""

    def __init__(self, timeout: int = RUN_TIMEOUT_SECONDS):
        self.timeout = min(max(int(timeout), 1), 60)
        self.workspace_root = Path(
            tempfile.mkdtemp(prefix="vr-coding-lessons-")
        ).resolve()

        self._prune_workspaces()

    def _prune_workspaces(self) -> None:
        """Remove stale runner directories left by stopped DEV processes."""
        cutoff = time.time() - 24 * 60 * 60
        for candidate in Path(tempfile.gettempdir()).glob("vr-coding-lessons-*"):
            if candidate.resolve() == self.workspace_root:
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                continue

    def close(self, lesson: CodingLesson) -> None:
        """Remove this lesson's files when its session finishes or is abandoned."""
        path = (self.workspace_root / lesson.workspace_id).resolve()
        if path.parent == self.workspace_root:
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _limits() -> None:
        # Keep live coding scripts tightly bounded and predictable.
        cpu_seconds = 15
        memory_bytes = 1536 * 1024 * 1024
        open_files = 32

        resource.setrlimit(
            resource.RLIMIT_CPU,
            (cpu_seconds, cpu_seconds),
        )

        resource.setrlimit(
            resource.RLIMIT_AS,
            (memory_bytes, memory_bytes),
        )

        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (
                MAX_ARTIFACT_BYTES,
                MAX_ARTIFACT_BYTES,
            ),
        )

        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (open_files, open_files),
        )

        resource.setrlimit(
            resource.RLIMIT_CORE,
            (0, 0),
        )

    async def run(self, lesson: CodingLesson, student_id: str) -> RunResult:
        self._prune_workspaces()
        if not lesson.owns(student_id):
            raise PermissionError("only the lesson owner can run code")
        if lesson.language not in ALLOWED_LANGUAGES:
            raise ValueError("unsupported language")
        if not lesson.code.strip():
            raise ValueError("there is no code to run")

        bwrap = shutil.which("bwrap")
        interpreter_name, filename = ALLOWED_LANGUAGES[lesson.language]

        python_prefix = None

        if lesson.language == "python":
            configured_python = os.environ.get(
                "VR_CODING_PYTHON",
                "",
            ).strip()

            configured_prefix = os.environ.get(
                "VR_CODING_PYTHON_PREFIX",
                "",
            ).strip()

            # start-teaching.sh exports the interpreter it was launched with.
            # When it is missing (a test, or the server started another way)
            # fall back to the interpreter running the server rather than
            # failing every single run with a configuration message.
            if not configured_python or not configured_prefix:
                configured_python = sys.executable
                configured_prefix = sys.prefix
                logger.info(
                    "Coding runtime: VR_CODING_PYTHON is not set; "
                    f"using the running interpreter ({configured_python})"
                )

            interpreter_path = Path(configured_python).resolve()
            python_prefix = Path(configured_prefix).resolve()

            if (
                not interpreter_path.is_file()
                or not os.access(interpreter_path, os.X_OK)
                or not python_prefix.is_dir()
                or not interpreter_path.is_relative_to(python_prefix)
            ):
                result = RunResult(
                    lesson.language,
                    filename,
                    error=f"The Python runtime at {interpreter_path} is not usable.",
                )
                lesson.last_result = result
                return result

        else:
            interpreter = next(
                (
                    candidate
                    for candidate in (
                        f"/usr/bin/{interpreter_name}",
                        f"/bin/{interpreter_name}",
                    )
                    if Path(candidate).is_file() and os.access(candidate, os.X_OK)
                ),
                None,
            )

            if not interpreter:
                result = RunResult(
                    lesson.language,
                    filename,
                    error="Selected runtime is unavailable.",
                )
                lesson.last_result = result
                return result

            interpreter_path = Path(interpreter).resolve()

        # The helper sets process limits, then execs the real interpreter. It
        # runs INSIDE the sandbox, so it must live under a folder the sandbox
        # mounts. sys.executable is not safe here: under `uv run` it resolves
        # into ~/.local/share/uv/python, which is never mounted, and every run
        # failed with "bwrap: execvp ... No such file or directory".
        # Resolve through symlinks too: on Debian /usr/bin/python3 points into
        # /etc/alternatives, which is not mounted either.
        mounted_roots = [
            Path(p) for p in ("/usr", "/bin", "/lib", "/lib64") if Path(p).exists()
        ]
        if python_prefix is not None:
            mounted_roots.append(python_prefix)
        helper_candidates = []
        if lesson.language == "python":
            helper_candidates.append(str(interpreter_path))
        helper_candidates += ["/usr/bin/python3", "/bin/python3", sys.executable]
        helper_interpreter = None
        for candidate in helper_candidates:
            if not candidate:
                continue
            resolved = Path(candidate).resolve()
            if (
                resolved.is_file()
                and os.access(resolved, os.X_OK)
                and any(resolved.is_relative_to(root) for root in mounted_roots)
            ):
                helper_interpreter = str(resolved)
                break

        if not bwrap or not helper_interpreter:
            result = RunResult(
                lesson.language,
                filename,
                error=(
                    "Isolated runner unavailable because bubblewrap "
                    "or its helper interpreter is missing."
                ),
            )
            lesson.last_result = result
            return result

        root = (self.workspace_root / lesson.workspace_id).resolve()
        if root.parent != self.workspace_root:
            raise ValueError("invalid lesson workspace")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            script = root / filename

            # A previous run deliberately leaves the script read-only.
            # Restore owner write permission before replacing its contents.
            if script.exists():
                os.chmod(script, 0o600)

            script.write_text(
                lesson.code,
                encoding="utf-8",
            )

            # Sandbox only needs to read the finished source file.
            os.chmod(script, 0o400)
            argv = [
                bwrap,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--ro-bind",
                "/usr",
                "/usr",
            ]

            if lesson.language == "python" and python_prefix is not None:
                argv.extend(
                    [
                        "--ro-bind",
                        str(python_prefix),
                        str(python_prefix),
                    ]
                )
            for system_path in ("/bin", "/lib", "/lib64"):
                if Path(system_path).exists():
                    argv.extend(["--ro-bind", system_path, system_path])
            argv.extend(
                [
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--tmpfs",
                    "/tmp",
                    "--dir",
                    "/lesson",
                    "--bind",
                    str(root),
                    "/lesson",
                    "--chdir",
                    "/lesson",
                    "--setenv",
                    "HOME",
                    "/lesson",
                    "--setenv",
                    "PATH",
                    (
                        str(python_prefix / "bin") + ":/usr/local/bin:/usr/bin:/bin"
                        if lesson.language == "python" and python_prefix is not None
                        else "/usr/local/bin:/usr/bin:/bin"
                    ),
                    "--setenv",
                    "PYTHONNOUSERSITE",
                    "1",
                    "--setenv",
                    "MPLBACKEND",
                    "Agg",
                    "--setenv",
                    "MPLCONFIGDIR",
                    "/tmp/matplotlib",
                ]
            )
            for name, value in SINGLE_THREAD_MATH.items():
                argv.extend(["--setenv", name, value])
            argv.extend(
                [
                    "--",
                    helper_interpreter,
                    "-c",
                    "import os,resource,sys; n=int(sys.argv[1]); "
                    "resource.setrlimit(resource.RLIMIT_NPROC,(n,n)); "
                    "os.execv(sys.argv[2],sys.argv[2:])",
                    str(sandbox_process_limit()),
                    str(interpreter_path),
                    f"/lesson/{filename}",
                ]
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=self._limits,
                # Never the server's environment: it can hold API keys and
                # publishing tokens, and generated code could print them on
                # stream. bubblewrap passes this environment through, so the
                # sandboxed program sees only these few harmless variables.
                env=sandbox_environment(),
            )
            exceeded_output = asyncio.Event()

            async def capture(stream: asyncio.StreamReader) -> bytes:
                chunks: list[bytes] = []
                size = 0
                while True:
                    block = await stream.read(4096)
                    if not block:
                        return b"".join(chunks)
                    allowed = MAX_OUTPUT_BYTES - size
                    chunks.append(block[:allowed])
                    size += min(len(block), allowed)
                    if len(block) > allowed:
                        exceeded_output.set()
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return b"".join(chunks)

            stdout_task = asyncio.create_task(capture(proc.stdout))
            stderr_task = asyncio.create_task(capture(proc.stderr))
            timed_out = False
            execution_timeout = self.timeout
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=execution_timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            else:
                # Do not leave background children alive after the lesson
                # process exits (they share its isolated process group).
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            result = RunResult(
                lesson.language,
                filename,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
                proc.returncode,
                timed_out=timed_out,
                error=(
                    f"Execution exceeded {execution_timeout} seconds."
                    if timed_out
                    else "Output limit exceeded."
                    if exceeded_output.is_set()
                    else ""
                ),
            )
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        lesson.last_result = result
        lesson.step += 1
        return result

    def artifacts(self, lesson: CodingLesson) -> list[dict[str, str]]:
        """Return small viewer-safe images/data files created in this lesson."""
        root = (self.workspace_root / lesson.workspace_id).resolve()
        if root.parent != self.workspace_root or not root.is_dir():
            return []
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".csv": "text/csv",
            ".json": "application/json",
            ".txt": "text/plain",
        }
        found: list[dict[str, str]] = []
        total_bytes = 0
        for path in sorted(root.rglob("*")):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.name in {"main.py", "main.js"}
            ):
                continue
            mime = mime_types.get(path.suffix.lower())
            size = path.stat().st_size
            if (
                mime
                and size <= MAX_ARTIFACT_BYTES
                and total_bytes + size <= MAX_ARTIFACT_TOTAL_BYTES
            ):
                found.append(
                    {
                        "name": path.name,
                        "mime": mime,
                        "data_base64": base64.b64encode(path.read_bytes()).decode(
                            "ascii"
                        ),
                    }
                )
                total_bytes += size
            if len(found) >= 4:
                break
        return found
