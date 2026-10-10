"""Jigasi bridge tests: wire protocol, websocket streaming, persistence."""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fakes import FakeEngine, PositionTranscriber, encoded_chunk, speech_audio
from stenograph.api.app import create_app
from stenograph.bridge.manager import BridgeManager
from stenograph.bridge.protocol import FrameError, is_eof, normalize_language, parse_frame
from stenograph.bridge.session import MeetingSession
from stenograph.config import Settings
from stenograph.events import EventBus
from stenograph.live.manager import LiveManager
from stenograph.live.streamer import SAMPLE_RATE
from stenograph.service import TranscriptionService
from stenograph.storage import JobRepository
from test_live_batch import BatchRecorder


def _wait_until(condition, timeout: float = 15.0, pause: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(pause)
    return condition()


def frame(participant_id: str, language: str, audio: np.ndarray) -> bytes:
    """Build one wire frame the way Jigasi's WhisperWebsocket does."""
    header = f"{participant_id}|{language}".encode().ljust(60, b"\x00")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    return header + pcm.tobytes()


def test_parse_frame_roundtrip() -> None:
    """A Jigasi frame round-trips: header + int16 PCM back to float audio."""
    audio = encoded_chunk(3)
    data = frame("abcd-1234", "ru-RU", audio)
    participant_id, language, decoded = parse_frame(data)
    assert participant_id == "abcd-1234"
    assert language == "ru"
    assert decoded.shape == audio.shape
    assert np.allclose(decoded, audio, atol=1e-3)


def test_frame_edge_cases_and_languages() -> None:
    """EOF detection, malformed frames and language normalisation."""
    assert is_eof(b"\x00")
    assert not is_eof(b"\x00\x00")
    assert not is_eof(b"")
    with pytest.raises(FrameError):
        parse_frame(b"short")
    with pytest.raises(FrameError):
        parse_frame(b"|ru" + b"\x00" * 57 + b"\x01\x02")
    assert normalize_language("en-US") == "en"
    assert normalize_language("ru") == "ru"
    assert normalize_language("multi") is None
    assert normalize_language("") is None
    assert normalize_language("auto") is None


def _make_stack(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(settings, repo, bus, engine_factory=lambda n, s: FakeEngine())
    bridge = BridgeManager(
        settings, repo, bus, transcriber_factory=lambda language: PositionTranscriber()
    )
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))
    return client, repo


def test_bridge_streams_captions_and_persists(tmp_path: Path) -> None:
    """Captions flow back over the websocket; the session lands as a done job."""
    client, repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-1") as websocket:
        for index in range(8):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))

        # The worker ticks shortly after the frames arrive, but commits finals
        # only when more audio shows up or the session is flushed. So: wait
        # for the first caption, then send EOF (Jigasi's flush), then finals.
        messages = []
        for _ in range(30):
            message = websocket.receive_json()
            messages.append(message)
            if message["type"] in ("partial", "final"):
                break
        assert any(item["type"] == "partial" for item in messages), messages[:5]
        assert any("фраза" in item["text"] for item in messages)

        websocket.send_bytes(b"\x00")
        for _ in range(30):
            message = websocket.receive_json()
            messages.append(message)
            if message["type"] == "final":
                break

        final = messages[-1]
        assert final["type"] == "final"
        assert final["participant_id"] == "p1"
        assert "фраза" in final["text"]

    deadline = time.monotonic() + 10
    jobs = []
    while time.monotonic() < deadline:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        if jobs and jobs[0].status == "done":
            break
        time.sleep(0.05)

    assert jobs, "job не создан"
    job = jobs[0]
    assert job.status == "done", job.error
    assert job.segments
    assert any("фраза" in segment.text for segment in job.segments)
    assert job.text.startswith("Спикер 1: ")
    assert job.meta["participants"] == {"p1": "Спикер 1"}
    audio_file = tmp_path / "jitsi" / job.id / "p1.wav"
    assert audio_file.is_file()
    assert audio_file.stat().st_size > 44


def test_jitsi_status_reports_live_meetings(tmp_path: Path) -> None:
    """The Jitsi tab sees live meetings: participants, counters, audio seconds."""
    client, _repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-status") as websocket:
        for index in range(8):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))
        for _ in range(30):
            message = websocket.receive_json()
            if message["type"] in ("partial", "final"):
                break

        status = client.get("/api/jitsi/status").json()
        assert status["active"] is True
        meeting = status["meetings"][0]
        assert meeting["meeting_id"] == "room-status"
        assert meeting["job_id"]
        assert meeting["duration_sec"] >= 0
        assert meeting["participants"], meeting
        first = meeting["participants"][0]
        assert first["label"] == "Спикер 1"
        assert first["audio_sec"] > 0
        assert first["last_frame_sec"] is not None
        assert meeting["stopping"] is False
        assert meeting["idle_stop_sec"] == 600.0
        assert meeting["silence_sec"] >= 0

    assert _wait_until(
        lambda: client.get("/api/jitsi/status").json()["active"] is False
    ), "снятая встреча должна исчезнуть из статуса"


def test_jitsi_reprocess_merges_participants(tmp_path: Path) -> None:
    """A finished meeting is re-transcribed per speaker file and merged."""
    client, repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-improve") as websocket:
        for index in range(8):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))
            websocket.send_bytes(frame("p2", "ru-RU", encoded_chunk(40 + index)))
        websocket.send_bytes(b"\x00")

    def meeting() -> Any:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        return jobs[0] if jobs else None

    assert _wait_until(
        lambda: meeting() is not None and meeting().status.value == "done", timeout=10.0
    ), meeting()

    response = client.post(f"/api/jobs/{meeting().id}/reprocess")
    assert response.status_code == 201, response.text
    child_id = response.json()["id"]
    child: dict = {}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        child = client.get(f"/api/jobs/{child_id}").json()
        if child["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert child["status"] == "done", child
    speakers = {segment["speaker"] for segment in child["segments"]}
    assert speakers == {"Спикер 1", "Спикер 2"}, speakers
    assert child["meta"]["source_kind"] == "jitsi"
    assert "Спикер 1:" in child["text"] and "Спикер 2:" in child["text"], child["text"]


def test_jitsi_reprocess_rerun_repoints_parent(tmp_path: Path) -> None:
    """A rerun creates a fresh child; the recording points at the latest one.

    Every child keeps its backlink to the recording, older attempts stay in
    the history as separate jobs.
    """
    client, repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-rerun") as websocket:
        for index in range(6):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))
        websocket.send_bytes(b"\x00")

    def meeting() -> Any:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        return jobs[0] if jobs else None

    assert _wait_until(
        lambda: meeting() is not None and meeting().status.value == "done", timeout=10.0
    )

    first = client.post(f"/api/jobs/{meeting().id}/reprocess")
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]
    assert _wait_until(
        lambda: (repo.get(first_id) or meeting()).status.value == "done", timeout=10.0
    )

    second = client.post(f"/api/jobs/{meeting().id}/reprocess")
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    assert second_id != first_id

    parent = repo.get(meeting().id)
    assert parent.meta["reprocess_job"] == second_id, parent.meta
    first_child = repo.get(first_id)
    assert first_child.meta["parent"] == meeting().id


def test_bridge_auto_chains_reprocess(tmp_path: Path) -> None:
    """Ending a meeting hands the recording to the quality re-pass."""
    handed: list[str] = []

    def reprocess(job: Any) -> None:
        handed.append(job.id)
        return None

    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(settings, repo, bus, engine_factory=lambda n, s: FakeEngine())
    bridge = BridgeManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        reprocess=reprocess,
        auto_reprocess=True,
    )
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))

    with client.websocket_connect("/ws/room-chain") as websocket:
        for index in range(8):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))
        websocket.send_bytes(b"\x00")

    assert _wait_until(lambda: len(handed) == 1, timeout=10.0), handed


def test_bridge_language_override(tmp_path: Path) -> None:
    """MEETSCRIBE_BRIDGE_LANGUAGE overrides whatever Jigasi claims per frame."""
    delivered: list[str | None] = []

    def factory(language: str | None) -> Any:
        delivered.append(language)
        return PositionTranscriber()

    settings = Settings(
        data_dir=tmp_path,
        live_step_sec=0.4,
        live_max_window_sec=10.0,
        bridge_language="ru",
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(settings, repo, bus, engine_factory=lambda n, s: FakeEngine())
    bridge = BridgeManager(settings, repo, bus, transcriber_factory=factory)
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))

    with client.websocket_connect("/ws/room-lang") as websocket:
        websocket.send_bytes(frame("p1", "en-US", encoded_chunk(0)))
        websocket.send_bytes(frame("p1", "en-US", encoded_chunk(1)))

        def participant_language() -> str | None:
            meetings = client.get("/api/jitsi/status").json()["meetings"]
            if not meetings or not meetings[0]["participants"]:
                return None
            return meetings[0]["participants"][0]["language"]

        assert _wait_until(lambda: participant_language() == "ru", timeout=5.0), (
            participant_language()
        )

    assert delivered and delivered[0] == "ru", delivered
    jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
    assert jobs and jobs[0].language == "ru", jobs


def test_bridge_second_participant_and_reconnect(tmp_path: Path) -> None:
    """A reconnect with the same meeting id replaces the stale session."""
    client, repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-x") as websocket:
        websocket.send_bytes(frame("p1", "ru", encoded_chunk(0)))
    with client.websocket_connect("/ws/room-x") as websocket:
        websocket.send_bytes(frame("p2", "ru", encoded_chunk(1)))
        websocket.send_bytes(b"\x00")

    deadline = time.monotonic() + 10
    jobs = []
    while time.monotonic() < deadline:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        if jobs and all(job.status == "done" for job in jobs):
            break
        time.sleep(0.05)

    assert len(jobs) == 2, [(job.id, job.status) for job in jobs]
    second = jobs[0]
    assert second.status == "done"
    assert second.meta["participants"] == {"p2": "Спикер 1"}


def _make_pooled_stack(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(settings, repo, bus, engine_factory=lambda n, s: FakeEngine())
    batch = BatchRecorder()
    live = LiveManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        batch_factory=lambda language: batch,
    )
    bridge = BridgeManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        pool=live,
    )
    client = TestClient(create_app(settings=settings, service=service, live=live, bridge=bridge))
    return client, repo, batch


def test_bridge_participants_join_the_batched_pool(tmp_path: Path) -> None:
    """Two meetings' participants are decoded in ONE shared batched pass.

    Falsification: with per-meeting ticking each participant runs its own
    engine call — the recorded batch sizes never reach two.
    """
    client, repo, batch = _make_pooled_stack(tmp_path)
    with (
        client.websocket_connect("/ws/room-p") as first,
        client.websocket_connect("/ws/room-q") as second,
    ):
        for index in range(8):
            first.send_bytes(frame("p1", "ru", encoded_chunk(index)))
            second.send_bytes(frame("p2", "ru", encoded_chunk(40 + index)))

        assert _wait_until(lambda: max(batch.sizes, default=0) >= 2), batch.sizes

        got = {"first": False, "second": False}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(got.values()):
            for key, socket in (("first", first), ("second", second)):
                if got[key]:
                    continue
                message = socket.receive_json()
                if message["type"] in ("partial", "final") and "фраза" in message["text"]:
                    got[key] = True
        assert all(got.values()), got

        first.send_bytes(b"\x00")
        second.send_bytes(b"\x00")

    deadline = time.monotonic() + 15
    jobs = []
    while time.monotonic() < deadline:
        jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
        if len(jobs) == 2 and all(job.status == "done" for job in jobs):
            break
        time.sleep(0.05)
    assert len(jobs) == 2, [(job.id, job.status) for job in jobs]
    assert all(job.status == "done" for job in jobs), [job.error for job in jobs]
    assert all(job.segments for job in jobs)


def test_bridge_feed_never_waits_for_the_serve_guard(tmp_path: Path) -> None:
    """A new participant may arrive while a serve round is in flight.

    ``feed`` (the event loop / websocket handler) must not block on the decode
    pool's serve guard — the pooled decode thread holds that guard while it
    applies results and waits for this session's lock, so any path from the
    session lock into the guard wedges the whole server (recall the 40-person
    Jitsi hang: py-spy showed exactly this inversion).

    Falsification: with ``adopt`` taken under the session lock (the original
    code) this ``feed`` blocks — the guard is held here by the test — and the
    wait below times out.
    """
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    batch = BatchRecorder()
    live = LiveManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        batch_factory=lambda language: batch,
    )
    session = MeetingSession(
        "room-lock",
        settings,
        repo,
        bus,
        lambda language: PositionTranscriber(),
        pool=live,
    )
    guard = live._serve_guard
    guard.acquire()  # simulate a long serve round in flight
    try:
        done = threading.Event()

        def feed_new_participant() -> None:
            session.feed("late", "ru", encoded_chunk(0))
            done.set()

        worker = threading.Thread(target=feed_new_participant, daemon=True)
        worker.start()
        assert done.wait(5.0), (
            "feed заблокировался на serve-guard — вернулась инверсия S→G (дедлок сервера)"
        )
        worker.join(timeout=5.0)
    finally:
        guard.release()
    session.stop()


def test_bridge_keeps_real_pauses_on_the_meeting_timeline(tmp_path: Path) -> None:
    """Паузы между репликами сохраняются: дорожка идёт по часам встречи.

    Jigasi присылает только речь (в 5-минутной встрече файл может быть 39 с),
    поэтому без явной вставки тишины дорожки участников сжаты и «единую
    запись» из них честно не собрать. Кадр ставится на позицию
    (приход − длительность), разрыв заполняется тишиной.
    """
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    session = MeetingSession(
        "room-timeline", settings, repo, EventBus(), lambda language: PositionTranscriber()
    )
    assert session.job.meta["audio_timeline"] == "realtime"

    session.feed("p1", "ru", speech_audio(1.0))
    session._started_monotonic -= 4.0  # в часах встречи прошла пауза 4 с
    session.feed("p1", "ru", speech_audio(1.0))
    session.stop()

    path = Path(session.job.meta["audio"]["Спикер 1"])
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    seconds = data.size / SAMPLE_RATE
    assert 3.5 < seconds < 4.5, f"пауза потеряна: файл {seconds:.2f} с вместо ~4"
    assert not data[int(1.2 * SAMPLE_RATE) : int(2.8 * SAMPLE_RATE)].any(), "в паузе не тишина"
    tail = data[int(3.6 * SAMPLE_RATE) : int(3.9 * SAMPLE_RATE)]
    assert (np.abs(tail) > 300).any(), "вторая реплика не на своём месте по часам"


def test_bridge_catches_the_tracker_up_when_decoding_turns_on(tmp_path: Path) -> None:
    """Включение распознавания на ходу не сдвигает часы: пропуск — тишиной.

    Record-only встреча пишет файл по часам встречи, трекер же молчит — при
    переключении на лету он должен сначала получить тишину за прошедшее
    время, иначе времена реплик считались бы от точки включения.
    """
    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    session = MeetingSession(
        "room-catchup",
        settings,
        repo,
        EventBus(),
        lambda language: PositionTranscriber(),
        transcribe=False,
    )
    session._pump_participants = lambda: None  # детерминизм: pending читаем сами

    session.feed("p1", "ru", speech_audio(1.0))  # файл: [0..1], распознавание выключено
    session._started_monotonic -= 4.0
    session.set_transcribe(True)  # включили во время встречи
    session.feed("p1", "ru", speech_audio(1.0))

    with session._lock:
        chunks = list(session._pending["p1"])
    seconds = [chunk.size / SAMPLE_RATE for chunk in chunks]
    total = sum(seconds)
    assert 3.7 < total < 4.4, f"трекер не догнал часы встречи: {seconds}"
    assert 0.7 < seconds[0] < 1.4, f"первым в трекер идёт догон тишиной: {seconds}"
    assert not chunks[0].any() and chunks[-1].any()
    session.stop()


def test_jitsi_stop_api_finalizes_the_meeting(tmp_path: Path) -> None:
    """Остановка встречи из UI: запись финализируется, сокет Jigasi закрывается.

    Сессия завершается как обычное окончание встречи (задача done, дорожки,
    авто-улучшение), причина пишется в meta, а websocket закрывается — иначе
    Jigasi остался бы в комнате и продолжал слать субтитры в пустоту.
    """
    handed: list[str] = []

    def reprocess(job: Any) -> None:
        handed.append(job.id)
        return None

    settings = Settings(data_dir=tmp_path, live_step_sec=0.4, live_max_window_sec=10.0)
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    bus = EventBus()
    service = TranscriptionService(settings, repo, bus, engine_factory=lambda n, s: FakeEngine())
    bridge = BridgeManager(
        settings,
        repo,
        bus,
        transcriber_factory=lambda language: PositionTranscriber(),
        reprocess=reprocess,
        auto_reprocess=True,
    )
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))

    with client.websocket_connect("/ws/room-stop-now") as websocket:
        for index in range(8):
            websocket.send_bytes(frame("p1", "ru-RU", encoded_chunk(index)))
        assert _wait_until(
            lambda: bool(client.get("/api/jitsi/status").json()["meetings"]), timeout=5.0
        )

        stopped = client.post("/api/jitsi/stop", data={"meeting_id": "room-stop-now"})
        assert stopped.status_code == 200, stopped.text
        payload = stopped.json()
        assert payload["status"] == "done", payload
        assert payload["meta"]["stop_reason"] == "manual", payload["meta"]
        assert "вручную" in payload["message"], payload["message"]

        # сервер закрывает сокет: читаем до кадра закрытия
        with pytest.raises(WebSocketDisconnect):
            for _ in range(50):
                websocket.receive_json()

    assert _wait_until(lambda: bool(handed), timeout=10.0), handed
    assert _wait_until(
        lambda: client.get("/api/jitsi/status").json()["active"] is False, timeout=5.0
    )


def test_jitsi_stop_api_accepts_the_job_id(tmp_path: Path) -> None:
    """Кнопка на странице задачи шлёт job_id — остановка находится и так."""
    client, _repo = _make_stack(tmp_path)
    with client.websocket_connect("/ws/room-stop-job") as websocket:
        websocket.send_bytes(frame("p1", "ru", encoded_chunk(0)))
        assert _wait_until(
            lambda: bool(client.get("/api/jitsi/status").json()["meetings"]), timeout=5.0
        )
        job_id = client.get("/api/jitsi/status").json()["meetings"][0]["job_id"]

        stopped = client.post("/api/jitsi/stop", data={"job_id": job_id})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["id"] == job_id
        assert stopped.json()["status"] == "done"

        with pytest.raises(WebSocketDisconnect):
            for _ in range(50):
                websocket.receive_json()


def test_jitsi_stop_api_validation(tmp_path: Path) -> None:
    """Без идентификатора — 400; несуществующая встреча — 404."""
    client, _repo = _make_stack(tmp_path)
    assert client.post("/api/jitsi/stop").status_code == 400
    assert client.post("/api/jitsi/stop", data={"meeting_id": "нет-такой"}).status_code == 404


def test_jitsi_idle_watchdog_stops_a_silent_meeting(tmp_path: Path) -> None:
    """Тишина дольше порога — встреча завершается сама.

    Зомби-клиент (забытая вкладка с выключенным микрофоном) держит комнату
    живой, но речи в ней нет: watchdog сам закрывает запись, и в задаче это
    видно по stop_reason=idle. Кадры речи сбрасывают таймер.

    Фальсификация: без вызова watchdog'а из pump-цикла сессия остаётся
    running до EOF, и ожидание ниже падает по таймауту.
    """
    settings = Settings(
        data_dir=tmp_path,
        live_step_sec=0.4,
        live_max_window_sec=10.0,
        jitsi_idle_stop_sec=1.5,
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda n, s: FakeEngine()
    )
    bridge = BridgeManager(
        settings,
        repo,
        service.bus,
        transcriber_factory=lambda language: PositionTranscriber(),
    )
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))

    with client.websocket_connect("/ws/room-idle") as websocket:
        websocket.send_bytes(frame("p1", "ru", encoded_chunk(0)))
        assert _wait_until(
            lambda: bool(client.get("/api/jitsi/status").json()["meetings"]), timeout=5.0
        )
        meeting = client.get("/api/jitsi/status").json()["meetings"][0]
        assert meeting["idle_stop_sec"] == 1.5
        assert meeting["stopping"] is False
        job_id = meeting["job_id"]

        # речи больше нет — watchdog должен запросить остановку сам
        def stopping() -> bool:
            meetings_now = client.get("/api/jitsi/status").json()["meetings"]
            return bool(meetings_now) and meetings_now[0]["stopping"] is True

        assert _wait_until(stopping, timeout=8.0), "watchdog не остановил молчащую встречу"

        # сервер закрывает сокет; читаем до кадра закрытия
        with pytest.raises(WebSocketDisconnect):
            for _ in range(50):
                websocket.receive_json()

    assert _wait_until(
        lambda: client.get("/api/jitsi/status").json()["active"] is False, timeout=5.0
    ), "сессия должна покинуть реестр после закрытия сокета"
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done", job
    assert job["meta"]["stop_reason"] == "idle", job["meta"]
    assert "без речи" in job["message"], job["message"]


def test_jitsi_idle_watchdog_can_be_disabled(tmp_path: Path) -> None:
    """Порог 0 выключает авто-стоп: молчащая встреча пишется до конца."""
    settings = Settings(
        data_dir=tmp_path,
        live_step_sec=0.4,
        live_max_window_sec=10.0,
        jitsi_idle_stop_sec=0.0,
    )
    settings.ensure_dirs()
    repo = JobRepository(settings.db_path)
    service = TranscriptionService(
        settings, repo, EventBus(), engine_factory=lambda n, s: FakeEngine()
    )
    bridge = BridgeManager(
        settings,
        repo,
        service.bus,
        transcriber_factory=lambda language: PositionTranscriber(),
    )
    client = TestClient(create_app(settings=settings, service=service, bridge=bridge))

    with client.websocket_connect("/ws/room-idle-off") as websocket:
        websocket.send_bytes(frame("p1", "ru", encoded_chunk(0)))
        assert _wait_until(
            lambda: bool(client.get("/api/jitsi/status").json()["meetings"]), timeout=5.0
        )
        time.sleep(2.0)  # заведомо больше «тихого» порога соседнего теста
        status = client.get("/api/jitsi/status").json()
        assert status["active"] is True, "выключенный watchdog не должен останавливать встречу"
        assert status["meetings"][0]["stopping"] is False
        websocket.send_bytes(b"\x00")

    assert _wait_until(
        lambda: bool([job for job in repo.list_jobs() if job.kind == "jitsi"])
        and all(job.status == "done" for job in repo.list_jobs() if job.kind == "jitsi"),
        timeout=10.0,
    )
    jobs = [job for job in repo.list_jobs() if job.kind == "jitsi"]
    assert "stop_reason" not in jobs[0].meta, jobs[0].meta  # обычный конец — без причины
