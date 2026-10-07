"""In-process event bus for job updates.

Thread-safe pub/sub: worker threads publish, SSE/WebSocket endpoints and the
CLI subscribe. Event contract (JSON-ready dicts; the same contract will be
reused by the live mode and the Jitsi bridge):

- {"type": "status", "status": ..., "message": ..., "progress": int}
- {"type": "meta", "meta": {...}}
- {"type": "progress", "value": int, "message": str}
- {"type": "segment", "segment": {...}}
- {"type": "segments_replaced", "segments": [...]}
- {"type": "partial", "track": str, "speaker": str, "text": str}  # live: unstable tail
- {"type": "level", "track": str, "rms": float}                  # live: input level meter
- {"type": "done", "text": str, "meta": {...}}
- {"type": "error", "message": str}
- {"type": "cancelled"}
"""

from __future__ import annotations

import queue
import threading
from typing import Any


class EventBus:
    """Fan-out bus: one queue per subscriber, keyed by job id."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, job_id: str) -> queue.Queue[dict[str, Any]]:
        """Create a queue that will receive this job's future events."""
        channel: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(channel)
        return channel

    def unsubscribe(self, job_id: str, channel: queue.Queue[dict[str, Any]]) -> None:
        """Stop receiving events for a job."""
        with self._lock:
            channels = self._subscribers.get(job_id, [])
            if channel in channels:
                channels.remove(channel)
            if not channels:
                self._subscribers.pop(job_id, None)

    def publish(self, job_id: str, event: dict[str, Any]) -> None:
        """Publish an event to every current subscriber (never blocks)."""
        with self._lock:
            channels = list(self._subscribers.get(job_id, []))
        for channel in channels:
            channel.put(event)
