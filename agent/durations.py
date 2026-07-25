"""ISO 8601 duration parsing (§4). Minutes to weeks, which is the range the
holding policy must support. Deliberately strict: an unparseable duration is a
config error, never a silent default."""
from __future__ import annotations

import re
from datetime import timedelta

_PATTERN = re.compile(
    r"^P(?!$)(?:(?P<weeks>\d+(?:\.\d+)?)W)?"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?!$)(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


class DurationError(ValueError):
    pass


def parse_duration(text: str) -> timedelta:
    if not isinstance(text, str):
        raise DurationError(f"duration must be a string, got {type(text).__name__}")
    m = _PATTERN.match(text.strip())
    if not m:
        raise DurationError(
            f"{text!r} is not an ISO 8601 duration (expected e.g. PT15M, PT4H, P2D, P1W)"
        )
    parts = {k: float(v) for k, v in m.groupdict().items() if v is not None}
    if not parts:
        raise DurationError(f"{text!r} has no components")
    return timedelta(
        weeks=parts.get("weeks", 0.0),
        days=parts.get("days", 0.0),
        hours=parts.get("hours", 0.0),
        minutes=parts.get("minutes", 0.0),
        seconds=parts.get("seconds", 0.0),
    )


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total <= 0:
        return "PT0S"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    out = "P" + (f"{days}D" if days else "")
    time_part = "".join(
        p for p in (f"{hours}H" if hours else "", f"{minutes}M" if minutes else "",
                    f"{seconds}S" if seconds else "") if p
    )
    return out + (f"T{time_part}" if time_part else "")
