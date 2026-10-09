"""Queue classes: files outrank improvements, auto-chained work runs last."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeEngine
from stenograph.config import Settings
from stenograph.domain.models import Job
from stenograph.events import EventBus
from stenograph.service import (
    PRIORITY_REPROCESS,
    PRIORITY_REPROCESS_AUTO,
    TranscriptionService,
    queue_priority,
)
from stenograph.storage import JobRepository


def _make_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TranscriptionService:
    """A service whose worker thread exits at once (picks are driven manually)."""
    monkeypatch.setattr(TranscriptionService, "_work_loop", lambda self: None)
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    return TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda name, prepared: FakeEngine()
    )


def test_queue_runs_files_before_improvements_and_autos_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pick order: fresh file → manual improvement → auto improvement.

    Falsification: with the old FIFO pick the first pick returns the auto
    job (enqueued first) — the «file first» assertion then fails.
    """
    service = _make_service(tmp_path, monkeypatch)
    auto = Job(kind="reprocess", source_name="авто-улучшение")
    auto.meta["auto"] = True
    manual = Job(kind="reprocess", source_name="ручное улучшение")
    file_job = Job(kind="file", source_name="свежий файл")
    for job in (auto, manual, file_job):
        service._enqueue(job)

    assert queue_priority(auto) > queue_priority(manual) > queue_priority(file_job)
    assert service._pick_queued() == file_job.id
    assert service._pick_queued() == manual.id
    assert service._pick_queued() == auto.id
    assert service._pick_queued() is None


def test_chain_reprocess_marks_the_child_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live-stop chain creates its child in the lowest queue class."""
    service = _make_service(tmp_path, monkeypatch)
    track = tmp_path / "mic.wav"
    track.write_bytes(b"RIFF0000WAVE")

    live = Job(kind="live", source_name="Live — тест")
    live.meta["audio"] = {"mic": str(track)}
    child = service.chain_reprocess(live)
    assert child.meta.get("auto") is True
    assert queue_priority(child) == PRIORITY_REPROCESS_AUTO

    manual_live = Job(kind="live", source_name="Live — ручной")
    manual_live.meta["audio"] = {"mic": str(track)}
    manual = service.reprocess_job(manual_live)
    assert manual.meta.get("auto") is None
    assert queue_priority(manual) == PRIORITY_REPROCESS
