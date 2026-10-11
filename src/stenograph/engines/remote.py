"""ASR engine backed by the external service (no local model).

MEETSCRIBE_ASR_BACKEND=remote makes the server a thin client: the models
live in asr_service.py, this process never loads them. The engines registry
returns this class from make_asr() in remote mode, so every file/reprocess
job ships its audio over HTTP instead of decoding in-process.

Cancellation is cooperative AND reaches through the service: a watcher
thread polls ``is_cancelled`` and aborts the in-flight request by its id
(POST /v1/transcriptions/cancel), so a long MOSS run stops at its next
generated token instead of decoding to the end. pause_gate is honored
before the call only, since the upload is one shot.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from .. import asr_client
from ..asr_client import AsrServiceError, RemoteAsrClient
from ..config import Settings
from ..domain.errors import JobCancelled
from .base import (
    AsrResult,
    CancelCallback,
    NoSpeechError,
    PauseGate,
    ProgressCallback,
    SegmentCallback,
    TranscribeOptions,
)

_CLIENTS: dict[tuple[str, float], RemoteAsrClient] = {}
_CLIENTS_LOCK = threading.Lock()


def remote_client_for(settings: Settings) -> RemoteAsrClient:
    """Клиент сервиса под ТЕКУЩИЕ адрес и тайм-аут (меняются в рантайме).

    Кэш по (url, timeout): при смене адреса в «Настройках» следующий же
    вызов (файл или окно субтитров) уйдёт на новый сервис — без
    перезапуска; старые клиенты доживают своё и остаются в кэше.
    """
    url = settings.asr_url.rstrip("/")
    if not url:
        raise AsrServiceError("адрес ASR-сервиса не задан (Настройки → Внешний ASR-сервис)")
    key = (url, float(settings.asr_timeout_sec))
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            # Класс резолвим через модуль: живые тесты патчат его там.
            client = asr_client.RemoteAsrClient(key[0], file_timeout=key[1])
            _CLIENTS[key] = client
        return client


class RemoteAsrEngine:
    """Drop-in AsrEngine that ships work to the external ASR service."""

    def __init__(
        self,
        name: str,
        settings: Settings,
        *,
        client: RemoteAsrClient | None = None,
    ) -> None:
        self.name = name
        self._settings = settings
        self._client = client  # None: клиент резолвится по текущим настройкам

    def _active_client(self) -> RemoteAsrClient:
        """Клиент под текущий адрес сервиса (настройки могут меняться)."""
        if self._client is not None:
            return self._client
        return remote_client_for(self._settings)

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
        *,
        on_progress: ProgressCallback | None = None,
        on_segment: SegmentCallback | None = None,
        is_cancelled: CancelCallback | None = None,
        pause_gate: PauseGate | None = None,
    ) -> AsrResult:
        """Upload the file to the service; mirror the local engines' contract."""
        if is_cancelled is not None and is_cancelled():
            raise JobCancelled()
        if pause_gate is not None:
            pause_gate()
        client = self._active_client()
        request_id = uuid.uuid4().hex
        stopping = threading.Event()
        if is_cancelled is not None:
            # Пока сервис считает, следим за флагом: отмена обязана рвать и
            # удалённый вызов, иначе многоминутный MOSS доситывается целиком.
            def _watch() -> None:
                while not stopping.wait(0.5):
                    if is_cancelled():
                        client.cancel_transcription(request_id)
                        return

            threading.Thread(target=_watch, name="asr-cancel-watch", daemon=True).start()
        try:
            result = client.transcribe_file(
                audio_path,
                model=self.name,
                language=options.language,
                request_id=request_id,
            )
        finally:
            stopping.set()
        if not result.segments:
            raise NoSpeechError(f"{self.name}: речь не найдена (внешний ASR)")
        if on_segment is not None:
            for segment in result.segments:
                on_segment(segment)
        if is_cancelled is not None and is_cancelled():
            raise JobCancelled()
        return result
