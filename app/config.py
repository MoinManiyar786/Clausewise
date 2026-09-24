"""Application settings loaded from environment variables.

Only one secret is required: ``GEMINI_API_KEY``. Everything else has a safe
default, and the app still runs (in offline, rule-based mode) without a key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    max_doc_chars: int = 60_000          # ~15k tokens: keeps prompts fast and cheap
    max_question_chars: int = 1_000
    max_upload_bytes: int = 5 * 1024 * 1024
    max_pdf_pages: int = 60
    rate_limit_per_minute: int = 20
    cache_size: int = 128
    cache_ttl_seconds: int = 1800
    llm_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "").strip() or cls.gemini_model,
            rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", cls.rate_limit_per_minute),
        )
