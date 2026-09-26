"""Sanitising viewer-provided text before it is displayed or prompted."""

from __future__ import annotations

import re
import unicodedata

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


def prompt_quote(text: str, max_chars: int = 280) -> str:
    """Viewer text safe to embed in a quoted prompt line."""
    return clean_viewer_text(text, max_chars).replace('"', "'")
