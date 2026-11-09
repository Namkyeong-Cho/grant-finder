"""Ad hoc target searches keep input, evidence, and failures outside the workspace."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.server import create_server
from grantfinder.storage import write_json
from grantfinder.workspace import Workspace


class TargetProvider:
    def __init__(self, mode="ok"):
        self.calls = []
        self.mode = mode

    def search(self, query, max_results):
        self.calls.append((query, max_results))
        if self.mode == "error" or (self.mode == "partial" and len(self.calls) == 1):
            raise RuntimeError("synthetic search timeout")
        if self.mode == "empty":
            return []
        return [
            {"title": "  Public  Research Support ", "href": "https://EXAMPLE.org:443/call?id=1&utm_source=test#main",
             "body": "Public  funding\nsummary"},
            {"title": "Duplicate", "url": "https://example.org/call?id=1"},
            {"title": "Another call", "url": "https://example.org/call?id=2"},
            {"title": "Private URL", "url": "http://127.0.0.1/private"},
            {"title": "Local file", "url": "file:///etc/passwd"},
        ]


class TargetSearchFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = Workspace(self.root)
        self.provider = TargetProvider()
        provider = patch("grantfinder.web_search.DDGSSearchProvider", return_value=self.provider)
        provider.start()
        self.addCleanup(provider.stop)
        model = patch("grantfinder.workspace.OllamaClient", side_effect=AssertionError("must not call model"))
        self.model = model.start()
        self.addCleanup(model.stop)


class TargetSearchTests(TargetSearchFixture):
    def test_input_is_searched_without_profile_or_model_and_results_have_evidence(self):
        text = "광학 계산자원"
        with patch.object(self.workspace, "_load", side_effect=AssertionError("must not load workspace")):
            value = self.workspace.search_targets({"text": text})
        self.assertEqual(set(value), {"queries", "results", "statuses"})
        self.assertEqual(len(value["queries"]), 4)
        self.assertTrue(all(text in query for query in value["queries"]))
        self.assertIn(text, value["queries"])
        self.assertEqual(len(value["results"]), 2)
        first = value["results"][0]
        self.assertEqual(first["title"], "Public Research Support")
        self.assertEqual(first["url"], "https://example.org/call?id=1")
        self.assertEqual(first["snippet"], "Public funding summary")
        self.assertEqual(first["queries"], value["queries"])
        self.assertEqual(first["checked_at"], first["last_seen_at"])
        self.assertTrue(first["first_seen_at"])
        self.assertTrue(all(status["status"] == "ok" for status in value["statuses"]))
        self.assertEqual(self.provider.calls, [(query, 4) for query in value["queries"]])
        self.model.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_long_description_is_fully_reflected_in_at_most_six_bounded_queries(self):
        text = "".join(f"{index:04d}" for index in range(250))
        value = self.workspace.search_targets({"text": text})
        self.assertLessEqual(len(value["queries"]), 6)
        self.assertTrue(all(len(query) <= 300 for query in value["queries"]))
        # Chunked searches include the entire input; no tail is silently discarded.
        primary = [query.removesuffix(" 연구비 연구지원 공고") for query in value["queries"]
                   if query.endswith(" 연구비 연구지원 공고")]
        self.assertEqual("".join(primary), text)

    def test_word_boundaries_still_keep_the_full_input_within_the_query_limit(self):
        text = " ".join([str(index) + "x" * 128 for index in range(7)])
        value = self.workspace.search_targets({"text": text})
        self.assertLessEqual(len(value["queries"]), 6)
        primary = [query.removesuffix(" 연구비 연구지원 공고") for query in value["queries"]
                   if query.endswith(" 연구비 연구지원 공고")]
        self.assertEqual("".join(primary).replace(" ", ""), text.replace(" ", ""))

    def test_invalid_input_fails_before_search_or_file_creation(self):
        for raw in (None, [], "optics", {}, {"text": None}, {"text": 4}, {"text": False},
                    {"text": []}, {"text": " \n\t "}, {"text": "\x00"}, {"text": "x" * 1001}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.workspace.search_targets(raw)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_failure_preserves_successes_and_each_query_status(self):
        self.provider.mode = "partial"
        value = self.workspace.search_targets({"text": "optics"})
        self.assertEqual(len(value["results"]), 2)
        self.assertEqual(value["statuses"][0]["status"], "error")
        self.assertIn("synthetic search timeout", value["statuses"][0]["error"])
        self.assertTrue(all(item["status"] == "ok" for item in value["statuses"][1:]))
        self.assertNotIn(value["queries"][0], value["results"][0]["queries"])

    def test_all_failures_are_returned_as_statuses_without_old_results(self):
        self.provider.mode = "error"
        value = self.workspace.search_targets({"text": "optics"})
        self.assertEqual(value["results"], [])
        self.assertEqual(len(value["statuses"]), len(value["queries"]))
        self.assertTrue(all(item["status"] == "error" for item in value["statuses"]))
        self.assertTrue(all(item["checked_at"] and not item["cache_hit"] for item in value["statuses"]))

    def test_empty_search_is_distinct_from_failure(self):
        self.provider.mode = "empty"
        value = self.workspace.search_targets({"text": "optics"})
        self.assertEqual(value["results"], [])
        self.assertTrue(all(item["status"] == "empty" and "error" not in item for item in value["statuses"]))

    def test_only_separate_cache_changes_and_repeated_search_reuses_it(self):
        identifier = "cv_1234567890abcdef"
        write_json(self.root / "state/workspaces" / f"{identifier}.json", {
            "profile": {"id": identifier, "name": "Synthetic Researcher", "keywords": ["biology"]},
            "known": [{"kind": "program", "name": "Keep selection"}],
            "results": [{"id": "keep_result"}], "applications": [{"id": "keep_plan"}]})
        write_json(self.root / "state/workspace_active.json", {"id": identifier})
        write_json(self.root / "state/selection_draft.json", {"known": [], "custom_sources": []})
        write_json(self.root / "cache/websearch/existing.json", {"results": ["keep cache"]})
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        original = self.workspace.search_targets({"text": "optics"})
        calls = len(self.provider.calls)
        self.provider.mode = "error"
        again = self.workspace.search_targets({"text": "optics"})
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(original["results"], again["results"])
        self.assertTrue(all(item["cache_hit"] for item in again["statuses"]))
        self.assertEqual([item["checked_at"] for item in original["statuses"]],
                         [item["checked_at"] for item in again["statuses"]])
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertTrue(all(after[path] == content for path, content in before.items()))
        added = set(after) - set(before)
        self.assertTrue(added)
        self.assertTrue(all(path.parts[:2] == ("cache", "target-search") for path in added))


class TargetSearchEndpointTests(TargetSearchFixture):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.root, port=0, workspace=self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, value, headers=None):
        request = urllib.request.Request(self.address + "/api/target-search", data=json.dumps(value).encode(),
                                         method="POST", headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_no_profile_returns_search_payload_without_workspace_or_job(self):
        status, value = self.post({"text": "optics"})
        self.assertEqual(status, 200)
        self.assertEqual(set(value), {"queries", "results", "statuses"})
        self.assertEqual(len(value["results"]), 2)
        self.assertFalse((self.root / "state").exists())

    def test_all_search_failures_still_return_per_query_statuses(self):
        self.provider.mode = "error"
        status, value = self.post({"text": "optics"})
        self.assertEqual(status, 200)
        self.assertEqual(value["results"], [])
        self.assertTrue(all(item["status"] == "error" for item in value["statuses"]))

    def test_invalid_or_foreign_requests_do_not_reach_search(self):
        for value, headers in (({"text": ""}, {}), ({"text": 123}, {}), ({"text": "x" * 1001}, {}),
                               ({"text": "optics"}, {"Origin": "https://foreign.example"})):
            with self.subTest(value=value, headers=headers):
                status, body = self.post(value, headers)
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
