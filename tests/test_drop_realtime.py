"""drop_realtime_on_finalize: завершение не дожимает рилтайм-хвост.

Настройка: когда встреча или запись завершается, недораспознанный хвост
субтитров/live-текста отбрасывается вместо дожимa через декодер. Записи на
диске остаются полными — качество (улучшение) распознает всё после.
"""

from __future__ import annotations

import time
from pathlib import Path

from fakes import PositionTranscriber, speech_audio
from stenograph.bridge.session import MeetingSession
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus
from stenograph.events import EventBus
from stenograph.live.manager import _LiveSession
from stenograph.storage import JobRepository


def _bridge_session(tmp_path: Path, *, drop: bool) -> MeetingSession:
    settings = Settings(
        data_dir=tmp_path,
        drop_realtime_on_finalize=drop,
        live_step_sec=0.4,
        live_max_window_sec=10.0,
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    return MeetingSession(
        "room-drop",
        settings,
        repo,
        EventBus(),
        lambda language: PositionTranscriber(),
    )


def test_bridge_drop_realtime_skips_the_final_flush(tmp_path: Path) -> None:
    """С включённой настройкой завершение не дожимает хвост через декодер."""
    session = _bridge_session(tmp_path / "drop", drop=True)
    try:
        session.feed("p1", "ru-RU", speech_audio(2.0))
        session.request_stop("manual")
        assert session.wait_finished(10.0), "сессия не финализировалась"
        assert session.job.status == JobStatus.DONE
        assert session.job.meta.get("realtime_dropped") is True
        assert session.job.segments == [], session.job.segments  # хвост отброшен
        # Запись на диске при этом полная — отбрасывается только realtime-текст.
        assert (tmp_path / "drop" / "jitsi" / session.job.id / "p1.wav").is_file()
    finally:
        session.stop()


def test_bridge_without_the_flag_still_flushes_the_tail(tmp_path: Path) -> None:
    """Без настройки поведение прежнее: хвост дожимается и попадает в текст."""
    session = _bridge_session(tmp_path / "keep", drop=False)
    try:
        session.feed("p1", "ru-RU", speech_audio(2.0))
        session.request_stop("manual")
        assert session.wait_finished(10.0), "сессия не финализировалась"
        assert session.job.status == JobStatus.DONE
        assert session.job.meta.get("realtime_dropped") is None
        assert session.job.segments, "без настройки хвост обязан распознаться"
    finally:
        session.stop()


def _live_session(tmp_path: Path, *, drop: bool) -> _LiveSession:
    settings = Settings(
        data_dir=tmp_path,
        drop_realtime_on_finalize=drop,
        live_step_sec=0.4,
        live_max_window_sec=10.0,
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    job = Job(
        kind="live",
        source_name="Live — тест отбрасывания",
        status=JobStatus.RUNNING,
        started_at=time.time(),
        language="ru",
    )
    repo.save(job)
    session = _LiveSession(
        job=job,
        settings=settings,
        repo=repo,
        bus=EventBus(),
        transcriber_factory=lambda language: PositionTranscriber(),
        capture_factory=None,
        tracks=("mic",),
        transcribe=True,
    )
    session.start()
    return session


def test_live_drop_realtime_skips_the_final_flush(tmp_path: Path) -> None:
    """Live: при завершении хвост отбрасывается, а не прогоняется через модель."""
    session = _live_session(tmp_path / "drop", drop=True)
    session.feed("mic", speech_audio(2.0))
    # Живой ход декодера (как у менеджера): фид и тик без коммита.
    session.serve(max_ticks=6, max_sec=2.0)
    session.feed("mic", speech_audio(3.0))
    session.request_stop()
    # Завершение: при drop — финализация без дожимания и без окна.
    assert session.serve(max_ticks=6, max_sec=2.0) is False
    assert session.finished
    assert session.job.status == JobStatus.DONE
    assert session.job.meta.get("realtime_dropped") is True
    assert session.job.segments == [], session.job.segments


def test_live_without_the_flag_still_flushes_the_tail(tmp_path: Path) -> None:
    """Live без настройки: хвост дожимается при финализации."""
    session = _live_session(tmp_path / "keep", drop=False)
    session.feed("mic", speech_audio(2.0))
    session.serve(max_ticks=6, max_sec=2.0)
    session.feed("mic", speech_audio(3.0))
    session.request_stop()
    assert session.serve(max_ticks=6, max_sec=2.0) is False
    assert session.finished
    assert session.job.meta.get("realtime_dropped") is None
    assert session.job.segments, "без настройки хвост обязан распознаться"
