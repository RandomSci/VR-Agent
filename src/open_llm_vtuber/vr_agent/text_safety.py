"""Sanitising viewer-provided text before it is displayed or prompted."""

from __future__ import annotations

import re
import unicodedata

# The longest viewer comment VR Agent supports end to end (reader, buffer,
# router, games, prompts, comment card). Longer comments are trimmed to this
# once, at ingestion, and logged; nothing downstream cuts them shorter.
VIEWER_TEXT_MAX = 100

_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]")
_WS_RE = re.compile(r"\s+")


def clean_viewer_text(text: str, max_chars: int) -> str:
    """Strip control and bidi-override characters, collapse whitespace, cap length.

    The frontend renders the result with textContent only, never innerHTML, so
    this is about tidy display and prompt hygiene rather than HTML escaping.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = _WS_RE.sub(" ", text)  # newlines/tabs become spaces first
    text = _CONTROL_RE.sub("", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


_REPEAT_RE = re.compile(r"(.{1,3}?)\1{5,}")


def is_garbage(text: str) -> bool:
    """Repeated-character noise like "WWWWWWWWWW" or "!!!!!!!!!!!!".

    Short normal messages ("5", "ok", "rock") are never garbage; only long
    runs where one character (or a tiny pattern) makes up most of the text.
    """
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if len(compact) < 8:
        return False
    most = max(compact.count(ch) for ch in set(compact))
    if most / len(compact) >= 0.6:
        return True
    match = _REPEAT_RE.search(compact)
    return bool(match and len(match.group(0)) / len(compact) >= 0.8)


def trim_viewer_text(text: str) -> tuple[str, bool]:
    """Cap a comment at VIEWER_TEXT_MAX on a word boundary. (text, trimmed)."""
    text = str(text or "").strip()
    if len(text) <= VIEWER_TEXT_MAX:
        return text, False
    cut = text[: VIEWER_TEXT_MAX - 1]
    space = cut.rfind(" ")
    if space >= VIEWER_TEXT_MAX * 0.7:
        cut = cut[:space]
    return cut.rstrip() + "…", True


def prompt_quote(text: str, max_chars: int = 280) -> str:
    """Viewer text safe to embed in a quoted prompt line."""
    return clean_viewer_text(text, max_chars).replace('"', "'")
