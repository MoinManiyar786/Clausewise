"""ClauseWise application factory."""
from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify

from .config import Settings
from .routes import api
from .services.assistant import LegalAssistant
from .services.cache import TTLCache
from .services.llm import GeminiClient, JSONModel
from .services.ratelimit import RateLimiter

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_SECURITY_HEADERS = {
    # No inline scripts/styles, no third-party origins: the UI is fully self-hosted.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def create_app(settings: Settings | None = None, llm: JSONModel | None = None) -> Flask:
    """Build the app. ``settings`` and ``llm`` can be injected for testing."""
    load_dotenv()
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config.update(
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
        MAX_DOC_CHARS=settings.max_doc_chars,
        MAX_PDF_PAGES=settings.max_pdf_pages,
        JSON_SORT_KEYS=False,
    )

    llm = llm or GeminiClient(settings.gemini_api_key, settings.gemini_model, settings.llm_timeout_seconds)
    app.extensions["assistant"] = LegalAssistant(
        llm, TTLCache(settings.cache_size, settings.cache_ttl_seconds),
        settings.max_doc_chars, settings.max_question_chars,
    )
    app.extensions["rate_limiter"] = RateLimiter(settings.rate_limit_per_minute)
    app.register_blueprint(api)

    @app.after_request
    def _headers(response):
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"  # responses may contain document text
        return response

    @app.errorhandler(404)
    def _not_found(_exc):
        return jsonify(error="Not found."), 404

    @app.errorhandler(405)
    def _method(_exc):
        return jsonify(error="Method not allowed."), 405

    if not llm.available:
        app.logger.warning("GEMINI_API_KEY not set: running in offline rule-based mode.")
    return app
