"""HTTP API. Thin layer: parse JSON, delegate to LegalAssistant, map errors."""
from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

from .services.extract import extract_text
from .services.validation import UserContext, ValidationError

log = logging.getLogger(__name__)
api = Blueprint("api", __name__)


def _client_id() -> str:
    # Behind a trusted proxy, configure ProxyFix instead of reading headers directly.
    return request.remote_addr or "unknown"


def rate_limited(view: Callable):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.extensions["rate_limiter"].allow(_client_id()):
            return jsonify(error="Too many requests. Please wait a minute and try again."), 429
        return view(*args, **kwargs)
    return wrapper


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object.")
    return data


def _assistant():
    return current_app.extensions["assistant"]


@api.errorhandler(ValidationError)
def _validation_error(exc: ValidationError):
    return jsonify(error=str(exc)), 400


@api.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    return jsonify(error="The upload is too large. The limit is 5 MB."), 413


@api.errorhandler(Exception)
def _unexpected(exc: Exception):
    log.exception("Unhandled error: %s", type(exc).__name__)
    return jsonify(error="Something went wrong on our side. Please try again."), 500


@api.get("/")
def index():
    return send_from_directory(current_app.static_folder, "index.html")


@api.get("/api/health")
def health():
    return jsonify(status="ok", ai_available=_assistant().llm.available)


@api.post("/api/analyze")
@rate_limited
def analyze():
    body = _json_body()
    return jsonify(_assistant().analyze(body.get("text"), UserContext.from_payload(body.get("context"))))


@api.post("/api/ask")
@rate_limited
def ask():
    body = _json_body()
    history = body.get("history") if isinstance(body.get("history"), list) else []
    return jsonify(_assistant().ask(body.get("text"), body.get("question"),
                                    UserContext.from_payload(body.get("context")), history))


@api.post("/api/compare")
@rate_limited
def compare():
    body = _json_body()
    return jsonify(_assistant().compare(body.get("text_a"), body.get("text_b"),
                                        UserContext.from_payload(body.get("context"))))


@api.post("/api/prepare")
@rate_limited
def prepare():
    body = _json_body()
    return jsonify(_assistant().prepare(body.get("text"), body.get("situation"),
                                        UserContext.from_payload(body.get("context"))))


@api.post("/api/extract")
@rate_limited
def extract():
    upload = request.files.get("file")
    if upload is None:
        raise ValidationError("Please choose a file to upload.")
    text = extract_text(upload.filename or "", upload.read(), current_app.config["MAX_PDF_PAGES"])
    limit = current_app.config["MAX_DOC_CHARS"]
    return jsonify(text=text[:limit], truncated=len(text) > limit, characters=min(len(text), limit))
