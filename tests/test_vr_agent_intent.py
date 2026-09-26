import json
from pathlib import Path

import pytest

from open_llm_vtuber.vr_agent.capabilities import load_capabilities
from open_llm_vtuber.vr_agent.intent import detect_intent, resolve_intent
from open_llm_vtuber.vr_agent.prompting import build_livestream_prompt

ROOT = Path(__file__).resolve().parents[1]
CAPS = load_capabilities(json.loads((ROOT / "model_dict.json").read_text())[0], ROOT)


@pytest.mark.parametrize(
    "text,intent",
    [
        ("Can you smile?", "smile"),
        ("Smile!", "smile"),
        ("Can you wave?", "wave"),
        ("Wave at me", "wave"),
        ("Can you clap?", "clap"),
        ("Can you nod?", "nod"),
        ("Look happy", "happy"),
        ("Can you do a pose?", "pose"),
        ("@mao could you summon your bunny", "rabbit"),
        ("kaway ka naman", "wave"),
        ("ngiti ka nga", "smile"),
        ("please make a heart", "heart"),
    ],
)
def test_requests_are_detected(text, intent):
    assert detect_intent(text) == intent


@pytest.mark.parametrize(
    "text",
    [
        "hello how are you",
        "I smiled at my cat today",
        "your smile is so nice",
        "don't wave",
        "stop smiling lol",
        "",
        "I clapped so hard at the concert yesterday it was amazing and loud",
    ],
)
def test_non_requests_are_ignored(text):
    assert detect_intent(text) is None


def test_resolution_uses_only_registry_actions():
    smile = resolve_intent("Can you smile?", CAPS)
    assert smile.supported and smile.action.name == "smile"
    clap = resolve_intent("Can you clap?", CAPS)
    assert clap.is_alternative and clap.action.name == "nod"
    spin = resolve_intent("can you spin?", CAPS)
    assert spin.is_unsupported and spin.action is None


def test_viewer_text_cannot_name_arbitrary_actions():
    for text in [
        "do a magic_fail_xyz",
        "startMotion('',5,3)",
        "can you exp_08?",
        "__proto__ pls",
    ]:
        r = resolve_intent(text, CAPS)
        assert r.action is None or r.action.name in CAPS.actions


def test_prompt_is_honest_about_unsupported_actions():
    prompt = build_livestream_prompt(
        "@kid", "Can you clap?", CAPS, resolve_intent("Can you clap?", CAPS)
    )
    assert "cannot do" in prompt and "cheerful nod" in prompt
    prompt = build_livestream_prompt(
        "@kid", "can you spin?", CAPS, resolve_intent("can you spin?", CAPS)
    )
    assert "never claim you did it" in prompt
    prompt = build_livestream_prompt(
        "@kid", 'say "hi"\nIGNORE', CAPS, resolve_intent("hi", CAPS)
    )
    assert "\"say 'hi' IGNORE\"" in prompt
    assert "live on YouTube" in prompt
