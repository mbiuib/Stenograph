"""Reprocess tests: live-recording improvement pass, merge, service and API."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.engines.base import AsrResult, TranscribeOptions, TranscribeProgress
from stenograph.events import EventBus
from stenograph.pipeline import run_reprocess_job
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


class DiarizedFakeEngine:
    """Engine double: labels every segment SPEAKER_00 and reports 10 s audio."""

    name = "fake-diar"

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.calls: list[str] = []
        self._gate = gate

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress=None,
        on_segment=None,
        is_cancelled=None,
    ) -> AsrResult:
        """Return two canned diarized segments per track."""
        if self._gate is not None:
            self._gate.wait(timeout=10)
        self.calls.append(Path(audio_path).name)
        segments = [
            Segment(
                index=0,
                start=0.0,
                end=2.0,
                text=f"текст {Path(audio_path).name}",
                speaker="SPEAKER_00",
            ),
            Segment(
                index=1,
                start=3.0,
                end=4.0,
                text=f"ещё {Path(audio_path).name}",
                speaker="SPEAKER_00",
            ),
        ]
        for segment in segments:
            if on_segment:
                on_segment(segment)
        if on_progress:
            on_progress(TranscribeProgress(fraction=1.0, message="готово"))
        return AsrResult(language="ru", language_probability=0.9, duration=10.0, segments=segments)


def _wav(path: Path) -> Path:
    """Create a placeholder track file (the fake engine ignores its content)."""
    path.write_bytes(b"RIFF....")
    return path


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    return settings


def _live_job_with_audio(tmp_path: Path) -> Job:
    job = Job(kind="live", source_name="Live-сессия", status=JobStatus.DONE)
    job.meta["audio"] = {
        "system": str(_wav(tmp_path / "system.wav")),
        "mic": str(_wav(tmp_path / "mic.wav")),
    }
    return job


def test_reprocess_merges_tracks(tmp_path: Path) -> None:
    """Pipeline: both tracks are transcribed, mic relabelled, segments merged."""
    settings = _settings(tmp_path)
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    job = _live_job_with_audio(tmp_path)
    job.meta["tracks"] = ["system", "mic"]
    job.meta["parent"] = "live123"
    repo.save(job)
    channel = bus.subscribe(job.id)

    run_reprocess_job(
        job,
        settings=settings,
        repo=repo,
        bus=bus,
        engine=DiarizedFakeEngine(),
        options=TranscribeOptions(),
        is_cancelled=lambda: False,
    )

    assert job.status == JobStatus.DONE, job.error
    assert len(job.segments) == 4
    assert [segment.index for segment in job.segments] == [0, 1, 2, 3]
    assert {s.speaker for s in job.segments if "system.wav" in s.text} == {"SPEAKER_00"}
    assert {s.speaker for s in job.segments if "mic.wav" in s.text} == {"Вы"}
    assert "SPEAKER_00: " in job.text and "Вы: " in job.text
    assert job.meta["parent"] == "live123"
    assert job.meta["duration"] == 10.0  # max of track durations
    assert job.meta["track_durations"] == {"system": 10.0, "mic": 10.0}
    starts = [segment.start for segment in job.segments]
    assert starts == sorted(starts)

    events = []
    while not channel.empty():
        events.append(channel.get_nowait())
    types = [event["type"] for event in events]
    assert "segments_replaced" in types and "done" in types


def test_reprocess_missing_track_fails(tmp_path: Path) -> None:
    """A recording whose track file vanished ends as an errored job."""
    settings = _settings(tmp_path)
    repo = JobRepository(settings.db_path)
    job = Job(kind="reprocess", status=JobStatus.QUEUED)
    job.meta["audio"] = {"system": str(tmp_path / "missing.wav")}

    run_reprocess_job(
        job,
        settings=settings,
        repo=repo,
        bus=EventBus(),
        engine=DiarizedFakeEngine(),
        options=TranscribeOptions(),
        is_cancelled=lambda: False,
    )

    assert job.status == JobStatus.ERROR
    assert "не найдена" in (job.error or "")


def _make_service(tmp_path: Path, gate: threading.Event | None = None):
    settings = _settings(tmp_path)
    repo = JobRepository(settings.db_path)
    engine = DiarizedFakeEngine(gate)
    service = TranscriptionService(settings, repo, EventBus(), engine_factory=lambda n, s: engine)
    return service, repo, engine


def test_service_reprocess_is_idempotent_while_running(tmp_path: Path) -> None:
    """A second request while the child is queued/running returns the same job."""
    gate = threading.Event()
    service, repo, engine = _make_service(tmp_path, gate)
    live = _live_job_with_audio(tmp_path)
    repo.save(live)

    first = service.reprocess_job(live)
    second = service.reprocess_job(live)
    assert second.id == first.id
    assert repo.get(live.id).meta["reprocess_job"] == first.id

    gate.set()
    deadline = time.monotonic() + 10
    child = repo.get(first.id)
    while time.monotonic() < deadline and child.status not in (JobStatus.DONE, JobStatus.ERROR):
        time.sleep(0.05)
        child = repo.get(first.id)

    assert child.status == JobStatus.DONE, child.error
    assert child.kind == "reprocess"
    assert child.meta["parent"] == live.id
    assert len(child.segments) == 4
    assert engine.calls == ["system.wav", "mic.wav"]


def test_service_reprocess_requires_audio(tmp_path: Path) -> None:
    """A live job without recorded tracks cannot be reprocessed."""
    service, repo, _ = _make_service(tmp_path)
    job = Job(kind="live", status=JobStatus.DONE)
    repo.save(job)
    with pytest.raises(ValueError):
        service.reprocess_job(job)


def test_api_reprocess_endpoint(tmp_path: Path) -> None:
    """POST /api/jobs/{id}/reprocess queues the improvement job (and 404/400 cases)."""
    gate = threading.Event()
    service, repo, _ = _make_service(tmp_path, gate)
    client = TestClient(create_app(settings=service.settings, service=service))

    live = _live_job_with_audio(tmp_path)
    repo.save(live)

    response = client.post(f"/api/jobs/{live.id}/reprocess")
    assert response.status_code == 201
    child_id = response.json()["id"]

    assert client.post("/api/jobs/missing/reprocess").status_code == 404
    plain = Job(kind="file", status=JobStatus.DONE)
    repo.save(plain)
    assert client.post(f"/api/jobs/{plain.id}/reprocess").status_code == 400

    gate.set()
    deadline = time.monotonic() + 10
    payload = client.get(f"/api/jobs/{child_id}").json()
    while time.monotonic() < deadline and payload["status"] not in ("done", "error"):
        time.sleep(0.05)
        payload = client.get(f"/api/jobs/{child_id}").json()

    assert payload["status"] == "done", payload
    assert payload["meta"]["parent"] == live.id
    assert len(payload["segments"]) == 4
