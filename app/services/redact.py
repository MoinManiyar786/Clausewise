"""Privacy-first PII redaction, applied *before* any text leaves the server.

Placeholders are numbered per type ("[EMAIL_1]") so the model can still
reason about "the same person or number" without seeing the real value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Order matters: specific patterns run before the broad phone pattern.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,15}\d\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("AADHAAR", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("PHONE", re.compile(r"(?<![\w\[])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,5}[\s.-]?\d{3,4}(?:[\s.-]?\d{2,4})?(?![\w\]])")),
]


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 16:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact(text: str) -> RedactionResult:
    """Replace personal identifiers with stable numbered placeholders."""
    counts: dict[str, int] = {}
    seen: dict[tuple[str, str], str] = {}

    def replacer(kind: str):
        def _sub(match: re.Match[str]) -> str:
            value = match.group(0)
            if kind == "CARD" and not _luhn_ok(value):
                return value
            if kind == "PHONE" and sum(c.isdigit() for c in value) < 10:
                return value  # don't eat amounts, years or clause numbers
            key = (kind, value)
            if key not in seen:
                counts[kind] = counts.get(kind, 0) + 1
                seen[key] = f"[{kind}_{counts[kind]}]"
            return seen[key]
        return _sub

    for kind, pattern in _PATTERNS:
        text = pattern.sub(replacer(kind), text)
    return RedactionResult(text=text, counts=counts)
