"""External ASR service: our engines behind an OpenAI-compatible HTTP API.

Runs whisper/moss in a SEPARATE process so the main server's API can never be
frozen by model inference (a heavy ramp used to block the event loop for
minutes). The engines are the very same code and models the server uses
built-in — only the process boundary is new.

Endpoints:

  GET  /health                   — liveness + engine list
  GET  /v1/models                — one "model" per engine (whisper, moss)
  POST /v1/audio/transcriptions  — multipart: file, model, language,
                                   response_format=json|verbose_json (OpenAI)
  POST /v1/realtime/windows      — internal realtime extension: base64 WAV
                                   windows in, word tuples out

The main server switches to this service with MEETSCRIBE_ASR_BACKEND=remote
and MEETSCRIBE_ASR_URL (see asr_client.py); with the default "local" it keeps
its built-in engines and this service can run alongside, unused.

Run: .venv/Scripts/python.exe -m stenograph.asr_service
Port: MEETSCRIBE_ASR_PORT (default 9001); host: MEETSCRIBE_ASR_HOST.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import __version__, model_budget
from .asr_client import decode_wav_b64
from .config import Settings, get_settings
from .engines import available_asr, get_asr
from .engines.base import AsrEngine, AsrResult, NoSpeechError, TranscribeOptions

log = logging.getLogger(__name__)


class EnginePool:
    """Lazily built engines with one call lock per engine.

    Model inference is not re-entrant: concurrent HTTP requests for the same
    engine serialize on its lock (mirrors the live pool's single engine lock).
    """

    def __init__(self, build: Callable[[str], AsrEngine]) -> None:
        self._build = build
        self._lock = threading.Lock()
        self._engines: dict[str, AsrEngine] = {}
        self._call_locks: dict[str, threading.Lock] = {}

    def get(self, name: str) -> tuple[AsrEngine, threading.Lock]:
        """Engine instance and its call lock; KeyError for an unknown name."""
        with self._lock:
            engine = self._engines.get(name)
            if engine is None:
                engine = self._build(name)  # unknown names raise KeyError here
                self._engines[name] = engine
                self._call_locks[name] = threading.Lock()
            return engine, self._call_locks[name]

    def loaded(self) -> list[str]:
        """Names whose engines were already built in this process."""
        with self._lock:
            return sorted(self._engines)


class WindowBatch(BaseModel):
    """Internal realtime extension: a batch of raw audio windows."""

    model: str = ""
    language: str | None = None
    windows: list[str]  # each: base64 of a 16 kHz mono int16 WAV


def _text_of(result: AsrResult) -> str:
    """Human text of a result: speaker-prefixed lines when speakers exist."""
    lines = []
    for segment in result.segments:
        lines.append(f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text)
    return "\n".join(lines)


LIVE_ENGINE_NAME = "whisper-live"


def advertised_engines() -> list[str]:
    """Engine names this service answers to (registry + the live alias)."""
    return sorted([*available_asr(), LIVE_ENGINE_NAME])


def build_default_engine(name: str, settings: Settings) -> AsrEngine:
    """Build a service engine: registry engines + the realtime turbo alias.

    Realtime window batches speak the LIVE (turbo) model: at the same
    caption latency it decodes several times faster than the big
    file-pass model, which matters when many meetings share the service.
    """
    if name == LIVE_ENGINE_NAME:
        from .engines.whisper import FasterWhisperEngine

        return FasterWhisperEngine(
            model=settings.live_model,
            models_dir=settings.models_dir,
            device=settings.device,
            compute_type=settings.compute_type,
        )
    return get_asr(name, settings)


def create_app(
    settings: Settings | None = None,
    engine_factory: Callable[[str], AsrEngine] | None = None,
) -> FastAPI:
    """Build the ASR service; inject an engine factory for tests."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    model_budget.configure(settings.model_idle_unload_sec)
    build = engine_factory or (lambda name: build_default_engine(name, settings))
    pool = EnginePool(build)

    app = FastAPI(title="Стенограф ASR", version=__version__)
    app.state.settings = settings
    app.state.pool = pool

    @app.get("/health")
    def health() -> dict:
        """Liveness and the engine list of this service."""
        return {
            "status": "ok",
            "version": __version__,
            "engines": advertised_engines(),
            "loaded": pool.loaded(),
        }

    @app.get("/v1/models")
    def models() -> dict:
        """OpenAI-compatible model list — one entry per engine."""
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "stenograph"}
                for name in advertised_engines()
            ],
        }

    @app.post("/v1/audio/transcriptions")
    def transcriptions(
        file: UploadFile = File(...),
        model: str = Form(""),
        language: str = Form(""),
        response_format: str = Form("json"),
    ) -> dict:
        """OpenAI-compatible transcription over the registered engines."""
        name = model.strip() or settings.engine
        try:
            engine, call_lock = pool.get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        suffix = Path(file.filename or "").suffix or ".wav"
        with NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            temp = Path(handle.name)
            shutil.copyfileobj(file.file, handle)
        try:
            normalized = language.strip()
            options = TranscribeOptions(
                language=None if normalized.lower() in ("", "auto") else normalized
            )
            with call_lock:
                try:
                    result = engine.transcribe(temp, options)
                except NoSpeechError:
                    result = AsrResult()
        finally:
            temp.unlink(missing_ok=True)
        text = _text_of(result)
        if response_format == "verbose_json":
            return {
                "task": "transcribe",
                "language": result.language or None,
                "duration": result.duration,
                "text": text,
                "segments": [
                    {
                        "id": index,
                        "start": segment.start,
                        "end": segment.end,
                        "text": segment.text,
                        "speaker": segment.speaker,
                    }
                    for index, segment in enumerate(result.segments)
                ],
            }
        return {"text": text}

    @app.post("/v1/realtime/windows")
    def realtime_windows(request: WindowBatch) -> dict:
        """Realtime window batch used by the main server's live/Jitsi paths."""
        name = request.model.strip() or settings.engine
        try:
            engine, call_lock = pool.get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        batch = getattr(engine, "transcribe_batch", None)
        if batch is None:
            raise HTTPException(
                status_code=400, detail=f"engine '{name}' does not support realtime windows"
            )
        try:
            windows = [decode_wav_b64(item) for item in request.windows]
        except Exception as exc:  # base64/wave garbage from a client → a clean 400
            raise HTTPException(status_code=400, detail=f"bad window payload: {exc}") from exc
        with call_lock:
            results = batch(windows, language=request.language)
        return {
            "windows": [
                [[float(start), float(end), text] for start, end, text in words]
                for words in results
            ]
        }

    return app


def main() -> None:
    """Run the service with uvicorn (port: MEETSCRIBE_ASR_PORT)."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    host = os.environ.get("MEETSCRIBE_ASR_HOST", "0.0.0.0")
    uvicorn.run(create_app(settings), host=host, port=settings.asr_port)


if __name__ == "__main__":
    main()
