"""Character capability registry built from what the Live2D model really has.

The registry answers two questions for the rest of VR Agent:

* Which physical actions can the current character actually perform?
* How does a named action map onto a concrete Live2D motion or expression?

Everything starts from the model's ``.model3.json``. Semantic names such as
"nod" or "smile" come from an optional ``vr_agent_actions.json`` file stored
next to the model. Each annotated entry is validated against the model file:
an entry that points at a motion file or expression that the model does not
define is dropped with a warning, so the character never advertises an action
it cannot do. Viewer text never reaches this module directly; callers only ask
for actions by registry name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger

ACTIONS_FILE_NAME = "vr_agent_actions.json"
_ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class CharacterAction:
    """One allowlisted action the character can perform."""

    name: str
    kind: str  # "motion" or "expression"
    label: str
    description: str
    intents: tuple[str, ...] = ()
    motion_group: Optional[str] = None
    motion_index: Optional[int] = None
    motion_file: Optional[str] = None
    duration_seconds: float = 0.0
    expression_name: Optional[str] = None
    expression_index: Optional[int] = None
    hold_seconds: float = 0.0
    idle: bool = False
    idle_weight: float = 0.0
    viewer_requestable: bool = True

    def to_frontend(self) -> dict[str, Any]:
        """Payload the frontend needs to execute this action. No free text."""
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "idle": self.idle,
            "idle_weight": self.idle_weight,
        }
        if self.kind == "motion":
            payload.update(
                {
                    "group": self.motion_group,
                    "index": self.motion_index,
                    "duration": round(self.duration_seconds, 3),
                }
            )
        else:
            payload.update(
                {
                    "expression": self.expression_name,
                    "expression_index": self.expression_index,
                    "hold": self.hold_seconds,
                }
            )
        return payload


@dataclass
class CharacterCapabilities:
    """Registry of the current model's supported actions."""

    model_name: str
    actions: dict[str, CharacterAction] = field(default_factory=dict)
    alternatives: dict[str, str] = field(default_factory=dict)
    motion_groups: dict[str, int] = field(default_factory=dict)
    expression_names: list[str] = field(default_factory=list)
    idle_group: Optional[str] = None
    large_motion_actions: tuple[str, ...] = ()
    large_motion_min_gap_seconds: float = 90.0
    annotated: bool = False

    # ----- lookups -------------------------------------------------------
    def get(self, name: str) -> Optional[CharacterAction]:
        return self.actions.get(name)

    def action_for_intent(self, intent: str) -> Optional[CharacterAction]:
        for action in self.actions.values():
            if action.viewer_requestable and intent in action.intents:
                return action
        return None

    def alternative_for_intent(self, intent: str) -> Optional[CharacterAction]:
        alt_name = self.alternatives.get(intent)
        if not alt_name:
            return None
        return self.actions.get(alt_name)

    def viewer_actions(self) -> list[CharacterAction]:
        return [a for a in self.actions.values() if a.viewer_requestable]

    def idle_actions(self) -> list[CharacterAction]:
        return [a for a in self.actions.values() if a.idle and a.kind == "motion"]

    # ----- serialisation -------------------------------------------------
    def prompt_summary(self) -> str:
        """Short list of physical actions for the conversational agent."""
        labels = [a.label for a in self.viewer_actions()]
        if not labels:
            return "You cannot perform any special on-stream gestures right now."
        return "; ".join(labels)

    def to_frontend(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "idle_group": self.idle_group,
            "large_motion_actions": list(self.large_motion_actions),
            "large_motion_min_gap_seconds": self.large_motion_min_gap_seconds,
            "actions": {n: a.to_frontend() for n, a in self.actions.items()},
        }

    def describe(self) -> dict[str, Any]:
        """Developer-facing description (logs and the status route)."""
        return {
            "model": self.model_name,
            "annotated": self.annotated,
            "motion_groups": self.motion_groups,
            "expressions": self.expression_names,
            "viewer_actions": {
                a.name: {"kind": a.kind, "label": a.label, "intents": list(a.intents)}
                for a in self.viewer_actions()
            },
            "idle_actions": [a.name for a in self.idle_actions()],
            "alternatives": self.alternatives,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return json.loads(path.read_text(encoding=encoding))
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"Could not decode {path}")


def _resolve_model3_path(
    model_info: dict[str, Any], project_root: Path
) -> Optional[Path]:
    url = str(model_info.get("url") or "")
    if not url or url.startswith("http"):
        return None
    candidate = (project_root / url.lstrip("/")).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        logger.warning(f"Refusing model path outside project: {url}")
        return None
    return candidate if candidate.is_file() else None


def _find_actions_file(model3_path: Path, project_root: Path) -> Optional[Path]:
    models_root = (project_root / "live2d-models").resolve()
    current = model3_path.parent
    while True:
        candidate = current / ACTIONS_FILE_NAME
        if candidate.is_file():
            return candidate
        if current == models_root or current.parent == current:
            return None
        try:
            current.relative_to(models_root)
        except ValueError:
            return None
        current = current.parent


def _motion_duration(path: Path) -> float:
    try:
        meta = _read_json(path).get("Meta", {})
        return float(meta.get("Duration") or 0.0)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not read motion duration from {path}: {exc}")
        return 0.0


def load_capabilities(
    model_info: dict[str, Any], project_root: Path | str = "."
) -> CharacterCapabilities:
    """Build the registry for a model described by a ``model_dict.json`` entry."""
    project_root = Path(project_root)
    model_name = str(model_info.get("name") or "unknown")
    caps = CharacterCapabilities(model_name=model_name)

    model3_path = _resolve_model3_path(model_info, project_root)
    if not model3_path:
        logger.warning(
            f"VR Agent: could not locate model3.json for '{model_name}'; no actions available."
        )
        return caps

    model3 = _read_json(model3_path)
    refs = model3.get("FileReferences", {})
    motions: dict[str, list[dict[str, Any]]] = refs.get("Motions", {}) or {}
    expressions: list[dict[str, Any]] = refs.get("Expressions", []) or []

    caps.motion_groups = {group: len(items) for group, items in motions.items()}
    caps.expression_names = [str(e.get("Name")) for e in expressions]
    idle_group = model_info.get("idleMotionGroupName") or "Idle"
    caps.idle_group = idle_group if idle_group in motions else None

    # file path -> (group, index)
    motion_lookup: dict[str, tuple[str, int]] = {}
    for group, items in motions.items():
        for index, item in enumerate(items):
            file_ref = str(item.get("File", "")).replace("\\", "/")
            motion_lookup[file_ref] = (group, index)

    actions_path = _find_actions_file(model3_path, project_root)
    if not actions_path:
        _add_generic_idle_motions(caps, motions, model3_path)
        logger.info(
            f"VR Agent: no {ACTIONS_FILE_NAME} for '{model_name}'. "
            f"Using {len(caps.actions)} unnamed idle motions; no viewer actions advertised."
        )
        return caps

    spec = _read_json(actions_path)
    caps.annotated = True
    for name, entry in (spec.get("actions") or {}).items():
        action = _build_action(
            name, entry, motion_lookup, caps.expression_names, model3_path
        )
        if action:
            caps.actions[name] = action

    for intent, target in (spec.get("alternatives") or {}).items():
        if target in caps.actions:
            caps.alternatives[str(intent)] = target
        else:
            logger.warning(
                f"VR Agent: alternative '{intent}' -> '{target}' ignored (unknown action)"
            )

    idle_spec = spec.get("idle") or {}
    caps.large_motion_actions = tuple(
        a for a in idle_spec.get("large_motion_actions", []) if a in caps.actions
    )
    caps.large_motion_min_gap_seconds = float(
        idle_spec.get("large_motion_min_gap_seconds", caps.large_motion_min_gap_seconds)
    )

    logger.info(
        f"VR Agent capabilities for '{model_name}': "
        f"viewer actions={[a.name for a in caps.viewer_actions()]}, "
        f"idle motions={[a.name for a in caps.idle_actions()]}"
    )
    return caps


def _build_action(
    name: str,
    entry: dict[str, Any],
    motion_lookup: dict[str, tuple[str, int]],
    expression_names: list[str],
    model3_path: Path,
) -> Optional[CharacterAction]:
    if not _ACTION_NAME_RE.match(name):
        logger.warning(f"VR Agent: invalid action name '{name}' skipped")
        return None
    kind = entry.get("type")
    common = dict(
        name=name,
        kind=kind,
        label=str(entry.get("label") or name.replace("_", " ")),
        description=str(entry.get("description") or ""),
        intents=tuple(str(i) for i in entry.get("intents", [])),
        viewer_requestable=bool(entry.get("viewer_requestable", True)),
    )
    if kind == "motion":
        file_ref = str(entry.get("motion", "")).replace("\\", "/")
        located = motion_lookup.get(file_ref)
        if not located:
            logger.warning(
                f"VR Agent: action '{name}' references motion '{file_ref}' "
                "which the model does not define. Skipped."
            )
            return None
        group, index = located
        return CharacterAction(
            **common,
            motion_group=group,
            motion_index=index,
            motion_file=file_ref,
            duration_seconds=_motion_duration(model3_path.parent / file_ref),
            idle=bool(entry.get("idle", False)),
            idle_weight=float(
                entry.get("idle_weight", 1.0 if entry.get("idle") else 0.0)
            ),
        )
    if kind == "expression":
        expr = str(entry.get("expression", ""))
        if expr not in expression_names:
            logger.warning(
                f"VR Agent: action '{name}' references expression '{expr}' "
                "which the model does not define. Skipped."
            )
            return None
        return CharacterAction(
            **common,
            expression_name=expr,
            expression_index=expression_names.index(expr),
            hold_seconds=float(entry.get("hold_seconds", 4.0)),
        )
    logger.warning(f"VR Agent: action '{name}' has unknown type '{kind}'. Skipped.")
    return None


def _add_generic_idle_motions(
    caps: CharacterCapabilities,
    motions: dict[str, list[dict[str, Any]]],
    model3_path: Path,
) -> None:
    """Without annotations we only know motions exist, not what they mean.

    They are still safe to play as idle movement, but they are never offered
    to viewers or to the conversational agent because they have no meaning.
    """
    for group, items in motions.items():
        if group == caps.idle_group:
            continue
        for index, item in enumerate(items):
            safe_group = re.sub(r"[^a-z0-9]", "", group.lower()) or "default"
            name = f"motion_{safe_group}_{index}"[:32]
            file_ref = str(item.get("File", "")).replace("\\", "/")
            caps.actions[name] = CharacterAction(
                name=name,
                kind="motion",
                label=name,
                description="",
                motion_group=group,
                motion_index=index,
                motion_file=file_ref,
                duration_seconds=_motion_duration(model3_path.parent / file_ref),
                idle=True,
                idle_weight=1.0,
                viewer_requestable=False,
            )
