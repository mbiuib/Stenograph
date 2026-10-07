"""SQLite persistence for jobs.

One database file, WAL mode, schema versioned through PRAGMA user_version so
future migrations stay trivial. The repository is the only place that knows
about SQL; everything above works with domain objects.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .domain.models import Job, JobStatus

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    source_name  TEXT NOT NULL DEFAULT '',
    source_path  TEXT,
    status       TEXT NOT NULL,
    progress     INTEGER NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT '',
    language     TEXT,
    text         TEXT NOT NULL DEFAULT '',
    segments     TEXT NOT NULL DEFAULT '[]',
    error        TEXT,
    meta         TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

_COLUMNS = (
    "id, kind, source_name, source_path, status, progress, message, language, "
    "text, segments, error, meta, created_at, started_at, finished_at"
)


class JobRepository:
    """Thread-safe job storage on top of sqlite3."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            row = self._conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0]) if row else 0
            if version < _SCHEMA_VERSION:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                log.info("initialized database schema v%d", _SCHEMA_VERSION)

    def save(self, job: Job) -> None:
        """Insert or update a job."""
        values = (
            job.id,
            job.kind,
            job.source_name,
            job.source_path,
            str(job.status),
            job.progress,
            job.message,
            job.language,
            job.text,
            json.dumps([s.model_dump() for s in job.segments], ensure_ascii=False),
            job.error,
            json.dumps(job.meta, ensure_ascii=False),
            job.created_at,
            job.started_at,
            job.finished_at,
        )
        placeholders = ", ".join("?" * 15)
        update_clause = ", ".join(
            f"{column.strip()} = excluded.{column.strip()}"
            for column in _COLUMNS.split(",")
            if column.strip() != "id"
        )
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO jobs ({_COLUMNS}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {update_clause}",
                values,
            )

    def get(self, job_id: str) -> Job | None:
        """Fetch one job by id."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[Job]:
        """List jobs, newest first."""
        query = f"SELECT {_COLUMNS} FROM jobs"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(str(status))
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def delete(self, job_id: str) -> None:
        """Remove a job."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def count_by_status(self) -> dict[str, int]:
        """Job counts per status."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def totals(self) -> dict[str, float]:
        """Aggregated audio seconds and processing seconds over finished jobs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "COALESCE(SUM(json_extract(meta, '$.duration')), 0) AS audio_seconds, "
                "COALESCE(SUM(json_extract(meta, '$.processing_seconds')), 0) "
                "AS processing_seconds "
                "FROM jobs WHERE status = 'done'"
            ).fetchone()
        audio = float(row["audio_seconds"] or 0.0) if row else 0.0
        processing = float(row["processing_seconds"] or 0.0) if row else 0.0
        return {"audio_seconds": audio, "processing_seconds": processing}

    def activity(self, days: int = 30) -> list[dict]:
        """Daily job counts and audio seconds for the last N days."""
        threshold = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(created_at, 'unixepoch', 'localtime') AS day, "
                "COUNT(*) AS jobs, "
                "COALESCE(SUM(json_extract(meta, '$.duration')), 0) AS audio_seconds "
                "FROM jobs WHERE created_at >= ? "
                "GROUP BY day ORDER BY day",
                (threshold,),
            ).fetchall()
        return [
            {
                "date": row["day"],
                "jobs": row["jobs"],
                "audio_seconds": float(row["audio_seconds"] or 0.0),
            }
            for row in rows
        ]

    def engine_usage(self) -> list[dict]:
        """Finished job counts per ASR engine."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(json_extract(meta, '$.engine'), '—') AS engine, "
                "COUNT(*) AS jobs "
                "FROM jobs WHERE status = 'done' GROUP BY engine ORDER BY jobs DESC"
            ).fetchall()
        return [{"engine": row["engine"], "jobs": row["jobs"]} for row in rows]


def _row_to_job(row: sqlite3.Row) -> Job:
    """Map a database row back to a Job model."""
    return Job(
        id=row["id"],
        kind=row["kind"],
        source_name=row["source_name"],
        source_path=row["source_path"],
        status=JobStatus(row["status"]),
        progress=row["progress"],
        message=row["message"],
        language=row["language"],
        text=row["text"],
        segments=json.loads(row["segments"] or "[]"),
        error=row["error"],
        meta=json.loads(row["meta"] or "{}"),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
