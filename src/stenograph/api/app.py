"""FastAPI application factory.

Thin HTTP layer: routes validate input, call the service and stream events
from the bus. Business logic lives in the service and the pipeline.
"""

from __future__ import annotations

import functools
import json
import logging
import queue
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings, get_settings
from ..domain.models import JobStatus
from ..engines import available_asr
from ..service import TranscriptionService, build_default_service

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, service: TranscriptionService | None = None
) -> FastAPI:
    """Build the web application; inject a service for tests."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    service = service or build_default_service(settings)

    app = FastAPI(title="Стенограф", version=__version__)
    app.state.settings = settings
    app.state.service = service

    @app.get("/api/health")
    def health() -> dict:
        """Liveness probe."""
        return {"status": "ok", "version": __version__}

    @app.get("/api/jobs")
    def list_jobs(status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """List recent jobs, newest first."""
        status_enum = JobStatus(status) if status else None
        jobs = service.list_jobs(status=status_enum, limit=limit, offset=offset)
        return [job.model_dump() for job in jobs]

    @app.post("/api/jobs", status_code=201)
    async def create_job(
        file: UploadFile,
        language: str | None = Form(default=None),
        engine: str | None = Form(default=None),
    ) -> dict:
        """Upload a media file and queue it for transcription."""
        if engine is not None and engine not in available_asr():
            raise HTTPException(
                status_code=400,
                detail=f"unknown engine '{engine}'; available: {', '.join(available_asr())}",
            )
        suffix = Path(file.filename or "upload").suffix.lower()
        upload_path = settings.uploads_dir / f"{uuid.uuid4().hex}{suffix}"
        with upload_path.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                dest.write(chunk)
        job = service.submit_file(
            upload_path,
            source_name=file.filename or upload_path.name,
            language=language,
            engine=engine,
        )
        return job.model_dump()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        """Full job snapshot."""
        job = service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.model_dump()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        """Request cancellation of a queued or running job."""
        if service.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"cancelled": service.cancel(job_id)}

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str) -> None:
        """Delete a job."""
        if service.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        service.delete(job_id)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> StreamingResponse:
        """Server-sent events for one job: a snapshot first, then live updates."""
        job = service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

        channel = service.bus.subscribe(job_id)
        snapshot = {"type": "snapshot", "job": job.model_dump()}

        async def event_stream() -> AsyncIterator[str]:
            try:
                yield _sse(snapshot)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await anyio.to_thread.run_sync(
                            functools.partial(channel.get, True, 15)
                        )
                    except queue.Empty:
                        yield ": ping\n\n"
                        continue
                    yield _sse(event)
                    if event.get("type") in ("done", "error", "cancelled"):
                        break
            finally:
                service.bus.unsubscribe(job_id, channel)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/engines")
    def list_engines() -> dict:
        """Registered ASR engines and the default one."""
        return {"available": available_asr(), "default": service.engine_name}

    @app.get("/api/config")
    def get_config() -> dict:
        """Non-secret effective settings, for the UI."""
        return {
            "engine": service.engine_name,
            "whisper_model": settings.whisper_model,
            "language": settings.language,
            "device": settings.device,
            "compute_type": settings.compute_type,
            "models_dir": str(settings.models_dir) if settings.models_dir else None,
        }

    @app.get("/api/queue")
    def get_queue() -> dict:
        """Active and waiting jobs (the worker processes strictly FIFO)."""
        view = service.queue_view()
        active = view["active"]
        return {
            "active": active.model_dump() if active else None,
            "waiting": [job.model_dump() for job in view["waiting"]],
        }

    @app.get("/api/stats")
    def get_stats() -> dict:
        """Aggregated statistics for the dashboard."""
        counts = service.repo.count_by_status()
        totals = service.repo.totals()
        speed = (
            totals["audio_seconds"] / totals["processing_seconds"]
            if totals["processing_seconds"] > 0
            else None
        )
        return {
            "jobs": {"total": sum(counts.values()), "by_status": counts},
            "audio_seconds": totals["audio_seconds"],
            "processing_seconds": totals["processing_seconds"],
            "avg_speed_factor": round(speed, 2) if speed else None,
            "engines": service.repo.engine_usage(),
            "activity": service.repo.activity(30),
            "recent": [job.model_dump() for job in service.list_jobs(limit=8)],
        }

    # -- static frontend (built by Vite into <repo>/frontend/dist) ----------

    frontend_dist = settings.frontend_dist or (
        Path(__file__).resolve().parents[3] / "frontend" / "dist"
    )
    if frontend_dist.is_dir():
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str) -> FileResponse:
            """Serve the SPA shell; unknown /api paths still return 404."""
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            candidate = frontend_dist / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app


def _sse(payload: dict) -> str:
    """Format one server-sent event frame."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
