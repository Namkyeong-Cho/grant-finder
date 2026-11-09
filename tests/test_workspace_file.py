"""The file link serves exactly the current saved JSON and no arbitrary paths."""

import json
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
from grantfinder.workspace import Workspace


class WorkspaceFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = Workspace(self.root)
        state = self.workspace.upload_cv("profile.json", json.dumps({"name": "Synthetic Researcher", "keywords": ["mathematics"]}).encode())
        self.identifier = state["profile"]["id"]
        self.saved_file = self.root / "state/workspaces" / f"{self.identifier}.json"
        # Non-default spacing and trailing newlines prove the file is not regenerated.
        saved = json.loads(self.saved_file.read_bytes())
        saved["test_marker"] = "Temporary fixture only"
        self.original_bytes = (json.dumps(saved, ensure_ascii=False, indent=3) + "\n\n").encode()
        self.saved_file.write_bytes(self.original_bytes)
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
        self.directory.cleanup()

    def get(self, query=None, headers=None):
        query = "profile_id=" + self.identifier if query is None else query
        request = urllib.request.Request(self.address + "/api/workspace/file?" + query, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_returns_exact_saved_bytes_as_json_without_runtime_metadata(self):
        status, headers, payload = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(payload, self.original_bytes)
        saved = json.loads(payload)
        self.assertNotIn("storage", saved)
        self.assertNotIn("catalog", saved)
        self.assertNotIn("known_options", saved)
        self.assertEqual(self.saved_file.read_bytes(), self.original_bytes)

    def test_profile_switch_returns_conflict_instead_of_another_researchers_file(self):
        other = self.workspace.upload_cv("other.json", b'{"name":"Another Researcher"}')
        status, _, payload = self.get()
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(payload))
        status, _, payload = self.get("profile_id=" + other["profile"]["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["profile"]["name"], "Another Researcher")
        self.assertEqual(self.saved_file.read_bytes(), self.original_bytes)

    def test_missing_or_duplicate_profile_identifier_cannot_choose_a_file(self):
        for query in ("", "profile_id=", "profile_id=../../other", "path=/etc/passwd",
                      f"profile_id={self.identifier}&profile_id={self.identifier}"):
            with self.subTest(query=query):
                self.assertEqual(self.get(query)[0], 409)

    def test_missing_saved_workspace_returns_not_found(self):
        self.workspace.clear_saved()
        status, _, payload = self.get()
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(payload))

    def test_host_and_origin_checks_also_protect_the_file_route(self):
        for headers in ({"Origin": "https://outside.example.org"}, {"Origin": "null"}, {"Host": "outside.example.org"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.get(headers=headers)[0], 400)
        self.assertEqual(self.get(headers={"Origin": self.address})[0], 200)

    def test_runtime_storage_metadata_and_query_path_cannot_redirect_the_file_read(self):
        outside = self.root / "unrelated.json"
        outside.write_text('{"unrelated":true}', encoding="utf-8")
        state = self.workspace.get()
        state["storage"]["workspace_path"] = str(outside)
        with patch.object(self.workspace, "get", return_value=state):
            status, _, payload = self.get(f"profile_id={self.identifier}&path={outside}")
        self.assertEqual(status, 200)
        self.assertEqual(payload, self.original_bytes)

    def test_symlink_cannot_open_another_file(self):
        outside = self.root / "unrelated.json"
        outside.write_bytes(self.original_bytes)
        self.saved_file.unlink()
        self.saved_file.symlink_to(outside)
        status, _, payload = self.get()
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(payload))
        self.assertEqual(outside.read_bytes(), self.original_bytes)


if __name__ == "__main__":
    unittest.main()
