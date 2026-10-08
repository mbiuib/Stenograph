"""Minimal OpenAI-compatible chat client for local text models (LM Studio etc).

One place for the HTTP details: reasoning-capable models may burn the whole
token budget on hidden ``reasoning_content`` before writing the visible answer,
so every request carries a generous ``max_tokens`` and an empty visible reply
triggers exactly one retry with a doubled budget.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """The endpoint is unreachable or returned nothing usable."""


class LlmClient:
    """Chat-completions client for a local OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 600.0,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._api_key = api_key

    def describe(self) -> str:
        """Human-readable target of this client, recorded in job metadata."""
        return f"{self.model} @ {self.base_url}"

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """One chat completion; returns the assistant's visible text."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        budget = max_tokens or self.max_tokens
        for attempt, tokens in enumerate((budget, budget * 2), start=1):
            payload["max_tokens"] = tokens
            text = self._post(payload)
            if text:
                return text
            log.warning(
                "LLM вернула пустой ответ (попытка %d, %d токенов): "
                "возможно, всё ушло в reasoning",
                attempt,
                tokens,
            )
        raise LlmError(
            "модель вернула пустой ответ; проверьте, загружена ли она в LM Studio"
        )

    def _post(self, payload: dict) -> str:
        """Send one request and extract the visible assistant content."""
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        started = time.monotonic()
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM недоступна по адресу {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise LlmError(
                f"LLM вернула ошибку {response.status_code}: {response.text[:300]}"
            )
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LlmError(f"неожиданный ответ LLM: {response.text[:200]}") from exc
        content = str(message.get("content") or "").strip()
        reasoning = str(message.get("reasoning_content") or "")
        log.debug(
            "llm: %.1f с, %d символов ответа, %d символов reasoning",
            time.monotonic() - started,
            len(content),
            len(reasoning),
        )
        return content
