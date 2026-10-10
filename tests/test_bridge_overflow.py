"""Jitsi bridge overload guards: batched drain, per-round cap, backlog cap.

The ultra-load run (10 Oct) collapsed at 12 meetings: each decode round
drained the whole per-participant backlog frame by frame — each ``feed``
re-copied the entire window, so collection time grew with the backlog
(28 s of a 30 s round) until rounds never caught up. These tests pin the
three guards that bound it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest

from fakes import PositionTranscriber, speech_audio
from stenograph.bridge.session import MeetingSession
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.storage import JobRepository


class FeedSpy:
    """Tracker double recording every feed() call instead of buffering."""

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def feed(self, audio: np.ndarray) -> None:
        """Record one feed call for later assertions."""
        self.calls.append(np.asarray(audio))

    def tick(self) -> bool:
        """No-op inference step (the tests drive the drain directly)."""
        return False

    def flush(self) -> None:
        """No-op final commit."""
        return None

    def ready(self) -> bool:
        """Never ready — nothing is inferred in these tests."""
        return False


@pytest.fixture()
def session_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """MeetingSession with the pump thread neutralised (no background drain)."""
    monkeypatch.setattr(MeetingSession, "_pump_loop", lambda self: None)
    monkeypatch.setattr(MeetingSession, "_check_idle", lambda self: None)

    def build(**overrides) -> MeetingSession:
        settings = Settings(
            data_dir=tmp_path,
            live_step_sec=0.4,
            live_max_window_sec=10.0,
            **overrides,
        )
        settings.ensure_dirs()
        repo = JobRepository(settings.db_path)
        return MeetingSession(
            "room-overflow", settings, repo, EventBus(), lambda language: PositionTranscriber()
        )

    return build


def _seed(session: MeetingSession, participant_id: str, chunks: list[np.ndarray]) -> None:
    """Register a participant and queue raw chunks for it (bypassing feed())."""
    with session._lock:
        session._create_participant(participant_id, "ru")
        session._participants[participant_id].tracker = FeedSpy()
        session._pending[participant_id] = list(chunks)
        session._pending_samples[participant_id] = int(sum(chunk.size for chunk in chunks))


def _chunks(count: int, seconds: float = 0.2) -> list[np.ndarray]:
    return [np.full(int(seconds * 16000), 0.05, dtype=np.float32) for _ in range(count)]


def test_drain_merges_the_backlog_into_one_feed(session_factory) -> None:
    """The whole backlog goes to the tracker in ONE merged call.

    Per-frame feeding copied the window on every call — the quadratic cost
    that collapsed the decode round under load.
    """
    session = session_factory(bridge_drain_max_sec=0.0)  # unlimited
    chunks = _chunks(100)
    _seed(session, "p1", chunks)
    session._drain_pending("p1")
    spy: FeedSpy = session._participants["p1"].tracker
    assert len(spy.calls) == 1, f"ожидался один слитый feed, получили {len(spy.calls)}"
    assert spy.calls[0].size == sum(chunk.size for chunk in chunks)
    assert session._pending["p1"] == []
    assert session._pending_samples["p1"] == 0


def test_drain_takes_only_the_capped_amount_per_round(session_factory) -> None:
    """One round never drains more than ``bridge_drain_max_sec`` of a backlog."""
    session = session_factory(bridge_drain_max_sec=1.0)
    _seed(session, "p1", _chunks(10))  # 2.0 s queued
    session._drain_pending("p1")
    spy: FeedSpy = session._participants["p1"].tracker
    assert len(spy.calls) == 1
    assert spy.calls[0].size == int(1.0 * 16000)  # cap hit exactly
    assert session._pending_samples["p1"] == int(1.0 * 16000)  # остаток дождался

    session._drain_pending("p1")
    assert spy.calls[1].size == int(1.0 * 16000)
    assert session._pending_samples["p1"] == 0


def test_pending_backlog_is_capped_dropping_the_oldest(
    session_factory, caplog: pytest.LogCaptureFixture
) -> None:
    """Frames beyond ``bridge_pending_max_sec`` are dropped oldest-first."""
    session = session_factory(bridge_pending_max_sec=1.0)
    caplog.set_level(logging.WARNING, logger="stenograph.bridge.session")
    newest = speech_audio(0.4)
    with caplog.at_level(logging.WARNING, logger="stenograph.bridge.session"):
        for _ in range(8):  # 3.2 s pushed through the real intake path
            session.feed("p1", "ru", speech_audio(0.4))
        session.feed("p1", "ru", newest)

    queued = session._pending["p1"]
    total = session._pending_samples["p1"]
    assert total <= int(1.0 * 16000), "очередь обязана держаться в пределах кэпа"
    assert queued and queued[-1] is newest, "самый свежий кадр не должен выпадать"
    assert "переполнена" in caplog.text, "о дропе бэклога нужно предупреждать"
