"""Exercise the three-tab flow through real HTTP and persistent local files."""

import base64
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.jobs import DiscoveryJobs
from grantfinder.server import create_server
from grantfinder.storage import write_json
from grantfinder.workspace import Workspace


class LocalPlan:
    model = "test-local"

    def chat(self, messages, schema=None):
        return {"message": {"content": json.dumps({"queries": ["optics faculty grant"],
                "recommendations": [{"id": "optics", "reason": "광학 연구 지원", "matched_keywords": ["optics"]}]})}}


class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "track_a", "title": "Optics Grant A", "sponsor": "Foundation", "support_type": "research_grant",
             "eligibility_summary": "Optics faculty research", "source_url": "https://fund.example.org/a"},
            {"id": "track_b", "title": "Optics Grant B", "sponsor": "Foundation", "support_type": "equipment",
             "eligibility_summary": "Optics equipment", "source_url": "https://fund.example.org/b"}]})
        write_json(self.root / "data/program_groups.json", {"groups": [
            {"id": "optics", "title": "Optics Grant", "candidate_ids": ["track_a", "track_b"]}]})
        self.client = LocalPlan()

        def factory(root, model=None, profile_id=None):
            return Workspace(root, client=self.client, profile_id=profile_id)

        self.workspace = factory(self.root)
        self.started, self.release = threading.Event(), threading.Event()

        def runner(bound, mode, refresh, progress):
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("test job not released")
            return bound.search("catalog", progress=progress)

        self.jobs = DiscoveryJobs(self.root, workspace=self.workspace, workspace_factory=factory, runner=runner)
        self.server = create_server(self.root, port=0, workspace=self.workspace, jobs=self.jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.release.set()
        for thread in list(self.jobs.threads.values()):
            thread.join(timeout=3)
        self.jobs.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, route, method="GET", value=None):
        data = json.dumps(value).encode() if value is not None else None
        request = urllib.request.Request(self.address + route, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def upload(self, name="Researcher"):
        cv = json.dumps({"name": name, "keywords": ["optics"]}).encode()
        return self.request("/api/cv", "POST", {"filename": "cv.json", "content_base64": base64.b64encode(cv).decode()})

    def finish_search(self):
        self.release.set()
        for thread in list(self.jobs.threads.values()):
            thread.join(timeout=3)
        status, state = self.request("/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(state["job"]["status"], "succeeded", state["job"])
        return state

    def test_setup_background_discovery_and_selected_plan_survive_reload(self):
        _, state = self.upload()
        self.assertTrue(state["known_options"]["programs"])
        _, state = self.request("/api/known", "PUT", {"items": [
            {"kind": "program", "program_id": "optics", "name": "Optics Grant", "url": ""}]})
        self.assertEqual(state["known"][0]["program_id"], "optics")
        _, state = self.request("/api/automation", "PUT", {"enabled": True, "interval_hours": 24})
        self.assertTrue(state["automation"]["enabled"])
        started = time.monotonic()
        status, state = self.request("/api/discovery", "POST", {"mode": "all"})
        self.assertEqual(status, 202)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn(state["job"]["status"], {"queued", "running"})
        self.assertTrue(self.started.wait(1))
        state = self.finish_search()
        self.assertEqual(len(state["results"]), 1)
        self.assertTrue(state["memory"]["programs"])
        status, state = self.request("/api/applications", "POST", {"program_id": "optics", "track_ids": ["track_b"]})
        self.assertEqual(status, 200)
        application = state["applications"][0]
        self.assertEqual(application["selected_track_ids"], ["track_b"])
        identifier = application["id"]
        status, state = self.request(f"/api/applications/{identifier}", "PATCH",
                                     {"status": "preparing", "priority": "high", "notes": "견적서 확인"})
        self.assertEqual(status, 200)
        # New object and new HTTP read both recover the same stored plan.
        reloaded = Workspace(self.root).get()
        self.assertEqual(reloaded["applications"][0]["notes"], "견적서 확인")
        self.assertEqual(reloaded["applications"][0]["status"], "preparing")
        _, state = self.request("/api/workspace")
        self.assertEqual(state["applications"][0]["priority"], "high")
        _, memory = self.request("/api/memory")
        self.assertTrue(memory["memory"]["sources"])
        self.assertEqual(memory["applications"][0]["notes"], "견적서 확인")

    def test_plan_api_rejects_unknown_tracks_and_does_not_cross_cv_boundaries(self):
        self.upload()
        self.request("/api/discovery", "POST", {})
        self.finish_search()
        status, _ = self.request("/api/applications", "POST", {"program_id": "optics", "track_ids": ["made_up"]})
        self.assertEqual(status, 400)
        _, state = self.request("/api/applications", "POST", {"program_id": "optics", "track_ids": ["track_a"]})
        identifier = state["applications"][0]["id"]
        _, other = self.upload("Another Researcher")
        self.assertEqual(other["applications"], [])
        status, _ = self.request(f"/api/applications/{identifier}", "PATCH", {"status": "submitted"})
        self.assertEqual(status, 400)
        self.upload()
        status, state = self.request(f"/api/applications/{identifier}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(state["applications"], [])

    def test_discovery_requires_cv_and_rejects_cross_origin_writes(self):
        status, _ = self.request("/api/discovery", "POST", {})
        self.assertEqual(status, 400)
        self.assertFalse(self.jobs.threads)
        self.upload()
        request = urllib.request.Request(self.address + "/api/automation", data=b'{"enabled": true}',
                                         method="PUT", headers={"Content-Type": "application/json", "Origin": "https://other.example"})
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.opener.open(request, timeout=3)
        self.assertEqual(context.exception.code, 400)
        self.assertFalse(self.workspace.get()["automation"]["enabled"])


if __name__ == "__main__":
    unittest.main()
