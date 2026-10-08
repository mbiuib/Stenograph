"""LLM post-processing tests: transcript prep, chunking, map-reduce, client."""

from __future__ import annotations

import json

import pytest

from fakes import FakeLlm
from stenograph.domain.errors import JobCancelled
from stenograph.domain.models import Job, Segment
from stenograph.llm.analyzer import analyze, split_transcript, transcript_text
from stenograph.llm.client import LlmClient, LlmError


def _job(segments: int = 20, chars_per_segment: int = 60) -> Job:
    """A finished job with a synthetic speaker-labelled transcript."""
    items = [
        Segment(
            index=index,
            start=float(index),
            end=float(index + 1),
            text=f"тезис{index} " + "а" * chars_per_segment,
            speaker=f"Спикер {index % 2 + 1}",
        )
        for index in range(segments)
    ]
    return Job(kind="file", source_name="встреча.mp4", segments=items, text="ignored")


def test_transcript_text_rebuilds_with_speakers() -> None:
    """Segments win over raw text and carry speaker labels."""
    job = Job(
        kind="file",
        segments=[
            Segment(index=0, start=0.0, end=1.0, text="привет", speaker="Спикер 1"),
            Segment(index=1, start=1.0, end=2.0, text="да"),
        ],
        text="не используется",
    )
    assert transcript_text(job) == "Спикер 1: привет\nда"


def test_split_transcript_short_is_single_chunk() -> None:
    """Short transcripts stay in one piece."""
    assert split_transcript("привет", 100) == ["привет"]


def test_split_transcript_keeps_order_with_overlap() -> None:
    """Long transcripts split on lines with a small overlap between chunks."""
    lines = [f"строка {index:03d} " + "x" * 30 for index in range(30)]
    text = "\n".join(lines)
    chunks = split_transcript(text, 200, overlap_lines=2)
    assert len(chunks) > 1
    assert chunks[0].startswith("строка 000")
    assert chunks[-1].endswith(lines[-1])
    assert any("строка" in chunk for chunk in chunks)
    first_tail = chunks[0].splitlines()[-1]
    assert first_tail in chunks[1]  # overlap carried forward


def test_analyze_single_chunk_uses_direct_prompt() -> None:
    """A short transcript goes through exactly one LLM call."""
    llm = FakeLlm()
    text, meta = analyze(_job(segments=2, chars_per_segment=10), "protocol", llm, max_chars=9000)
    assert len(llm.calls) == 1
    assert text == "ответ #1"
    assert meta["chunks"] == 1
    assert meta["model"] == "fake-llm"
    system, user = llm.calls[0]
    assert "протокол" in system.lower()
    assert "расшифровка" in user.lower()


def test_analyze_multi_chunk_maps_then_reduces() -> None:
    """Long transcripts map per fragment and reduce into one document."""
    llm = FakeLlm()
    progress: list[tuple[int, int, str]] = []
    chunks_seen = len(split_transcript(transcript_text(_job()), 300))
    text, meta = analyze(
        _job(),
        "summary",
        llm,
        max_chars=300,
        on_progress=lambda step, total, message: progress.append((step, total, message)),
    )
    assert meta["chunks"] == chunks_seen > 1
    assert len(llm.calls) == chunks_seen + 1  # map + one reduce
    assert text == f"ответ #{chunks_seen + 1}"
    assert [step for step, _, _ in progress] == list(range(1, chunks_seen + 2))
    assert all(total == chunks_seen + 1 for _, total, _ in progress)
    reduce_user = llm.calls[-1][1]
    assert "### Фрагмент 1" in reduce_user


def test_analyze_honours_cancellation() -> None:
    """Cancellation is polled between LLM calls."""
    llm = FakeLlm()
    gate = {"checks": 0}

    def cancelled() -> bool:
        gate["checks"] += 1
        return gate["checks"] > 1

    with pytest.raises(JobCancelled):
        analyze(_job(), "protocol", llm, max_chars=300, is_cancelled=cancelled)


# -- HTTP client behaviour -----------------------------------------------------


class _StubResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if payload is None else json.dumps(payload)

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _message(content: str, reasoning: str = "") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning,
                }
            }
        ]
    }


def test_client_parses_visible_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client returns message.content and sends the expected payload."""
    import stenograph.llm.client as client_module

    seen: list[dict] = []

    def fake_post(url, *, json=None, headers=None, timeout=None):
        seen.append(json)
        return _StubResponse(200, _message("привет"))

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    client = LlmClient("http://127.0.0.1:1234/v1", "test-model")
    assert client.chat("система", "запрос") == "привет"
    assert seen[0]["model"] == "test-model"
    assert seen[0]["stream"] is False
    assert seen[0]["messages"][0]["role"] == "system"


def test_client_retries_when_reasoning_ate_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty visible answer triggers one retry with a doubled budget."""
    import stenograph.llm.client as client_module

    seen: list[dict] = []

    def fake_post(url, *, json=None, headers=None, timeout=None):
        seen.append(json)
        if len(seen) == 1:
            return _StubResponse(200, _message("", reasoning="размышляю…"))
        return _StubResponse(200, _message("готово"))

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    client = LlmClient("http://x/v1", "m", max_tokens=100)
    assert client.chat("s", "u") == "готово"
    assert len(seen) == 2
    assert seen[1]["max_tokens"] == 200


def test_client_raises_readable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP failures and empty answers surface as LlmError."""
    import stenograph.llm.client as client_module

    def http_500(url, *, json=None, headers=None, timeout=None):
        return _StubResponse(500, text="boom")

    monkeypatch.setattr(client_module.httpx, "post", http_500)
    with pytest.raises(LlmError, match="500"):
        LlmClient("http://x/v1", "m").chat("s", "u")

    def always_empty(url, *, json=None, headers=None, timeout=None):
        return _StubResponse(200, _message("", reasoning="…"))

    monkeypatch.setattr(client_module.httpx, "post", always_empty)
    with pytest.raises(LlmError):
        LlmClient("http://x/v1", "m", max_tokens=50).chat("s", "u")
