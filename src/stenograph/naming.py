"""Human-friendly default names for recordings and sessions.

Live sessions used to share one identical name; a date/time suffix makes
them distinguishable in the job list at a glance (and they can be renamed
by hand afterwards).
"""

from __future__ import annotations

import datetime as dt

_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def timestamped(prefix: str, now: dt.datetime | None = None) -> str:
    """Return ``prefix — 8 окт, 23:41`` (local time; ``now`` for tests)."""
    stamp = now or dt.datetime.now()
    return f"{prefix} — {stamp.day} {_MONTHS[stamp.month - 1]}, {stamp:%H:%M}"
