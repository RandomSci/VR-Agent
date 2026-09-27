"""Local contextual dialogue: picking lines without an LLM, voicing them from
pre-rendered clips without a TTS request.

Exchanges are indexed by trigger ("banter", "region_enter", "after_owl" ...).
A pick keeps only exchanges whose conditions hold right now, that are off
cooldown and that were not heard recently, then chooses one by weight (more
specific lines are favoured). With hundreds of exchanges the work per pick is
a filter over one trigger's list, so it stays cheap as the library grows.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from . import conditions
from .content import AdventureLibrary, Exchange
from .state import AdventureState

# How many of the most recent exchanges are skipped when picking.
RECENT_WINDOW = 24


class DialoguePicker:
    def __init__(self, library: AdventureLibrary):
        self.library = library
        self.picks = 0
        self.misses = 0

    def eligible(
        self, trigger: str, ctx: conditions.Context, state: AdventureState
    ) -> list[Exchange]:
        pool = self.library.by_trigger.get(trigger, [])
        now = state.active_minutes
        recent = list(state.dialogue_history)[-min(RECENT_WINDOW, max(0, len(pool) - 1)) :]
        out = []
        for ex in pool:
            if ex.id in recent:
                continue
            last = state.dialogue_last.get(ex.id)
            if last is not None and now - last < ex.cooldown:
                continue
            if conditions.matches(ex.when, ctx):
                out.append(ex)
        return out

    def pick(
        self,
        trigger: str,
        ctx: conditions.Context,
        state: AdventureState,
        rng: random.Random,
    ) -> Optional[Exchange]:
        options = self.eligible(trigger, ctx, state)
        if not options:
            self.misses += 1
            return None
        weights = [
            max(0.01, ex.weight) * (1.0 + 0.6 * conditions.specificity(ex.when))
            for ex in options
        ]
        choice = rng.choices(options, weights=weights, k=1)[0]
        state.dialogue_last[choice.id] = state.active_minutes
        state.dialogue_history.append(choice.id)
        self.picks += 1
        return choice


def text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class VoiceClips:
    """Pre-rendered voice clips for local dialogue (made once with the free
    Edge voices by scripts/adventure/render_voices.py). A line without a
    matching clip is shown as a caption only, so a missing file never fails.
    """

    URL_BASE = "/vr-agent/voices/adventure/"

    def __init__(self, folder: Optional[Path | str]):
        self.folder = Path(folder) if folder else None
        self.clips: dict[str, dict[str, Any]] = {}
        self.missing = 0
        self.load()

    def load(self) -> None:
        self.clips = {}
        if not self.folder:
            return
        manifest = self.folder / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for line_id, entry in (data.get("lines") or {}).items():
                if isinstance(entry, dict) and entry.get("file"):
                    self.clips[str(line_id)] = entry
        except FileNotFoundError:
            logger.info("Adventure: no voice clip manifest, dialogue shows as captions")
        except Exception as exc:
            logger.warning(f"Adventure: voice manifest unreadable ({exc}); captions only")

    def get(self, line_id: str, text: str) -> Optional[dict[str, Any]]:
        entry = self.clips.get(line_id)
        if not entry or entry.get("key") != text_key(text):
            self.missing += 1
            return None
        name = str(entry["file"])
        if "/" in name or ".." in name or not self.folder or not (self.folder / name).is_file():
            self.missing += 1
            return None
        volumes = entry.get("volumes") or []
        return {
            "clip": self.URL_BASE + name,
            "duration": float(entry.get("duration") or 2.0),
            "volumes": [round(float(v), 3) for v in volumes[:2000]],
            "slice": int(entry.get("slice") or 40),
        }
