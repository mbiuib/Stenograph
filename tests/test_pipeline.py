"""Pipeline tests driven by a fake engine (no GPU, no ffmpeg)."""

from pathlib import Path

import pytest

from fakes import FakeEngine
from stenograph.config import Settings
from stenograph.domain.errors import JobCancelled
from stenograph.domain.models import Job, JobStatus
from stenograph.engines.base import TranscribeOptions
from stenograph.events import EventBus
from stenograph.pipeline import run_file_job
from stenograph.storage import JobRepository


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    return settings


def test_pipeline_replaces_storage_name_in_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read error names the user's file, not the uuid copy on disk."""
    settings = _settings(tmp_path)
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    source = tmp_path / "85c7e83a5d31445bbfdc7f9da29fb695.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    job = Job(source_name="Стандартная запись 3.pdf", source_path=str(source))
    repo.save(job)

    def fake_extract(src: Path, dst: Path, st: Settings) -> None:
        raise ValueError(
            f"не удалось прочитать файл «{Path(src).name}»: это не аудио/видео или он повреждён"
        )

    monkeypatch.setattr("stenograph.pipeline.extract_audio", fake_extract)

    run_file_job(
        job,
        settings=settings,
        repo=repo,
        bus=bus,
        engine=FakeEngine(),
        options=TranscribeOptions(),
        is_cancelled=lambda: False,
    )

    assert job.status == JobStatus.ERROR
    assert "Стандартная запись 3.pdf" in (job.error or "")
    assert "85c7e83a" not in (job.error or "")


def test_pipeline_runs_to_completion(tmp_path: Path) -> None:
    """A successful run ends with DONE, stored segments and a done event."""
    settings = _settings(tmp_path)
    repo = JobRepository(settings.db_path)
    bus = EventBus()

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\x00" * 32)

    job = Job(source_name="clip.wav", source_path=str(audio))
    repo.save(job)
    channel = bus.subscribe(job.id)

    run_file_job(
        job,
        settings=settings,
        repo=repo,
        bus=bus,
        engine=FakeEngine(),
        options=TranscribeOptions(),
        is_cancelled=lambda: False,
    )

    assert job.status == JobStatus.DONE
    assert job.text == "раз\nдва"
    assert job.language == "ru"
    assert job.progress == 100

    events = []
    while not channel.empty():
        events.append(channel.get())
    assert any(e["type"] == "done" for e in events)
    assert any(e["type"] == "segment" for e in events)

    stored = repo.get(job.id)
    assert stored is not None and stored.status == JobStatus.DONE
    assert len(stored.segments) == 2


def test_pipeline_cancellation(tmp_path: Path) -> None:
    """An engine that raises JobCancelled ends the job as CANCELLED."""
    settings = _settings(tmp_path)
    repo = JobRepository(settings.db_path)
    bus = EventBus()

    class CancellingEngine(FakeEngine):
        def transcribe(
            self, audio_path, options, *, on_progress=None, on_segment=None, is_cancelled=None
        ):
            raise JobCancelled()

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\x00" * 32)
    job = Job(source_name="clip.wav", source_path=str(audio))

    run_file_job(
        job,
        settings=settings,
        repo=repo,
        bus=bus,
        engine=CancellingEngine(),
        options=TranscribeOptions(),
        is_cancelled=lambda: True,
    )
    assert job.status == JobStatus.CANCELLED
