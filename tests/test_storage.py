"""Repository round-trip tests on a temporary database."""

from pathlib import Path

from stenograph.domain.models import Job, JobStatus, Segment
from stenograph.storage import JobRepository


def test_roundtrip(tmp_path: Path) -> None:
    """Save, load, update, list and delete a job."""
    repo = JobRepository(tmp_path / "test.db")
    job = Job(source_name="a.mp3", source_path=str(tmp_path / "a.mp3"))
    job.segments = [Segment(index=0, start=0.0, end=1.5, text="привет", speaker="SPEAKER_00")]
    job.text = "привет"
    job.status = JobStatus.DONE
    job.meta = {"duration": 1.5}
    repo.save(job)

    loaded = repo.get(job.id)
    assert loaded is not None
    assert loaded.text == "привет"
    assert loaded.segments[0].speaker == "SPEAKER_00"
    assert loaded.meta["duration"] == 1.5
    assert loaded.status == JobStatus.DONE

    loaded.progress = 100
    repo.save(loaded)
    again = repo.get(job.id)
    assert again is not None and again.progress == 100

    assert [j.id for j in repo.list_jobs()] == [job.id]
    assert repo.list_jobs(status=JobStatus.ERROR) == []

    repo.delete(job.id)
    assert repo.get(job.id) is None
    assert repo.list_jobs() == []


def test_update_progress_touches_only_tick_fields(tmp_path: Path) -> None:
    """Узкая запись прогресса не переписывает транскрипт и мета."""
    repo = JobRepository(tmp_path / "tick.db")
    job = Job(source_name="a.mp3")
    job.segments = [Segment(index=0, start=0.0, end=1.0, text="привет")]
    job.text = "привет"
    job.meta["audio"] = {"system": "x.wav"}
    repo.save(job)

    repo.update_progress(job.id, 42, "Транскрибация…")

    stored = repo.get(job.id)
    assert stored is not None
    assert (stored.progress, stored.message) == (42, "Транскрибация…")
    assert stored.text == "привет"
    assert len(stored.segments) == 1
    assert stored.meta.get("audio") == {"system": "x.wav"}


def test_get_many_fetches_slim_in_one_query(tmp_path: Path) -> None:
    """Пакетная выборка: словарь по id, slim по умолчанию, detail — по запросу."""
    repo = JobRepository(tmp_path / "many.db")
    jobs = [Job(source_name=f"файл {i}") for i in range(3)]
    for job in jobs:
        job.text = "секрет"
        repo.save(job)

    found = repo.get_many([jobs[0].id, jobs[2].id, "нет-такого"])
    assert set(found) == {jobs[0].id, jobs[2].id}
    assert found[jobs[0].id].text == ""  # slim: транскрипт не читается

    full = repo.get_many([jobs[1].id], slim=False)
    assert full[jobs[1].id].text == "секрет"
    assert repo.get_many([]) == {}


def test_v1_database_backfills_metric_columns(tmp_path: Path) -> None:
    """Миграция v4: duration/processing_seconds/engine заполняются из meta."""
    import json as jsonlib
    import sqlite3

    from stenograph import storage as storage_module

    db = tmp_path / "legacy4.db"
    conn = sqlite3.connect(db)
    conn.executescript(storage_module._SCHEMA)  # схема v1: колонок метрик нет
    conn.execute("PRAGMA user_version=1")
    conn.execute(
        "INSERT INTO jobs (id, kind, source_name, status, meta, created_at, segments, text) "
        "VALUES ('j1', 'file', 'j1', 'done', ?, 100.0, '[]', '')",
        (jsonlib.dumps({"duration": 12.5, "processing_seconds": 5.0, "engine": "moss"}),),
    )
    conn.commit()
    conn.close()

    repo = JobRepository(db)  # открытие мигрирует базу до v4

    totals = repo.totals()
    assert totals["audio_seconds"] == 12.5
    assert totals["processing_seconds"] == 5.0
    assert repo.engine_usage() == [{"engine": "moss", "jobs": 1}]


def test_metric_columns_follow_meta_on_save(tmp_path: Path) -> None:
    """save() поддерживает метрики-колонки синхронно с meta (дашборд читает их)."""
    repo = JobRepository(tmp_path / "metrics.db")
    job = Job(source_name="a.mp3")
    job.meta.update({"duration": 61.0, "processing_seconds": 7.2, "engine": "gigaam"})
    repo.save(job)

    with repo._lock:
        row = repo._conn.execute(
            "SELECT duration, processing_seconds, engine FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
    assert (row["duration"], row["processing_seconds"], row["engine"]) == (61.0, 7.2, "gigaam")
