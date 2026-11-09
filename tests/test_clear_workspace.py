"""Clear only a selected researcher's saved data, without racing discovery jobs."""

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

from grantfinder.jobs import DiscoveryJobs
from grantfinder.server import create_server
from grantfinder.storage import read_json, write_json
from grantfinder.workspace import Workspace, WorkspaceBusyError


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "shared", "title": "Shared research funding", "source_url": "https://funding.example.org/call"}]})
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "shared-source", "name": "Shared source", "url": "https://funding.example.org/", "domains": ["funding.example.org"]}], "pages": []})
        self.workspace = Workspace(self.root)
        self.first = self.upload("First Researcher")
        self.second = self.upload("Second Researcher")
        self.shared_before = {path: path.read_bytes() for path in (self.root / "data").glob("*.json")}

    def upload(self, name):
        return self.workspace.upload_cv("profile.json", json.dumps({"name": name, "keywords": ["mathematics"]}).encode())["profile"]["id"]

    def path(self, identifier):
        return self.root / "state/workspaces" / f"{identifier}.json"

    def job_path(self, identifier):
        return self.root / "state/jobs" / f"{identifier}.json"

    def assert_shared_unchanged(self):
        for path, payload in self.shared_before.items():
            self.assertEqual(path.read_bytes(), payload)


class ClearWorkspaceTests(WorkspaceFixture):
    def test_clear_current_removes_only_its_workspace_job_and_active_pointer(self):
        write_json(self.job_path(self.first), {"status": "succeeded", "profile_id": self.first})
        write_json(self.job_path(self.second), {"status": "failed", "profile_id": self.second})
        before = self.path(self.first).read_bytes()
        cache = self.root / "cache/review-evidence.json"
        write_json(cache, {"text": "Existing evidence cache"})
        state = self.workspace.clear_saved()
        self.assertIsNone(state["profile"])
        self.assertIsNone(state["storage"]["workspace_path"])
        self.assertEqual(state["known"], [])
        self.assertEqual(state["results"], [])
        self.assertEqual(state["applications"], [])
        self.assertFalse(self.path(self.second).exists())
        self.assertFalse(self.job_path(self.second).exists())
        self.assertFalse((self.root / "state/workspace_active.json").exists())
        self.assertEqual(self.path(self.first).read_bytes(), before)
        self.assertTrue(self.job_path(self.first).exists())
        self.assertTrue(cache.exists())
        self.assertEqual(state["catalog"]["program_count"], 1)
        self.assertEqual(len(state["known_options"]["sites"]), 1)
        self.assert_shared_unchanged()

    def test_bound_workspace_clears_its_own_path_and_keeps_another_active_profile(self):
        bound = Workspace(self.root, profile_id=self.first)
        pointer = (self.root / "state/workspace_active.json").read_bytes()
        second = self.path(self.second).read_bytes()
        state = bound.clear_saved()
        self.assertIsNone(state["profile"])
        self.assertFalse(self.path(self.first).exists())
        self.assertEqual(self.path(self.second).read_bytes(), second)
        self.assertEqual((self.root / "state/workspace_active.json").read_bytes(), pointer)
        self.assertEqual(self.workspace.get()["profile"]["id"], self.second)
        self.assert_shared_unchanged()

    def test_bound_current_workspace_also_clears_its_active_pointer(self):
        Workspace(self.root, profile_id=self.second).clear_saved()
        self.assertFalse((self.root / "state/workspace_active.json").exists())
        self.assertTrue(self.path(self.first).exists())

    def test_queued_and_running_jobs_reject_clear_without_changing_any_saved_bytes(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                write_json(self.job_path(self.second), {"status": status, "profile_id": self.second})
                before = {path: path.read_bytes() for path in (self.root / "state").rglob("*.json")}
                with self.assertRaisesRegex(WorkspaceBusyError, "대기·진행"):
                    self.workspace.clear_saved()
                self.assertEqual({path: path.read_bytes() for path in (self.root / "state").rglob("*.json")}, before)

    def test_empty_clear_is_idempotent_and_does_not_select_another_researcher(self):
        self.workspace.clear_saved()
        self.assertIsNone(self.workspace.clear_saved()["profile"])
        self.assertTrue(self.path(self.first).exists())
        self.assert_shared_unchanged()

    def test_invalid_active_identifier_cannot_delete_an_arbitrary_file(self):
        unrelated = self.root / "unrelated.json"
        unrelated.write_text("keep", encoding="utf-8")
        write_json(self.root / "state/workspace_active.json", {"id": "../../unrelated"})
        with self.assertRaises(ValueError):
            self.workspace.clear_saved()
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertTrue(self.path(self.first).exists())
        self.assertTrue(self.path(self.second).exists())

    def test_waiting_scheduler_start_cannot_recreate_a_deleted_job(self):
        manager = DiscoveryJobs(self.root, workspace=self.workspace, runner=lambda workspace, *args: workspace.get())
        reached_lock = threading.Event()
        class SignalledLock:
            def __init__(self):
                self.inner = threading.RLock()
            def __enter__(self):
                reached_lock.set()
                self.inner.acquire()
                return self
            def __exit__(self, *args):
                self.inner.release()
        manager.lock = SignalledLock()
        errors = []
        def start():
            try:
                manager.start("catalog", profile_id=self.second)
            except Exception as error:
                errors.append(error)
        manager.lock.inner.acquire()
        thread = threading.Thread(target=start)
        thread.start()
        try:
            self.assertTrue(reached_lock.wait(2))
            self.workspace.clear_saved()
        finally:
            manager.lock.inner.release()
        thread.join(timeout=2)
        manager.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertFalse(self.path(self.second).exists())
        self.assertFalse(self.job_path(self.second).exists())
        self.assertEqual(manager.threads, {})


class ClearWorkspaceHTTPTests(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.jobs = DiscoveryJobs(self.root, workspace=self.workspace)
        self.server = create_server(self.root, port=0, workspace=self.workspace, jobs=self.jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.addCleanup(self.close_server)

    def close_server(self):
        self.jobs.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def delete(self, headers=None):
        request = urllib.request.Request(self.address + "/api/workspace", method="DELETE", headers=headers or {})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_delete_route_returns_an_empty_workspace_without_affecting_other_profiles(self):
        before = self.path(self.first).read_bytes()
        status, state = self.delete({"Origin": self.address})
        self.assertEqual(status, 200)
        self.assertIsNone(state["profile"])
        self.assertIsNone(state["job"])
        self.assertIsNone(state["storage"]["workspace_path"])
        self.assertEqual(self.path(self.first).read_bytes(), before)
        self.assert_shared_unchanged()

    def test_delete_route_rejects_queued_and_running_jobs_with_conflict(self):
        for state in ("queued", "running"):
            with self.subTest(state=state):
                write_json(self.job_path(self.second), {"status": state, "profile_id": self.second})
                before = self.path(self.second).read_bytes()
                status, payload = self.delete()
                self.assertEqual(status, 409)
                self.assertIn("진행", payload["error"])
                self.assertEqual(self.path(self.second).read_bytes(), before)

    def test_live_worker_is_rejected_even_if_its_record_says_interrupted(self):
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(3), daemon=True)
        self.jobs.threads[self.second] = worker
        write_json(self.job_path(self.second), {"status": "interrupted", "profile_id": self.second})
        worker.start()
        try:
            status, payload = self.delete()
            self.assertEqual(status, 409)
            self.assertIn("진행", payload["error"])
            self.assertTrue(self.path(self.second).exists())
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertEqual(self.delete()[0], 200)

    def test_delete_rejects_foreign_origin_before_any_file_is_removed(self):
        status, payload = self.delete({"Origin": "https://outside.example.org"})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertTrue(self.path(self.first).exists())
        self.assertTrue(self.path(self.second).exists())


if __name__ == "__main__":
    unittest.main()
