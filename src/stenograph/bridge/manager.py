"""Registry of active Jigasi bridge sessions.

Jigasi dials one websocket per room and reuses the same connection id when it
reconnects — the manager replaces a stale session when the same meeting id
connects again.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import Settings
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
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._bus = bus
        self._factory = transcriber_factory or default_transcriber_factory(settings)
        self._pool = pool  # shared live decode pool; None = per-meeting ticking
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

    def active(self) -> list[dict]:
        """Snapshot of active sessions (for the status endpoint)."""
        with self._lock:
            return [
                {"meeting_id": session.meeting_id, "job_id": session.job.id}
                for session in self._sessions.values()
            ]
