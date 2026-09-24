"""Test doubles and fixtures shared across test modules."""
from __future__ import annotations

from app.services.llm import LLMError

LEASE = (
    "RESIDENTIAL LEASE. The Landlord and the Tenant agree as follows. Tenant email: jane.doe@example.com.\n\n"
    "1. This Lease shall automatically renew for successive one-year terms unless the Tenant gives notice.\n\n"
    "2. Tenant shall pay rent of 1,200 each month. A late fee of 50 applies after the 5th day.\n\n"
    "3. The security deposit is non-refundable if the Tenant leaves early.\n\n"
    "4. Tenant shall indemnify and hold harmless the Landlord from any and all claims.\n\n"
    "5. Landlord may terminate this Lease at any time for any reason."
)

SUMMONS = (
    "IN THE COURT OF THE CIVIL JUDGE. SUMMONS. You are hereby required to appear before the court "
    "at the hearing on 12 October. The plaintiff claims unpaid rent from the defendant. "
    "You must file a written response within 10 days of receiving this summons."
)


class FakeLLM:
    """Records prompts and returns canned JSON (or raises)."""

    def __init__(self, response: dict | None = None, error: Exception | None = None, available: bool = True):
        self.response = response or {}
        self.error = error
        self.available = available
        self.calls: list[str] = []
        self.max_tokens: list[int] = []

    def generate_json(self, system: str, prompt: str, temperature: float = 0.2,
                      max_output_tokens: int = 4096) -> dict:
        self.calls.append(prompt)
        self.max_tokens.append(max_output_tokens)
        if self.error:
            raise self.error
        return dict(self.response)


def failing_llm() -> FakeLLM:
    return FakeLLM(error=LLMError("boom"))


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.responses.pop(0)
