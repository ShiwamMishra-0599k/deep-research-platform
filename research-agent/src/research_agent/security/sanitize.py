"""Helpers for treating externally retrieved content as untrusted data."""
from __future__ import annotations

import re

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f  ]")
_DELIM_RE = re.compile(r"</?\s*untrusted_content[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

OPEN_TAG = "<untrusted_content>"
CLOSE_TAG = "</untrusted_content>"


def field(value: object, max_len: int = 300) -> str:
    """Make a value safe for one field of a pipe-delimited structured line.

    Removes '|', CR/LF and other control characters so injected content can't
    forge extra fields or rows.
    """
    text = "" if value is None else str(value)
    text = text.replace("|", "/")
    text = _CONTROL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:max_len]


def strip_html(value: object, max_len: int = 300) -> str:
    text = "" if value is None else str(value)
    text = _TAG_RE.sub(" ", text)
    return field(text, max_len)


def wrap_untrusted(content: str) -> str:
    """Wrap tool output so the model can tell data from instructions.

    Any literal delimiter inside the content is neutralised first so the content
    cannot close the block early.
    """
    cleaned = _DELIM_RE.sub("[removed-delimiter]", content)
    return f"{OPEN_TAG}\n{cleaned}\n{CLOSE_TAG}"
