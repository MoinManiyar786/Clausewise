"""ClauseWise orchestration layer.

Each public method follows the same pipeline:

    validate -> redact PII -> local rules scan -> (cached) AI call
             -> schema coercion -> citation verification -> decisions

If the AI is unavailable or fails, a deterministic rule-based result is
returned instead (``meta.mode == "offline"``), so the user is never left
with nothing.
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Callable

from . import prompts, rules
from .cache import TTLCache, make_key
from .llm import JSONModel, LLMError
from .redact import redact
from .validation import (
    UserContext, clean_text, coerce_analysis, coerce_answer, coerce_brief,
    coerce_comparison, require_document, ValidationError,
)

log = logging.getLogger(__name__)

DISCLAIMER = ("ClauseWise gives general legal information, not legal advice. "
              "Laws differ by place and situation. For decisions that matter, talk to a qualified professional.")

HIGH_STAKES_TYPES = {"court_or_legal_notice", "loan_or_credit"}

# Output budgets sized to each task's JSON schema: short answers stay cheap.
MAX_OUTPUT_TOKENS = {"analyze": 6144, "compare": 4096, "prepare": 3072, "ask": 1536}


class LegalAssistant:
    def __init__(self, llm: JSONModel, cache: TTLCache, max_doc_chars: int = 60_000,
                 max_question_chars: int = 1_000) -> None:
        self.llm = llm
        self.cache = cache
        self.max_doc_chars = max_doc_chars
        self.max_question_chars = max_question_chars
        # Redaction is the costliest local step; follow-up questions reuse it.
        self._prepared = TTLCache(max_size=64, ttl_seconds=1800)

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    def _prepare(self, raw_text: Any, ctx: UserContext, label: str = "document") -> tuple[str, dict]:
        text = require_document(raw_text, self.max_doc_chars, label)
        if not ctx.redact_pii:
            return text, {}
        key = make_key("prepared", text)
        cached = self._prepared.get(key)
        if cached is None:
            result = redact(text)
            cached = (result.text, result.counts)
            self._prepared.set(key, cached)
        return cached[0], dict(cached[1])

    def _call(self, task: str, prompt: str, coerce: Callable[[dict], dict]) -> dict | None:
        """Cached AI call. Returns None on any failure so callers can fall back."""
        if not self.llm.available:
            return None
        key = make_key(task, prompt)
        cached = self.cache.get(key)
        if cached is not None:
            return {**cached, "_cached": True}
        try:
            result = coerce(self.llm.generate_json(prompts.SYSTEM, prompt,
                                                   max_output_tokens=MAX_OUTPUT_TOKENS.get(task, 4096)))
        except LLMError as exc:
            log.warning("AI call for %s failed: %s", task, exc)
            return None
        self.cache.set(key, result)
        return result

    @staticmethod
    def _hints(findings: list[dict], urgency: rules.Urgency, doc_type: str) -> str:
        lines = [f"- Detected type: {rules.DOC_TYPE_LABELS[doc_type]}",
                 f"- Urgency: {urgency.level} {('(' + '; '.join(urgency.reasons) + ')') if urgency.reasons else ''}"]
        lines += [f"- Possible {f['risk']} risk: {f['title']}" for f in findings[:10]]
        return "\n".join(lines)

    @staticmethod
    def _verify(items: list[dict], text: str) -> None:
        """Mark each quote as verified if it truly appears in the document."""
        for item in items:
            quote = item.get("quote", "")
            span = rules.find_span(text, quote) if quote else None
            item["verified"] = span is not None
            if span:
                item["span"] = list(span)

    def _decide_professional(self, doc_type: str, urgency: rules.Urgency, clauses: list[dict],
                             ai_flag: bool = False, ai_reason: str = "") -> dict:
        """Transparent rules for when to recommend a human professional."""
        high = sum(1 for c in clauses if c.get("risk") == "high")
        reasons = []
        if urgency.level == "urgent":
            reasons.append("This looks time-sensitive (" + ", ".join(urgency.reasons[:3]) + ").")
        if doc_type in HIGH_STAKES_TYPES:
            reasons.append(f"{rules.DOC_TYPE_LABELS[doc_type]}s can have serious consequences.")
        if high >= 3:
            reasons.append(f"{high} high-risk clauses were found.")
        if ai_flag and ai_reason:
            reasons.append(ai_reason)
        level = "now" if urgency.level == "urgent" else "recommended" if reasons else "optional"
        return {"level": level, "reasons": reasons[:4]}

    def _meta(self, mode: str, ctx: UserContext, redactions: dict, **extra: Any) -> dict:
        return {"mode": mode, "disclaimer": DISCLAIMER, "redactions": redactions,
                "context": {"role": ctx.role, "reading_level": ctx.reading_level, "language": ctx.language,
                            "jurisdiction": ctx.jurisdiction or None}, **extra}

    # ------------------------------------------------------------------ #
    # 1. Understand a document
    # ------------------------------------------------------------------ #
    def analyze(self, raw_text: Any, ctx: UserContext) -> dict:
        text, redactions = self._prepare(raw_text, ctx)
        submitted = raw_text.strip() if isinstance(raw_text, str) else ""
        doc_type = rules.detect_doc_type(text)
        urgency = rules.detect_urgency(text)
        findings = rules.scan_red_flags(text, ctx.role)

        ai = self._call("analyze", prompts.analysis_prompt(text, ctx.as_prompt(), self._hints(findings, urgency, doc_type)),
                        coerce_analysis)
        if ai:
            result = ai
            self._merge_rule_findings(result["clauses"], findings)
        else:
            result = self._offline_analysis(text, doc_type, findings)

        cached = result.pop("_cached", False)
        for group in ("clauses", "obligations", "deadlines", "inconsistencies"):
            self._verify(result[group], text)
        result["clauses"].sort(key=lambda c: -rules.SEVERITY_ORDER.get(c["risk"], 1))

        result["risk_counts"] = {lvl: sum(1 for c in result["clauses"] if c["risk"] == lvl) for lvl in rules.RISK_LEVELS}
        result["professional_help"] = self._decide_professional(
            doc_type, urgency, result["clauses"], result.pop("needs_professional", False),
            result.pop("professional_reason", ""))
        # Highlight offsets refer to the analysed text. Send it back only when it differs from
        # what the browser submitted (e.g. after redaction); otherwise the client reuses its copy.
        result["document"] = {"text": text if text != submitted else None, "type": doc_type, "type_label": rules.DOC_TYPE_LABELS[doc_type],
                              "readability": rules.readability(text), "characters": len(text)}
        result["urgency"] = {"level": urgency.level, "reasons": urgency.reasons,
                             "shortest_deadline_days": urgency.shortest_deadline_days}
        result["meta"] = self._meta("ai" if ai else "offline", ctx, redactions, cached=cached,
                                    injection_warning=rules.looks_like_injection(text))
        return result

    @staticmethod
    def _merge_rule_findings(clauses: list[dict], findings: list[dict]) -> None:
        """Add rule-detected risks the AI missed, so nothing important is dropped."""
        def words(s: str) -> set[str]:
            return set(re.findall(r"[a-z]{3,}", s.lower()))

        existing = [words(c.get("quote", "") + " " + c.get("title", "")) for c in clauses]
        for f in findings:
            fw = words(f["quote"])
            if fw and any(len(fw & ew) / len(fw) >= 0.5 for ew in existing):
                continue
            clauses.append({"title": f["title"], "quote": f["quote"], "plain_meaning": f["why_it_matters"],
                            "risk": f["risk"], "why_it_matters": f["why_it_matters"],
                            "who_it_favors": "unclear", "source": "rules"})
            existing.append(fw)

    @staticmethod
    def _offline_analysis(text: str, doc_type: str, findings: list[dict]) -> dict:
        sentences = rules.split_sentences(text)
        obligations = [{"party": "", "obligation": s[:300], "quote": s[:400]}
                       for s in sentences if re.search(r"\b(shall|must|agrees? to|is required to)\b", s, re.I)][:8]
        deadlines = [{"what": s[:200], "when": m.group(0), "quote": s[:400]} for s in sentences
                     for m in [re.search(r"within \d+ \w+|\bby [A-Z][a-z]+ \d{1,2}|\d{1,2} days", s)] if m][:8]
        clauses = [{"title": f["title"], "quote": f["quote"], "plain_meaning": f["why_it_matters"],
                    "risk": f["risk"], "why_it_matters": f["why_it_matters"], "who_it_favors": "unclear",
                    "source": "rules"} for f in findings]
        label = rules.DOC_TYPE_LABELS[doc_type]
        return {
            "summary": (f"This looks like a {label.lower()}. The AI service is not available, so this is an automatic "
                        f"scan for common risk patterns. It found {len(clauses)} clause(s) worth checking."),
            "key_points": [f"{c['title']}: {c['why_it_matters']}" for c in clauses[:6]],
            "parties": [], "key_terms": [], "clauses": clauses, "obligations": obligations,
            "deadlines": deadlines, "inconsistencies": [], "missing_protections": [],
            "next_steps": ["Read each highlighted clause in full.",
                           "Ask the other party to explain or change anything you don't accept.",
                           "Talk to a qualified professional before signing if the stakes are high."],
            "checklist": ["Write down every date and deadline.", "Keep a signed copy of the final version.",
                          "Get any promised changes in writing."],
        }

    # ------------------------------------------------------------------ #
    # 2. Ask a question about a document
    # ------------------------------------------------------------------ #
    def ask(self, raw_text: Any, raw_question: Any, ctx: UserContext, raw_history: Any = None) -> dict:
        question = clean_text(raw_question, self.max_question_chars)
        if len(question) < 3:
            raise ValidationError("Please type a question.")
        text, redactions = self._prepare(raw_text, ctx)
        if ctx.redact_pii:
            question = redact(question).text
        history = "\n".join(f"- {clean_text(h, 200)}" for h in (raw_history or [])[-4:] if isinstance(h, str))

        ai = self._call("ask", prompts.question_prompt(text, question, ctx.as_prompt(), history), coerce_answer)
        result = ai or self._offline_answer(text, question)
        cached = result.pop("_cached", False)
        self._verify(result["citations"], text)
        # Grounding rule: an answer claiming support must have at least one verified quote.
        if result["found_in_document"] and result["citations"] and not any(c["verified"] for c in result["citations"]):
            result["confidence"] = "low"
            result["grounding_warning"] = "The quoted text could not be found in your document. Double-check this answer."
        result["meta"] = self._meta("ai" if ai else "offline", ctx, redactions, cached=cached)
        return result

    @staticmethod
    def _offline_answer(text: str, question: str) -> dict:
        stop = {"the", "a", "an", "is", "are", "what", "how", "can", "i", "my", "do", "does", "to", "of", "in",
                "if", "for", "and", "or", "it", "this", "be", "when", "who", "will", "me"}
        terms = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in stop}
        scored = []
        for sentence in rules.split_sentences(text):
            words = set(re.findall(r"[a-z]{3,}", sentence.lower()))
            score = len(terms & words) + 0.5 * sum(1 for t in terms for w in words if w.startswith(t[:5]) and w != t)
            if score:
                scored.append((score, sentence))
        top = [s for _, s in sorted(scored, key=lambda x: -x[0])[:3]]
        if not top:
            return {"answer": "I couldn't find anything in the document that matches your question. Try different "
                              "words, or ask a professional.", "found_in_document": False, "confidence": "low",
                    "citations": [], "follow_up_questions": []}
        return {"answer": "The AI service is unavailable, so here are the passages that best match your question. "
                          "Read them carefully:",
                "found_in_document": True, "confidence": "low",
                "citations": [{"quote": s[:600], "relevance": "Keyword match"} for s in top],
                "follow_up_questions": []}

    # ------------------------------------------------------------------ #
    # 3. Compare two documents
    # ------------------------------------------------------------------ #
    def compare(self, raw_a: Any, raw_b: Any, ctx: UserContext) -> dict:
        text_a, red_a = self._prepare(raw_a, ctx, "first document")
        text_b, red_b = self._prepare(raw_b, ctx, "second document")
        ai = self._call("compare", prompts.comparison_prompt(text_a, text_b, ctx.as_prompt()), coerce_comparison)
        result = ai or self._offline_compare(text_a, text_b, ctx.role)
        cached = result.pop("_cached", False)
        result["similarity"] = round(difflib.SequenceMatcher(None, text_a, text_b, autojunk=False).quick_ratio() * 100)
        flags_a = {f["id"]: f for f in rules.scan_red_flags(text_a, ctx.role)}
        flags_b = {f["id"]: f for f in rules.scan_red_flags(text_b, ctx.role)}
        result["risk_changes"] = {
            "added_in_b": [flags_b[k]["title"] for k in flags_b.keys() - flags_a.keys()],
            "removed_in_b": [flags_a[k]["title"] for k in flags_a.keys() - flags_b.keys()],
        }
        redactions = {k: red_a.get(k, 0) + red_b.get(k, 0) for k in {*red_a, *red_b}}
        result["meta"] = self._meta("ai" if ai else "offline", ctx, redactions, cached=cached)
        return result

    @staticmethod
    def _offline_compare(text_a: str, text_b: str, role: str) -> dict:
        sa, sb = rules.split_sentences(text_a), rules.split_sentences(text_b)
        diffs, only_a, only_b = [], [], []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
            if op == "replace":
                for a, b in zip(sa[i1:i2], sb[j1:j2]):
                    diffs.append({"topic": "Changed wording", "doc_a": a[:500], "doc_b": b[:500],
                                  "impact": "Wording changed. Check whether your rights or costs changed.",
                                  "better_for_you": "unclear", "risk": "medium"})
            elif op == "delete":
                only_a += [s[:400] for s in sa[i1:i2]]
            elif op == "insert":
                only_b += [s[:400] for s in sb[j1:j2]]
        return {"overview": f"Automatic text comparison (AI unavailable): {len(diffs)} changed passage(s), "
                            f"{len(only_a)} removed and {len(only_b)} added.",
                "differences": diffs[:15], "only_in_a": only_a[:8], "only_in_b": only_b[:8],
                "questions_to_raise": ["Why was each of these passages changed?",
                                       "Can the changes be explained in writing?"]}

    # ------------------------------------------------------------------ #
    # 4. Prepare for a legal professional
    # ------------------------------------------------------------------ #
    def prepare(self, raw_text: Any, raw_situation: Any, ctx: UserContext) -> dict:
        situation = clean_text(raw_situation, 3000)
        has_doc = isinstance(raw_text, str) and raw_text.strip()
        if not has_doc and len(situation) < 20:
            raise ValidationError("Describe your situation in a few sentences, or add a document.")
        text, redactions = self._prepare(raw_text, ctx) if has_doc else ("", {})
        if ctx.redact_pii and situation:
            situation = redact(situation).text
        combined = f"{text}\n{situation}"
        doc_type = rules.detect_doc_type(combined)
        urgency = rules.detect_urgency(combined)
        findings = rules.scan_red_flags(text, ctx.role) if text else []

        ai = self._call("prepare", prompts.brief_prompt(text, situation, ctx.as_prompt(),
                                                        self._hints(findings, urgency, doc_type)), coerce_brief)
        result = ai or self._offline_brief(doc_type, urgency, findings, situation)
        cached = result.pop("_cached", False)
        result["professional_help"] = self._decide_professional(doc_type, urgency, findings)
        result["urgency"] = {"level": urgency.level, "reasons": urgency.reasons,
                             "shortest_deadline_days": urgency.shortest_deadline_days}
        result["meta"] = self._meta("ai" if ai else "offline", ctx, redactions, cached=cached)
        return result

    @staticmethod
    def _offline_brief(doc_type: str, urgency: rules.Urgency, findings: list[dict], situation: str) -> dict:
        professionals = {
            "court_or_legal_notice": "A litigation lawyer or your local legal aid office",
            "lease_or_rental": "A tenant/landlord advice service or property lawyer",
            "employment": "An employment lawyer or labour/workers' rights office",
            "loan_or_credit": "A consumer credit counsellor or banking lawyer",
            "terms_of_service": "A consumer protection body or consumer lawyer",
            "privacy_policy": "Your data protection authority or a privacy lawyer",
        }
        questions = [f"How does the '{f['title'].lower()}' clause affect me, and can it be changed?" for f in findings[:5]]
        questions += ["What are my options, and what does each one cost?", "Is there a deadline I must meet?"]
        return {
            "situation_summary": situation or f"I have a {rules.DOC_TYPE_LABELS[doc_type].lower()} I want to understand.",
            "professional_type": professionals.get(doc_type, "A general practice lawyer or legal aid clinic"),
            "urgency_note": ("This may be urgent: " + "; ".join(urgency.reasons)) if urgency.reasons
                            else "No obvious deadline was detected, but check the document for dates.",
            "questions_for_professional": questions,
            "documents_to_gather": ["The full document, including all pages and attachments",
                                    "Any emails, letters or messages about it", "Receipts or payment records",
                                    "Photos or other evidence, if relevant"],
            "facts_to_write_down": ["When and how you received the document", "Every date and amount mentioned",
                                    "What you have already said or done in response"],
            "options": [],
            "email_draft": ("Subject: Request for a consultation\n\nHello,\n\nI would like to book a consultation about "
                            f"a {rules.DOC_TYPE_LABELS[doc_type].lower()}. [Briefly describe the situation]. "
                            "Could you tell me your availability and fees?\n\nThank you,\n[Your name]\n[Your phone]"),
        }
