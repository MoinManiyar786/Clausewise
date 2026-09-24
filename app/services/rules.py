"""Deterministic, offline legal-text heuristics.

This module gives ClauseWise a reliable baseline that needs no network:

* document-type detection
* red-flag clause detection, weighted by *who the user is* (a non-compete is
  a high risk for an employee but low for an employer)
* urgency detection (court summons, eviction, short deadlines) that drives
  the "talk to a professional now" decision
* readability scoring (Flesch reading ease)
* prompt-injection detection for untrusted document text

The AI layer builds on top of these results; if the AI is unavailable the
app falls back to them entirely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ROLES = (
    "general", "tenant", "landlord", "employee", "employer",
    "freelancer", "client", "consumer", "small_business",
)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
RISK_LEVELS = ("high", "medium", "low")

# --------------------------------------------------------------------------- #
# Document types
# --------------------------------------------------------------------------- #
DOC_TYPES: dict[str, tuple[str, ...]] = {
    "court_or_legal_notice": ("summons", "plaintiff", "defendant", "court of", "hearing",
                              "legal notice", "notice to quit", "notice to vacate", "cease and desist"),
    "lease_or_rental": ("lease", "landlord", "tenant", "premises", "rent", "security deposit", "lessee"),
    "employment": ("employee", "employer", "salary", "employment", "probation", "termination of employment"),
    "nda": ("confidential information", "non-disclosure", "disclosing party", "receiving party"),
    "service_or_freelance": ("contractor", "statement of work", "deliverables", "services", "invoice"),
    "terms_of_service": ("terms of service", "terms of use", "user account", "you agree", "our services"),
    "privacy_policy": ("privacy policy", "personal data", "cookies", "data controller", "third parties"),
    "loan_or_credit": ("borrower", "lender", "interest rate", "principal", "repayment", "emi", "collateral"),
    "purchase_or_sale": ("buyer", "seller", "purchase price", "goods", "warranty", "delivery"),
}

DOC_TYPE_LABELS = {
    "court_or_legal_notice": "Court or legal notice",
    "lease_or_rental": "Lease / rental agreement",
    "employment": "Employment agreement",
    "nda": "Non-disclosure agreement",
    "service_or_freelance": "Service / freelance contract",
    "terms_of_service": "Terms of service",
    "privacy_policy": "Privacy policy",
    "loan_or_credit": "Loan / credit agreement",
    "purchase_or_sale": "Purchase / sale agreement",
    "general_legal_document": "General legal document",
}


def detect_doc_type(text: str) -> str:
    lower = text.lower()
    scores = {kind: sum(lower.count(k) for k in keys) for kind, keys in DOC_TYPES.items()}
    # Notices are rare but critical: a couple of hits outweighs generic contract words.
    scores["court_or_legal_notice"] *= 3
    best, score = max(scores.items(), key=lambda kv: kv[1])
    return best if score >= 2 else "general_legal_document"


# --------------------------------------------------------------------------- #
# Red flags
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RedFlag:
    id: str
    title: str
    patterns: tuple[str, ...]
    severity: str
    why: str
    role_severity: dict[str, str] = field(default_factory=dict)
    compiled: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Compile once at import time instead of on every request.
        object.__setattr__(self, "compiled", tuple(re.compile(p, re.IGNORECASE) for p in self.patterns))


RED_FLAGS: tuple[RedFlag, ...] = (
    RedFlag("auto_renewal", "Automatic renewal",
            (r"automatic(?:ally)?\s+renew", r"auto[- ]renew", r"renew(?:s|ed)?\s+for\s+(?:successive|additional)"),
            "medium", "The agreement may continue (and keep charging you) unless you cancel in time.",
            {"consumer": "high", "client": "high"}),
    RedFlag("unilateral_change", "One side can change the terms",
            (r"(?:may|reserves? the right to)\s+(?:modify|amend|change|update)\s+(?:these|this|the)\s+(?:terms|agreement)",
             r"at (?:its|our) sole discretion"),
            "medium", "Terms could change later without your agreement.",
            {"consumer": "high", "freelancer": "high"}),
    RedFlag("indemnity", "Indemnity (you cover their losses)",
            (r"\bindemnif(?:y|ies|ication)\b", r"hold\s+harmless"),
            "high", "You could be required to pay for the other side's losses or legal costs.",
            {"landlord": "medium", "employer": "medium"}),
    RedFlag("liability_cap", "Limited liability for the other side",
            (r"limitation of liability", r"shall not be liable", r"in no event shall", r"liability .{0,40}shall not exceed"),
            "medium", "If something goes wrong, the amount you can recover may be very limited.",
            {"consumer": "high", "client": "high", "tenant": "high"}),
    RedFlag("arbitration", "Mandatory arbitration / no class action",
            (r"\barbitration\b", r"waive[sd]?\s+(?:any|the)?\s*right\s+to\s+(?:a\s+)?(?:jury|class|court)", r"class action waiver"),
            "medium", "Disputes may have to go to private arbitration instead of a court.",
            {"consumer": "high", "employee": "high"}),
    RedFlag("non_compete", "Non-compete restriction",
            (r"non[- ]?compet", r"shall not .{0,60}(?:compete|competing|competitor)", r"not engage in any (?:similar|competing) business"),
            "high", "It may restrict where you can work or do business after this ends.",
            {"employer": "low", "client": "low", "small_business": "medium"}),
    RedFlag("penalty", "Penalties, late fees or forfeiture",
            (r"\bpenalt(?:y|ies)\b", r"late (?:fee|charge|payment)", r"\bforfeit", r"liquidated damages"),
            "medium", "Missing a payment or deadline could cost you extra money.",
            {"tenant": "high", "consumer": "high", "freelancer": "medium"}),
    RedFlag("termination_without_cause", "Termination without cause or notice",
            (r"terminate .{0,40}(?:without cause|for any reason|at any time)", r"without (?:prior )?notice"),
            "medium", "The other side may be able to end the agreement suddenly.",
            {"employee": "high", "freelancer": "high", "tenant": "high"}),
    RedFlag("deposit_deductions", "Deposit can be withheld",
            (r"(?:security )?deposit .{0,80}(?:non-refundable|deduct|withh[oe]ld|forfeit)",),
            "medium", "Part or all of your deposit may not come back.",
            {"tenant": "high", "landlord": "low"}),
    RedFlag("ip_assignment", "You give up intellectual property",
            (r"assigns? .{0,40}(?:all )?(?:right, title and interest|intellectual property)", r"work made for hire", r"work for hire"),
            "medium", "Work you create may belong entirely to the other side.",
            {"freelancer": "high", "employee": "medium", "employer": "low", "client": "low"}),
    RedFlag("data_sharing", "Your data may be shared or sold",
            (r"(?:share|sell|disclose) .{0,40}(?:personal (?:data|information))? .{0,20}(?:third part(?:y|ies)|partners|affiliates)",),
            "medium", "Your personal information may be passed to other companies.",
            {"consumer": "high"}),
    RedFlag("confidentiality_perpetual", "Confidentiality with no end date",
            (r"(?:perpetual|indefinite(?:ly)?|survive .{0,30}termination)",),
            "low", "Some duties may continue even after the agreement ends.",
            {}),
    RedFlag("personal_guarantee", "Personal guarantee",
            (r"personal(?:ly)? guarant", r"jointly and severally"),
            "high", "You may be personally responsible for debts, not just a company.",
            {"small_business": "high"}),
    RedFlag("waiver_of_rights", "You waive legal rights",
            (r"waive[sd]? (?:any|all) (?:rights?|claims?)", r"release[sd]? .{0,30}(?:all|any) claims"),
            "high", "You may lose the ability to make a claim later.",
            {}),
)


# --------------------------------------------------------------------------- #
# Urgency
# --------------------------------------------------------------------------- #
_URGENT_PATTERNS = tuple(re.compile(p) for p in (
    r"\bsummons\b", r"\beviction\b", r"notice to (?:quit|vacate)", r"cease and desist",
    r"\bfinal (?:notice|demand|warning)\b", r"\bhearing\b", r"\bwarrant\b", r"\bforeclos",
    r"appear (?:before|in) (?:the )?court", r"legal (?:action|proceedings) will be",
))
_DEADLINE_PATTERN = re.compile(
    r"within\s+(\d{1,3})\s*(?:\(\w+\)\s*)?(calendar\s+|business\s+|working\s+)?(day|days|hours|week|weeks)",
    re.IGNORECASE,
)


@dataclass
class Urgency:
    level: str                      # "none" | "elevated" | "urgent"
    reasons: list[str] = field(default_factory=list)
    shortest_deadline_days: int | None = None


def detect_urgency(text: str) -> Urgency:
    lower = text.lower()
    reasons = [m.group(0) for m in (p.search(lower) for p in _URGENT_PATTERNS) if m]
    shortest: int | None = None
    for match in _DEADLINE_PATTERN.finditer(text):
        amount, unit = int(match.group(1)), match.group(3).lower()
        days = amount / 24 if unit.startswith("hour") else amount * 7 if unit.startswith("week") else amount
        days_int = max(0, int(days))
        shortest = days_int if shortest is None else min(shortest, days_int)
    if shortest is not None and shortest <= 14:
        reasons.append(f"deadline of {shortest} day(s) mentioned")

    if reasons and (len(reasons) >= 2 or any(k in " ".join(reasons) for k in ("summons", "eviction", "hearing", "warrant", "court"))):
        level = "urgent"
    elif reasons:
        level = "elevated"
    else:
        level = "none"
    return Urgency(level=level, reasons=reasons[:5], shortest_deadline_days=shortest)


# --------------------------------------------------------------------------- #
# Clause extraction helpers
# --------------------------------------------------------------------------- #
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+(?=[A-Z0-9(\"'])|\n{2,}")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if len(s.strip()) > 3]


def _sentence_around(text: str, start: int, end: int, limit: int = 320) -> str:
    left = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start))
    right_candidates = [i for i in (text.find(". ", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    sentence = text[left + 1:right].strip()
    if len(sentence) > limit:
        mid = (start - left)
        lo = max(0, mid - limit // 2)
        sentence = sentence[lo:lo + limit].strip()
    return sentence


def severity_for(flag: RedFlag, role: str) -> str:
    return flag.role_severity.get(role, flag.severity)


def scan_red_flags(text: str, role: str = "general") -> list[dict]:
    """Return one finding per triggered red flag, sorted by severity."""
    findings = []
    for flag in RED_FLAGS:
        for pattern in flag.compiled:
            match = pattern.search(text)
            if match:
                findings.append({
                    "id": flag.id,
                    "title": flag.title,
                    "risk": severity_for(flag, role),
                    "quote": _sentence_around(text, match.start(), match.end()),
                    "why_it_matters": flag.why,
                    "source": "rules",
                })
                break
    findings.sort(key=lambda f: -SEVERITY_ORDER[f["risk"]])
    return findings


# --------------------------------------------------------------------------- #
# Readability
# --------------------------------------------------------------------------- #
def _syllables(word: str) -> int:
    word = word.lower()
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups) - (1 if word.endswith("e") and len(groups) > 1 else 0)
    return max(1, count)


def readability(text: str) -> dict:
    """Flesch reading ease (0-100, higher = easier) with a plain-language label."""
    words = re.findall(r"[A-Za-z]+", text)
    sentences = max(1, len(split_sentences(text)))
    if not words:
        return {"score": None, "label": "Not enough text"}
    syllables = sum(_syllables(w) for w in words)
    score = 206.835 - 1.015 * (len(words) / sentences) - 84.6 * (syllables / len(words))
    score = round(max(0.0, min(100.0, score)), 1)
    if score >= 60:
        label = "Easy to read"
    elif score >= 40:
        label = "Fairly hard to read"
    elif score >= 20:
        label = "Hard to read"
    else:
        label = "Very hard to read (typical legal text)"
    return {"score": score, "label": label}


# --------------------------------------------------------------------------- #
# Prompt injection
# --------------------------------------------------------------------------- #
_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts)|"
    r"you are now|system prompt|disregard (?:the )?(?:above|instructions)|"
    r"act as (?:an? )?(?:ai|assistant|dan)",
    re.IGNORECASE,
)


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


# --------------------------------------------------------------------------- #
# Grounding: locate quotes inside the document
# --------------------------------------------------------------------------- #
_QUOTE_NORMALISE = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"})


def find_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """Find ``needle`` in ``haystack`` ignoring case, whitespace and quote style.

    Character-for-character normalisation keeps offsets valid in the original.
    """
    words = re.findall(r"\S+", (needle or "").translate(_QUOTE_NORMALISE))
    words = [w.strip(".,;:\"'()[]…") for w in words]
    words = [w for w in words if w][:30]
    if len(words) < 3:
        return None
    pattern = r"[\s\W]+".join(re.escape(w) for w in words)
    match = re.search(pattern, haystack.translate(_QUOTE_NORMALISE), re.IGNORECASE)
    return (match.start(), match.end()) if match else None
