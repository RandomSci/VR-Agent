"""A real notebook kernel for class mode: cells share variables, like Jupyter.

One sandboxed Python process (the same bubblewrap jail as the script runner:
no network, no access to the server's files or keys) stays alive for the
whole class, so `x = 5` in one cell is still there in the next. Each cell
returns its printed output, the value of its last line (with LaTeX for sympy
objects), any matplotlib figures as PNG, and a readable error.

A cell that runs too long kills the kernel; a fresh one starts and the class
goes on (the lesson re-runs what it needs).
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from .coding_lesson import SINGLE_THREAD_MATH, sandbox_environment, sandbox_process_limit

MARKER = "\x1e__VRK__"
CHILD = Path(__file__).with_name("kernel_child.py")
MEMORY_BYTES = 2 * 1024**3
MAX_STRAY = 6000


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20_000_000, 20_000_000))


def _python() -> tuple[Path, Path]:
    configured = os.environ.get("VR_CODING_PYTHON", "").strip()
    prefix = os.environ.get("VR_CODING_PYTHON_PREFIX", "").strip()
    if not configured or not prefix:
        configured, prefix = sys.executable, sys.prefix
    return Path(configured).resolve(), Path(prefix).resolve()


class NotebookKernel:
    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.workdir = Path(tempfile.mkdtemp(prefix="vr-class-kernel-"))
        self._lock = asyncio.Lock()
        self._counter = 0
        self.restarts = 0

    # ------------------------------------------------------------- process
    def _argv(self) -> list[str]:
        interpreter, prefix = _python()
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("bubblewrap is not installed")
        shutil.copyfile(CHILD, self.workdir / "kernel_child.py")
        argv = [bwrap, "--die-with-parent", "--new-session", "--unshare-all",
                "--ro-bind", "/usr", "/usr", "--ro-bind", str(prefix), str(prefix)]
        for system_path in ("/bin", "/lib", "/lib64"):
            if Path(system_path).exists():
                argv += ["--ro-bind", system_path, system_path]
        argv += [
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--dir", "/class", "--bind", str(self.workdir), "/class", "--chdir", "/class",
            "--setenv", "HOME", "/class",
            "--setenv", "PATH", f"{prefix / 'bin'}:/usr/local/bin:/usr/bin:/bin",
            "--setenv", "PYTHONNOUSERSITE", "1",
            "--setenv", "PYTHONUNBUFFERED", "1",
            "--setenv", "MPLBACKEND", "Agg",
            "--setenv", "MPLCONFIGDIR", "/tmp/matplotlib",
        ]
        for name, value in SINGLE_THREAD_MATH.items():
            argv += ["--setenv", name, value]
        argv += [
            "--", str(interpreter), "-c",
            "import os,resource,sys; n=int(sys.argv[1]); "
            "resource.setrlimit(resource.RLIMIT_NPROC,(n,n)); "
            "os.execv(sys.argv[2],sys.argv[2:])",
            str(sandbox_process_limit()), str(interpreter), "-u", "/class/kernel_child.py",
        ]
        return argv

    async def start(self) -> None:
        await self.stop()
        self.proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_limits,
            env=sandbox_environment(),
            limit=8 * 1024 * 1024,
        )
        ready = await self._read_reply(timeout=30)
        if not ready or ready.get("id") != "ready":
            await self.stop()
            raise RuntimeError("the notebook kernel did not start")
        logger.info("Class notebook kernel started")
        # Warm the slow imports once (matplotlib builds its font cache on the
        # first import) so the first plotting cell on stream is not the slow one.
        await self._send_raw(
            "try:\n import matplotlib, matplotlib.pyplot\nexcept Exception:\n pass\n"
            "try:\n import numpy, sympy\nexcept Exception:\n pass"
        )
        await self._read_reply(timeout=90)

    async def _send_raw(self, code: str) -> None:
        assert self.proc and self.proc.stdin
        self._counter += 1
        self.proc.stdin.write((json.dumps({"id": self._counter, "code": code}) + "\n").encode())
        await self.proc.stdin.drain()

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                pass

    async def _read_reply(self, timeout: float) -> Optional[dict[str, Any]]:
        """The next MARKER line; stray output before it is attached."""
        assert self.proc and self.proc.stdout
        stray: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return None
            try:
                raw = await asyncio.wait_for(self.proc.stdout.readline(), left)
            except asyncio.TimeoutError:
                return None
            if not raw:
                if stray:
                    logger.warning(f"Notebook kernel output before exit: {''.join(stray)[-500:]}")
                return None
            line = raw.decode("utf-8", "replace")
            if line.startswith(MARKER):
                try:
                    reply = json.loads(line[len(MARKER):])
                except ValueError:
                    continue
                if stray:
                    reply["stdout"] = ("".join(stray) + reply.get("stdout", ""))[-MAX_STRAY:]
                return reply
            stray.append(line)

    # ------------------------------------------------------------- public
    async def execute(self, code: str, timeout: float = 25) -> dict[str, Any]:
        async with self._lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()
            self._counter += 1
            request = {"id": self._counter, "code": str(code or "")[:20000]}
            assert self.proc and self.proc.stdin
            self.proc.stdin.write((json.dumps(request) + "\n").encode())
            await self.proc.stdin.drain()
            reply = await self._read_reply(timeout)
            if reply is None:
                self.restarts += 1
                await self.stop()
                return {
                    "stdout": "",
                    "result": None,
                    "images": [],
                    "error": {
                        "name": "Timeout",
                        "value": f"the cell ran longer than {int(timeout)} seconds",
                        "line": None,
                        "trace": "The cell took too long, so the notebook restarted.",
                    },
                    "restarted": True,
                }
            return reply

    async def reset(self) -> None:
        async with self._lock:
            await self.start()

    async def close(self) -> None:
        await self.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)
