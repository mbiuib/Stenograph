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
  POST /v1/transcriptions/cancel — abort an in-flight transcription by its
                                   request id (the main server's cancel)
  GET  /metrics                  — resource snapshot (the main server's
                                   monitor proxies it in remote mode)
  POST /models/unload            — unload idle engines (frees VRAM/RAM)

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
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import __version__, metrics, model_budget
from .asr_client import decode_wav_b64
from .config import Settings, get_settings
from .domain.errors import JobCancelled
from .engines import available_asr, get_asr
from .engines.base import AsrEngine, AsrResult, NoSpeechError, TranscribeOptions

log = logging.getLogger(__name__)


def _engine_footprint(engine: AsrEngine) -> int | None:
    """Measured VRAM of a loaded copy of ``engine`` (None = unknown)."""
    model = str(getattr(engine, "model_ref", None) or getattr(engine, "model_id", ""))
    return metrics.model_vram_mb(str(getattr(engine, "name", "")), model)


def can_grow_engine(name: str, engine: AsrEngine) -> bool:
    """Fit another copy of ``engine`` into the free VRAM?

    The estimate is the already-loaded first copy's measured footprint
    (:func:`metrics.model_vram_mb`) plus a margin — no measurement, no
    growth (we don't gamble GPU memory).
    """
    gpu = metrics.gpu_info()
    if gpu is None:
        return False
    measured = _engine_footprint(engine)
    if not measured:
        return False
    need = int(measured * 1.15) + 256
    return int(gpu.get("vram_free_mb") or 0) >= need


class EnginePool:
    """Lazily built engines; every name owns one replica, grown on demand.

    Model inference is not re-entrant, so each replica carries its own call
    lock and concurrent requests for the same name serialize per replica.
    When every replica is busy and the configured limit allows more, one
    extra replica is built in the BACKGROUND (; the triggering request serves
    from the first replica and waits at most one in-flight call). Growth is
    gated by free VRAM and backs off after a refusal or a failed build.
    Extra copies let file transcriptions and live windows run without
    queueing behind each other.
    """

    _GROW_RETRY_NO_MEASURE_SEC = 15.0  # первая копия, возможно, ещё грузится
    _GROW_RETRY_FAILED_SEC = 120.0  # отказ по памяти или сбой загрузки

    def __init__(
        self,
        build: Callable[[str], AsrEngine],
        *,
        copies: Callable[[str], int] | None = None,
        can_grow: Callable[[str, AsrEngine], bool] | None = None,
    ) -> None:
        self._build = build
        self._copies = copies or (lambda name: 1)
        self._can_grow = can_grow or (lambda name, engine: True)
        self._lock = threading.Lock()
        self._replicas: dict[str, list[tuple[AsrEngine, threading.Lock]]] = {}
        self._growing: set[str] = set()
        self._grow_retry_at: dict[str, float] = {}

    # gigaam-live — тот же самый движок, что gigaam: без канонизации сервис
    # держал бы по копии модели на каждый алиас (файлы + окна = две копии VRAM).
    _POOL_ALIASES = {"gigaam-live": "gigaam"}

    def get(self, name: str) -> tuple[AsrEngine, threading.Lock]:
        """Engine replica and its call lock; KeyError for an unknown name."""
        name = self._POOL_ALIASES.get(name, name)
        spawn = False
        with self._lock:
            replicas = self._replicas.get(name)
            if not replicas:
                replicas = [(self._build(name), threading.Lock())]  # unknown names raise KeyError
                self._replicas[name] = replicas
            for engine, call_lock in replicas:
                if not call_lock.locked():
                    return engine, call_lock
            # Все копии заняты: текущий запрос обслуживает первая (ждёт максимум
            # один текущий вызов), а следующая копия строится фоном.
            if len(replicas) < self._copies(name):
                spawn = True
            chosen = replicas[0]
        if spawn:
            self._spawn_growth(name)
        return chosen

    def loaded(self) -> list[str]:
        """Names whose engines were already built in this process."""
        with self._lock:
            return sorted(self._replicas)

    def copy_counts(self) -> dict[str, int]:
        """How many replicas each engine currently holds (for the monitor)."""
        with self._lock:
            return {name: len(replicas) for name, replicas in self._replicas.items()}

    def copy_limits(self) -> dict[str, int]:
        """Configured replica cap per engine (how far growth may go)."""
        with self._lock:
            names = list(self._replicas)
        return {name: max(1, int(self._copies(name))) for name in names}

    def _spawn_growth(self, name: str) -> None:
        """Kick a background replica build unless one is running or backing off."""
        with self._lock:
            if name in self._growing or time.monotonic() < self._grow_retry_at.get(name, 0.0):
                return
            self._growing.add(name)
        threading.Thread(
            target=self._grow, args=(name,), name=f"asr-grow-{name}", daemon=True
        ).start()

    def _set_retry(self, name: str, delay: float) -> None:
        """Remember a retry moment; also marks the current attempt as finished."""
        with self._lock:
            self._grow_retry_at[name] = time.monotonic() + delay
            self._growing.discard(name)

    def _grow(self, name: str) -> None:
        """Build and warm one more replica; VRAM-gated, backing off on refusals."""
        try:
            with self._lock:
                sample = self._replicas[name][0][0]  # первая копия — эталон памяти
            if _engine_footprint(sample) is None:
                self._set_retry(name, self._GROW_RETRY_NO_MEASURE_SEC)
                return
            if not self._can_grow(name, sample):
                self._set_retry(name, self._GROW_RETRY_FAILED_SEC)
                return
            fresh = self._build(name)
            loader = getattr(fresh, "_load", None)
            if callable(loader):
                loader()  # греем копию здесь же: первый запрос не должен ждать загрузку
        except Exception:  # noqa: BLE001 — рост не должен ронять сервис
            log.exception("asr: не удалось поднять копию движка %s", name)
            self._set_retry(name, self._GROW_RETRY_FAILED_SEC)
            return
        with self._lock:
            self._replicas[name].append((fresh, threading.Lock()))
            self._growing.discard(name)
            self._grow_retry_at.pop(name, None)  # успех снимает бэкофф
        log.info(
            "asr: поднята ещё одна копия движка %s (%d всего)", name, len(self._replicas[name])
        )


class WindowBatch(BaseModel):
    """Internal realtime extension: a batch of raw audio windows."""

    model: str = ""
    language: str | None = None
    windows: list[str]  # each: base64 of a 16 kHz mono int16 WAV


class CancelRequest(BaseModel):
    """POST /v1/transcriptions/cancel: abort a request by its id."""

    request_id: str


class CancelRegistry:
    """In-flight transcription requests of this service (id → cancel event).

    The main server attaches a request id to every upload; to stop a
    runaway transcription (a cancelled job must not keep the GPU busy for
    minutes) it POSTs /v1/transcriptions/cancel and the engine raises at
    its next cooperative checkpoint.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def start(self, request_id: str) -> threading.Event:
        """Register a fresh request; returns its cancel event."""
        event = threading.Event()
        with self._lock:
            self._events[request_id] = event
        return event

    def finish(self, request_id: str) -> None:
        """Forget a request that answered (normally or by cancel)."""
        with self._lock:
            self._events.pop(request_id, None)

    def cancel(self, request_id: str) -> bool:
        """Ask the in-flight request to stop; False when it is not running."""
        with self._lock:
            event = self._events.get(request_id)
        if event is None:
            return False
        event.set()
        return True

    def active(self) -> int:
        """How many transcriptions are being served right now."""
        with self._lock:
            return len(self._events)


def _text_of(result: AsrResult) -> str:
    """Human text of a result: speaker-prefixed lines when speakers exist."""
    lines = []
    for segment in result.segments:
        lines.append(f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text)
    return "\n".join(lines)


LIVE_ENGINE_NAME = "whisper-live"
GIGAAM_LIVE_ENGINE_NAME = "gigaam-live"


def advertised_engines() -> list[str]:
    """Engine names this service answers to (registry + the realtime aliases)."""
    return sorted([*available_asr(), LIVE_ENGINE_NAME, GIGAAM_LIVE_ENGINE_NAME])


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
    if name == GIGAAM_LIVE_ENGINE_NAME:
        from .engines.gigaam import GigaAmEngine

        return GigaAmEngine(
            model=settings.gigaam_model,
            revision=settings.gigaam_revision,
            models_dir=settings.models_dir,
            device=settings.device,
            chunk_sec=settings.gigaam_chunk_sec,
        )
    return get_asr(name, settings)


def create_app(
    settings: Settings | None = None,
    engine_factory: Callable[[str], AsrEngine] | None = None,
) -> FastAPI:
    """Build the ASR service; inject an engine factory for tests."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    # Рантайм-переопределения из «Настроек» сервера: без этого UI-правки
    # сервисных ключей (gigaam_*, движок по умолчанию) не доходили бы до
    # процесса, который реально грузит модели. Только чтение.
    from .settings_service import SettingsService

    overrides_path = (
        Path(settings.settings_overrides) if settings.settings_overrides else None
    )
    SettingsService(settings, path=overrides_path)
    model_budget.configure(settings.model_idle_unload_sec)
    metrics.prime()  # baseline for the per-model VRAM/RAM deltas
    build = engine_factory or (lambda name: build_default_engine(name, settings))
    pool = EnginePool(
        build,
        copies=lambda name: settings.gigaam_copies if name == "gigaam" else 1,
        can_grow=can_grow_engine,
    )
    cancels = CancelRegistry()

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
            "active_transcriptions": cancels.active(),
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
        request_id: str = Form(""),
    ) -> dict:
        """OpenAI-compatible transcription over the registered engines.

        ``request_id`` (our extension) registers the call in the cancel
        registry: the main server aborts a cancelled job through
        POST /v1/transcriptions/cancel instead of waiting for the full run.
        """
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
            cancel_event = cancels.start(request_id) if request_id.strip() else None
            try:
                with call_lock:
                    try:
                        result = engine.transcribe(
                            temp,
                            options,
                            is_cancelled=cancel_event.is_set if cancel_event else None,
                        )
                    except NoSpeechError:
                        result = AsrResult()
            except JobCancelled:
                raise HTTPException(status_code=409, detail="transcription cancelled") from None
            finally:
                if cancel_event is not None:
                    cancels.finish(request_id)
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

    @app.post("/v1/transcriptions/cancel")
    def cancel_transcription(payload: CancelRequest) -> dict:
        """Abort an in-flight transcription; it stops at its next engine checkpoint."""
        return {"cancelled": cancels.cancel(payload.request_id)}

    @app.get("/metrics")
    def metrics_snapshot() -> dict:
        """Resource snapshot of this process (the main server proxies it)."""
        snap = metrics.snapshot()
        for key in ("live", "bridge", "queue", "jobs", "llm"):
            snap.pop(key, None)
        busy = model_budget.busy_models()
        copies = pool.copy_counts()
        limits = pool.copy_limits()
        for row in snap.get("models", []):
            name = str(row.get("engine"))
            row["busy"] = (row.get("engine"), row.get("model")) in busy
            row["copies"] = copies.get(name, 1)
            row["copies_limit"] = limits.get(name, 1)
        snap["engines_loaded"] = pool.loaded()
        return snap

    @app.post("/models/unload")
    def unload_models() -> dict:
        """Unload idle engines now (frees VRAM/RAM); busy ones are kept."""
        return {"unloaded": model_budget.evict_idle(force=True)}

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
