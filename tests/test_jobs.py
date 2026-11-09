"""Background search remains responsive and bound to its original CV."""

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.jobs import DiscoveryJobs
from grantfinder.storage import read_json, write_json
from grantfinder.workspace import Workspace


class LocalClient:
    def chat(self, messages, schema=None):
        return {"message": {"content": json.dumps({"queries": ["optics research grant"], "recommendations": [
            {"id": "shared", "reason": "광학 연구 연결", "matched_keywords": ["optics"]}]})}}


class DiscoveryJobsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client = LocalClient()
        self.workspace = Workspace(self.root, client=self.client)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "shared", "title": "Shared Optics Research Grant", "sponsor": "Foundation",
             "support_type": "research_grant", "eligibility_summary": "Faculty optics research",
             "source_url": "https://foundation.example/grant", "application_timing": "매년 공모"}]})
        self.first = self.upload("First Researcher")["profile"]["id"]
        self.managers, self.releases = [], []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for event in self.releases:
            event.set()
        for manager in self.managers:
            for thread in manager.threads.values():
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "worker thread did not stop")
            if manager.scheduler:
                manager.scheduler.join(timeout=2)
        self.temporary.cleanup()

    def upload(self, name):
        return self.workspace.upload_cv("profile.json", json.dumps({"name": name, "keywords": ["optics"]}).encode())

    def manager(self, runner):
        def factory(root, model=None, profile_id=None):
            return Workspace(root, model=model, client=self.client, profile_id=profile_id)
        manager = DiscoveryJobs(self.root, workspace=self.workspace, workspace_factory=factory, runner=runner)
        self.managers.append(manager)
        return manager

    def blocker(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def runner(workspace, mode, refresh, progress):
            progress("fetching", "공개 연구지원 원문을 읽고 있습니다.")
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test worker timed out")
            return workspace.get()
        return runner, entered, release

    def join(self, manager, profile_id=None):
        thread = manager.threads[profile_id or self.first]
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_start_returns_immediately_and_duplicate_job_is_not_started(self):
        runner, entered, release = self.blocker()
        manager = self.manager(runner)
        began = time.monotonic()
        first = manager.start("catalog")
        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(entered.wait(2))
        again = manager.start("web")
        self.assertEqual(first["job"]["id"], again["job"]["id"])
        self.assertEqual(again["job"]["mode"], "catalog")
        release.set()
        self.join(manager)
        self.assertEqual(manager.status(self.first)["status"], "succeeded")

    def test_progress_is_persisted_without_browser_polling(self):
        runner, entered, release = self.blocker()
        manager = self.manager(runner)
        manager.start()
        self.assertTrue(entered.wait(2))
        saved = read_json(self.root / "state/jobs" / f"{self.first}.json")
        self.assertEqual(saved["stage"], "fetching")
        self.assertIn("원문", saved["message"])
        release.set()
        self.join(manager)

    def test_worker_keeps_original_profile_when_active_cv_changes(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        captured = []
        def runner(workspace, mode, refresh, progress):
            captured.append(workspace.get()["profile"]["id"])
            entered.set()
            release.wait(4)
            return workspace.save_known([{"name": "Original profile only", "kind": "program"}])
        manager = self.manager(runner)
        manager.start("catalog")
        self.assertTrue(entered.wait(2))
        second = self.upload("Second Researcher")["profile"]["id"]
        release.set()
        self.join(manager)
        self.assertEqual(captured, [self.first])
        self.assertEqual(self.workspace.get()["profile"]["id"], second)
        self.assertEqual(self.workspace.get()["known"], [])
        old = Workspace(self.root, profile_id=self.first).get()
        self.assertEqual(old["known"][0]["name"], "Original profile only")

    def test_application_edits_during_review_survive_result_commit(self):
        self.workspace.search("catalog")
        self.workspace.add_application({"program_id": "shared"})
        application_id = self.workspace.get()["applications"][0]["id"]
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def review(profile, results, client, **kwargs):
            entered.set()
            release.wait(4)
            return results
        def runner(workspace, mode, refresh, progress):
            return workspace.search("catalog", refresh=refresh, progress=progress, reviewer=review)
        manager = self.manager(runner)
        manager.start("catalog")
        self.assertTrue(entered.wait(2))
        self.workspace.update_application(application_id, {"notes": "Edited while searching", "status": "preparing"})
        release.set()
        self.join(manager)
        application = self.workspace.get()["applications"][0]
        self.assertEqual(application["notes"], "Edited while searching")
        self.assertEqual(application["status"], "preparing")
        self.assertEqual(manager.status(self.first)["status"], "succeeded")

    def test_automatic_search_only_starts_when_enabled_and_due(self):
        calls = []
        manager = self.manager(lambda workspace, mode, refresh, progress: (calls.append(mode) or workspace.get()))
        self.assertFalse(manager.tick())
        self.workspace.save_automation({"enabled": True, "interval_hours": 24})
        saved = self.workspace._load()
        now = datetime.now(timezone.utc)
        saved.update(searched_at=now.isoformat(), results_stale=False)
        saved["automation"]["next_run_at"] = (now + timedelta(hours=24)).isoformat()
        self.workspace._save(saved)
        self.assertFalse(manager.tick(now))
        self.assertEqual(calls, [])
        self.assertTrue(manager.tick(now + timedelta(hours=25)))
        self.join(manager)
        self.assertEqual(calls, ["all"])

    def test_failure_cooldown_prevents_immediate_retry(self):
        self.workspace.save_automation({"enabled": True, "interval_hours": 24})
        calls = []
        def runner(workspace, mode, refresh, progress):
            calls.append(mode)
            raise RuntimeError("temporary search failure")
        manager = self.manager(runner)
        manager.start()
        self.join(manager)
        job = manager.status(self.first)
        self.assertEqual(job["status"], "failed")
        ended = datetime.fromisoformat(job["finished_at"])
        self.assertFalse(manager.tick(ended + timedelta(minutes=14)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(manager.tick(ended + timedelta(minutes=16)))
        self.join(manager)
        self.assertEqual(len(calls), 2)

    def test_restart_recovers_dead_pid_without_changing_last_results(self):
        self.workspace.search("catalog")
        before = self.workspace.get()["results"]
        write_json(self.root / "state/jobs" / f"{self.first}.json", {
            "id": "old-job", "profile_id": self.first, "status": "running", "pid": 999999999})
        with patch("grantfinder.jobs._alive", return_value=False):
            manager = self.manager(lambda workspace, mode, refresh, progress: workspace.get())
        self.assertEqual(manager.status(self.first)["status"], "interrupted")
        self.assertEqual(self.workspace.get()["results"], before)

    def test_scheduler_starts_search_without_a_browser_request(self):
        self.workspace.save_automation({"enabled": True, "interval_hours": 24})
        ran = threading.Event()
        def runner(workspace, mode, refresh, progress):
            ran.set()
            return workspace.get()
        manager = self.manager(runner)
        manager.start_scheduler()
        self.assertTrue(ran.wait(2))
        self.join(manager)
        self.assertEqual(manager.status(self.first)["status"], "succeeded")

    def test_server_close_remains_interrupted_after_runner_returns_or_raises(self):
        for should_fail in (False, True):
            with self.subTest(should_fail=should_fail):
                entered, release = threading.Event(), threading.Event()
                self.releases.append(release)
                def runner(workspace, mode, refresh, progress):
                    entered.set()
                    release.wait(4)
                    if should_fail:
                        raise RuntimeError("wrapped interruption")
                    return workspace.get()
                manager = self.manager(runner)
                manager.start("catalog")
                self.assertTrue(entered.wait(2))
                manager.close()
                release.set()
                self.join(manager)
                self.assertEqual(manager.status(self.first)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
