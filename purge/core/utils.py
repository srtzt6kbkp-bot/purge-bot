from __future__ import annotations

import re

_DURATION_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>[smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_duration(value: str | None) -> int | None:
    """Parse compact duration strings like 10m, 2h, 1d or combinations like 1h30m."""
    if value is None:
        return None

    text = value.strip().lower()
    if not text:
        return None

    if not re.fullmatch(r"(?:\d+\s*[smhdw]\s*)+", text):
        return None

    total = 0
    for match in _DURATION_RE.finditer(text):
        unit = match.group("unit").lower()
        seconds = int(match.group("value")) * _UNIT_SECONDS[unit]
        total += seconds

    return total or None


def parse_id(value: str | int | None) -> int | None:
    """Extract a Discord snowflake from an ID or a message URL."""
    if value is None:
        return None
    if isinstance(value, int):
        return value

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"(\d{10,20})", text)
    if match is None:
        return None
    return int(match.group(1))
