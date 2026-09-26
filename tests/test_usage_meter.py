"""The usage meter counts every LLM request and every TTS generation."""

import asyncio

from open_llm_vtuber.vr_agent.usage import UsageMeter, count_llm_calls, usage


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def chat_completion(self, messages, system=None, tools=None):
        self.calls += 1
        return iter(["hi"])


def test_llm_wrapper_counts_each_request_and_keeps_the_class():
    usage.reset()
    llm = count_llm_calls(FakeLLM(), source="test")
    count_llm_calls(llm)  # idempotent, never double counts
    assert isinstance(llm, FakeLLM)
    list(llm.chat_completion([]))
    list(llm.chat_completion([]))
    snap = usage.snapshot()
    assert snap["llm_requests"] == 2 and llm.calls == 2
    assert snap["llm_by_source"] == {"test": 2}


def test_tts_manager_counts_each_generation(tmp_path):
    from open_llm_vtuber.conversations.tts_manager import TTSTaskManager

    class FakeTTS:
        _vr_usage_source = "luna"

        async def async_generate_audio(self, text, file_name_no_ext):
            return str(tmp_path / f"{file_name_no_ext}.wav")

    usage.reset()
    manager = TTSTaskManager()
    asyncio.run(manager._generate_audio(FakeTTS(), "hello"))
    asyncio.run(manager._generate_audio(FakeTTS(), "again"))
    assert usage.snapshot()["tts_requests"] == 2
    assert usage.snapshot()["tts_by_source"] == {"luna": 2}


def test_meter_snapshot_starts_at_zero():
    meter = UsageMeter()
    snap = meter.snapshot()
    assert snap["llm_requests"] == 0 and snap["tts_requests"] == 0
    assert snap["viewer_triggered_interactions"] == 0
    meter.record_viewer_interaction()
    assert meter.snapshot()["viewer_triggered_interactions"] == 1
