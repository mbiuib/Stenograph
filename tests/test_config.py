"""Settings helpers: language normalization for engines."""

from __future__ import annotations

from stenograph.config import clean_language


def test_clean_language_maps_auto_and_blank_to_none() -> None:
    """""/"auto" (any case, surrounding spaces) mean auto-detect — None."""
    assert clean_language(None) is None
    assert clean_language("") is None
    assert clean_language("   ") is None
    assert clean_language("auto") is None
    assert clean_language("AUTO") is None
    assert clean_language(" auto ") is None


def test_clean_language_keeps_real_codes() -> None:
    """Explicit codes survive, trimmed."""
    assert clean_language("ru") == "ru"
    assert clean_language(" en ") == "en"
    assert clean_language("ru-RU") == "ru-RU"
