"""Game Registry: which games are installed, built from each game's config.yaml.

A game is a subpackage of ``open_llm_vtuber.games`` with a ``config.yaml``
and a ``game.py`` exposing ``load(config, game_dir)`` that returns a
``GameFactory``. Anything that fails to load is listed as disabled with the
reason, and is never offered to viewers or characters. Counting and listing
games is answered from here, never by an LLM.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from loguru import logger

from .base import Game, GameInfo

GAMES_DIR = Path(__file__).resolve().parent
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


@dataclass
class GameFactory:
    info: GameInfo
    create: Callable[..., Game]  # create(rng=..., options=...) -> Game
    config: dict[str, Any] = field(default_factory=dict)


def normalise_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


class GameRegistry:
    def __init__(self, factories: Optional[dict[str, GameFactory]] = None):
        self._factories: dict[str, GameFactory] = dict(factories or {})
        self.problems: dict[str, str] = {}

    @classmethod
    def discover(cls, games_dir: Path = GAMES_DIR) -> "GameRegistry":
        registry = cls()
        for child in sorted(games_dir.iterdir()):
            if not child.is_dir() or not (child / "config.yaml").is_file():
                continue
            game_id = child.name
            if not _ID_RE.match(game_id):
                continue
            try:
                config = (
                    yaml.safe_load((child / "config.yaml").read_text(encoding="utf-8"))
                    or {}
                )
                if not isinstance(config, dict):
                    raise ValueError("config.yaml must be a mapping")
                module = importlib.import_module(f"{__package__}.{game_id}.game")
                factory = module.load(config, child)
                if factory.info.id != game_id:
                    raise ValueError(f"id '{factory.info.id}' does not match folder")
                registry._factories[game_id] = factory
                if not factory.info.enabled:
                    registry.problems[game_id] = "disabled in config.yaml"
            except Exception as exc:
                registry.problems[game_id] = str(exc)[:200]
                logger.error(f"Game '{game_id}' unavailable: {exc}")
        logger.info(
            f"Game registry: {[f.info.display_name for f in registry.enabled()]}"
            + (f" problems={registry.problems}" if registry.problems else "")
        )
        return registry

    # ------------------------------------------------------------------
    def enabled(self) -> list[GameFactory]:
        return [f for f in self._factories.values() if f.info.enabled]

    def get(self, game_id: Optional[str]) -> Optional[GameFactory]:
        factory = self._factories.get(game_id or "")
        return factory if factory and factory.info.enabled else None

    def default(self) -> Optional[GameFactory]:
        games = self.enabled()
        return games[0] if games else None

    def find(self, name: str) -> Optional[GameFactory]:
        """Match a spoken game name ("trivia", "trivia battle", "quiz")."""
        wanted = normalise_name(name)
        if not wanted:
            return None
        for factory in self.enabled():
            names = {
                normalise_name(factory.info.id),
                normalise_name(factory.info.display_name),
                *(normalise_name(a) for a in factory.info.aliases),
            }
            if wanted in names:
                return factory
        return None

    def names(self) -> list[str]:
        return [f.info.display_name for f in self.enabled()]

    def count(self) -> int:
        return len(self.enabled())

    def summary_sentence(self) -> str:
        """Grounded, deterministic answer to 'what games can you play?'."""
        names = self.names()
        if not names:
            return "We don't have any games installed right now."
        if len(names) == 1:
            return f"We can play {names[0]} right now. It's our only game so far."
        return f"We can play {', '.join(names[:-1])} or {names[-1]}."

    def count_sentence(self) -> str:
        n = self.count()
        if n == 0:
            return "We don't have any games yet."
        names = ", ".join(self.names())
        return f"We currently have {n} game{'s' if n != 1 else ''}: {names}."

    def describe(self) -> dict[str, Any]:
        return {
            "games": [f.info.describe() for f in self._factories.values()],
            "problems": dict(self.problems),
        }

    def prompt_summary(self) -> str:
        """Short line for character prompts when games come up."""
        names = self.names()
        if not names:
            return "No games are installed."
        return (
            "Installed games (the only ones you can play): "
            + ", ".join(names)
            + ". Any other game is not available yet."
        )
