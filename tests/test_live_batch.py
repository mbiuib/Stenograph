"""Batched live decoding: several sessions served by one inference pass."""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeEngine, PositionTranscriber, encoded_chunk, speech_audio
from stenograph.api.app import create_app
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.live.manager import LiveManager
from stenograph.live.streamer import StreamTracker, WindowWord
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository

TRACK_MIC = 0


class BatchRecorder:
    """Batch transcriber double: decodes each packed window independently."""

    def __init__(self) -> None:
        self.single = PositionTranscriber()
        self.sizes: list[int] = []

    def __call__(self, windows: list[np.ndarray]) -> list[list[WindowWord]]:
        """Decode every packed window and remember the batch sizes."""
        self.sizes.append(len(windows))
        return [self.single(window) for window in windows]


def _frame(track: int, index: int) -> bytes:
    pcm = (np.clip(encoded_chunk(index), -1.0, 1.0) * 32767.0).astype("<i2")
    return bytes([track]) + pcm.tobytes()


def _make_stack(tmp_path: Path, batch_factory):
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(
        settings, repo, bus, engine_factory=lambda name, prepared: FakeEngine()
    )
    live = LiveManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        batch_factory=batch_factory,
    )
    return service, live


def _wait_until(condition, timeout: float = 15.0, pause: float = 0.1) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return True
        sleep(pause)
    return condition()


def _wait_done(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = monotonic() + timeout
    job: dict = {}
    while monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        sleep(0.2)
    return job


# -- streamer window capping ---------------------------------------------------


def test_pending_window_cap_keeps_the_rest_buffered() -> None:
    """A capped window marks only its prefix; the remainder stays as lag."""
    tracker = StreamTracker(
        lambda audio: [],
        on_final=lambda start, end, text: None,
        on_partial=lambda text: None,
        step_sec=0.4,
    )
    tracker.feed(speech_audio(3.0))

    window = tracker.pending_window(cap_sec=1.0)

    assert window is not None
    assert abs(window.size / 16000 - 1.0) < 0.02
    tracker.apply_words([])
    assert tracker.buffer_seconds > 1.8  # the remainder stays buffered
    assert tracker.lag_seconds > 0.5  # and still counts as lag for the next step


def test_apply_window_commits_one_pass_and_holds_the_edge() -> None:
    """A batched window commits up to cap − hold in a single pass.

    Falsification: through the LocalAgreement path (``apply_words`` with an
    empty previous hypothesis) this very call submits nothing — the first
    pass only records ``prev`` — so no text and no drain would appear here.
    """
    finals: list[tuple[float, float, str]] = []
    tracker = StreamTracker(
        lambda audio: [],
        on_final=lambda start, end, text: finals.append((start, end, text)),
        on_partial=lambda text: None,
        step_sec=0.4,
    )
    tracker.feed(speech_audio(6.0))

    window = tracker.pending_window(cap_sec=3.0)
    assert window is not None
    words = [(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(6)]  # 0..3 s
    tracker.apply_window(words, hold_sec=0.5)

    # committed: every word ending at or before 3.0 − 0.5 = 2.5 s
    assert finals, "однораундовый коммит обязан отдать текст сразу"
    assert finals[0][2].split() == ["w0", "w1", "w2", "w3", "w4"]
    assert finals[0][1] <= 2.5 + 1e-6
    # the held edge stays buffered and keeps the stream drained by ~cap − hold
    assert tracker.buffer_seconds > 3.5
    assert abs(tracker.buffer_seconds - (6.0 - 2.4)) < 0.05


# -- manager-level batching ----------------------------------------------------


def test_batch_round_serves_two_sessions_in_one_pass(tmp_path: Path) -> None:
    """Two live sessions are transcribed from the same batched engine call."""
    batch = BatchRecorder()
    service, live = _make_stack(tmp_path, batch_factory=lambda language: batch)
    client = TestClient(create_app(settings=service.settings, service=service, live=live))

    with (
        client.websocket_connect("/ws/live") as first,
        client.websocket_connect("/ws/live") as second,
    ):
        first.send_json({"type": "start", "tracks": ["mic"]})
        first_id = first.receive_json()["job_id"]
        second.send_json({"type": "start", "tracks": ["mic"]})
        second_id = second.receive_json()["job_id"]

        for step in range(8):
            first.send_bytes(_frame(TRACK_MIC, step))
            second.send_bytes(_frame(TRACK_MIC, 40 + step))
            sleep(0.2)

        # both sessions must end up inside at least one shared batch
        assert _wait_until(lambda: max(batch.sizes, default=0) >= 2), batch.sizes

        def both_have_text() -> bool:
            one = client.get(f"/api/jobs/{first_id}").json()
            two = client.get(f"/api/jobs/{second_id}").json()
            return bool(one["segments"]) and bool(two["segments"])

        assert _wait_until(both_have_text), batch.sizes
        segments = client.get(f"/api/jobs/{first_id}").json()["segments"]
        assert any("фраза" in segment["text"] for segment in segments), segments

        first.send_json({"type": "stop"})
        second.send_json({"type": "stop"})

    assert _wait_done(client, first_id)["status"] == "done"
    assert _wait_done(client, second_id)["status"] == "done"
