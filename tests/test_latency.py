"""Latency: the single-message fast path, natural first chunks, and streaming
LLM to TTS (speech starts before the model has finished). Runs the real
pipeline from scripts/latency_bench.py with simulated LLM and TTS timing."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_llm_vtuber.utils.sentence_divider import first_clause_split

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Oh, stars are my favorite", ("Oh,", "stars are my favorite")),
        ("Well, I think so", ("Well,", "I think so")),
        (
            "Stars are lovely, especially Orion",
            ("Stars are lovely,", "especially Orion"),
        ),
        ("I, for one, agree", ("I, for one,", "agree")),
        ("No comma here", None),
    ],
)
def test_first_clause_split_avoids_broken_fragments(text, expected):
    assert first_clause_split(text) == expected


def _bench():
    spec = importlib.util.spec_from_file_location(
        "latency_bench", ROOT / "scripts" / "latency_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["latency_bench"] = module
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    base = dict(
        scenario="single",
        repeat=1,
        llm_first_token=0.2,
        llm_tps=40.0,
        tts_base=0.2,
        tts_per_char=0.002,
        cooldown=3.0,
        gap=0.3,
        play_delay=0.01,
        no_fast_path=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(scope="module")
def bench():
    return _bench()


def test_speech_starts_before_the_llm_finishes(bench):
    out = asyncio.run(bench.run(_args()))
    marks = out["results"][0]["marks_s"]
    assert marks["tts_request_started"] < marks["llm_completed"]
    assert marks["audio_playback_started"] < marks["llm_completed"]
    # The first chunk is a natural clause, not the whole reply.
    assert out["first_tts_chunk"] and len(out["first_tts_chunk"]) < 40


def test_single_message_skips_the_response_cooldown(bench):
    fast = asyncio.run(bench.run(_args(scenario="back_to_back", cooldown=5.0, gap=1.0)))
    queue = [r["spans_ms"]["queue_and_selection"] for r in fast["results"]]
    assert max(queue) < 150, queue  # never waits out the 5 s cooldown

    slow = asyncio.run(
        bench.run(
            _args(scenario="back_to_back", cooldown=5.0, gap=1.0, no_fast_path=True)
        )
    )
    queue_slow = [r["spans_ms"]["queue_and_selection"] for r in slow["results"]]
    assert max(queue_slow) > 1000, queue_slow  # the old path waited for the cooldown


def test_pipeline_overhead_is_small(bench):
    out = asyncio.run(bench.run(_args()))
    spans = out["results"][0]["spans_ms"]
    ours = (
        spans["queue_and_selection"]
        + spans["character_routing"]
        + spans["first_speakable_chunk"]
        + spans["audio_prepare_and_send"]
    )
    assert ours < 150, spans
