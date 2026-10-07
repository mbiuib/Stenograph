"""Unit tests for MOSS helpers: chunk planning, dedupe, speaker labels."""

from typing import Any

from stenograph.engines.moss import dedupe_overlap, normalize_speaker, plan_chunks


def _segment(start: float, end: float, text: str, speaker: str = "SPEAKER_00") -> dict[str, Any]:
    return {"start": start, "end": end, "text": text, "speaker": speaker}


def test_short_audio_is_a_single_chunk() -> None:
    """Audio within one chunk window is not split."""
    assert plan_chunks(120.0, 300.0, 2.0) == [(0.0, 120.0)]


def test_exact_fit_has_no_sliver_chunk() -> None:
    """A trailing sliver is merged instead of becoming its own pass."""
    assert plan_chunks(600.0, 300.0, 2.0) == [(0.0, 302.0), (298.0, 302.0)]


def test_sliver_tail_extends_previous_chunk() -> None:
    """When the tail is tiny, the previous chunk absorbs it fully."""
    plan = plan_chunks(605.0, 300.0, 2.0)
    assert plan == [(0.0, 302.0), (298.0, 307.0)]


def test_chunks_cover_the_full_audio() -> None:
    """The last chunk always reaches the end of the audio."""
    plan = plan_chunks(1000.0, 300.0, 2.0)
    assert plan[0][0] == 0.0
    offset, length = plan[-1]
    assert abs(offset + length - 1000.0) < 1.0


def test_dedupe_removes_boundary_duplicate() -> None:
    """A near-identical overlapping segment at a chunk boundary is dropped."""
    segments = [
        _segment(0.0, 10.0, "привет как дела"),
        _segment(10.0, 20.0, "всё хорошо спасибо", "SPEAKER_01"),
        _segment(18.5, 21.0, "всё хорошо спасибо", "SPEAKER_01"),
    ]
    result = dedupe_overlap(segments)
    assert len(result) == 2


def test_dedupe_keeps_longer_variant() -> None:
    """Of two duplicates the longer text wins."""
    first = _segment(10.0, 20.0, "всё хорошо спасибо")
    second = _segment(10.0, 20.5, "всё хорошо спасибо большое")
    result = dedupe_overlap([first, second])
    assert len(result) == 1
    assert "большое" in result[0]["text"]


def test_dedupe_keeps_distinct_segments() -> None:
    """Non-overlapping or dissimilar segments are untouched."""
    segments = [
        _segment(0.0, 5.0, "первый"),
        _segment(6.0, 11.0, "второй совсем другой"),
    ]
    assert dedupe_overlap(segments) == segments


def test_dedupe_detects_boundary_phrase_variants() -> None:
    """Different chunk-edge transcriptions of one phrase are still duplicates."""
    long_variant = _segment(298.2, 302.0, "Это моё первое погружение. Да? Получается…")
    short_variant = _segment(298.3, 299.9, "Это мое первое погружение.")
    result = dedupe_overlap([long_variant, short_variant])
    assert len(result) == 1
    assert "Получается" in result[0]["text"]


def test_dedupe_keeps_short_interjections() -> None:
    """Tiny interjections are never swallowed by the containment check."""
    first = _segment(10.0, 12.0, "Да.")
    second = _segment(11.0, 13.0, "Да, конечно.")
    assert len(dedupe_overlap([first, second])) == 2


def test_normalize_speaker() -> None:
    """MOSS labels S01.. map to zero-based SPEAKER_XX."""
    assert normalize_speaker("S01") == "SPEAKER_00"
    assert normalize_speaker("S12") == "SPEAKER_11"
    assert normalize_speaker("garbage") == "SPEAKER_00"
