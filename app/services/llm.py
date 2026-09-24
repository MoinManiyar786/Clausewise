"""Minimal Google Gemini client over REST.

Using REST via ``requests`` (instead of a heavy SDK) keeps the dependency
footprint and repository size small, and makes the client trivial to mock.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_RETRYABLE = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """The model call failed or returned something unusable."""


class JSONModel(Protocol):
    available: bool

    def generate_json(self, system: str, prompt: str, temperature: float = 0.2,
                      max_output_tokens: int = 4096) -> dict[str, Any]:
        ...


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse model output into a dict, tolerating code fences or stray prose."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model did not return JSON") from None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("Model returned malformed JSON") from exc
    if not isinstance(data, dict):
        raise LLMError("Model JSON was not an object")
    return data


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout: int = 60,
                 session: requests.Session | None = None, max_retries: int = 2,
                 thinking_budget: int = 512) -> None:
        self._api_key = api_key
        self._thinking_budget = max(0, thinking_budget)
        self._model = model
        self._timeout = timeout
        self._session = session or requests.Session()
        self._max_retries = max_retries

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def build_payload(self, system: str, prompt: str, temperature: float, max_output_tokens: int) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "maxOutputTokens": max_output_tokens,
        }
        # Structured extraction doesn't benefit from long hidden reasoning. On Gemini 2.5
        # Flash models, a small thinking budget cuts latency and token cost substantially.
        if "2.5-flash" in self._model:
            config["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        }

    def generate_json(self, system: str, prompt: str, temperature: float = 0.2,
                      max_output_tokens: int = 4096) -> dict[str, Any]:
        if not self.available:
            raise LLMError("No API key configured")
        payload = self.build_payload(system, prompt, temperature, max_output_tokens)
        url = _ENDPOINT.format(model=self._model)
        # Key goes in a header, never the URL, so it can't leak into logs.
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                log.warning("Gemini network error (attempt %d): %s", attempt + 1, type(exc).__name__)
                if attempt == self._max_retries:
                    raise LLMError("Could not reach the AI service") from exc
            else:
                if resp.status_code == 200:
                    return parse_json_response(self._extract_text(resp.json()))
                log.warning("Gemini HTTP %s (attempt %d)", resp.status_code, attempt + 1)
                if resp.status_code not in _RETRYABLE or attempt == self._max_retries:
                    raise LLMError(f"AI service error (HTTP {resp.status_code})")
            time.sleep(0.8 * (2 ** attempt))
        raise LLMError("AI service unavailable")  # pragma: no cover

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        try:
            candidate = data["candidates"][0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                raise LLMError("AI response was cut off (too long)")
            parts = candidate["content"]["parts"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            reason = (data.get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None
            raise LLMError(f"Empty AI response{f' ({reason})' if reason else ''}") from exc
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
