"""Unit tests for the long-segment re-splitting helper."""

from typing import Any

from stenograph.engines.whisper import resplit_long_segments


def _words(start: float, count: int, step: float = 0.5) -> list[dict[str, Any]]:
    return [
        {"start": start + i * step, "end": start + (i + 1) * step, "word": f" w{i}"}
        for i in range(count)
    ]


def test_short_segment_passes_through() -> None:
    """Segments within the limit are returned unchanged."""
    seg = {"start": 0.0, "end": 10.0, "text": "привет", "words": _words(0.0, 5)}
    assert resplit_long_segments([seg]) == [seg]


def test_long_segment_is_split_at_word_boundaries() -> None:
    """A two-minute segment becomes several short, word-aligned pieces."""
    words = _words(0.0, 240, step=0.5)  # 120 seconds of speech
    seg = {"start": 0.0, "end": 120.0, "text": "…", "words": words}
    parts = resplit_long_segments([seg], max_sec=25.0)

    assert len(parts) > 1
    assert all(part["end"] - part["start"] <= 25.5 for part in parts)
    assert parts[0]["start"] == 0.0
    assert abs(parts[-1]["end"] - 120.0) < 1e-6
    # No words are lost or duplicated by the split.
    joined = "".join(p["text"].replace(" ", "") for p in parts)
    assert joined == "".join(w["word"].replace(" ", "") for w in words)


def test_segment_without_words_is_untouched() -> None:
    """Without word timestamps there is nothing to split on."""
    seg = {"start": 0.0, "end": 100.0, "text": "длинный", "words": []}
    assert resplit_long_segments([seg]) == [seg]
