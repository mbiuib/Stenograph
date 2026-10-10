"""Client for the external ASR service: wire formats, errors, wiring."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from stenograph.asr_client import (
    AsrServiceError,
    RemoteAsrClient,
    decode_wav_b64,
    encode_wav_b64,
)
from stenograph.config import Settings
from stenograph.domain.errors import JobCancelled
from stenograph.engines import make_asr
from stenograph.engines.base import NoSpeechError, TranscribeOptions
from stenograph.engines.remote import RemoteAsrEngine
from stenograph.engines.whisper import FasterWhisperEngine
from stenograph.live import manager as live_manager


def _tone(seconds: float) -> np.ndarray:
    """A deterministic tone the codec tests can round-trip."""
    t = np.linspace(0.0, seconds, int(16000 * seconds), endpoint=False)
    return (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


def _settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(data_dir=tmp_path, **kwargs)


def test_wav_codec_roundtrip() -> None:
    """encode/decode is lossless at int16 precision."""
    audio = _tone(0.25)
    restored = decode_wav_b64(encode_wav_b64(audio))
    assert restored.shape == audio.shape
    assert np.allclose(restored, audio, atol=1e-4)


def test_transcribe_file_parses_verbose_json(tmp_path) -> None:
    """The file call posts multipart and builds AsrResult from verbose_json."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "language": "ru",
                "duration": 2.5,
                "segments": [
                    {"start": 0.0, "end": 1.2, "text": "привет", "speaker": None},
                    {"start": 1.2, "end": 2.5, "text": "мир", "speaker": "S1"},
                ],
            },
        )

    client = RemoteAsrClient("http://asr.test", transport=httpx.MockTransport(handler))
    audio_file = tmp_path / "speech.wav"
    audio_file.write_bytes(b"RIFF....")
    result = client.transcribe_file(audio_file, model="whisper", language="ru")
    assert seen["path"] == "/v1/audio/transcriptions"
    assert b'name="model"' in seen["body"] and b"whisper" in seen["body"]
    assert b"verbose_json" in seen["body"]
    assert result.language == "ru"
    assert result.duration == 2.5
    assert [(s.start, s.end, s.text, s.speaker) for s in result.segments] == [
        (0.0, 1.2, "привет", None),
        (1.2, 2.5, "мир", "S1"),
    ]


def test_transcribe_windows_roundtrip() -> None:
    """Windows are base64 WAVs; word tuples come back per window."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"windows": [[[0.0, 0.3, "раз"]], [[0.1, 0.5, "два"]]]}
        )

    client = RemoteAsrClient("http://asr.test", transport=httpx.MockTransport(handler))
    words = client.transcribe_windows([_tone(0.3), _tone(0.5)], model="whisper", language="ru")
    assert seen["path"] == "/v1/realtime/windows"
    assert seen["body"]["model"] == "whisper"
    assert seen["body"]["language"] == "ru"
    assert len(seen["body"]["windows"]) == 2
    assert len(decode_wav_b64(seen["body"]["windows"][0])) == 4800
    assert words == [[(0.0, 0.3, "раз")], [(0.1, 0.5, "два")]]


def test_service_error_becomes_exception(tmp_path) -> None:
    """Non-200 replies raise AsrServiceError with the body excerpt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="повалилось")

    client = RemoteAsrClient("http://asr.test", transport=httpx.MockTransport(handler))
    audio_file = tmp_path / "a.wav"
    audio_file.write_bytes(b"x")
    with pytest.raises(AsrServiceError, match="500"):
        client.transcribe_file(audio_file, model="whisper")


def test_make_asr_switches_on_the_backend(tmp_path) -> None:
    """Local → the registered engine; remote → the HTTP wrapper."""
    local = make_asr("whisper", _settings(tmp_path, asr_backend="local"))
    assert isinstance(local, FasterWhisperEngine)
    remote = make_asr(
        "whisper", _settings(tmp_path, asr_backend="remote", asr_url="http://svc.test")
    )
    assert isinstance(remote, RemoteAsrEngine)
    assert remote.name == "whisper"


def test_remote_engine_mirrors_engine_contract(tmp_path) -> None:
    """Segments replay into on_segment; empty reply → NoSpeechError; cancel → JobCancelled."""
    speech = {
        "language": "ru",
        "duration": 1.0,
        "segments": [{"start": 0.0, "end": 1.0, "text": "привет"}],
    }

    def make_client(payload: dict) -> RemoteAsrClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        return RemoteAsrClient("http://asr.test", transport=httpx.MockTransport(handler))

    audio_file = tmp_path / "a.wav"
    audio_file.write_bytes(b"RIFF....")
    settings = _settings(tmp_path, asr_url="http://asr.test")

    collected: list = []
    engine = RemoteAsrEngine("whisper", settings, client=make_client(speech))
    result = engine.transcribe(
        audio_file, TranscribeOptions(language="ru"), on_segment=collected.append
    )
    assert [s.text for s in result.segments] == ["привет"]
    assert [s.text for s in collected] == ["привет"]

    silent = RemoteAsrEngine(
        "whisper", settings, client=make_client({"language": "ru", "duration": 1.0, "segments": []})
    )
    with pytest.raises(NoSpeechError):
        silent.transcribe(audio_file, TranscribeOptions())

    cancelled = RemoteAsrEngine("whisper", settings, client=make_client(speech))
    with pytest.raises(JobCancelled):
        cancelled.transcribe(audio_file, TranscribeOptions(), is_cancelled=lambda: True)


def test_realtime_factories_go_remote(tmp_path, monkeypatch) -> None:
    """The live/Jitsi window factories marshal windows to the service."""
    calls: list[dict] = []

    class RecordingClient:
        def __init__(self, base_url, **kwargs):
            calls.append({"base_url": base_url})

        def transcribe_windows(self, windows, *, model, language=None, timeout=None):
            calls.append({"model": model, "language": language, "windows": len(windows)})
            return [[(0.0, 1.0, "ок")] for _ in windows]

    monkeypatch.setattr("stenograph.asr_client.RemoteAsrClient", RecordingClient)
    settings = _settings(tmp_path, asr_backend="remote", asr_url="http://recorder.test:9")
    factory, batch_factory = live_manager._engine_bundle(settings)
    words = factory("ru")(_tone(1.0))
    batch_words = batch_factory("ru")([_tone(0.5), _tone(0.5)])
    assert calls[0]["base_url"] == "http://recorder.test:9"
    assert calls[1] == {"model": "whisper", "language": "ru", "windows": 1}
    assert calls[2] == {"model": "whisper", "language": "ru", "windows": 2}
    assert words == [(0.0, 1.0, "ок")]
    assert batch_words == [[(0.0, 1.0, "ок")], [(0.0, 1.0, "ок")]]
