"""One coding worker: newest request wins, and run never executes a version
nobody asked for."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.coding_worker import CodingJob, CodingWorker  # noqa: E402


class Recorder:
    """Stands in for generate + write + run, and records what really ran."""

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.executed: list[dict] = []
        self.source = "original"
        self.ran: list[str] = []

    async def __call__(self, job: CodingJob) -> dict:
        await asyncio.sleep(self.delay)
        if job.writes_code:
            self.source = job.instruction
        if job.should_run:
            self.ran.append(self.source)
        self.executed.append(
            {"action": job.action, "instruction": job.instruction, "ran": job.should_run}
        )
        return {"ok": True, "action": job.action, "source": self.source}


def job(action: str, instruction: str = "") -> CodingJob:
    return CodingJob(action=action, owner_id="v1", language="web", instruction=instruction)


def test_one_job_at_a_time():
    recorder = Recorder()
    worker = CodingWorker(recorder)

    async def go():
        first = worker.enqueue(job("modify", "purple"))
        assert worker.busy
        await worker.drain()
        return await first

    result = asyncio.run(go())
    assert result["ok"] and recorder.executed == [
        {"action": "modify", "instruction": "purple", "ran": False}
    ]


def test_the_newest_request_replaces_a_waiting_one():
    """purple becomes stale, blue replaces it."""
    recorder = Recorder()
    worker = CodingWorker(recorder)

    async def go():
        running = worker.enqueue(job("modify", "red"))  # occupies the worker
        purple = worker.enqueue(job("modify", "purple"))  # waits
        blue = worker.enqueue(job("modify", "blue"))  # replaces purple
        await worker.drain()
        return await running, await purple, await blue

    red, purple, blue = asyncio.run(go())
    assert purple["status"] == "superseded"
    assert [e["instruction"] for e in recorder.executed] == ["red", "blue"]
    assert recorder.source == "blue"
    assert worker.superseded == 1


def test_run_while_busy_runs_the_newest_change_not_the_old_source():
    """"run it when you're done" must not throw away the pending change."""
    recorder = Recorder()
    worker = CodingWorker(recorder)

    async def go():
        worker.enqueue(job("modify", "red"))
        worker.enqueue(job("modify", "purple"))
        worker.enqueue(job("modify", "blue"))
        attached = worker.enqueue(job("run"))
        await worker.drain()
        return await attached

    attached = asyncio.run(go())
    assert attached["attached"] is True
    # Exactly one execution happened, and it was of the newest source.
    assert recorder.ran == ["blue"]
    assert recorder.source == "blue"


def test_a_run_attached_to_a_stale_job_still_applies_to_the_newest():
    recorder = Recorder()
    worker = CodingWorker(recorder)

    async def go():
        worker.enqueue(job("modify", "red"))
        worker.enqueue(job("modify", "purple"))
        worker.enqueue(job("run"))  # attaches to purple
        worker.enqueue(job("modify", "blue"))  # replaces purple
        await worker.drain()

    asyncio.run(go())
    assert recorder.ran == ["blue"]


def test_run_with_nothing_in_flight_executes_the_existing_source():
    recorder = Recorder()
    worker = CodingWorker(recorder)

    async def go():
        result = worker.enqueue(job("run"))
        await worker.drain()
        return await result

    result = asyncio.run(go())
    assert result["ok"] and recorder.ran == ["original"]
    assert recorder.source == "original"  # run never rewrote anything


def test_a_failing_job_does_not_stop_the_next_one():
    calls = {"n": 0}

    async def flaky(job: CodingJob) -> dict:
        calls["n"] += 1
        if job.instruction == "boom":
            raise RuntimeError("generator exploded")
        return {"ok": True, "action": job.action}

    worker = CodingWorker(flaky)

    async def go():
        bad = worker.enqueue(job("modify", "boom"))
        good = worker.enqueue(job("modify", "fine"))
        await worker.drain()
        return await bad, await good

    bad, good = asyncio.run(go())
    assert bad["ok"] is False and "failed" in bad["error"]
    assert good["ok"] is True and worker.failures == 1


def test_only_one_pending_job_is_ever_held():
    recorder = Recorder()
    worker = CodingWorker(recorder)

    async def go():
        worker.enqueue(job("modify", "a"))
        for name in "bcdefgh":
            worker.enqueue(job("modify", name))
            assert worker.pending is not None
        await worker.drain()

    asyncio.run(go())
    assert [e["instruction"] for e in recorder.executed] == ["a", "h"]
    assert worker.pending is None and worker.active is None
