"""External ASR service: OpenAI-compatible endpoints over our engines."""

from __future__ import annotations

import base64
import io
import threading
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeEngine, speech_audio
from stenograph.asr_service import build_default_engine, create_app
from stenograph.config import Settings
from stenograph.domain.errors import JobCancelled
from stenograph.engines.base import AsrResult
from stenograph.engines.whisper import FasterWhisperEngine


class FakeWindowEngine(FakeEngine):
    """FakeEngine plus the realtime-window methods of the whisper engine."""

    def transcribe_batch(self, windows, *, language=None):
        """Echo each window's duration so callers can see which audio arrived."""
        return [
            [(0.0, round(len(window) / 16000.0, 2), f"окно {index}")]
            for index, window in enumerate(windows)
        ]


def _wav_bytes(seconds: float = 1.0) -> bytes:
    """A small 16 kHz mono WAV payload for the upload."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes((speech_audio(seconds) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


@pytest.fixture()
def client(tmp_path: Path):
    """TestClient with a recording engine factory; yields (client, seen)."""
    settings = Settings(data_dir=tmp_path, engine="whisper")
    settings.ensure_dirs()
    seen: list[str] = []

    def build(name: str):
        if name not in {"whisper", "moss"}:
            raise KeyError(f"unknown ASR engine '{name}'; available: ['moss', 'whisper']")
        seen.append(name)
        return FakeWindowEngine() if name == "whisper" else FakeEngine()

    with TestClient(create_app(settings=settings, engine_factory=build)) as test_client:
        yield test_client, seen


def test_health_and_models(client) -> None:
    """The service advertises both engines."""
    test_client, _ = client
    health = test_client.get("/health").json()
    assert health["status"] == "ok"
    ids = [item["id"] for item in test_client.get("/v1/models").json()["data"]]
    assert "whisper" in ids and "moss" in ids and "whisper-live" in ids


def test_transcribe_json_uses_the_requested_engine(client) -> None:
    """POST /v1/audio/transcriptions routes to the requested engine."""
    test_client, seen = client
    response = test_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", _wav_bytes(), "audio/wav")},
        data={"model": "moss", "language": "ru"},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "раз\nдва"
    assert seen == ["moss"], "движок должен выбираться по параметру model"


def test_transcribe_verbose_json_carries_segments(client) -> None:
    """verbose_json returns OpenAI-style segments with timings."""
    test_client, _ = client
    response = test_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", _wav_bytes(), "audio/wav")},
        data={"model": "whisper", "response_format": "verbose_json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["language"] == "ru"
    assert body["duration"] == 2.0
    assert [segment["text"] for segment in body["segments"]] == ["раз", "два"]
    assert body["segments"][0]["start"] == 0.0


def test_default_model_falls_back_to_settings_engine(client) -> None:
    """Without model= the service uses MEETSCRIBE_ENGINE."""
    test_client, seen = client
    response = test_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", _wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 200
    assert seen[-1] == "whisper"


def test_unknown_model_is_404(client) -> None:
    """An unknown engine name maps to 404 (like OpenAI's model_not_found)."""
    test_client, _ = client
    response = test_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", _wav_bytes(), "audio/wav")},
        data={"model": "nope"},
    )
    assert response.status_code == 404


def test_realtime_windows_roundtrip(client) -> None:
    """Windows decode to audio and come back as word tuples."""
    test_client, seen = client
    windows = [
        base64.b64encode(_wav_bytes(0.3)).decode("ascii"),
        base64.b64encode(_wav_bytes(0.5)).decode("ascii"),
    ]
    response = test_client.post(
        "/v1/realtime/windows",
        json={"model": "whisper", "language": "ru", "windows": windows},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["windows"] == [[[0.0, 0.3, "окно 0"]], [[0.0, 0.5, "окно 1"]]]
    assert seen == ["whisper"]


def test_realtime_windows_requires_a_window_capable_engine(client) -> None:
    """Moss has no window methods — the endpoint says so explicitly."""
    test_client, _ = client
    response = test_client.post(
        "/v1/realtime/windows",
        json={
            "model": "moss",
            "windows": [base64.b64encode(_wav_bytes(0.3)).decode("ascii")],
        },
    )
    assert response.status_code == 400


def test_metrics_endpoint_reports_engines(client) -> None:
    """GET /metrics shows the loaded engines (the monitor proxies it)."""
    test_client, _ = client
    window = base64.b64encode(_wav_bytes(0.3)).decode("ascii")
    test_client.post("/v1/realtime/windows", json={"model": "whisper", "windows": [window]})
    data = test_client.get("/metrics").json()
    assert "system" in data and "models" in data
    assert data["engines_loaded"] == ["whisper"]
    assert "llm" not in data  # that section belongs to the main server


def test_metrics_reports_copy_counts(client, monkeypatch) -> None:
    """Строки моделей несут счётчик копий: Монитор покажет «копий: N из M»."""
    test_client, _ = client
    monkeypatch.setattr(
        "stenograph.asr_service.metrics.snapshot",
        lambda **kwargs: {
            "system": {},
            "models": [{"engine": "whisper", "model": "fake-model"}],
        },
    )
    row = test_client.get("/metrics").json()["models"][0]
    assert row["busy"] is False
    assert row["copies"] == 1 and row["copies_limit"] == 1


def test_unload_endpoint_reports_a_list(client) -> None:
    """POST /models/unload answers with the unloaded names (may be empty)."""
    test_client, _ = client
    data = test_client.post("/models/unload").json()
    assert data == {"unloaded": []}


def test_default_engine_maps_the_live_alias(tmp_path) -> None:
    """The realtime alias builds the LIVE model, not the file-pass one."""
    settings = Settings(data_dir=tmp_path, live_model="large-v3-turbo")
    engine = build_default_engine("whisper-live", settings)
    assert isinstance(engine, FasterWhisperEngine)
    assert engine.model_id == "large-v3-turbo"


def test_cancel_aborts_a_running_transcription(tmp_path: Path) -> None:
    """POST /v1/transcriptions/cancel обрывает идущую генерацию по request_id."""
    settings = Settings(data_dir=tmp_path, engine="whisper")
    settings.ensure_dirs()
    started = threading.Event()

    class SlowEngine:
        """«Долгая» модель: считает, пока не отменят (или до таймаута)."""

        name = "slow"

        def transcribe(
            self,
            audio_path,
            options,
            *,
            on_progress=None,
            on_segment=None,
            is_cancelled=None,
            pause_gate=None,
        ):
            started.set()
            for _ in range(300):  # до 15 с, если отмену не пробросить
                if is_cancelled is not None and is_cancelled():
                    raise JobCancelled()
                time.sleep(0.05)
            return AsrResult(language="ru", language_probability=0.0, duration=1.0)

    app = create_app(settings=settings, engine_factory=lambda name: SlowEngine())
    with TestClient(app) as test_client:
        result: dict = {}

        def submit() -> None:
            result["response"] = test_client.post(
                "/v1/audio/transcriptions",
                files={"file": ("speech.wav", _wav_bytes(), "audio/wav")},
                data={"model": "whisper", "request_id": "req-cancel-1"},
            )

        worker = threading.Thread(target=submit)
        worker.start()
        assert started.wait(5), "транскрибация не началась"
        cancel = test_client.post(
            "/v1/transcriptions/cancel", json={"request_id": "req-cancel-1"}
        )
        assert cancel.status_code == 200
        assert cancel.json() == {"cancelled": True}
        worker.join(timeout=10)
        assert not worker.is_alive(), "отмена не оборвала генерацию"
        assert result["response"].status_code == 409
        # Повторная отмена — no-op: запрос уже завершён и снят с реестра.
        again = test_client.post(
            "/v1/transcriptions/cancel", json={"request_id": "req-cancel-1"}
        )
        assert again.json() == {"cancelled": False}


def test_cancel_of_unknown_request_is_false(client) -> None:
    """Отмена неизвестного/уже завершённого запроса — безвредный false."""
    test_client, _ = client
    response = test_client.post(
        "/v1/transcriptions/cancel", json={"request_id": "nope"}
    )
    assert response.status_code == 200
    assert response.json() == {"cancelled": False}

