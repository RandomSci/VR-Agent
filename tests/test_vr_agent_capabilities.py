import json
import shutil
from pathlib import Path

from open_llm_vtuber.vr_agent.capabilities import load_capabilities

ROOT = Path(__file__).resolve().parents[1]


def mao_info():
    return json.loads((ROOT / "model_dict.json").read_text(encoding="utf-8"))[0]


def test_mao_registry_matches_real_model_files():
    caps = load_capabilities(mao_info(), ROOT)
    assert caps.annotated
    assert caps.motion_groups == {"Idle": 1, "": 6}
    assert caps.expression_names == [f"exp_0{i}" for i in range(1, 9)]
    nod = caps.get("nod")
    assert (nod.motion_group, nod.motion_index) == ("", 0)
    assert abs(nod.duration_seconds - 3.47) < 0.01
    assert caps.get("smile").expression_index == 1
    # Nothing the model lacks is advertised.
    for missing in ("clap", "wave", "dance", "wink"):
        assert caps.get(missing) is None
        assert caps.action_for_intent(missing) is None
    assert caps.alternative_for_intent("wave").name == "hat_tip"
    assert "summon a rabbit" in caps.prompt_summary()
    idle = {a.name for a in caps.idle_actions()}
    assert "magic_fail" not in idle and "nod" in idle


def test_entries_pointing_at_missing_files_are_dropped(tmp_path):
    model_dir = tmp_path / "live2d-models" / "mao_pro"
    shutil.copytree(
        ROOT / "live2d-models" / "mao_pro" / "runtime",
        model_dir / "runtime",
        ignore=shutil.ignore_patterns("*.png", "*.moc3"),
    )
    spec = json.loads(
        (ROOT / "live2d-models/mao_pro/vr_agent_actions.json").read_text()
    )
    spec["actions"]["clap"] = {"type": "motion", "motion": "motions/clap.motion3.json"}
    spec["actions"]["wink"] = {"type": "expression", "expression": "exp_99"}
    spec["actions"]["Bad Name!"] = {"type": "expression", "expression": "exp_01"}
    spec["alternatives"]["hug"] = "clap"
    (model_dir / "vr_agent_actions.json").write_text(json.dumps(spec))
    caps = load_capabilities(mao_info(), tmp_path)
    assert caps.get("clap") is None
    assert caps.get("wink") is None
    assert "Bad Name!" not in caps.actions
    assert "hug" not in caps.alternatives
    assert caps.get("nod") is not None


def test_unannotated_model_only_gets_unnamed_idle_motions(tmp_path):
    model_dir = tmp_path / "live2d-models" / "shizuku"
    shutil.copytree(
        ROOT / "live2d-models" / "shizuku" / "runtime",
        model_dir / "runtime",
        ignore=shutil.ignore_patterns("*.png", "*.moc3"),
    )
    info = {
        "name": "shizuku",
        "url": "/live2d-models/shizuku/runtime/shizuku.model3.json",
        "idleMotionGroupName": "Idle",
        "emotionMap": {},
    }
    caps = load_capabilities(info, tmp_path)
    assert not caps.annotated
    assert caps.viewer_actions() == []
    assert len(caps.idle_actions()) == 3  # FlickUp, Tap, Flick3; Idle group excluded
    assert caps.prompt_summary().startswith("You cannot")


def test_model_path_outside_project_is_refused(tmp_path):
    caps = load_capabilities({"name": "x", "url": "/../../etc/passwd"}, tmp_path)
    assert caps.actions == {}
