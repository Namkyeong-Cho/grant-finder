"""Local dashboard transport and upload boundaries, without model calls."""

import base64
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.server import create_server


class FakeWorkspace:
    def __init__(self):
        self.calls = []
        self.value = {"profile": None, "known": [], "results": [],
                      "catalog": {"program_count": 0, "track_count": 0},
                      "searched_at": None, "queries": [], "errors": []}

    def get(self):
        return self.value

    def upload_cv(self, filename, payload):
        self.calls.append(("cv", filename, payload))
        self.value["profile"] = {"id": "uploaded", "name": "Test Researcher"}
        return self.value

    def save_profile(self, profile):
        self.calls.append(("profile", profile))
        self.value["profile"] = profile
        return self.value

    def import_homepage(self, url):
        self.calls.append(("homepage", url))
        self.value["profile"] = {"id": "imported-homepage", "name": "Homepage Researcher",
                                  "documents": {"cv": False}, "cv_import": {"url": url}}
        return self.value

    def save_known(self, items):
        self.calls.append(("known", items))
        self.value["known"] = items
        return self.value

    def search(self, mode):
        self.calls.append(("search", mode))
        return self.value


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "outputs").mkdir()
        self.report = self.root / "outputs/latest_report.html"
        self.report.write_text("existing report", encoding="utf-8")
        self.workspace = FakeWorkspace()
        self.server = create_server(self.root, port=0, workspace=self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, path, method="GET", value=None, headers=None, raw=None):
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        fields = {"Content-Type": "application/json"} if body is not None else {}
        fields.update(headers or {})
        request = urllib.request.Request(self.address + path, data=body, method=method, headers=fields)
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if response.headers.get("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, data

    def test_blank_workspace_does_not_inherit_a_personal_identity(self):
        status, value = self.request("/api/workspace")
        self.assertEqual(status, 200)
        self.assertIsNone(value["profile"])
        self.assertEqual(value["known"], [])
        self.assertEqual(value["results"], [])
        self.assertNotIn("Previous Synthetic Researcher", json.dumps(value))
        self.assertEqual(self.workspace.calls, [])

    def test_cv_upload_preserves_filename_and_file_bytes(self):
        payload = "Research CV\n분야: 수학\n".encode("utf-8")
        status, value = self.request("/api/cv", "POST", {
            "filename": "research_cv.txt", "content_base64": base64.b64encode(payload).decode()})
        self.assertEqual(status, 200)
        self.assertEqual(self.workspace.calls, [("cv", "research_cv.txt", payload)])
        self.assertEqual(value["profile"]["id"], "uploaded")
        self.assertEqual(self.report.read_text(encoding="utf-8"), "existing report")

    def test_invalid_upload_or_request_data_cannot_reach_workspace(self):
        cases = [({"filename": "cv.txt", "content_base64": "%%%"}, None, None),
                 ({"filename": "cv.txt", "content_base64": []}, None, None),
                 (None, b"not-json", None),
                 ([], None, None),
                 ({"filename": "cv.txt", "content_base64": "eA=="}, None,
                  {"Content-Type": "text/plain"})]
        for value, raw, headers in cases:
            with self.subTest(value=value, raw=raw, headers=headers):
                status, body = self.request("/api/cv", "POST", value, raw=raw, headers=headers)
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.assertEqual(self.workspace.calls, [])

    def test_homepage_input_uses_the_same_workspace_response(self):
        address = "https://researcher.example.edu/about"
        status, value = self.request("/api/homepage", "POST", {"url": address})
        self.assertEqual(status, 200)
        self.assertEqual(self.workspace.calls, [("homepage", address)])
        self.assertEqual(value["profile"]["cv_import"]["url"], address)
        self.assertFalse(value["profile"]["documents"]["cv"])
        self.assertEqual(self.report.read_text(encoding="utf-8"), "existing report")

    def test_foreign_host_or_origin_rejected_before_mutation(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
                        {"Origin": "null"}, {"Host": f"127.0.0.1:{self.server.server_port + 1}"}):
            with self.subTest(headers=headers):
                status, body = self.request("/api/known", "PUT", {"items": []}, headers=headers)
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.assertEqual(self.workspace.calls, [])
        # Browser requests issued by the local dashboard remain accepted.
        status, _ = self.request("/api/known", "PUT", {"items": []},
                                 headers={"Origin": self.address})
        self.assertEqual(status, 200)
        self.assertEqual(self.workspace.calls, [("known", [])])

    def test_unknown_paths_do_not_serve_workspace_files(self):
        for path, method, value in (("/missing", "GET", None),
                                    ("/outputs/latest_report.html", "GET", None),
                                    ("/../data/profile.json", "GET", None),
                                    ("/api/unknown", "POST", {})):
            with self.subTest(path=path):
                status, body = self.request(path, method, value)
                self.assertEqual(status, 404)
                self.assertIn("error", body)
        self.assertEqual(self.workspace.calls, [])
        self.assertEqual(self.report.read_text(encoding="utf-8"), "existing report")

    def test_dashboard_combines_researcher_input_and_json_in_the_first_of_four_steps(self):
        status, payload = self.request("/")
        self.assertEqual(status, 200)
        html = payload.decode("utf-8")
        for label in ("연구자 정보", "탐색 대상 선정", "지원 후보", "마감 전 지원"):
            self.assertTrue(label in html, f"{label} 탭 누락")
        first = html.index('id="panel-input"')
        second = html.index('id="panel-sources"')
        self.assertTrue(first < html.index('id="cv-file"') < second)
        self.assertTrue(first < html.index('id="profile-conditions-text"') < second)
        self.assertNotIn('id="panel-profile"', html)
        self.assertNotIn("Previous Synthetic Researcher", html)
        self.assertEqual(self.report.read_text(encoding="utf-8"), "existing report")


if __name__ == "__main__":
    unittest.main()
