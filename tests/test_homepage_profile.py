"""Homepage imports stay local, bounded, owner-specific, and address-stable."""

import hashlib
import json
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.homepage_profile import (
    _fetch_page, _PublicRedirects, normalize_homepage_url, parse_homepage_profile,
)

PUBLIC_HOST = [(2, 1, 6, "", ("93.184.216.34", 443))]
BIO = b"<main><h1>Example Researcher</h1><p>Assistant Professor researching optics, imaging, and computational science.</p></main>"


class Client:
    model = "synthetic-installed-local-model"
    def __init__(self, raw=None):
        self.raw = raw if raw is not None else {"name": "Example Researcher", "position": "Assistant Professor", "keywords": ["optics"]}
        self.calls = []
    def chat(self, messages, schema=None):
        self.calls.append((messages, schema))
        return {"message": {"content": json.dumps(self.raw)}}


class HomepageProfileTests(unittest.TestCase):
    def test_readable_homepage_without_identity_keeps_source_metadata_and_text_record(self):
        html = (b"<main><p>Office hours and contact information are listed here. "
                b"Updates are posted throughout the academic year.</p></main>")
        raw = {"name": None, "keywords": [], "research_summary": None,
               "research_tracks": [], "position": None, "active_projects": []}
        client = Client(raw)
        entered, effective = "https://example.org/contact", "https://example.org/contact/"
        with tempfile.TemporaryDirectory() as temporary:
            with patch("grantfinder.homepage_profile._fetch_page", return_value=(html, "text/html", effective)):
                profile = parse_homepage_profile(entered, client, temporary)
            identifier = "cv_" + hashlib.sha256(entered.encode()).hexdigest()[:16]
            record = json.loads((Path(temporary) / (identifier + ".json")).read_text())
        for field in ("name", "research_summary", "position", "phd_date", "nationality"):
            self.assertIsNone(profile[field])
        for field in ("keywords", "research_tracks", "active_projects"):
            self.assertEqual(profile[field], [])
        self.assertEqual(profile["id"], identifier)
        self.assertFalse(profile["documents"]["cv"])
        source = profile["cv_import"]
        self.assertEqual(source["method"], "homepage_ollama_local")
        self.assertEqual(source["url"], entered)
        self.assertEqual(source["source_urls"], [effective])
        self.assertEqual(source["model"], client.model)
        self.assertIsNone(source["filename"])
        self.assertIsNotNone(datetime.fromisoformat(source["checked_at"]).tzinfo)
        self.assertEqual(record["url"], entered)
        self.assertEqual(record["source_urls"], source["source_urls"])
        self.assertEqual(record["checked_at"], source["checked_at"])
        self.assertIn("Office hours and contact information", record["text"])
        self.assertEqual(len(client.calls), 1)
        self.assertIn(record["text"], client.calls[0][0][1]["content"])

    def test_empty_model_object_is_allowed_after_readable_homepage_fetch(self):
        client = Client({})
        with patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO, "text/html", "https://example.org/")):
            profile = parse_homepage_profile("https://example.org/", client)
        self.assertIsNone(profile["name"])
        self.assertIsNone(profile["position"])
        self.assertIsNone(profile["research_summary"])
        self.assertEqual(profile["keywords"], [])
        self.assertEqual(profile["research_tracks"], [])
        self.assertEqual(profile["cv_import"]["source_urls"], ["https://example.org/"])
        self.assertEqual(len(client.calls), 1)

    def test_missing_identity_does_not_bypass_model_json_or_date_validation(self):
        for raw in ([], {"phd_date": "2026-02-30"}):
            with self.subTest(raw=raw), \
                    patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO, "text/html", "https://example.org/")):
                with self.assertRaises(ValueError):
                    parse_homepage_profile("https://example.org/", Client(raw))
        client = Client()
        client.chat = lambda *args, **kwargs: {"message": {"content": "not JSON"}}
        with patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO, "text/html", "https://example.org/")):
            with self.assertRaisesRegex(ValueError, "JSON"):
                parse_homepage_profile("https://example.org/", client)

    def test_homepage_uses_local_cv_parser_and_preserves_missing_country(self):
        client = Client()
        with patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO, "text/html", "https://example.org/")):
            profile = parse_homepage_profile("example.org", client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(profile["country"])
        self.assertIsNone(profile["nationality"])
        self.assertFalse(profile["documents"]["cv"])
        self.assertEqual(profile["cv_import"]["method"], "homepage_ollama_local")
        self.assertEqual(profile["cv_import"]["url"], "https://example.org/")
        self.assertEqual(profile["cv_import"]["source_urls"], ["https://example.org/"])
        self.assertIn("학생·공동저자", client.calls[0][0][0]["content"])
        self.assertIn("<cv_document>", client.calls[0][0][1]["content"])

    def test_profile_id_comes_from_canonical_entered_url_not_page_or_claimed_id(self):
        client = Client({"id": "someone-else", "name": "First Owner", "keywords": ["optics"]})
        with patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO, "text/html", "https://example.org/")):
            first = parse_homepage_profile("example.org/#intro", client)
        client.raw = {"id": first["id"], "name": "Changed text", "keywords": ["mathematics"]}
        with patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO.replace(b"optics", b"mathematics"), "text/html", "https://example.org/")):
            changed = parse_homepage_profile("https://EXAMPLE.org/?utm_source=x", client)
            different = parse_homepage_profile("https://another.example.org/", client)
        self.assertEqual(first["id"], changed["id"])
        self.assertNotEqual(first["id"], different["id"])
        self.assertNotEqual(first["id"], "someone-else")

    def test_main_plus_only_two_same_origin_relevant_pages_are_fetched(self):
        links = (b'<nav><a href="/privacy">Privacy</a><a href="/research">Research</a>'
                 b'<a href="https://external.org/cv">External CV</a><a href="/about">About</a>'
                 b'<a href="/cv.pdf">CV</a><a href="/profile">Profile</a></nav>')
        called = []
        def fetch(url, origin=None):
            called.append((url, origin))
            return (BIO + links if len(called) == 1 else BIO), "text/html", url
        client = Client()
        with patch("grantfinder.homepage_profile._fetch_page", side_effect=fetch):
            profile = parse_homepage_profile("https://researcher.example.org/", client)
        self.assertEqual([item[0] for item in called], ["https://researcher.example.org/", "https://researcher.example.org/cv.pdf", "https://researcher.example.org/about"])
        self.assertEqual(len(profile["cv_import"]["source_urls"]), 3)
        self.assertTrue(all(origin == ("https", "researcher.example.org", 443) for _, origin in called[1:]))

    def test_private_hosts_credentials_and_private_redirects_are_rejected(self):
        for address in ("http://127.0.0.1/", "http://[::1]/", "http://localhost/", "https://user:pass@example.org/", "file:///private"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                normalize_homepage_url(address)
        with patch("grantfinder.homepage_profile.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
            with self.assertRaisesRegex(ValueError, "공개"):
                _fetch_page("https://example.org/")
        handler = _PublicRedirects()
        request = urllib.request.Request("https://example.org/")
        with self.assertRaises(ValueError):
            handler.redirect_request(request, None, 302, "redirect", {}, "http://169.254.169.254/latest")
        with patch("grantfinder.homepage_profile.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.10", 80))]):
            with self.assertRaisesRegex(ValueError, "공개"):
                handler.redirect_request(request, None, 302, "redirect", {}, "http://private-alias.example.org/")

    def test_child_redirect_cannot_escape_its_original_site(self):
        handler = _PublicRedirects(("https", "example.org", 443))
        with patch("grantfinder.homepage_profile.socket.getaddrinfo", return_value=PUBLIC_HOST):
            with self.assertRaisesRegex(ValueError, "같은 홈페이지"):
                handler.redirect_request(urllib.request.Request("https://example.org/cv"), None, 302, "redirect", {}, "https://another.org/cv")

    def test_page_size_limit_reads_only_limit_plus_one_bytes(self):
        class Response:
            headers = {"Content-Type": "text/html"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size):
                self.requested_size = size
                return b"x" * size
        response = Response()
        class Opener:
            def open(self, request, timeout): return response
        with patch("grantfinder.homepage_profile.socket.getaddrinfo", return_value=PUBLIC_HOST), \
                patch("grantfinder.homepage_profile.urllib.request.build_opener", return_value=Opener()), \
                patch("grantfinder.homepage_profile.MAX_PAGE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "2MB"):
                _fetch_page("https://example.org/")
        self.assertEqual(response.requested_size, 11)

    def test_empty_javascript_and_login_pages_do_not_call_model(self):
        client = Client()
        for html in (b"<main></main>", b"<main>Please enable JavaScript to continue using this website and its services.</main>",
                     b'<input type="password"><p>Sign in to the secure university services using your email and password.</p>'):
            with self.subTest(html=html), patch("grantfinder.homepage_profile._fetch_page", return_value=(html, "text/html", "https://example.org/")):
                with self.assertRaisesRegex(ValueError, "CV"):
                    parse_homepage_profile("https://example.org/", client)
        self.assertEqual(client.calls, [])

    def test_failed_refetch_does_not_silently_reuse_cached_text(self):
        client = Client()
        with tempfile.TemporaryDirectory() as temporary:
            with patch("grantfinder.homepage_profile._fetch_page", return_value=(BIO, "text/html", "https://example.org/")):
                parse_homepage_profile("https://example.org/", client, temporary)
            self.assertEqual(len(list(Path(temporary).glob("cv_*.json"))), 1)
            with patch("grantfinder.homepage_profile._fetch_page", side_effect=ValueError("unavailable")):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    parse_homepage_profile("https://example.org/", client, temporary)
        self.assertEqual(len(client.calls), 1)

    def test_pdf_homepage_uses_existing_pdf_text_extraction(self):
        client = Client()
        with patch("grantfinder.homepage_profile._fetch_page", return_value=(b"%PDF-mock", "application/pdf", "https://example.org/cv.pdf")), \
                patch("grantfinder.homepage_profile.extract_cv_text", return_value="Example Researcher has optics and computational imaging research experience.") as extract:
            profile = parse_homepage_profile("https://example.org/cv.pdf", client)
        extract.assert_called_once_with("homepage.pdf", b"%PDF-mock")
        self.assertEqual(profile["cv_import"]["source_urls"], ["https://example.org/cv.pdf"])


if __name__ == "__main__":
    unittest.main()
