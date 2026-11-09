"""Anonymous target selections persist without a fabricated researcher profile."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.cv_profile import normalize_profile
from grantfinder.server import create_server
from grantfinder.storage import read_json, write_json
from grantfinder.workspace import Workspace


class NoModel:
    def chat(self, *args, **kwargs):
        raise AssertionError("Selection persistence must not call a model")


class SelectionDraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "grant", "title": "Shared research grant", "source_url": "https://funding.example/grant"}]})
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "foundation", "name": "Foundation", "url": "https://funding.example/",
             "domains": ["funding.example"]}], "pages": []})
        self.workspace = Workspace(self.root, client=NoModel())
        self.draft_path = self.root / "state/selection_draft.json"
        self.active_path = self.root / "state/workspace_active.json"
        self.items = [{"kind": "site", "name": "My research office", "url": "https://school.example/research",
                       "notes": "학내 연구지원 공고"}]

    def upload(self, name="First researcher"):
        return self.workspace.upload_cv("researcher.json", json.dumps({"name": name, "keywords": ["optics"]}).encode())

    def detach(self):
        self.active_path.unlink(missing_ok=True)

    def test_blank_workspace_exposes_catalog_without_creating_profile(self):
        state = self.workspace.get()
        self.assertIsNone(state["profile"])
        self.assertFalse(state["selection_draft"])
        self.assertEqual(state["catalog"], {"program_count": 1, "track_count": 1})
        self.assertEqual(state["known_options"]["programs"][0]["id"], "grant")
        self.assertEqual(state["known_options"]["sites"][0]["id"], "foundation")
        self.assertFalse((self.root / "state").exists())

    def test_save_and_reload_custom_selection_without_researcher(self):
        state = self.workspace.save_known(self.items)
        self.assertIsNone(state["profile"])
        self.assertTrue(state["selection_draft"])
        self.assertIsNone(state["storage"]["workspace_path"])
        self.assertFalse(self.active_path.exists())
        self.assertFalse((self.root / "state/workspaces").exists())
        stored = read_json(self.draft_path)
        self.assertEqual(set(stored), {"known", "custom_sources"})
        self.assertEqual(stored["known"][0]["source_id"], stored["custom_sources"][0]["id"])
        reloaded = Workspace(self.root).get()
        self.assertEqual(reloaded["known"], state["known"])
        self.assertEqual(reloaded["custom_sources"], state["custom_sources"])
        self.assertTrue(any(item["id"] == state["known"][0]["source_id"]
                            for item in reloaded["known_options"]["sites"]))
        self.assertEqual(Workspace(self.root).save_known(reloaded["known"])["known"], state["known"])

    def test_empty_selection_is_saved_and_custom_options_remain_available(self):
        custom = self.workspace.save_known(self.items)["custom_sources"]
        state = self.workspace.save_known([])
        self.assertTrue(state["selection_draft"])
        self.assertEqual(state["known"], [])
        self.assertEqual(state["custom_sources"], custom)
        self.assertEqual(Workspace(self.root).get()["known"], [])
        self.assertTrue(Workspace(self.root).get()["selection_draft"])

    def test_invalid_source_reference_does_not_replace_saved_draft(self):
        self.workspace.save_known(self.items)
        before = self.draft_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "ID"):
            self.workspace.save_known([{"kind": "site", "name": "Unknown", "source_id": "custom_unknown"}])
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_first_cv_consumes_draft_and_keeps_custom_reference_valid(self):
        draft = self.workspace.save_known(self.items)
        state = self.upload()
        self.assertEqual(state["known"], draft["known"])
        self.assertEqual(state["custom_sources"], draft["custom_sources"])
        self.assertFalse(state["selection_draft"])
        self.assertFalse(self.draft_path.exists())
        self.assertEqual(self.workspace.save_known(state["known"])["known"], draft["known"])
        persisted = read_json(self.workspace._path(state["profile"]["id"]))
        self.assertNotIn("selection_draft", persisted)
        self.assertNotIn("known_options", persisted)

    def test_first_manual_homepage_and_conditions_profiles_consume_draft(self):
        for method in ("manual", "homepage", "conditions"):
            with self.subTest(method=method):
                draft = self.workspace.save_known(self.items)
                raw = {"name": method, "keywords": ["optics"]}
                if method == "manual":
                    state = self.workspace.save_profile(raw)
                elif method == "homepage":
                    profile = normalize_profile(raw, "cv_0123456789abcdef")
                    with patch("grantfinder.homepage_profile.parse_homepage_profile", return_value=profile):
                        state = self.workspace.import_homepage("https://researcher.example/")
                else:
                    profile = normalize_profile(raw, "cv_fedcba9876543210")
                    with patch("grantfinder.workspace.parse_conditions_text", return_value=profile):
                        state = self.workspace.save_conditions("Optics research")
                self.assertEqual(state["known"], draft["known"])
                self.assertEqual(state["custom_sources"], draft["custom_sources"])
                self.assertFalse(self.draft_path.exists())
                self.workspace.clear_saved()

    def test_explicit_empty_draft_is_consumed_when_first_profile_is_created(self):
        self.workspace.save_known(self.items)
        draft = self.workspace.save_known([])
        state = self.upload()
        self.assertEqual(state["known"], [])
        self.assertEqual(state["custom_sources"], draft["custom_sources"])
        self.assertFalse(self.draft_path.exists())
        self.assertFalse(state["selection_draft"])

    def test_reopening_saved_cv_preserves_both_existing_selection_and_draft(self):
        first = self.upload()
        known = self.workspace.save_known([{"kind": "program", "name": "Prior selection"}])["known"]
        first_path = self.workspace._path(first["profile"]["id"])
        original = first_path.read_bytes()
        self.detach()
        draft = self.workspace.save_known(self.items)
        draft_bytes = self.draft_path.read_bytes()
        restored = self.upload()
        self.assertEqual(restored["known"], known)
        self.assertEqual(first_path.read_bytes(), original)
        self.assertEqual(self.draft_path.read_bytes(), draft_bytes)
        self.assertFalse(restored["selection_draft"])
        self.detach()
        self.assertEqual(self.workspace.get()["known"], draft["known"])
        second = self.upload("New researcher")
        self.assertEqual(second["known"], draft["known"])
        self.assertEqual(first_path.read_bytes(), original)
        self.assertFalse(self.draft_path.exists())

    def test_reopening_manual_profile_does_not_overwrite_prior_saved_state_with_draft(self):
        raw = {"name": "Manual researcher", "keywords": ["optics"]}
        first = self.workspace.save_profile(raw)
        prior = self.workspace.save_known([{"kind": "program", "name": "Prior manual selection"}])["known"]
        self.detach()
        self.workspace.save_known(self.items)
        draft_bytes = self.draft_path.read_bytes()
        restored = self.workspace.save_profile(raw)
        self.assertEqual(restored["profile"]["id"], first["profile"]["id"])
        self.assertEqual(restored["known"], prior)
        self.assertEqual(self.draft_path.read_bytes(), draft_bytes)

    def test_active_profile_saves_do_not_consume_or_change_a_separate_draft(self):
        first = self.upload()
        self.detach()
        self.workspace.save_known(self.items)
        before = self.draft_path.read_bytes()
        write_json(self.active_path, {"id": first["profile"]["id"]})
        state = self.workspace.save_known([{"kind": "program", "name": "Active researcher selection"}])
        self.assertFalse(state["selection_draft"])
        self.assertEqual(self.draft_path.read_bytes(), before)
        self.assertEqual(self.workspace.get()["known"], state["known"])
        second = self.upload("Second researcher")
        self.assertEqual(second["known"], [])
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_bound_worker_cannot_read_save_consume_or_clear_anonymous_draft(self):
        self.workspace.save_known(self.items)
        before = self.draft_path.read_bytes()
        bound = Workspace(self.root, client=NoModel(), profile_id="cv_0123456789abcdef")
        self.assertEqual(bound.get()["known"], [])
        self.assertFalse(bound.get()["selection_draft"])
        with self.assertRaisesRegex(ValueError, "CV"):
            bound.save_known(self.items)
        saved = bound.save_profile({"name": "Bound researcher"})
        self.assertEqual(saved["known"], [])
        self.assertFalse(self.active_path.exists())
        bound.clear_saved()
        self.assertEqual(self.draft_path.read_bytes(), before)

    def test_anonymous_clear_removes_only_draft_and_keeps_other_saved_files(self):
        prior = self.upload()
        path = self.workspace._path(prior["profile"]["id"])
        write_json(self.root / "state/jobs" / f"{prior['profile']['id']}.json", {"status": "succeeded"})
        self.detach()
        self.workspace.save_known(self.items)
        preserved = {item: item.read_bytes() for item in self.root.rglob("*.json") if item != self.draft_path}
        state = self.workspace.clear_saved()
        self.assertIsNone(state["profile"])
        self.assertFalse(state["selection_draft"])
        self.assertEqual(state["known"], [])
        self.assertEqual(state["custom_sources"], [])
        self.assertFalse(self.draft_path.exists())
        self.assertTrue(path.exists())
        for item, value in preserved.items():
            self.assertEqual(item.read_bytes(), value)

    def test_failed_first_profile_write_keeps_draft_for_retry(self):
        self.workspace.save_known(self.items)
        before = self.draft_path.read_bytes()
        with patch("grantfinder.workspace.write_json", side_effect=OSError("Synthetic write failure")):
            with self.assertRaises(OSError):
                self.upload()
        self.assertEqual(self.draft_path.read_bytes(), before)
        self.assertFalse(self.active_path.exists())
        self.assertIsNone(self.workspace.get()["profile"])
        self.assertEqual(self.upload()["known"], json.loads(before)["known"])

    def test_existing_known_http_endpoint_saves_and_reloads_without_profile(self):
        server = create_server(self.root, port=0, workspace=self.workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        address = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(address + "/api/known", method="PUT",
                                             data=json.dumps({"items": self.items}).encode(),
                                             headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=3) as response:
                saved = json.load(response)
                self.assertEqual(response.status, 200)
            self.assertIsNone(saved["profile"])
            self.assertTrue(saved["selection_draft"])
            with opener.open(address + "/api/workspace", timeout=3) as response:
                restored = json.load(response)
            self.assertEqual(restored["known"], saved["known"])
            self.assertFalse(self.active_path.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
