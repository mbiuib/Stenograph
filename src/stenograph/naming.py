"""Human-friendly default names for recordings and sessions.

Live sessions used to share one identical name; a date/time suffix makes
them distinguishable in the job list at a glance (and they can be renamed
by hand afterwards). The type prefix always comes first; the device tag
(browser/OS), the Jitsi room or the user's own title go after the stamp.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import unquote

_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def timestamped(prefix: str, now: dt.datetime | None = None) -> str:
    """Return ``prefix — 8 окт, 23:41`` (local time; ``now`` for tests)."""
    stamp = now or dt.datetime.now()
    return f"{prefix} — {stamp.day} {_MONTHS[stamp.month - 1]}, {stamp:%H:%M}"


def room_label(meeting_id: str | None, limit: int = 40) -> str:
    """A readable Jitsi room label: percent-decoded, single line, capped."""
    text = " ".join(unquote(str(meeting_id or "")).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def live_name(
    *,
    device: str | None = None,
    title: str | None = None,
    prefix: str = "Live",
    now: dt.datetime | None = None,
) -> str:
    """``Live — Chrome · Windows — 8 окт, 23:41 — Планёрка`` (optional parts).

    The type prefix is always first (even when a custom title was given);
    the title goes last. ``prefix`` overrides the type label (the server-side
    capture uses «Live (машина)»).
    """
    base = f"{prefix} — {device}" if device else prefix
    name = timestamped(base, now)
    title = (title or "").strip()
    return f"{name} — {title}" if title else name


def jitsi_name(*, room: str | None = None, now: dt.datetime | None = None) -> str:
    """``Jitsi — 8 окт, 23:41 — stenograph-demo`` (room at the end)."""
    name = timestamped("Jitsi", now)
    label = room_label(room)
    return f"{name} — {label}" if label else name
