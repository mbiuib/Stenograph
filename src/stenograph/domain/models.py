"""Core domain models: jobs, segments, statuses.

These models are persistence- and transport-agnostic: the same objects flow
through the pipeline, the SQLite repository and the HTTP API.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    """Lifecycle states of a transcription job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


def new_job_id() -> str:
    """Short, URL-safe job identifier."""
    return uuid.uuid4().hex[:12]


class Segment(BaseModel):
    """One transcribed utterance with timing and an optional speaker label."""

    index: int = 0
    start: float
    end: float
    text: str
    speaker: str | None = None


class Job(BaseModel):
    """A transcription job: file now, live session and Jitsi stream later."""

    id: str = Field(default_factory=new_job_id)
    kind: str = "file"  # file | live | jitsi
    source_name: str = ""
    source_path: str | None = None
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0  # 0..100, for the UI
    message: str = ""
    language: str | None = None
    text: str = ""
    segments: list[Segment] = Field(default_factory=list)
    error: str | None = None
    meta: dict = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
