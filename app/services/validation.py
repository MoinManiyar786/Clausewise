"""Input validation and defensive coercion of model output.

Model output is untrusted: every field is type-checked, trimmed, length-
limited and forced into allowed enum values before it reaches the browser.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .rules import ROLES

READING_LEVELS = ("plain", "standard", "detailed")
LANGUAGES = ("English", "Hindi", "Spanish", "French", "German", "Portuguese",
             "Tamil", "Telugu", "Bengali", "Marathi", "Arabic", "Chinese")
RISK = ("low", "medium", "high")


class ValidationError(ValueError):
    """Raised for bad user input; the message is safe to show to users."""


def clean_text(value: Any, max_len: int) -> str:
    """Normalise unicode, drop control characters, collapse runaway whitespace."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    value = re.sub(r"[ \t]{3,}", "  ", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()[:max_len]


@dataclass(frozen=True)
class UserContext:
    role: str = "general"
    jurisdiction: str = ""
    reading_level: str = "plain"
    language: str = "English"
    redact_pii: bool = True
    goal: str = ""

    @classmethod
    def from_payload(cls, data: Any) -> "UserContext":
        data = data if isinstance(data, dict) else {}
        role = data.get("role") if data.get("role") in ROLES else "general"
        level = data.get("reading_level") if data.get("reading_level") in READING_LEVELS else "plain"
        language = data.get("language") if data.get("language") in LANGUAGES else "English"
        jurisdiction = re.sub(r"[^\w\s,.'()-]", "", clean_text(data.get("jurisdiction"), 60))
        return cls(
            role=role,
            jurisdiction=jurisdiction,
            reading_level=level,
            language=language,
            redact_pii=data.get("redact_pii", True) is not False,
            goal=clean_text(data.get("goal"), 300),
        )

    def as_prompt(self) -> str:
        role = self.role.replace("_", " ")
        return (
            f"- The user is a: {role}. Judge risk from THIS person's point of view.\n"
            f"- Jurisdiction: {self.jurisdiction or 'unknown (do not assume a country; mention that rules vary)'}\n"
            f"- Reading level: {self.reading_level} "
            f"({'short sentences, everyday words, ~grade 6-8' if self.reading_level == 'plain' else 'clear but may use common legal terms' if self.reading_level == 'standard' else 'thorough, keep legal terms and explain them'})\n"
            f"- Respond in: {self.language} (keep exact quotes from the document in their original language)\n"
            f"- User's goal: {self.goal or 'understand the document and their options'}"
        )


def require_document(value: Any, max_chars: int, label: str = "document") -> str:
    text = clean_text(value, max_chars + 1)
    if len(text) < 40:
        raise ValidationError(f"Please paste or upload a longer {label} (at least 40 characters).")
    if len(text) > max_chars:
        raise ValidationError(f"The {label} is too long. Please keep it under {max_chars:,} characters.")
    return text


# --------------------------------------------------------------------------- #
# Output coercion
# --------------------------------------------------------------------------- #
def _s(value: Any, max_len: int = 600) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return clean_text(value, max_len) if isinstance(value, str) else ""


def _enum(value: Any, allowed: tuple[str, ...], default: str) -> str:
    value = str(value).strip().lower() if value is not None else ""
    return value if value in allowed else default


def _list(value: Any, max_items: int) -> list:
    return list(value)[:max_items] if isinstance(value, list) else []


def _str_list(value: Any, max_items: int = 10, max_len: int = 400) -> list[str]:
    return [s for s in (_s(v, max_len) for v in _list(value, max_items)) if s]


def _objects(value: Any, max_items: int, fields: dict[str, Any]) -> list[dict]:
    """Coerce a list of objects. ``fields`` maps name -> max_len or enum spec."""
    out = []
    for item in _list(value, max_items):
        if not isinstance(item, dict):
            continue
        obj = {}
        for name, spec in fields.items():
            if isinstance(spec, tuple):          # (allowed_values, default)
                obj[name] = _enum(item.get(name), spec[0], spec[1])
            else:
                obj[name] = _s(item.get(name), spec)
        # Keep only items with real text; enum defaults alone don't count as content.
        if any(obj[name] for name, spec in fields.items() if not isinstance(spec, tuple)):
            out.append(obj)
    return out


def coerce_analysis(raw: dict) -> dict:
    return {
        "summary": _s(raw.get("summary"), 1500),
        "key_points": _str_list(raw.get("key_points"), 8),
        "parties": _objects(raw.get("parties"), 6, {"name": 120, "role": 160}),
        "key_terms": _objects(raw.get("key_terms"), 10, {"term": 80, "explanation": 300}),
        "clauses": _objects(raw.get("clauses"), 15, {
            "title": 120, "quote": 600, "plain_meaning": 500,
            "risk": (RISK, "medium"), "why_it_matters": 400,
            "who_it_favors": (("you", "other party", "balanced", "unclear"), "unclear"),
        }),
        "obligations": _objects(raw.get("obligations"), 12, {"party": 80, "obligation": 300, "quote": 400}),
        "deadlines": _objects(raw.get("deadlines"), 10, {"what": 200, "when": 120, "quote": 400}),
        "inconsistencies": _objects(raw.get("inconsistencies"), 8, {"issue": 300, "quote": 400}),
        "missing_protections": _str_list(raw.get("missing_protections"), 6),
        "next_steps": _str_list(raw.get("next_steps"), 8),
        "checklist": _str_list(raw.get("checklist"), 10),
        "needs_professional": bool(raw.get("needs_professional", False)),
        "professional_reason": _s(raw.get("professional_reason"), 300),
    }


def coerce_answer(raw: dict) -> dict:
    return {
        "answer": _s(raw.get("answer"), 2500),
        "found_in_document": bool(raw.get("found_in_document", False)),
        "confidence": _enum(raw.get("confidence"), RISK, "low"),
        "citations": _objects(raw.get("citations"), 5, {"quote": 600, "relevance": 300}),
        "follow_up_questions": _str_list(raw.get("follow_up_questions"), 4, 200),
    }


def coerce_comparison(raw: dict) -> dict:
    return {
        "overview": _s(raw.get("overview"), 1500),
        "differences": _objects(raw.get("differences"), 15, {
            "topic": 120, "doc_a": 500, "doc_b": 500, "impact": 400,
            "better_for_you": (("a", "b", "same", "unclear"), "unclear"),
            "risk": (RISK, "medium"),
        }),
        "only_in_a": _str_list(raw.get("only_in_a"), 8),
        "only_in_b": _str_list(raw.get("only_in_b"), 8),
        "questions_to_raise": _str_list(raw.get("questions_to_raise"), 6),
    }


def coerce_brief(raw: dict) -> dict:
    return {
        "situation_summary": _s(raw.get("situation_summary"), 1500),
        "professional_type": _s(raw.get("professional_type"), 200),
        "urgency_note": _s(raw.get("urgency_note"), 300),
        "questions_for_professional": _str_list(raw.get("questions_for_professional"), 10),
        "documents_to_gather": _str_list(raw.get("documents_to_gather"), 10),
        "facts_to_write_down": _str_list(raw.get("facts_to_write_down"), 10),
        "options": _objects(raw.get("options"), 5, {"option": 160, "pros": 300, "cons": 300}),
        "email_draft": _s(raw.get("email_draft"), 2500),
    }
