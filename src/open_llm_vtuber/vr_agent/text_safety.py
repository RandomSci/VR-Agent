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


# Emoji and pictographs, the joiners and selectors that glue them together,
# and the symbol blocks the models like to sprinkle in (stars, hearts, arrows).
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, flags, extended
    "\U00002600-\U000027bf"  # misc symbols and dingbats
    "\U00002b00-\U00002bff"  # stars, arrows
    "\U00002190-\U000021ff"  # arrows
    "\U00002300-\U000023ff"  # watches, hourglasses, play buttons
    "\U000025a0-\U000025ff"  # geometric shapes
    "\U00002700-\U000027bf"
    "\U0000fe0e\U0000fe0f\U0000200d\U000020e3"  # selectors, joiner, keycap
    "\U000e0020-\U000e007f"  # tag characters (subdivision flags)
    "©®™〰〽"
    "]+"
)
# Typed emoticons: :) :D ;-) <3 xD :P (not inside words or links).
_EMOTICON_RE = re.compile(
    r"(?<![\w/])(?:[:;=8][-'^]?[)(DPpOo3/\\|\]\[*]+|<3+|[xX]D+|\^_*\^|T_T|:3)(?![\w/])"
)


def strip_emoji(text: str) -> str:
    """Text with every emoji, pictograph symbol and typed emoticon removed.

    Used on everything that is spoken: TTS reads emoji names out loud
    ("smiling face with hearts"), which sounds broken on stream.
    """
    if not text:
        return ""
    text = _EMOJI_RE.sub(" ", str(text))
    text = _EMOTICON_RE.sub(" ", text)
    text = re.sub(r"[ \t]+([.,!?;:])", r"\1", text)
    return _WS_RE.sub(" ", text).strip()
