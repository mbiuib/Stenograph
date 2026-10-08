"""Session naming: default date/time suffixes for recordings."""

from __future__ import annotations

import datetime as dt
import re

from stenograph.naming import timestamped


def test_timestamped_appends_russian_date_and_time() -> None:
    """The suffix reads naturally in Russian and keeps sessions distinguishable."""
    assert timestamped("Live", dt.datetime(2026, 10, 8, 23, 41)) == "Live — 8 окт, 23:41"


def test_timestamped_uses_local_now_without_an_explicit_moment() -> None:
    """Without a moment the helper formats the current local time."""
    name = timestamped("Jitsi")
    assert re.fullmatch(r"Jitsi — \d{1,2} [а-я]{3}, \d{2}:\d{2}", name)
