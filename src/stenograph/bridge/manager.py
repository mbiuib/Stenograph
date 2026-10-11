"""Registry of active Jigasi bridge sessions.

Jigasi dials one websocket per room and reuses the same connection id when it
reconnects — the manager replaces a stale session when the same meeting id
connects again.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..domain.models import Job
from ..events import EventBus
from ..live.manager import TranscriberFactory, default_transcriber_factory
from ..storage import JobRepository
from .session import MeetingSession

log = logging.getLogger(__name__)


class BridgeManager:
    """Owns meeting sessions created by incoming Jigasi websockets."""

    def __init__(
        self,
        settings: Settings,
        repo: JobRepository,
        bus: EventBus,
        *,
        transcriber_factory: TranscriberFactory | None = None,
        pool: Any | None = None,
        reprocess: Callable[[Job], Job | None] | None = None,
        auto_reprocess: bool = False,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._factory = transcriber_factory or default_transcriber_factory(settings)
        self._pool = pool  # shared live decode pool; None = per-meeting ticking
        self._reprocess = reprocess  # quality re-pass chain after a meeting ends
        self._auto_reprocess = auto_reprocess
        self._sessions: dict[str, MeetingSession] = {}
        self._lock = threading.Lock()
        limit = settings.bridge_finalize_concurrency
        self._finalize_gate: threading.Semaphore | None = (
            threading.Semaphore(limit) if limit > 0 else None
        )
        # Надзор за общим бюджетом бэклога (демон, 1 Гц). Поток запускается
        # всегда, а бюджет читается из настроек на каждом проходе — смена
        # лимита в рантайме действует сразу (0 = бюджет выключен).
        threading.Thread(
            target=self._budget_loop,
            name="stenograph-bridge-budget",
            daemon=True,
        ).start()

    def start(self, meeting_id: str) -> MeetingSession:
        """Start a session for a meeting connection (replacing a stale one)."""
        with self._lock:
            previous = self._sessions.pop(meeting_id, None)
            session = MeetingSession(
                meeting_id,
                self._settings,
                self._repo,
                self._bus,
                self._factory,
                pool=self._pool,
                reprocess=self._reprocess,
                auto_reprocess=self._auto_reprocess,
                transcribe=self._settings.realtime_transcribe,
                finalize_gate=self._finalize_gate,
            )
            self._sessions[meeting_id] = session
        if previous is not None:
            log.info("jitsi bridge: переподключение %s — закрываю прошлую сессию", meeting_id)
            previous.stop()
        return session

    def end(self, meeting_id: str, session: MeetingSession) -> None:
        """Deregister a session and make sure its worker has finalized."""
        with self._lock:
            if self._sessions.get(meeting_id) is session:
                self._sessions.pop(meeting_id, None)
        session.stop()

    def has_active(self) -> bool:
        """True while at least one bridge meeting is streaming (worker gate)."""
        with self._lock:
            return bool(self._sessions)

    def has_decoding(self) -> bool:
        """True while at least one meeting actually decodes (worker gate)."""
        with self._lock:
            return any(session.transcribe for session in self._sessions.values())

    def set_transcribe(self, meeting_id: str, enabled: bool) -> bool:
        """Flip realtime decoding of a running meeting; False when unknown."""
        with self._lock:
            session = self._sessions.get(meeting_id)
        if session is None:
            return False
        session.set_transcribe(enabled)
        return True

    def stop(
        self,
        *,
        meeting_id: str | None = None,
        job_id: str | None = None,
        reason: str = "manual",
    ) -> MeetingSession | None:
        """Request an early finalize of a running meeting (manual stop).

        Found either by the websocket ``meeting_id`` (Jitsi page) or by the
        recording's ``job_id`` (job page); None when no such meeting is live.
        The websocket watcher closes Jigasi's socket and the handler finalizes
        the job — callers may wait for the finalize via ``wait_finished()``.
        """
        with self._lock:
            if meeting_id is not None:
                session = self._sessions.get(meeting_id)
            else:
                session = next(
                    (item for item in self._sessions.values() if item.job.id == job_id),
                    None,
                )
        if session is not None:
            session.request_stop(reason)
        return session

    def status(self) -> dict:
        """Rich snapshot of active meetings for the Jitsi page."""
        with self._lock:
            sessions = list(self._sessions.values())
        meetings = [session.status() for session in sessions]
        return {"active": bool(meetings), "meetings": meetings}

    def _budget_loop(self) -> None:
        """Keep the total queued backlog within the configured budget (1 Hz).

        A saturated decoder drains its backlog after the meetings close; the
        per-stream caps alone would still allow hours of queued audio across
        hundreds of streams. The budget bounds that drain to minutes: realtime
        captions lose the oldest frames, recordings stay intact. The budget is
        re-read from the settings on every pass, so a runtime change applies
        immediately (0 disables the sweeper).
        """
        while True:
            time.sleep(1.0)
            try:
                budget = self._settings.bridge_total_pending_max_sec
                if budget > 0:
                    self._enforce_budget(budget)
            except Exception:  # noqa: BLE001 — the sweeper must never die
                log.exception("jitsi bridge: сбой надзора за бюджетом бэклога")

    def _enforce_budget(self, budget_sec: float) -> float:
        """One sweeper pass over the live sessions; returns dropped seconds."""
        with self._lock:
            sessions = list(self._sessions.values())
        excess = sum(session.pending_seconds() for session in sessions) - budget_sec
        if excess <= 0:
            return 0.0
        dropped = 0.0
        for session in sorted(sessions, key=lambda item: item.pending_seconds(), reverse=True):
            if excess <= 0:
                break
            removed = session.drop_backlog(excess)
            dropped += removed
            excess -= removed
        return dropped
