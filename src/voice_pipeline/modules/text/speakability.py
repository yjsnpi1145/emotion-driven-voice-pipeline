from __future__ import annotations

import unicodedata

_PAUSE_SYMBOLS = frozenset("….。、，,!！?？—-~～")


def is_speakable_text(value: str) -> bool:
    """Return whether text contains something a speech engine can pronounce."""
    return any(unicodedata.category(character)[0] in {"L", "N"} for character in value)


def is_pause_marker(value: str) -> bool:
    """Return whether a non-empty value contains only supported pause symbols."""
    compact = "".join(character for character in value if not character.isspace())
    return bool(compact) and all(character in _PAUSE_SYMBOLS for character in compact)

