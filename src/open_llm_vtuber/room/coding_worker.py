"""One coding worker, so the source is never written by two things at once.

Chat is faster than code. While Mika is writing a change, more comments keep
arriving, and the stream should end up in the state the viewers last asked
for, not in whichever state finished last.

    make it purple              -> starts
    actually make it blue       -> purple is stale, blue takes the pending slot
    run it when you're done     -> upgrades the pending blue job to also run

So there is exactly one active job and at most one pending job, which is
always the newest request. A plain "run" while a change is in flight attaches
to that change instead of executing the old source, which is why the stream
never runs a version nobody asked for.

Ordinary conversation never comes through here. Only create, modify and run
do, so chat stays responsive while code is being written.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

# What a submitted job ends up doing.
SUPERSEDED = "superseded"


@dataclass
class CodingJob:
    """One unit of work for the worker."""

    action: str  # create | create_and_run | modify | modify_and_run | run
    owner_id: str
    language: str = ""
    kind: str = ""  # capabilities.KINDS name: which library, guide and template
    instruction: str = ""  # what the viewer asked for, in their words
    speaker: str = ""  # the character who will talk about it
    also_run: bool = False  # a later "run it" attached itself to this job
    notes: dict[str, Any] = field(default_factory=dict)  # requirements etc.
    submitted_at: float = field(default_factory=time.time)
    future: Optional["asyncio.Future[dict[str, Any]]"] = None

    @property
    def writes_code(self) -> bool:
        return self.action in ("create", "create_and_run", "modify", "modify_and_run")

    @property
    def should_run(self) -> bool:
        return self.also_run or self.action in ("create_and_run", "modify_and_run", "run")

    def describe(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "language": self.language,
            "kind": self.kind,
            "also_run": self.also_run,
            "instruction": self.instruction[:120],
            "speaker": self.speaker,
        }


class CodingWorker:
    """Serialises every change to the live source.

    ``execute`` does the real work (generate, write, run) for one job and
    returns the result dict the character will talk about.
    """

    def __init__(
        self,
        execute: Callable[[CodingJob], Awaitable[dict[str, Any]]],
        clock=time.time,
    ):
        self.execute = execute
        self.clock = clock
        self.active: Optional[CodingJob] = None
        self.pending: Optional[CodingJob] = None
        self._task: Optional[asyncio.Task] = None
        self.done = 0
        self.superseded = 0
        self.failures = 0

    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.active is not None

    def state(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "active": self.active.describe() if self.active else None,
            "pending": self.pending.describe() if self.pending else None,
            "done": self.done,
            "superseded": self.superseded,
            "failures": self.failures,
        }

    # ------------------------------------------------------------------
    def enqueue(self, job: CodingJob) -> "asyncio.Future[dict[str, Any]]":
        """Queue a job. Returns a future with that job's result.

        A newer job replaces a waiting one; the replaced job's future is
        resolved as superseded so whoever was waiting is never left hanging.
        """
        loop = asyncio.get_event_loop()
        job.future = loop.create_future()

        # A bare "run" never discards work in flight: it asks for the result
        # of what is being written, which is what "run it when you're done"
        # means. The newest write is what gets run.
        if job.action == "run":
            target = self.pending or self.active
            if target is not None and target.writes_code:
                target.also_run = True
                logger.info("Coding worker: run attached to the newest change")
                job.future.set_result(
                    {
                        "ok": True,
                        "action": "run",
                        "attached": True,
                        "note": "will run the change that is being written",
                    }
                )
                return job.future

        if self.active is None:
            self.active = job
            self._task = asyncio.get_event_loop().create_task(
                self._drain(), name="vr-coding-worker"
            )
            return job.future

        if self.pending is not None:
            stale = self.pending
            self.superseded += 1
            logger.info(
                f"Coding worker: dropping stale {stale.action} "
                f"({stale.instruction[:40]!r}) for a newer request"
            )
            if stale.future and not stale.future.done():
                stale.future.set_result(
                    {
                        "ok": True,
                        "action": stale.action,
                        "status": SUPERSEDED,
                        "note": "a newer request replaced this one",
                    }
                )
            # A run that had attached to the stale job still applies to the
            # newest one: the viewer asked to see the result either way.
            job.also_run = job.also_run or stale.also_run
        self.pending = job
        return job.future

    async def submit(self, job: CodingJob) -> dict[str, Any]:
        """Queue a job and wait for its verified result."""
        return await self.enqueue(job)

    # ------------------------------------------------------------------
    async def _drain(self) -> None:
        while self.active is not None:
            job = self.active
            try:
                result = await self.execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the stream survives a broken job
                self.failures += 1
                logger.exception("Coding worker: job failed")
                result = {
                    "ok": False,
                    "action": job.action,
                    "error": f"The coding runner failed: {str(exc)[:300]}",
                }
            else:
                self.done += 1
            if job.future and not job.future.done():
                job.future.set_result(result)
            self.active = self.pending
            self.pending = None
        self._task = None

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for everything queued right now to finish (tests, shutdown)."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            task = self._task
            if task is None or task.done():
                if self.active is None and self.pending is None:
                    return
            await asyncio.sleep(0.01)
