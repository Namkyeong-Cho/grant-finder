"""Public board discovery, access boundaries, limits, and honest cache status."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.site_notices import collect_site_notices


class Fetcher:
    def __init__(self, html, effective=None):
        self.html, self.effective, self.calls = html, effective, []

    def __call__(self, url):
        self.calls.append(url)
        if isinstance(self.html, Exception):
            raise self.html
        return self.html.encode(), "text/html; charset=utf-8", self.effective or url


class SiteNoticesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        self.site = {"id": "university", "name": "Example University", "url": "https://university.example.org/board", "kind": "listing"}
        self.html = '<main><a href="/notice?id=1">2027 연구비 지원사업 공고</a></main>'

    def collect(self, fetcher, **kwargs):
        return collect_site_notices([self.site], self.cache, fetcher=fetcher, **kwargs)

    def test_funding_links_are_same_host_https_distinct_notices_and_not_jobs(self):
        html = '''<main>
        <a href="/notice?id=1&utm_source=x">2027 연구비 지원사업 공고</a>
        <a href="/notice?id=1#top">중복 연구지원 공고</a>
        <a href="/notice?id=2">Research grant proposals</a>
        <a href="/notice?id=3">학술 연구 공모</a>
        <a href="/hire">연구지원 직원 채용 공고</a>
        <a href="/recruit">Research grant recruitment vacancy</a>
        <a href="/faculty">학술연구 교수 초빙</a>
        <a href="https://other.example.org/grant">Research funding</a>
        <a href="https://child.university.example.org/grant">Research funding</a>
        <a href="http://university.example.org/grant">Research funding</a>
        <a href="javascript:open(1)">연구비 공모</a>
        <a href="/login">연구지원 로그인</a>
        <a href="/news">일반 학내 소식</a></main>'''
        results, statuses = self.collect(Fetcher(html))
        self.assertEqual([item["url"] for item in results], [f"https://university.example.org/notice?id={i}" for i in (1, 2, 3)])
        self.assertEqual(statuses[0]["status"], "ok")
        self.assertEqual(statuses[0]["found"], 3)
        self.assertTrue(all(item["source_id"] == "university" for item in results))
        self.assertTrue(all(item["discovered_from"] == self.site["url"] for item in results))
        self.assertTrue(all("신청자격" in item["snippet"] and "eligible" not in item for item in results))

    def test_notice_itself_is_included_only_when_its_heading_is_funding_related(self):
        self.site["kind"] = "notice"
        results, _ = self.collect(Fetcher('<title>대학 연구처</title><h1>2027 연구과제 공모</h1><p>공개 공고 상세입니다.</p>'))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], self.site["url"])
        self.assertEqual(results[0]["discovery_method"], "public_notice")
        results, statuses = self.collect(Fetcher('<title>교수 초빙 연구지원 안내</title><main>공개 채용 안내</main>'), refresh=True)
        self.assertEqual(results, [])
        self.assertEqual(statuses[0]["status"], "empty")

    def test_search_only_never_fetches_or_uses_a_previous_collection_cache(self):
        self.collect(Fetcher(self.html))
        self.site["collection_mode"] = "search_only"
        fetcher = Fetcher(AssertionError("Must not fetch"))
        results, statuses = self.collect(fetcher, refresh=True)
        self.assertEqual(results, [])
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(statuses[0]["status"], "search_only")
        self.assertEqual(statuses[0]["found"], 0)
        self.assertFalse(statuses[0]["cache_hit"])

    def test_password_forms_and_login_redirects_are_errors_not_empty_boards(self):
        cases = [Fetcher('<form><input TYPE="PASSWORD"></form>' + self.html),
                 Fetcher(self.html, 'https://university.example.org/member/login.do'),
                 Fetcher(self.html, 'https://sso.example.org/signin'),
                 Fetcher(self.html, 'https://university.example.org/index?mode=login')]
        for fetcher in cases:
            with self.subTest(effective=fetcher.effective):
                results, statuses = self.collect(fetcher, refresh=True)
                self.assertEqual(results, [])
                self.assertEqual(statuses[0]["status"], "error")
                self.assertIn("로그인", statuses[0]["error"])

    def test_errors_and_empty_boards_do_not_discard_another_site_success(self):
        sites = [dict(self.site, id=str(i), url=f"https://site{i}.example.org/board") for i in range(3)]
        def fetch(url):
            if "site0" in url:
                raise OSError("board unavailable")
            html = self.html if "site1" in url else '<main>현재 게시된 연구지원 공고가 없습니다.</main>'
            return html.encode(), "text/html", url
        results, statuses = collect_site_notices(sites, self.cache, fetcher=fetch)
        self.assertEqual([item["status"] for item in statuses], ["error", "ok", "empty"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_id"], "1")

    def test_cached_results_keep_the_check_time_and_refresh_keeps_first_seen(self):
        first = datetime(2026, 11, 8, 0, tzinfo=timezone.utc)
        fetcher = Fetcher(self.html)
        with patch("grantfinder.site_notices._now", return_value=first):
            original, initial_status = self.collect(fetcher)
        with patch("grantfinder.site_notices._now", return_value=first + timedelta(hours=2)):
            cached, statuses = self.collect(fetcher)
            refreshed, refresh_status = self.collect(fetcher, refresh=True)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(original, cached)
        self.assertEqual(statuses[0]["checked_at"], initial_status[0]["checked_at"])
        self.assertTrue(statuses[0]["cache_hit"])
        self.assertFalse(refresh_status[0]["cache_hit"])
        self.assertEqual(refreshed[0]["first_seen_at"], original[0]["first_seen_at"])
        self.assertNotEqual(refreshed[0]["last_seen_at"], original[0]["last_seen_at"])

    def test_cache_expires_after_24_hours_and_changed_url_does_not_reuse_it(self):
        first = datetime(2026, 11, 8, 0, tzinfo=timezone.utc)
        fetcher = Fetcher(self.html)
        with patch("grantfinder.site_notices._now", return_value=first):
            self.collect(fetcher)
        with patch("grantfinder.site_notices._now", return_value=first + timedelta(hours=24)):
            _, statuses = self.collect(fetcher)
            self.site["url"] += "?board=another"
            self.collect(fetcher)
        self.assertFalse(statuses[0]["cache_hit"])
        self.assertEqual(len(fetcher.calls), 3)

    def test_failed_refresh_never_relabels_old_success_as_fresh(self):
        self.collect(Fetcher(self.html))
        cache_path = next(self.cache.glob("*.json"))
        before = cache_path.read_bytes()
        results, statuses = self.collect(Fetcher(OSError("forbidden")), refresh=True)
        self.assertEqual(results, [])
        self.assertEqual(statuses[0]["status"], "error")
        self.assertIn("forbidden", statuses[0]["error"])
        self.assertFalse(statuses[0]["cache_hit"])
        self.assertEqual(cache_path.read_bytes(), before)

    def test_limits_bound_sites_each_board_and_total_candidates(self):
        sites = [dict(self.site, id=str(i), url=f"https://site{i}.example.org/board") for i in range(15)]
        html = '<main>' + ''.join(f'<a href="/notice?id={i}">연구비 공모 {i}</a>' for i in range(30)) + '</main>'
        fetcher = Fetcher(html)
        results, statuses = collect_site_notices(sites, self.cache, fetcher=fetcher)
        self.assertEqual(len(fetcher.calls), 12)
        self.assertEqual(len(statuses), 12)
        self.assertTrue(all(item["found"] == 20 for item in statuses))
        self.assertEqual(len(results), 120)

    def test_duplicate_notice_urls_across_boards_are_returned_once(self):
        sites = [self.site, dict(self.site, id="second", url=self.site["url"] + "?page=2")]
        results, statuses = collect_site_notices(sites, self.cache, fetcher=Fetcher(self.html))
        self.assertEqual(len(results), 1)
        self.assertEqual([status["found"] for status in statuses], [1, 1])

    def test_unsafe_or_non_https_source_urls_are_rejected_before_fetching(self):
        fetcher = Fetcher(self.html)
        for url in ("https://127.0.0.1/", "http://university.example.org/", "file:///tmp/cv", "https://user:pass@example.org/"):
            with self.subTest(url=url):
                results, statuses = collect_site_notices([dict(self.site, url=url)], self.cache, fetcher=fetcher)
                self.assertEqual(results, [])
                self.assertEqual(statuses[0]["status"], "error")
        self.assertEqual(fetcher.calls, [])

    def test_default_fetcher_reuses_public_homepage_fetcher_and_no_model(self):
        with patch("grantfinder.site_notices._fetch_page", return_value=(self.html.encode(), "text/html", self.site["url"])) as fetch, \
                patch("grantfinder.llm.OllamaClient.chat", side_effect=AssertionError("No model calls")):
            results, statuses = collect_site_notices([self.site], self.cache)
        fetch.assert_called_once_with(self.site["url"])
        self.assertEqual(len(results), 1)
        self.assertEqual(statuses[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
