"""Deterministic web-search evidence, URL identity, and cache behavior."""

import json
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.web_search import DDGSSearchProvider, normalize_url, search_web


class FakeProvider:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def search(self, query, max_results):
        self.calls.append((query, max_results))
        result = self.responses[query]
        if isinstance(result, Exception):
            raise result
        return result


class FakeDDGS:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def text(self, query, *, region, max_results, backend):
        self.calls.append({"query": query, "region": region, "max_results": max_results, "backend": backend})
        value = self.responses[backend]
        value = value(query) if callable(value) else value
        if isinstance(value, Exception):
            raise value
        return value


class WebSearchTests(unittest.TestCase):
    def fake_ddgs(self, responses):
        engine = FakeDDGS(responses)
        factory = Mock(return_value=engine)
        fake_module = SimpleNamespace(DDGS=factory)
        context = patch.dict(sys.modules, {"ddgs": fake_module})
        context.start()
        self.addCleanup(context.stop)
        return engine, factory

    def test_provider_uses_explicit_general_web_backend_and_valid_region(self):
        engine, factory = self.fake_ddgs({"duckduckgo": [{"title": "Call", "url": "https://example.org/call"}]})
        provider = DDGSSearchProvider()
        self.assertEqual(len(provider.search("연구비 공고", 4)), 1)
        self.assertEqual(engine.calls, [{"query": "연구비 공고", "region": "kr-ko", "max_results": 4, "backend": "duckduckgo"}])
        factory.assert_called_once_with(timeout=10)
        provider.search("research funding", 2)
        self.assertEqual(engine.calls[-1]["region"], "us-en")
        self.assertTrue(all(call["backend"] not in {"auto", "wikipedia", "grokipedia", "bing", "google"}
                            for call in engine.calls))

    def test_provider_falls_back_after_dns_failure_and_preserves_attempts(self):
        engine, _ = self.fake_ddgs({"duckduckgo": RuntimeError("DNSError: name resolution failed"),
                                    "brave": [{"title": "Grant", "url": "https://example.org/grant"}]})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["research funding"], Path(temporary))
        self.assertEqual(len(results), 1)
        self.assertEqual([call["backend"] for call in engine.calls], ["duckduckgo", "brave"])
        self.assertEqual(statuses[0]["status"], "ok")
        self.assertEqual([item["status"] for item in statuses[0]["backend_attempts"]], ["error", "ok"])
        self.assertIn("DNSError", statuses[0]["backend_attempts"][0]["error"])

    def test_provider_tries_next_engine_after_empty_or_malformed_reply(self):
        engine, _ = self.fake_ddgs({"duckduckgo": [], "brave": {"unexpected": "object"},
                                    "yahoo": [{"title": "Grant", "url": "https://example.org/grant"}]})
        provider = DDGSSearchProvider()
        self.assertEqual(len(provider.search("research funding", 4)), 1)
        self.assertEqual([call["backend"] for call in engine.calls], ["duckduckgo", "brave", "yahoo"])
        self.assertEqual([item["status"] for item in provider.last_attempts], ["empty", "error", "ok"])

    def test_provider_empty_sentinel_is_not_a_connection_failure(self):
        self.fake_ddgs({name: RuntimeError("No results found.") for name in ("duckduckgo", "brave", "yahoo")})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["research funding"], Path(temporary))
        self.assertEqual(results, [])
        self.assertEqual(statuses[0]["status"], "empty")
        self.assertTrue(all(item["status"] == "empty" for item in statuses[0]["backend_attempts"]))
        self.assertNotIn("error", statuses[0])

    def test_all_backend_failures_have_short_message_and_separate_details(self):
        self.fake_ddgs({name: RuntimeError(f"{name}: DNSError long diagnostic")
                        for name in ("duckduckgo", "brave", "yahoo")})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["research funding"], Path(temporary))
        self.assertEqual(results, [])
        status = statuses[0]
        self.assertEqual(status["status"], "error")
        self.assertLess(len(status["error"]), 100)
        self.assertNotIn("DNSError", status["error"])
        self.assertIn("DNSError", status["error_detail"])
        self.assertTrue(all(item["status"] == "error" for item in status["backend_attempts"]))
        self.assertFalse(status["cache_hit"])

    def test_no_results_with_a_failed_engine_remains_incomplete_not_empty(self):
        self.fake_ddgs({"duckduckgo": [], "brave": RuntimeError("timeout"), "yahoo": []})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["research funding"], Path(temporary))
        self.assertEqual(results, [])
        self.assertEqual(statuses[0]["status"], "error")
        self.assertEqual([item["status"] for item in statuses[0]["backend_attempts"]], ["empty", "error", "empty"])

    def test_backend_attempts_are_independent_for_each_query(self):
        engine, _ = self.fake_ddgs({name: (lambda query: [{"title": "Grant", "url": "https://example.org/grant"}]
                                          if query == "good" else RuntimeError("temporary failure"))
                                    for name in ("duckduckgo", "brave", "yahoo")})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["good", "bad"], Path(temporary))
        self.assertEqual(len(results), 1)
        self.assertEqual([item["status"] for item in statuses], ["ok", "error"])
        self.assertEqual(len(statuses[0]["backend_attempts"]), 1)
        self.assertEqual(len(statuses[1]["backend_attempts"]), 3)
        self.assertEqual(len(engine.calls), 4)

    def test_provider_cache_keeps_provenance_and_failed_refresh_keeps_success_bytes(self):
        engine, _ = self.fake_ddgs({"duckduckgo": RuntimeError("DNS failed"),
                                    "brave": [{"title": "Grant", "url": "https://example.org/grant"}]})
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            results, statuses = search_web(["research funding"], folder)
            cache = next(folder.glob("*.json"))
            before = cache.read_bytes()
            again, cached_statuses = search_web(["research funding"], folder)
            self.assertEqual(again, results)
            self.assertEqual(len(engine.calls), 2)
            self.assertTrue(cached_statuses[0]["cache_hit"])
            self.assertEqual(cached_statuses[0]["backend_attempts"], statuses[0]["backend_attempts"])
            engine.responses = {name: RuntimeError("DNS still failed") for name in ("duckduckgo", "brave", "yahoo")}
            failed, failed_statuses = search_web(["research funding"], folder, refresh=True)
            self.assertEqual(failed, [])
            self.assertEqual(failed_statuses[0]["status"], "error")
            self.assertFalse(failed_statuses[0]["cache_hit"])
            self.assertEqual(cache.read_bytes(), before)

    def test_new_strategy_does_not_reuse_or_modify_auto_backend_cache(self):
        self.fake_ddgs({"duckduckgo": [{"title": "New grant", "url": "https://example.org/new"}]})
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            key = hashlib.sha256(b"ddgs-text-v1\n4\nresearch funding").hexdigest()
            old = folder / f"{key}.json"
            old.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                                       "results": [{"title": "Old encyclopedia result", "url": "https://en.wikipedia.org/old"}]}))
            before = old.read_bytes()
            results, statuses = search_web(["research funding"], folder)
            self.assertEqual(results[0]["title"], "New grant")
            self.assertFalse(statuses[0]["cache_hit"])
            self.assertEqual(old.read_bytes(), before)

    def test_url_preserves_notice_id_and_removes_tracking(self):
        self.assertEqual(normalize_url("https://EXAMPLE.org:443/view?id=12&utm_source=a#top"),
                         "https://example.org/view?id=12")
        self.assertNotEqual(normalize_url("https://example.org/view?id=12"),
                            normalize_url("https://example.org/view?id=13"))
        self.assertEqual(normalize_url("https://example.org/view?nttId=23&bbs=5&gclid=x"),
                         "https://example.org/view?bbs=5&nttId=23")

    def test_nonpublic_and_unsafe_urls_are_skipped(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "http://127.0.0.1/call",
                    "http://169.254.169.254", "http://[::1]/", "http://localhost/",
                    "http://printer.local/", "https://user:pass@example.org/",
                    "https://example.org:8080/", "http://2130706433/", "http://127.1/",
                    "https://exa\\mple.org/", "http://[bad"):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), "")

    def test_duplicate_results_keep_queries_and_different_notice_ids(self):
        provider = FakeProvider({
            "math grants": [{"title": "Grant A", "href": "https://example.org/view?id=1&utm_source=a", "body": "Math"}],
            "faculty funding": [{"title": "Grant A", "url": "https://example.org/view?id=1#main"},
                                {"title": "Grant B", "url": "https://example.org/view?id=2"}],
        })
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["math grants", "faculty funding"], Path(temporary), provider=provider)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["queries"], ["math grants", "faculty funding"])
        self.assertEqual(results[0]["snippet"], "Math")
        self.assertTrue(all(item["status"] == "ok" for item in statuses))

    def test_cache_does_not_claim_a_new_search(self):
        first = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)
        provider = FakeProvider({"q": [{"title": "Grant", "url": "https://example.org/grant"}]})
        with tempfile.TemporaryDirectory() as temporary:
            with patch("grantfinder.web_search._now", return_value=first):
                original, original_status = search_web(["q"], Path(temporary), provider=provider)
            with patch("grantfinder.web_search._now", return_value=first + timedelta(hours=2)):
                again, statuses = search_web(["q"], Path(temporary), provider=provider)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(original, again)
        self.assertTrue(statuses[0]["cache_hit"])
        self.assertEqual(statuses[0]["checked_at"], original_status[0]["checked_at"])

    def test_failed_refresh_does_not_return_old_results(self):
        provider = FakeProvider({"q": [{"title": "Grant", "url": "https://example.org/grant"}]})
        with tempfile.TemporaryDirectory() as temporary:
            search_web(["q"], Path(temporary), provider=provider)
            provider.responses["q"] = RuntimeError("provider unavailable")
            results, statuses = search_web(["q"], Path(temporary), refresh=True, provider=provider)
            self.assertEqual(results, [])
            self.assertEqual(statuses[0]["status"], "error")
            self.assertIn("provider unavailable", statuses[0]["error"])
            self.assertFalse(statuses[0]["cache_hit"])
            # Failure has not overwritten the older successful cache.
            self.assertTrue(json.loads(next(Path(temporary).glob("*.json")).read_text())["results"])

    def test_partial_errors_and_empty_results_are_visible(self):
        provider = FakeProvider({"good": [{"title": "Grant", "url": "https://example.org/grant"}],
                                 "bad": RuntimeError("timeout"), "none": []})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web(["good", "bad", "none"], Path(temporary), provider=provider)
        self.assertEqual(len(results), 1)
        self.assertEqual([s["status"] for s in statuses], ["ok", "error", "empty"])

    def test_refresh_retains_first_seen_but_updates_last_seen(self):
        first = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)
        provider = FakeProvider({"q": [{"title": "Grant", "url": "https://example.org/grant"}]})
        with tempfile.TemporaryDirectory() as temporary:
            with patch("grantfinder.web_search._now", return_value=first):
                results, _ = search_web(["q"], Path(temporary), provider=provider)
            with patch("grantfinder.web_search._now", return_value=first + timedelta(days=2)):
                refreshed, statuses = search_web(["q"], Path(temporary), provider=provider)
        self.assertEqual(refreshed[0]["first_seen_at"], results[0]["first_seen_at"])
        self.assertNotEqual(refreshed[0]["last_seen_at"], results[0]["last_seen_at"])
        self.assertFalse(statuses[0]["cache_hit"])

    def test_limits_and_junk_results(self):
        provider = FakeProvider({f"q{i}": [{"title": "Bad", "url": "http://localhost/"},
                                         {"title": "", "url": "https://example.org/"},
                                         {"title": "Grant", "url": f"https://example.org/{i}"}]
                                 for i in range(30)})
        with tempfile.TemporaryDirectory() as temporary:
            results, statuses = search_web([f"q{i}" for i in range(30)], Path(temporary), provider=provider)
        self.assertEqual(len(statuses), 24)
        self.assertEqual(len(results), 24)
        self.assertLessEqual(len(results), 200)
        with self.assertRaises(ValueError):
            search_web(["x" * 301], Path("unused"))

    def test_missing_optional_dependency_is_actionable(self):
        with patch.dict(sys.modules, {"ddgs": None}):
            with self.assertRaisesRegex(RuntimeError, "requirements-search.txt"):
                DDGSSearchProvider().search("faculty grant", 4)


if __name__ == "__main__":
    unittest.main()
