"""Session naming: default date/time suffixes for recordings."""

from __future__ import annotations

import datetime as dt
import re

from stenograph.naming import jitsi_name, live_name, room_label, timestamped


def test_timestamped_appends_russian_date_and_time() -> None:
    """The suffix reads naturally in Russian and keeps sessions distinguishable."""
    assert timestamped("Live", dt.datetime(2026, 10, 8, 23, 41)) == "Live — 8 окт, 23:41"


def test_timestamped_uses_local_now_without_an_explicit_moment() -> None:
    """Without a moment the helper formats the current local time."""
    name = timestamped("Jitsi")
    assert re.fullmatch(r"Jitsi — \d{1,2} [а-я]{3}, \d{2}:\d{2}", name)


def test_live_name_puts_the_device_and_title_around_the_stamp() -> None:
    """Type prefix first, device tag after it, the user's title last."""
    stamp = dt.datetime(2026, 10, 8, 23, 41)
    assert (
        live_name(device="Chrome · Windows", now=stamp)
        == "Live — Chrome · Windows — 8 окт, 23:41"
    )
    assert (
        live_name(device="Chrome · Windows", title="Планёрка", now=stamp)
        == "Live — Chrome · Windows — 8 окт, 23:41 — Планёрка"
    )
    assert live_name(title="Планёрка", now=stamp) == "Live — 8 окт, 23:41 — Планёрка"


def test_live_name_supports_the_server_prefix() -> None:
    """The server-side capture keeps its «Live (машина)» label."""
    stamp = dt.datetime(2026, 10, 8, 23, 41)
    assert live_name(prefix="Live (машина)", now=stamp) == "Live (машина) — 8 окт, 23:41"
    assert (
        live_name(prefix="Live (машина)", title="Вебинар", now=stamp)
        == "Live (машина) — 8 окт, 23:41 — Вебинар"
    )


def test_jitsi_name_appends_the_room() -> None:
    """The room goes last, like the Live title."""
    stamp = dt.datetime(2026, 10, 8, 23, 41)
    assert jitsi_name(room="stenograph-demo", now=stamp) == "Jitsi — 8 окт, 23:41 — stenograph-demo"
    assert jitsi_name(now=stamp) == "Jitsi — 8 окт, 23:41"


def test_room_label_decodes_and_caps() -> None:
    """Percent-encoded room names arrive readable; long ones get truncated."""
    assert room_label("%d0%b9%d1%86%d1%83") == "йцу"
    assert room_label("  a   b  ") == "a b"
    assert len(room_label("x" * 60)) == 40
    assert room_label(None) == ""
