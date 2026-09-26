"""Six simulated hours of unattended streaming with occasional viewer bursts.

Checks that nothing grows without bound, games start only when asked and end
on their own, speech (TTS) only happens shortly after a viewer message, the
LLM is never called by the room itself, and the room returns to idle.
"""

from __future__ import annotations

import asyncio
import random

from open_llm_vtuber.vr_agent.usage import usage
from tests.test_zero_activity import Sim

SIX_HOURS = 6 * 3600


def test_six_hour_soak_stays_bounded_and_quiet():
    rng = random.Random(42)
    viewer_times: list[float] = []
    tts_times: list[float] = []

    async def scenario():
        usage.reset()
        sim = Sim()
        original = sim.tts.generate_audio

        def timed(text, file_name_no_ext=None):
            tts_times.append(sim.clock.t)
            return original(text, file_name_no_ext)

        sim.tts.generate_audio = timed
        await sim.connect()
        start = sim.clock.t
        next_burst = start + rng.uniform(600, 1800)
        while sim.clock.t - start < SIX_HOURS:
            if sim.clock.t >= next_burst:
                # A few minutes of chat: start a game, answer, zoom, chatter.
                for _ in range(rng.randint(3, 10)):
                    engine = sim.session.show.engine
                    options = [
                        "play trivia",
                        "zoom in on Luna",
                        "how are you both?",
                        "lol",
                        "what games do you have",
                    ]
                    if engine.playing and engine.active.question:
                        q = engine.active.question
                        options += [
                            q["correct_answer"],
                            q["wrong_answers"][0],
                            "next round",
                            "make it harder",
                        ]
                        if rng.random() < 0.1:
                            options.append("stop the game")
                    text = rng.choice(options)
                    viewer_times.append(sim.clock.t)
                    sim.chat(f"@viewer{rng.randint(1, 40)}", text)
                    await sim.run(rng.uniform(5, 25), step=1.0)
                next_burst = sim.clock.t + rng.uniform(900, 2700)
            await sim.run(30, step=1.0)
        # Long quiet period at the end.
        await sim.run(1200, step=1.0)
        return sim

    sim = asyncio.run(scenario())
    session = sim.session
    engine = session.show.engine

    # Games only started because viewers asked, and all of them ended.
    assert engine.games_started >= 2
    assert not engine.playing
    # Bounded memory everywhere.
    assert len(session.traces) <= 300
    assert len(session.bus.log) <= 300
    assert len(session.state.recent_lines) <= 12
    assert len(session.show.lines) <= 3
    assert all(len(c.recent_dialogue) <= 4 for c in session.state.characters.values())
    # Speech only right after viewer activity (speech window), never while idle.
    window = session.room.speech_window_seconds
    for t in tts_times:
        assert any(0 <= t - v <= window + 1 for v in viewer_times), (
            f"TTS at {t} without a recent viewer"
        )
    assert (
        usage.snapshot()["llm_requests"] == 0
    )  # replies to chatter run elsewhere; the room never calls the LLM
    assert not session.speech_allowed()
