"""StreamTracker unit tests: local agreement, window trimming, silence gates."""

from __future__ import annotations

from fakes import ScriptedTranscriber, silence_audio, speech_audio
from stenograph.live.streamer import StreamTracker


def _make_tracker(scripts, **kwargs):
    finals: list[tuple[float, float, str]] = []
    partials: list[str] = []
    transcriber = ScriptedTranscriber(scripts)
    tracker = StreamTracker(
        transcriber,
        on_final=lambda start, end, text: finals.append((start, end, text)),
        on_partial=partials.append,
        step_sec=0.5,
        **kwargs,
    )
    return tracker, transcriber, finals, partials


def test_commit_after_two_runs_agree() -> None:
    """A word is committed only when two consecutive hypotheses agree on it."""
    tracker, _, finals, partials = _make_tracker(
        [
            [(0.0, 0.4, "привет")],
            [(0.0, 0.4, "привет"), (0.5, 0.9, "мир")],
        ]
    )
    tracker.feed(speech_audio(1.0))
    assert tracker.tick() is True
    assert finals == []  # first hypothesis: nothing is stable yet
    assert partials[-1] == "привет"

    tracker.feed(speech_audio(1.0))
    assert tracker.tick() is True
    assert finals == [(0.0, 0.4, "привет")]
    assert partials[-1] == "мир"


def test_rebased_offsets_after_commit() -> None:
    """Committed audio is dropped and the tail keeps correct session times."""
    tracker, _, finals, _ = _make_tracker(
        [
            [(0.0, 0.4, "раз")],
            [(0.0, 0.4, "раз"), (0.6, 1.0, "два")],
            [(0.1, 0.5, "два"), (0.7, 1.1, "три")],
        ]
    )
    tracker.feed(speech_audio(1.0))
    tracker.tick()
    tracker.feed(speech_audio(1.0))
    tracker.tick()
    assert finals == [(0.0, 0.4, "раз")]

    tracker.feed(speech_audio(1.0))
    tracker.tick()
    # "два" was committed in the rebased window: 0.4 (cut) + 0.1..0.5
    assert finals[-1] == (0.5, 0.9, "два")


def test_step_gating_skips_small_feeds() -> None:
    """No inference runs until enough new audio has accumulated."""
    tracker, transcriber, _, _ = _make_tracker([])
    tracker.feed(speech_audio(0.2))
    assert tracker.tick() is False
    assert transcriber.windows == []


def test_flush_commits_the_tail() -> None:
    """flush() transcribes whatever remains and clears the window."""
    tracker, _, finals, partials = _make_tracker([[(0.0, 0.5, "конец")]])
    tracker.feed(speech_audio(1.0))
    tracker.flush()
    assert finals == [(0.0, 0.5, "конец")]
    assert tracker.buffer_seconds == 0.0
    assert partials[-1] == ""


def test_silence_is_skipped_and_long_silence_dropped() -> None:
    """Silence never reaches the engine; long silence is trimmed to a tail."""
    tracker, transcriber, _, _ = _make_tracker([])
    tracker.feed(silence_audio(3.0))
    assert tracker.tick() is False
    assert transcriber.windows == []
    assert tracker.buffer_seconds <= 0.6  # dropped down to silence_keep_sec


def test_short_silence_is_kept_for_context() -> None:
    """Silence below the drop threshold stays buffered (no inference)."""
    tracker, transcriber, _, _ = _make_tracker([])
    tracker.feed(silence_audio(1.0))
    tracker.tick()
    assert transcriber.windows == []
    assert tracker.buffer_seconds > 0.9


def test_max_window_forces_commit() -> None:
    """A window that never stabilizes is force-committed at max_window_sec."""
    tracker, _, finals, partials = _make_tracker(
        [
            [(0.1, 0.5, "альфа")],
            [(0.1, 0.5, "бета")],
            [(0.1, 0.5, "альфа")],
        ],
        max_window_sec=3.0,
    )
    for _ in range(3):
        tracker.feed(speech_audio(1.0))
        tracker.tick()

    assert finals == [(0.1, 0.5, "альфа")]  # the last hypothesis was committed
    assert tracker.buffer_seconds == 0.0
    assert partials[-1] == ""


def test_flush_on_silence_emits_nothing() -> None:
    """flush() over pure silence must not call the engine."""
    tracker, transcriber, finals, partials = _make_tracker([])
    tracker.feed(silence_audio(1.0))
    tracker.flush()
    assert transcriber.windows == []
    assert finals == []
    assert partials[-1] == ""
