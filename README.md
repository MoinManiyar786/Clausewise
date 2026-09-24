# ClauseWise — know what you're signing

ClauseWise is a GenAI assistant that helps everyday people understand legal documents. Paste or upload a lease, job offer, NDA, loan agreement, terms of service or legal notice and ClauseWise will explain it in plain language, mark the risky clauses directly in your document, answer your questions with verified quotes, compare two versions, and prepare you for a conversation with a lawyer.

> ClauseWise provides legal **information**, not legal advice. It is designed to help people understand and prepare, and it actively recommends a qualified professional when the stakes are high.

## Chosen vertical

**Legal information access** for non-lawyers. The primary persona is a person on the weaker side of a standard-form agreement: a tenant signing a lease, an employee reading an offer letter, a freelancer reviewing a client contract, a consumer accepting app terms, or someone who has just received a legal notice. These people usually can't afford a lawyer to read every document, and don't know which parts deserve a lawyer's attention.

## What it does

| Tool | What the user gets |
|---|---|
| **Understand it** | Plain summary, key points, parties, every risky clause (with risk level, who it favours, and what it means), obligations table, deadlines, inconsistencies, missing protections, a legal glossary, next steps and an interactive checklist. The original document is shown back with risky passages highlighted. |
| **Ask a question** | Answers grounded only in the document, with quotes that are checked against the source text. Suggests follow-up questions. |
| **Compare versions** | Differences that change money, rights, obligations or risk; which version is better *for you*; risks added or removed; questions to raise before signing. |
| **Prepare for a lawyer** | A neutral situation summary, the right kind of professional, questions to ask, documents to gather, facts to write down, possible paths with pros and cons, and a draft email to book a consultation. Works with or without a document. |

## Approach and decision logic

ClauseWise combines a **deterministic rules engine** with **Gemini**. The rules engine is fast, free, testable and always available. The model adds understanding and plain-language explanation. Each result is the product of both.

```
request ─► validate & clean ─► redact PII ─► rules engine ─────────────┐
                                              │ doc type, red flags,    │
                                              │ urgency, readability    │
                                              ▼                         │
                                    cached Gemini call (JSON mode)      │
                                              │  fails / no key? ───────┤ offline fallback
                                              ▼                         │
                         schema coercion ─► quote verification ─► merge ◄┘
                                              ▼
                                  decisions (professional help level)
```

Decisions that adapt to the user's context:

1. **Risk is judged from the user's side.** The user picks a role (tenant, landlord, employee, employer, freelancer, client, consumer, small business). Each red-flag rule carries role-specific severity: a non-compete is *high* risk for an employee but *low* for an employer; a deposit-forfeiture clause is *high* for a tenant but *low* for a landlord. The role is also passed to the model with the instruction to evaluate risk from that point of view.
2. **Urgency detection drives escalation.** Summons, eviction, notice to quit, hearings, final demands, and short deadlines ("within 7 days") are detected locally. The professional-help recommendation follows transparent rules: `now` for urgent documents, `recommended` for high-stakes document types (court notices, loans) or three or more high-risk clauses, otherwise `optional`. The reasons are shown to the user.
3. **Explanation depth and language.** Reading level (simple, some legal terms, full detail) and output language (12 options) shape the prompt. Exact quotes stay in the document's original language.
4. **Jurisdiction awareness.** If the user gives a country or state, it's used; if not, the model is told not to state country-specific law as fact.
5. **Grounding checks.** Every quote the model returns is located in the source text (tolerant of whitespace, case and quote style). Quotes that can't be found are labelled "Exact wording not found in your document" instead of being presented as fact. If an answer claims to be based on the document but none of its quotes are verified, its confidence is downgraded and a warning is shown.
6. **Nothing important is dropped.** Risks found by the rules engine that the model missed are merged into the clause list.
7. **Graceful degradation.** Without an API key, or if the model call fails, every tool still returns a useful result: rule-based clause detection, keyword passage retrieval for questions, sentence-level diffing for comparison, and a template brief tailored to the document type.

## Security and responsible AI

- **Privacy by default.** Emails, phone numbers, card numbers (Luhn-checked), SSNs, Aadhaar, PAN and IBANs are replaced with numbered placeholders *before* anything is sent to the model. Users can switch this off. Uploaded files are processed in memory and never written to disk. JSON responses are sent with `Cache-Control: no-store`.
- **Prompt-injection resistance.** Document text is wrapped in `<document>` tags, closing tags inside the text are neutralised, and the system prompt declares document content untrusted. Documents containing instruction-like text are flagged to the user.
- **Untrusted model output.** Every field is type-checked, trimmed, length-capped and forced into allowed enum values before reaching the browser.
- **XSS-safe UI.** The front end builds all dynamic content with `textContent`; it never uses `innerHTML` with data.
- **HTTP hardening.** Strict Content-Security-Policy (`default-src 'self'`, no inline scripts, no third-party origins), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, a restrictive Permissions-Policy.
- **Abuse limits.** Per-client rate limiting (20 requests/minute by default), 5 MB upload cap, 60-page PDF cap, 60,000-character document cap, file type verified by content signature.
- **Secret handling.** The API key is read from `.env` (git-ignored), sent in a request header rather than the URL, and never logged or returned. Internal errors return a generic message.
- **Honest scope.** A disclaimer is shown on every result, and the model is instructed to explain options rather than tell users what to do.

## Efficiency

- One model call per action, using Gemini's JSON response mode at low temperature: no multi-step chains.
- An in-memory LRU + TTL cache keyed by a SHA-256 hash of the prompt means repeated requests are instant and free.
- Local rules run in milliseconds and give the model hints, which keeps prompts focused.
- Only four small dependencies (Flask, requests, python-dotenv, pypdf); Gemini is called over REST instead of pulling in an SDK. No front-end framework, no external fonts or CDNs. The whole repository is well under 1 MB.

## Accessibility

- Semantic landmarks, headings in order, a skip link, labelled form controls and fieldsets.
- Tabs follow the WAI-ARIA pattern with arrow-key, Home and End navigation.
- Status and errors are announced through `aria-live` regions and `role="alert"`; focus moves to results when they load.
- Risk is never shown by colour alone: every highlight has a text label (hidden text for screen readers, visible tags on cards), and high risk also gets a wavy underline.
- Text-size controls, a high-contrast mode, `forced-colors` support and `prefers-reduced-motion` support.
- 44 px minimum touch targets, visible focus rings, colour contrast meeting WCAG AA, responsive down to small phones.
- "Read summary aloud" uses the browser's speech synthesis; results can be downloaded as text or printed cleanly.
- Plain-language output by default, and 12 output languages.

## Project structure

```
clausewise/
├── app/
│   ├── __init__.py          # app factory, security headers, dependency wiring
│   ├── config.py            # settings from environment
│   ├── routes.py            # thin HTTP layer + error mapping
│   └── services/
│       ├── assistant.py     # orchestration & decision logic, offline fallbacks
│       ├── rules.py         # doc type, red flags, urgency, readability, grounding
│       ├── llm.py           # Gemini REST client with retries
│       ├── prompts.py       # system + task prompts
│       ├── validation.py    # input validation, output schema coercion
│       ├── redact.py        # PII redaction
│       ├── extract.py       # PDF/TXT/MD text extraction
│       ├── cache.py         # TTL + LRU cache
│       └── ratelimit.py     # sliding-window rate limiter
├── static/                  # index.html, styles.css, app.js, samples.js
├── tests/                   # 66 tests: unit, logic and API
├── .github/workflows/       # CI running the test suite
├── .env.example
├── requirements.txt
└── run.py
```

## Running it

Requirements: Python 3.10 or newer.

```bash
git clone <your-repo-url> clausewise && cd clausewise
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then add your GEMINI_API_KEY
python run.py
```

Open http://127.0.0.1:8000 and click **Try a sample lease**. Without a key the app runs in offline mode and says so in the page header.

Environment variables (`.env`):

| Name | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | For AI features | Google AI Studio key |
| `GEMINI_MODEL` | No | Defaults to `gemini-2.5-flash` |

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest            # or: python -m unittest discover
```

The suite uses a fake model, so it needs no network or API key. It covers the rules engine (document types, role-weighted severity, urgency, readability, injection detection, quote matching), redaction, input validation and output coercion, the cache and rate limiter, the Gemini client (header-based auth, retries on transient errors, no retry on client errors, blocked responses), the decision logic (hallucinated quotes flagged, missed risks merged, PII never reaching the model, caching, fallback on failure, escalation for urgent documents, answer downgrading when ungrounded), and every API endpoint including security headers, upload validation, rate limiting and error-message hygiene.

## API

All endpoints accept and return JSON. `context` is optional: `{role, jurisdiction, reading_level, language, redact_pii}`.

| Method | Path | Body |
|---|---|---|
| GET | `/api/health` | — |
| POST | `/api/analyze` | `{text, context}` |
| POST | `/api/ask` | `{text, question, history?, context}` |
| POST | `/api/compare` | `{text_a, text_b, context}` |
| POST | `/api/prepare` | `{text?, situation, context}` |
| POST | `/api/extract` | multipart `file` (.pdf, .txt, .md) |

## Assumptions

- Users paste or upload text-based documents. Scanned image PDFs need OCR, which is out of scope; the app tells the user to paste the text instead.
- Documents are up to about 60,000 characters (roughly 25–30 pages), which covers most consumer contracts in a single model call.
- The rate limiter and cache are in-memory, which suits a single-instance deployment. A multi-instance deployment would move both to a shared store such as Redis.
- Regex-based redaction catches common identifiers but not names or addresses; users handling very sensitive documents can remove those manually.
- Rule-based risk patterns are written for English documents. The model can read other languages, but the offline fallback is English-only.
- ClauseWise is not a substitute for a lawyer, and says so on every result.
