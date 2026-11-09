"""Custom-site and bulk-known actions through the actual local HTTP routes."""

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.server import create_server
from grantfinder.storage import read_json
from grantfinder.workspace import Workspace


class LocalClient:
    model = "synthetic-local-model"
    def chat(self, messages, schema=None):
        return {"message": {"content": json.dumps({"queries": ["optics faculty grants"], "recommendations": []})}}


class CustomSourcesHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        for name in ("sources.json", "program_groups.json", "public_opportunities.json"):
            shutil.copyfile(ROOT / "data" / name, self.root / "data" / name)
        self.workspace = Workspace(self.root, client=LocalClient())
        collector = patch("grantfinder.workspace.collect_site_notices", return_value=([], []))
        collector.start()
        self.addCleanup(collector.stop)
        self.first_payload = b'{"name":"First HTTP Researcher","keywords":["optics"]}'
        self.first = self.workspace.upload_cv("profile.json", self.first_payload)["profile"]["id"]
        self.server = create_server(self.root, port=0, workspace=self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.discovery_jobs.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET", value=None):
        payload = json.dumps(value).encode() if value is not None else None
        headers = {"Content-Type": "application/json", "Origin": self.address} if payload is not None else {}
        request = urllib.request.Request(self.address + path, data=payload, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def add_source(self, url="https://outside.example/funding/", notes="Original manual note"):
        status, state = self.request("/api/sources", "POST", {"name": "Outside Foundation", "url": url, "notes": notes})
        self.assertEqual(status, 200, state)
        return state

    def test_all_82_programs_and_77_sites_fit_in_one_saved_known_list(self):
        status, state = self.request("/api/workspace")
        self.assertEqual(status, 200)
        options = state["known_options"]
        self.assertEqual(len(options["programs"]), 82)
        self.assertEqual(len(options["sites"]), 77)
        items = [{"id": "program_" + str(index), "kind": "program", "name": item["name"],
                  "url": item["url"], "program_id": item["id"], "notes": ""}
                 for index, item in enumerate(options["programs"])]
        items += [{"id": "site_" + str(index), "kind": "site", "name": item["name"],
                   "url": item["url"], "source_id": item["id"], "notes": ""}
                  for index, item in enumerate(options["sites"])]
        status, saved = self.request("/api/known", "PUT", {"items": items})
        self.assertEqual(status, 200, saved)
        self.assertEqual(len(saved["known"]), 159)
        self.assertEqual(len(Workspace(self.root, profile_id=self.first).get()["known"]), 159)

    def test_custom_source_is_persistent_and_tracking_slash_duplicates_preserve_notes(self):
        initial = self.add_source()
        sources = initial["custom_sources"]
        self.assertEqual(len(sources), 1)
        identifier = sources[0]["id"]
        self.assertIn(identifier, {item["id"] for item in initial["known_options"]["sites"]})
        duplicate = self.add_source("https://outside.example/funding?utm_source=newsletter#intro", notes="")
        self.assertEqual(len(duplicate["custom_sources"]), 1)
        self.assertEqual(duplicate["custom_sources"][0]["id"], identifier)
        self.assertEqual(duplicate["custom_sources"][0]["notes"], "Original manual note")
        reopened = Workspace(self.root, profile_id=self.first).get()
        self.assertEqual(reopened["custom_sources"][0]["id"], identifier)
        self.assertEqual(len(reopened["known_options"]["sites"]), 78)

    def test_invalid_custom_url_keeps_existing_profile_state_intact(self):
        self.add_source()
        path = self.root / "state/workspaces" / (self.first + ".json")
        before = read_json(path)
        for url in ("javascript:alert(1)", "http://127.0.0.1/", "http://localhost/", "https://user:pass@outside.example/", "ftp://outside.example/"):
            with self.subTest(url=url):
                status, body = self.request("/api/sources", "POST", {"name": "Invalid", "url": url})
                self.assertEqual(status, 400)
                self.assertIn("error", body)
                self.assertEqual(read_json(path), before)

    def test_custom_sources_and_known_items_do_not_cross_cv_workspaces(self):
        initial = self.add_source()
        self.assertEqual(len(initial["custom_sources"]), 1)
        self.workspace.upload_cv("another.json", b'{"name":"Second HTTP Researcher","keywords":["biology"]}')
        status, other = self.request("/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(other["custom_sources"], [])
        self.assertEqual(len(other["known_options"]["sites"]), 77)
        self.assertNotIn("outside.example", json.dumps(other["known"]))
        self.workspace.upload_cv("profile.json", self.first_payload)
        status, restored = self.request("/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(restored["custom_sources"][0]["id"], initial["custom_sources"][0]["id"])

    def test_custom_site_is_used_as_an_actual_web_search_constraint(self):
        self.add_source("https://outside.example/funding/")
        statuses = [{"query": "synthetic", "status": "empty", "found": 0}]
        with patch("grantfinder.workspace.search_web", return_value=([], statuses)) as search:
            status, state = self.request("/api/search", "POST", {"mode": "web"})
        self.assertEqual(status, 200, state)
        self.assertEqual(search.call_count, 1)
        queries = search.call_args.args[0]
        self.assertTrue(any("site:outside.example" in item for item in queries), queries)


if __name__ == "__main__":
    unittest.main()
