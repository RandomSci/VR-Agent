"""Livestream turns keep LLM history short and store compact memory."""

import pytest

agent_mod = pytest.importorskip("open_llm_vtuber.agent.agents.basic_memory_agent")

from open_llm_vtuber.agent.input_types import BatchInput, TextData, TextSource  # noqa: E402


def turn(text, **metadata):
    return BatchInput(
        texts=[TextData(source=TextSource.INPUT, content=text, from_name="v")],
        metadata=metadata or None,
    )


def test_history_is_bounded_and_memory_is_compact():
    agent = agent_mod.BasicMemoryAgent(llm=None, system="s", live2d_model=None)
    for i in range(100):
        sent = agent._to_messages(
            turn(
                f"LONG PROMPT {i} with instructions",
                history_limit=10,
                memory_text=f"@v: hi {i}",
            )
        )
        agent._add_message(f"reply {i}", "assistant")
        assert len(sent) <= 11  # 10 history + the new prompt
    assert len(agent._memory) <= 40
    assert agent._memory[-2]["content"] == "@v: hi 99"
    assert "instructions" not in agent._memory[-2]["content"]
    assert sent[-1]["content"][0]["text"] == "LONG PROMPT 99 with instructions"


def test_normal_conversations_are_unchanged():
    agent = agent_mod.BasicMemoryAgent(llm=None, system="s", live2d_model=None)
    for i in range(30):
        agent._to_messages(turn(f"hello {i}"))
    assert len(agent._memory) == 30
    assert agent._memory[-1]["content"] == "hello 29"
