"""ClauseWise application factory."""
from __future__ import annotations

import gzip
import logging
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

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
    "Cross-Origin-Resource-Policy": "same-origin",
}

_COMPRESSIBLE = ("text/", "application/json", "application/javascript", "image/svg+xml")
_MIN_COMPRESS_BYTES = 1024


def _is_cross_site() -> bool:
    """True when a state-changing request comes from another site (CSRF defence).

    Modern browsers send ``Sec-Fetch-Site``; older ones send ``Origin``. Requests
    without either (curl, server-to-server) are allowed and still rate-limited.
    """
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site:
        return fetch_site not in ("same-origin", "none")
    origin = request.headers.get("Origin")
    return bool(origin) and urlsplit(origin).netloc != request.host


def _compress(response: Response) -> Response:
    """Gzip text responses. Documents and analysis JSON shrink by roughly 70-80%."""
    if (response.status_code != 200 or "gzip" not in request.headers.get("Accept-Encoding", "")
            or response.headers.get("Content-Encoding")
            or not (response.mimetype or "").startswith(_COMPRESSIBLE)):
        return response
    source = response.response  # may be an open file wrapper for static files
    response.direct_passthrough = False  # static files are small; read them into memory
    data = response.get_data()
    if hasattr(source, "close"):
        source.close()  # get_data() buffered the body, so release the file handle now
    if len(data) < _MIN_COMPRESS_BYTES:
        return response
    response.set_data(gzip.compress(data, compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response


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
        SEND_FILE_MAX_AGE_DEFAULT=settings.static_max_age,
    )
    if settings.trust_proxy_hops:
        # Only trust X-Forwarded-* from the configured number of proxies, so clients
        # can't spoof their IP to dodge rate limits.
        n = settings.trust_proxy_hops
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)  # type: ignore[method-assign]

    llm = llm or GeminiClient(settings.gemini_api_key, settings.gemini_model, settings.llm_timeout_seconds,
                              thinking_budget=settings.llm_thinking_budget)
    app.extensions["assistant"] = LegalAssistant(
        llm, TTLCache(settings.cache_size, settings.cache_ttl_seconds),
        settings.max_doc_chars, settings.max_question_chars,
    )
    app.extensions["rate_limiter"] = RateLimiter(settings.rate_limit_per_minute)
    app.register_blueprint(api)

    @app.before_request
    def _guard():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if _is_cross_site():
                return jsonify(error="Cross-site requests are not allowed."), 403
            if request.mimetype != "multipart/form-data":
                # JSON bodies get a much tighter cap than file uploads.
                request.max_content_length = settings.max_json_bytes
                if (request.content_length or 0) > settings.max_json_bytes:
                    return jsonify(error="The request is too large."), 413
        return None

    @app.after_request
    def _headers(response):
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"  # responses may contain document text
        elif request.path == "/":
            response.headers["Cache-Control"] = "no-cache"  # always revalidate the page shell
        return _compress(response)

    @app.errorhandler(404)
    def _not_found(_exc):
        return jsonify(error="Not found."), 404

    @app.errorhandler(405)
    def _method(_exc):
        return jsonify(error="Method not allowed."), 405

    @app.errorhandler(413)
    def _too_large(_exc):
        return jsonify(error="The request is too large."), 413

    if not llm.available:
        app.logger.warning("GEMINI_API_KEY not set: running in offline rule-based mode.")
    return app
