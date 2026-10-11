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

from ..asr_client import RemoteAsrClient
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
        self._client = client or RemoteAsrClient(
            settings.asr_url, file_timeout=settings.asr_timeout_sec
        )

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
        request_id = uuid.uuid4().hex
        stopping = threading.Event()
        if is_cancelled is not None:
            # Пока сервис считает, следим за флагом: отмена обязана рвать и
            # удалённый вызов, иначе многоминутный MOSS доситывается целиком.
            def _watch() -> None:
                while not stopping.wait(0.5):
                    if is_cancelled():
                        self._client.cancel_transcription(request_id)
                        return

            threading.Thread(target=_watch, name="asr-cancel-watch", daemon=True).start()
        try:
            result = self._client.transcribe_file(
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
