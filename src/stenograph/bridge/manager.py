"""Registry of active Jigasi bridge sessions.

Jigasi dials one websocket per room and reuses the same connection id when it
reconnects — the manager replaces a stale session when the same meeting id
connects again.
"""

from __future__ import annotations

import logging
import threading
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
