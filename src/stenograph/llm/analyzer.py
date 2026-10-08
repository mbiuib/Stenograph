"""Chunked map-reduce analysis of a transcript through a local LLM.

Long transcripts are split on line boundaries with a small overlap; each
fragment goes through a map prompt, then the notes are reduced (recursively if
they are still too large for one request) into the final document.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..domain.errors import JobCancelled
from ..domain.models import Job
from . import prompts
from .client import LlmClient

log = logging.getLogger(__name__)

_OVERLAP_LINES = 2  # carried into the next chunk for continuity


def transcript_text(job: Job) -> str:
    """Speaker-labelled transcript: rebuild from segments, fall back to text."""
    if job.segments:
        lines: list[str] = []
        for segment in job.segments:
            text = segment.text.strip()
            if not text:
                continue
            speaker = (segment.speaker or "").strip()
            lines.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(lines)
    return job.text.strip()


def split_transcript(
    text: str, max_chars: int, *, overlap_lines: int = _OVERLAP_LINES
) -> list[str]:
    """Greedy line packing with a few overlap lines between neighbours."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and size + line_len > max_chars:
            chunks.append("\n".join(current))
            current = current[-overlap_lines:] if overlap_lines else []
            size = sum(len(item) + 1 for item in current)
        current.append(line)
        size += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def analyze(
    job: Job,
    analysis_type: str,
    client: LlmClient,
    *,
    max_chars: int,
    on_progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[str, dict]:
    """Run the analysis; returns the markdown document and its metadata.

    ``on_progress(step, total_steps, message)`` fires before every LLM call.
    """
    transcript = transcript_text(job)
    if not transcript:
        raise ValueError("у задачи нет текста для анализа")

    chunks = split_transcript(transcript, max_chars)
    steps = len(chunks) + (1 if len(chunks) > 1 else 0)
    step = 0

    def notify(message: str) -> None:
        nonlocal step
        step += 1
        if on_progress is not None:
            on_progress(step, steps, message)

    if len(chunks) == 1:
        notify("Готовлю ответ…")
        system, user = prompts.direct_prompt(analysis_type, chunks[0])
        final = client.chat(system, user)
    else:
        notes: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            if is_cancelled is not None and is_cancelled():
                raise JobCancelled()
            notify(f"Фрагмент {index} из {len(chunks)}…")
            system, user = prompts.chunk_prompt(analysis_type, chunk, index, len(chunks))
            notes.append(client.chat(system, user))
        notify("Собираю результат…")
        final = _reduce(client, analysis_type, notes, max_chars)

    meta = {
        "model": client.describe(),
        "chunks": len(chunks),
        "transcript_chars": len(transcript),
    }
    return final.strip(), meta


def _reduce(client: LlmClient, analysis_type: str, notes: list[str], max_chars: int) -> str:
    """Reduce notes to the final document; recurses if they overflow one request."""
    combined = "\n\n".join(
        f"### Фрагмент {index}\n{note}" for index, note in enumerate(notes, 1)
    )
    guard = max_chars * 2
    if len(combined) <= guard or len(notes) <= 1:
        system, user = prompts.reduce_prompt(analysis_type, combined)
        return client.chat(system, user)

    batches: list[str] = []
    current: list[str] = []
    size = 0
    for index, note in enumerate(notes, 1):
        block = f"### Фрагмент {index}\n{note}"
        if current and size + len(block) > guard:
            batches.append("\n\n".join(current))
            current = []
            size = 0
        current.append(block)
        size += len(block) + 2
    if current:
        batches.append("\n\n".join(current))

    intermediate: list[str] = []
    for batch in batches:
        system, user = prompts.reduce_prompt(analysis_type, batch)
        intermediate.append(client.chat(system, user))
    return _reduce(client, analysis_type, intermediate, max_chars)
