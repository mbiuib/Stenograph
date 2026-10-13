"""Список задач: постраничное дерево корней (курсор) и слим-пейлоад."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fakes import FakeEngine
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.events import EventBus
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository


def _service(tmp_path: Path) -> TranscriptionService:
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    return TranscriptionService(
        settings,
        JobRepository(settings.db_path),
        EventBus(),
        engine_factory=lambda name, settings: FakeEngine(),
    )


def _seed_tree(repo: JobRepository) -> None:
    """Три корня с детьми; у r1 ребёнок создан позже всех (дерево на разрыв)."""

    def save(
        job_id: str,
        kind: str,
        status: JobStatus,
        created: float,
        meta: dict,
        name: str,
    ) -> None:
        repo.save(
            Job(
                id=job_id,
                kind=kind,
                source_name=name,
                status=status,
                created_at=created,
                meta=meta,
            )
        )

    save("r1", "file", JobStatus.DONE, 100.0, {}, "Встреча Альфа")
    save("c1", "reprocess", JobStatus.DONE, 400.0, {"parent": "r1"}, "Улучшение Альфы")
    save("r2", "jitsi", JobStatus.DONE, 200.0, {}, "Встреча Бета")
    save(
        "c2",
        "analysis",
        JobStatus.DONE,
        210.0,
        {"parent": "r2", "analysis_type": "summary"},
        "Резюме Беты",
    )
    save("r3", "file", JobStatus.DONE, 300.0, {}, "Встреча Гамма")
    save("c3", "reprocess", JobStatus.ERROR, 305.0, {"parent": "r3"}, "Улучшение Гаммы")


def test_tree_pages_roots_with_their_children(tmp_path: Path) -> None:
    """Страница несёт 2 корня с детьми; курсор отдаёт следующих; без дублей."""
    service = _service(tmp_path)
    _seed_tree(service.repo)
    client = TestClient(create_app(settings=service.settings, service=service))

    page1 = client.get("/api/jobs/tree?limit=2").json()
    page1_ids = {job["id"] for job in page1["jobs"]}
    assert page1_ids == {"r3", "c3", "r2", "c2"}
    assert page1["has_more"] is True
    assert page1["next_before"]

    page2 = client.get(f"/api/jobs/tree?limit=2&before={page1['next_before']}").json()
    page2_ids = {job["id"] for job in page2["jobs"]}
    assert page2_ids == {"r1", "c1"}  # ребёнок r1 приезжает со своим корнем
    assert page2["has_more"] is False
    assert page1_ids.isdisjoint(page2_ids)


def test_tree_children_obey_status_and_kind_filters(tmp_path: Path) -> None:
    """Фильтры статуса/типа/поиска применяются и к корням, и к детям."""
    service = _service(tmp_path)
    _seed_tree(service.repo)
    client = TestClient(create_app(settings=service.settings, service=service))

    done = client.get("/api/jobs/tree?limit=10&status=done").json()
    assert {job["id"] for job in done["jobs"]} == {"r1", "c1", "r2", "c2", "r3"}

    jitsi = client.get("/api/jobs/tree?limit=10&kind=jitsi").json()
    assert {job["id"] for job in jitsi["jobs"]} == {"r2"}  # дети другого типа не примешиваются

    reprocess = client.get("/api/jobs/tree?limit=10&kind=reprocess").json()
    assert {job["id"] for job in reprocess["jobs"]} == {"c1", "c3"}  # у них есть родители

    search = client.get("/api/jobs/tree?limit=10&q=Бет").json()
    assert {job["id"] for job in search["jobs"]} == {"r2", "c2"}

    child_only = client.get("/api/jobs/tree?limit=10&q=Беты").json()
    assert {job["id"] for job in child_only["jobs"]} == {"c2"}  # совпавшая подзадача видна


def test_list_is_slim_but_detail_keeps_the_transcript(tmp_path: Path) -> None:
    """Список не несёт транскрипт и тяжёлые мета; detail — несёт."""
    service = _service(tmp_path)
    heavy_meta = {
        "engine": "moss",
        "duration": 12.5,
        "audio": {"Спикер 1": "data/jitsi/x/u01p01.wav"},
        "tracks": ["Спикер 1"],
    }
    service.repo.save(
        Job(
            id="heavy",
            source_name="Тяжёлая",
            status=JobStatus.DONE,
            text="полный текст",
            segments=[Segment(start=0.0, end=1.0, text="полный текст")],
            meta=heavy_meta,
            created_at=500.0,
        )
    )
    client = TestClient(create_app(settings=service.settings, service=service))

    listed = next(job for job in client.get("/api/jobs").json() if job["id"] == "heavy")
    assert listed["text"] == "" and listed["segments"] == []
    assert listed["meta"]["engine"] == "moss"
    assert listed["meta"]["duration"] == 12.5
    assert "audio" not in listed["meta"] and "tracks" not in listed["meta"]

    detail = client.get("/api/jobs/heavy").json()
    assert detail["text"] == "полный текст"
    assert detail["segments"][0]["text"] == "полный текст"
    assert detail["meta"]["audio"]

    # И сам репозиторий в slim-режиме не отдаёт транскрипт и тяжёлые мета.
    slim_job = next(job for job in service.list_jobs(slim=True) if job.id == "heavy")
    assert slim_job.text == "" and slim_job.segments == []
    assert "audio" not in slim_job.meta and "tracks" not in slim_job.meta


def test_queue_and_metrics_carry_slim_jobs(tmp_path: Path) -> None:
    """Очередь и метрики тоже не тащат транскрипты и тяжёлые мета."""
    service = _service(tmp_path)
    service.repo.save(
        Job(
            id="queued-heavy",
            source_name="В очереди",
            status=JobStatus.QUEUED,
            text="текст",
            segments=[Segment(start=0.0, end=1.0, text="текст")],
            meta={"audio": {"Спикер 1": "a.wav"}, "duration": 3.0, "parent": "r1"},
            created_at=600.0,
        )
    )
    client = TestClient(create_app(settings=service.settings, service=service))
    service._waiting.append("queued-heavy")  # очередь живёт в памяти сервиса

    waiting = client.get("/api/queue").json()["waiting"]
    row = next(job for job in waiting if job["id"] == "queued-heavy")
    assert row["text"] == "" and row["segments"] == []
    assert row["meta"]["parent"] == "r1" and row["meta"]["duration"] == 3.0
    assert "audio" not in row["meta"]
