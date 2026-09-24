# Security

## Reporting a vulnerability
Please open a private security advisory on GitHub rather than a public issue.

## Design summary
- Personal identifiers are redacted before any text is sent to the AI model.
- Documents are processed in memory and never written to disk or logged.
- Model output is treated as untrusted and validated against a schema.
- The browser UI never inserts dynamic content as HTML.
- Strict CSP, cross-site request blocking, HSTS on HTTPS, request size caps and per-client rate limiting.
- The only secret, `GEMINI_API_KEY`, lives in a git-ignored `.env` file and is sent in a request header, never a URL.
- Dependencies have minimum versions above known-vulnerable releases and are monitored by Dependabot.
