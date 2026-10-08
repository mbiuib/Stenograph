"""FastAPI application factory.

Thin HTTP layer: routes validate input, call the service and stream events
from the bus. Business logic lives in the service and the pipeline.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import queue
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..bridge.manager import BridgeManager
from ..bridge.protocol import FrameError, is_eof, parse_frame
from ..bridge.session import MeetingSession
from ..config import Settings, get_settings
from ..domain.models import JobStatus
from ..engines import available_asr
from ..live.capture import CaptureError, capture_supported, describe_devices
from ..live.manager import LiveManager
from ..live.web import parse_upload_frame
from ..service import TranscriptionService, build_default_service

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    service: TranscriptionService | None = None,
    live: LiveManager | None = None,
    bridge: BridgeManager | None = None,
) -> FastAPI:
    """Build the web application; inject a service/live/bridge manager for tests."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    service = service or build_default_service(settings)
    live = live or LiveManager(
        settings,
        service.repo,
        service.bus,
        reprocess=service.reprocess_job,
        auto_reprocess=settings.live_auto_reprocess,
    )
    bridge = bridge or BridgeManager(settings, service.repo, service.bus)

    app = FastAPI(title="Стенограф", version=__version__)
    app.state.settings = settings
    app.state.service = service
    app.state.live = live
    app.state.bridge = bridge

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

    @app.post("/api/jobs/{job_id}/reprocess", status_code=201)
    def reprocess_job(job_id: str, engine: str | None = Form(default=None)) -> dict:
        """Queue an offline re-transcription (default engine) of a live recording."""
        job = service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if engine is not None and engine not in available_asr():
            raise HTTPException(
                status_code=400,
                detail=f"unknown engine '{engine}'; available: {', '.join(available_asr())}",
            )
        try:
            child = service.reprocess_job(job, engine=engine)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return child.model_dump()

    @app.post("/api/jobs/{job_id}/analyze", status_code=201)
    def analyze_job(
        job_id: str, analysis_type: str = Form(alias="type", default="protocol")
    ) -> dict:
        """Queue protocol/summary generation for a finished job."""
        job = service.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status != JobStatus.DONE:
            raise HTTPException(status_code=400, detail="задача ещё не завершена")
        try:
            child = service.request_analysis(job, analysis_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return child.model_dump()

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
            "live_model": settings.live_model,
            "live_auto_reprocess": settings.live_auto_reprocess,
            "language": settings.language,
            "device": settings.device,
            "compute_type": settings.compute_type,
            "models_dir": str(settings.models_dir) if settings.models_dir else None,
            "llm": {
                "model": settings.llm_model,
                "base_url": settings.llm_base_url,
            },
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

    # -- live sessions -------------------------------------------------------

    @app.get("/api/live/status")
    def live_status() -> dict:
        """Live sessions and the transcription queue state, plus capture availability."""
        return live.status()

    @app.get("/api/live/devices")
    def live_devices() -> dict:
        """Default loopback/microphone device names shown by the live UI."""
        try:
            devices = describe_devices()
        except Exception as exc:  # noqa: BLE001 — report, do not crash the API
            return {"supported": capture_supported(), "devices": {}, "error": str(exc)}
        return {"supported": capture_supported(), "devices": devices}

    @app.post("/api/live/start", status_code=201)
    def live_start(
        tracks: str | None = Form(default=None),
        language: str | None = Form(default=None),
    ) -> dict:
        """Start a server-side capture session; tracks is a comma-separated list.

        Only one server-side (WASAPI) session can run at a time — browser
        sessions are unlimited and don't conflict with it.
        """
        selected = [item.strip() for item in (tracks or "").split(",") if item.strip()]
        try:
            job = live.start(selected or None, language)
        except CaptureError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:  # a server session is already running / bad tracks
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.model_dump()

    @app.post("/api/live/stop")
    def live_stop(job_id: str | None = Form(default=None)) -> dict:
        """Stop one live session and return the finalized job.

        Without ``job_id`` the server-side capture session is stopped; pass a
        browser session's id to stop that one instead.
        """
        job = live.stop_session(job_id) if job_id else live.stop()
        if job is None:
            raise HTTPException(status_code=404, detail="нет активной live-сессии")
        return job.model_dump()

    # -- browser live capture (WebSocket upload) ------------------------------

    @app.websocket("/ws/live")
    async def live_upload(websocket: WebSocket) -> None:
        """Browser live capture: the page uploads microphone/system audio.

        Handshake (text JSON): ``{"type":"start","tracks":["mic","system"],
        "language":"ru"}``; the answer is ``{"type":"ready","job_id":…}``.
        Binary frames: one track byte (0 = mic, 1 = system) + int16 LE
        16 kHz mono PCM. Any number of browser sessions can run at once; the
        shared decode queue transcribes them turn by turn. ``{"type":"stop"}``
        or a disconnect finalizes this session and chains the quality re-pass
        over its recorded audio.
        """
        await websocket.accept()
        session = None
        try:
            message = await websocket.receive()
            text = message.get("text")
            if message.get("type") != "websocket.receive" or not text:
                raise RuntimeError("первым сообщением должен быть JSON start")
            try:
                command = json.loads(text)
            except ValueError as exc:
                raise RuntimeError("не разобрать start-сообщение") from exc
            if command.get("type") != "start":
                raise RuntimeError("первым сообщением должен быть start")
            requested = [name for name in command.get("tracks") or [] if name in ("mic", "system")]
            session = live.start_web(requested or None, command.get("language") or None)
            log.info(
                "live: браузерная сессия %s подключена (%s)",
                session.job.id,
                ", ".join(session.tracks),
            )
            await websocket.send_json(
                {
                    "type": "ready",
                    "job_id": session.job.id,
                    "tracks": list(session.tracks),
                    "sample_rate": 16000,
                }
            )
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    log.info("live: браузерная сессия %s отключилась", session.job.id)
                    break
                data = message.get("bytes")
                if data is not None:
                    parsed = parse_upload_frame(data)
                    if parsed is not None:
                        session.feed(*parsed)
                    continue
                text = message.get("text")
                if not text:
                    continue
                try:
                    command = json.loads(text)
                except ValueError:
                    continue
                if command.get("type") == "stop":
                    break
        except RuntimeError as exc:
            log.warning("live: не удалось начать браузерную сессию: %s", exc)
            try:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception:  # noqa: BLE001 — the client may already be gone
                log.debug("live: не удалось отправить ошибку клиенту", exc_info=True)
        finally:
            if session is not None:
                await anyio.to_thread.run_sync(live.stop_session, session.job.id)
                log.info("live: браузерная сессия %s закрыта", session.job.id)
            with contextlib.suppress(Exception):  # closing a dead socket is fine
                await websocket.close()

    # -- Jigasi bridge (streaming-whisper protocol) ---------------------------

    @app.websocket("/ws/{meeting_id}")
    async def whisper_stream(websocket: WebSocket, meeting_id: str) -> None:
        """Transcription endpoint consumed by Jigasi's WhisperTranscriptionService.

        Jigasi dials this URL itself (whisper.websocket_url config) when a
        transcription is started in a conference; binary frames carry the
        60-byte participant header + int16 PCM, caption JSON flows back.
        """
        await websocket.accept()
        session = bridge.start(meeting_id)
        log.info("jitsi bridge: websocket подключён (%s)", meeting_id)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_caption_sender, websocket, session)
                try:
                    async for data in websocket.iter_bytes():
                        if is_eof(data):
                            log.info("jitsi bridge: получен EOF (%s)", meeting_id)
                            break
                        try:
                            participant_id, language, audio = parse_frame(data)
                        except FrameError as exc:
                            log.warning("jitsi bridge: плохой кадр (%s): %s", meeting_id, exc)
                            continue
                        session.feed(participant_id, language, audio)
                except WebSocketDisconnect:
                    log.info("jitsi bridge: websocket отключён (%s)", meeting_id)
                await anyio.to_thread.run_sync(session.stop)
        finally:
            bridge.end(meeting_id, session)
            log.info("jitsi bridge: сессия закрыта (%s)", meeting_id)

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


async def _caption_sender(websocket: WebSocket, session: MeetingSession) -> None:
    """Forward caption messages (partial/final JSON) from the worker to Jigasi."""
    while True:
        message = await anyio.to_thread.run_sync(session.dequeue, 0.5)
        if message is None:
            continue
        if message == "":  # session closed and drained
            break
        try:
            await websocket.send_text(message)
        except Exception:  # noqa: BLE001 — a dead client just ends the sender
            break
