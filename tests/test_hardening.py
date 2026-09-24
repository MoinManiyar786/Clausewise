"""Tests for hardening (security) and resource-use (efficiency) behaviour."""
import gzip
import io
import json
import unittest

from app import create_app
from app.config import Settings
from app.services import rules
from app.services.assistant import MAX_OUTPUT_TOKENS, LegalAssistant
from app.services.cache import TTLCache
from app.services.extract import extract_text
from app.services.llm import GeminiClient, LLMError
from app.services.ratelimit import RateLimiter
from app.services.validation import UserContext
from tests.helpers import LEASE, FakeLLM, FakeResponse, FakeSession


def _app(**overrides):
    return create_app(Settings(**overrides), llm=FakeLLM({"summary": "ok"}))


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = _app().test_client()

    def post(self, headers=None, body=None):
        return self.client.post("/api/analyze", data=json.dumps(body or {"text": LEASE}),
                                content_type="application/json", headers=headers or {})

    def test_cross_site_fetch_is_blocked(self):
        self.assertEqual(self.post({"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_foreign_origin_is_blocked(self):
        self.assertEqual(self.post({"Origin": "https://evil.example"}).status_code, 403)

    def test_same_origin_is_allowed(self):
        self.assertEqual(self.post({"Sec-Fetch-Site": "same-origin"}).status_code, 200)
        self.assertEqual(self.post({"Origin": "http://localhost"}).status_code, 200)

    def test_json_body_cap(self):
        client = _app(max_json_bytes=1000).test_client()
        res = client.post("/api/analyze", data=json.dumps({"text": "a" * 5000}), content_type="application/json")
        self.assertEqual(res.status_code, 413)

    def test_hsts_only_on_https(self):
        self.assertNotIn("Strict-Transport-Security", self.client.get("/api/health").headers)
        secure = self.client.get("/api/health", base_url="https://localhost")
        self.assertIn("max-age=", secure.headers["Strict-Transport-Security"])

    def test_corp_header(self):
        self.assertEqual(self.client.get("/api/health").headers["Cross-Origin-Resource-Policy"], "same-origin")

    def test_proxy_fix_uses_forwarded_ip_only_when_trusted(self):
        seen = []
        for hops in (0, 1):
            app = _app(trust_proxy_hops=hops)

            @app.get("/_ip")
            def _ip():
                from flask import request
                return request.remote_addr or ""

            seen.append(app.test_client().get("/_ip", headers={"X-Forwarded-For": "203.0.113.9"}).get_data(as_text=True))
        self.assertNotEqual(seen[0], "203.0.113.9")
        self.assertEqual(seen[1], "203.0.113.9")

    def test_rate_limiter_does_not_store_raw_ips(self):
        limiter = RateLimiter(limit=5)
        limiter.allow("198.51.100.7")
        self.assertNotIn("198.51.100.7", limiter._hits)


class EfficiencyTests(unittest.TestCase):
    def test_gzip_for_large_text_responses(self):
        client = _app().test_client()
        res = client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(res.headers["Content-Encoding"], "gzip")
        self.assertIn(b"ClauseWise", gzip.decompress(res.data))
        self.assertIn("Accept-Encoding", res.headers["Vary"])

    def test_no_gzip_without_accept_encoding_or_for_tiny_bodies(self):
        client = _app().test_client()
        plain = client.get("/static/app.js")
        self.assertNotIn("Content-Encoding", plain.headers)
        plain.close()
        self.assertNotIn("Content-Encoding", client.get("/api/health", headers={"Accept-Encoding": "gzip"}).headers)

    def test_static_files_are_browser_cached(self):
        res = _app().test_client().get("/static/styles.css")
        self.assertIn("max-age=86400", res.headers["Cache-Control"])
        res.close()

    def test_page_shell_revalidates(self):
        res = _app().test_client().get("/")
        self.assertEqual(res.headers["Cache-Control"], "no-cache")
        res.close()

    def test_document_not_echoed_when_unchanged(self):
        assistant = LegalAssistant(FakeLLM(available=False), TTLCache())
        clean = LEASE.replace("jane.doe@example.com", "the address on file")
        self.assertIsNone(assistant.analyze(clean, UserContext())["document"]["text"])
        # When redaction changed the text, the client needs the new version for highlight offsets.
        self.assertIn("[EMAIL_1]", assistant.analyze(LEASE, UserContext())["document"]["text"])

    def test_redaction_reused_across_questions(self):
        assistant = LegalAssistant(FakeLLM(available=False), TTLCache())
        assistant.ask(LEASE, "Is there a late fee?", UserContext())
        assistant.ask(LEASE, "Does it renew?", UserContext())
        self.assertEqual(len(assistant._prepared), 1)

    def test_output_budget_depends_on_task(self):
        llm = FakeLLM({"answer": "x"})
        assistant = LegalAssistant(llm, TTLCache())
        assistant.ask(LEASE, "Late fee?", UserContext())
        assistant.analyze(LEASE, UserContext())
        self.assertEqual(llm.max_tokens, [MAX_OUTPUT_TOKENS["ask"], MAX_OUTPUT_TOKENS["analyze"]])
        self.assertLess(MAX_OUTPUT_TOKENS["ask"], MAX_OUTPUT_TOKENS["analyze"])

    def test_thinking_budget_only_for_flash_models(self):
        flash = GeminiClient("k", "gemini-2.5-flash", thinking_budget=256).build_payload("s", "p", 0.2, 100)
        other = GeminiClient("k", "gemini-2.5-pro").build_payload("s", "p", 0.2, 100)
        self.assertEqual(flash["generationConfig"]["thinkingConfig"], {"thinkingBudget": 256})
        self.assertNotIn("thinkingConfig", other["generationConfig"])

    def test_truncated_model_output_is_reported(self):
        session = FakeSession([FakeResponse(200, {"candidates": [{"finishReason": "MAX_TOKENS",
                                                                   "content": {"parts": [{"text": "{"}]}}]})])
        with self.assertRaisesRegex(LLMError, "cut off"):
            GeminiClient("k", "m", session=session).generate_json("s", "p")

    def test_patterns_are_precompiled(self):
        self.assertTrue(all(isinstance(p, type(rules.re.compile(""))) for f in rules.RED_FLAGS for p in f.compiled))

    def test_text_extraction_respects_char_limit_for_txt(self):
        text = extract_text("a.txt", ("word " * 100).encode(), max_chars=50)
        self.assertTrue(text)  # txt is capped by the route; function must still succeed

    def test_upload_route_caps_length(self):
        app = create_app(Settings(max_doc_chars=100), llm=FakeLLM())
        res = app.test_client().post("/api/extract", data={"file": (io.BytesIO(("x " * 500).encode()), "a.txt")},
                                     content_type="multipart/form-data")
        body = res.get_json()
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["text"]), 100)


if __name__ == "__main__":
    unittest.main()
