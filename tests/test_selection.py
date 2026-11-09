"""Selection planning is local, persistent, and separate from opportunity discovery."""

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
from grantfinder.storage import read_json, write_json
from grantfinder.workspace import Workspace


class SelectionClient:
    model = "synthetic-selection-model"

    def __init__(self):
        self.calls = 0
        self.messages = []
        self.callback = None
        self.value = {"queries": ["optics research funding"], "recommendations": [
            {"id": "track_a", "reason": "광학 연구 지원 주제", "matched_keywords": ["optics", "invented"]},
            {"id": "family", "reason": "같은 사업의 다른 유형", "matched_keywords": ["optics"]},
            {"id": "not_in_catalog", "reason": "잘못된 ID", "matched_keywords": []}]}

    def chat(self, messages, schema=None):
        self.calls += 1
        self.messages.append(messages)
        if self.callback:
            self.callback()
        if isinstance(self.value, Exception):
            raise self.value
        return {"message": {"content": json.dumps(self.value, ensure_ascii=False)}}


class SelectionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "track_a", "title": "Optics A", "source_url": "https://foundation.example/grants/a",
             "eligibility_summary": "Optics research", "support_type": "research_grant"},
            {"id": "track_b", "title": "Optics B", "source_url": "https://foundation.example/grants/b"}]})
        write_json(self.root / "data/program_groups.json", {"groups": [
            {"id": "family", "title": "Optics Grant", "candidate_ids": ["track_a", "track_b"]}]})
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "foundation", "name": "Foundation", "url": "https://foundation.example/",
             "domains": ["foundation.example"]},
            {"id": "other", "name": "Other", "url": "https://other.example/", "domains": ["other.example"]}],
            "pages": []})
        self.client = SelectionClient()
        self.workspace = Workspace(self.root, client=self.client)
        self.workspace.save_profile({"name": "Synthetic Researcher", "keywords": ["optics"]})


class SelectionPreparationTests(SelectionFixture):
    def test_preparation_returns_grouped_suggestions_without_search_or_selection_changes(self):
        self.workspace.save_known([{"kind": "site", "source_id": "other", "name": "Other",
                                    "url": "https://other.example/", "notes": "사용자 메모"}])
        before = self.workspace.get()
        with patch("grantfinder.workspace.search_web") as web, patch("grantfinder.workspace.collect_site_notices") as sites:
            state = self.workspace.prepare_selection()
        web.assert_not_called()
        sites.assert_not_called()
        self.assertEqual(state["known"], before["known"])
        for key in ("results", "queries", "searched_at", "applications", "memory", "search_statuses"):
            self.assertEqual(state[key], before[key])
        plan = state["selection_plan"]
        self.assertEqual(plan["recommendations"], [
            {"id": "family", "reason": "광학 연구 지원 주제", "matched_keywords": ["optics"]}])
        self.assertEqual(plan["queries"], ["optics research funding"])
        self.assertEqual(plan["recommended_source_ids"], ["foundation"])
        self.assertFalse(plan["stale"])
        self.assertTrue(plan["prepared_at"])
        self.assertEqual(plan["model"], self.client.model)
        saved = read_json(self.root / "state/workspaces" / f'{state["profile"]["id"]}.json')
        self.assertEqual(saved["selection_plan"], plan)

    def test_reopening_valid_plan_does_not_call_model(self):
        first = self.workspace.prepare_selection()["selection_plan"]
        self.client.value = RuntimeError("must not run")
        reopened = Workspace(self.root, client=self.client).prepare_selection()["selection_plan"]
        self.assertEqual(reopened, first)
        self.assertEqual(self.client.calls, 1)

    def test_empty_recommendations_are_a_valid_reusable_plan(self):
        self.client.value = {"queries": [], "recommendations": []}
        self.assertEqual(self.workspace.prepare_selection()["selection_plan"]["recommendations"], [])
        self.workspace.prepare_selection()
        self.assertEqual(self.client.calls, 1)

    def test_profile_change_marks_old_suggestions_stale_and_replaces_them(self):
        first = self.workspace.prepare_selection()["selection_plan"]
        state = self.workspace.get()
        changed = self.workspace.save_profile(dict(state["profile"], keywords=["biology"]))
        self.assertTrue(changed["selection_plan"]["stale"])
        self.assertEqual(changed["selection_plan"]["prepared_at"], first["prepared_at"])
        updated = self.workspace.prepare_selection()["selection_plan"]
        self.assertFalse(updated["stale"])
        self.assertNotEqual(updated["profile_fingerprint"], first["profile_fingerprint"])
        self.assertEqual(self.client.calls, 2)

    def test_model_failure_keeps_prior_stale_suggestions_and_user_selection(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "program", "program_id": "family", "name": "Optics Grant"}])
        before = self.workspace.get()
        self.client.value = RuntimeError("synthetic model failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic model failure"):
            self.workspace.prepare_selection()
        self.assertEqual(self.workspace.get(), before)

    def test_profile_change_during_preparation_does_not_save_old_plan(self):
        before = self.workspace.get()["profile"]
        self.client.callback = lambda: self.workspace.save_profile(dict(before, keywords=["biology"]))
        with self.assertRaisesRegex(RuntimeError, "추천 준비 중"):
            self.workspace.prepare_selection()
        state = self.workspace.get()
        self.assertEqual(state["profile"]["keywords"], ["biology"])
        self.assertIsNone(state["selection_plan"])

    def test_selection_change_during_preparation_does_not_overwrite_it(self):
        self.client.callback = lambda: self.workspace.save_known([
            {"kind": "program", "name": "User custom grant", "notes": "keep this"}])
        with self.assertRaisesRegex(RuntimeError, "추천 준비 중"):
            self.workspace.prepare_selection()
        state = self.workspace.get()
        self.assertEqual(state["known"][0]["name"], "User custom grant")
        self.assertIsNone(state["selection_plan"])

    def test_plan_does_not_reactivate_previous_researcher(self):
        first_id = self.workspace.get()["profile"]["id"]
        def switch_profile():
            self.workspace.upload_cv("second.json", json.dumps({"name": "Second", "keywords": ["biology"]}).encode())
        self.client.callback = switch_profile
        state = self.workspace.prepare_selection()
        self.assertEqual(state["profile"]["id"], first_id)
        self.assertNotEqual(self.workspace.get()["profile"]["id"], first_id)
        self.assertIsNone(self.workspace.get()["selection_plan"])

    def test_catalog_or_model_change_invalidates_saved_plan(self):
        first = self.workspace.prepare_selection()["selection_plan"]
        pool = read_json(self.root / "data/public_opportunities.json")
        pool["candidates"][0]["eligibility_summary"] = "Changed public conditions"
        write_json(self.root / "data/public_opportunities.json", pool)
        second = self.workspace.prepare_selection()["selection_plan"]
        self.assertNotEqual(first["input_fingerprint"], second["input_fingerprint"])
        Workspace(self.root, model="another-model", client=self.client).prepare_selection()
        self.assertEqual(self.client.calls, 3)

    def test_shared_planning_cache_avoids_repeat_model_call_for_discovery(self):
        workspace = Workspace(self.root, model="synthetic-installed-model")
        with patch("grantfinder.workspace.OllamaClient", return_value=self.client) as model:
            workspace.prepare_selection()
            workspace.search("catalog")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.client.calls, 1)
        self.assertTrue(workspace.plan_cache_hit)

    def test_missing_profile_fails_before_model_call(self):
        self.workspace.clear_saved()
        with self.assertRaisesRegex(ValueError, "먼저 CV"):
            self.workspace.prepare_selection()
        self.assertEqual(self.client.calls, 0)


class SelectionScopeTests(SelectionFixture):
    def setUp(self):
        super().setUp()
        pool = read_json(self.root / "data/public_opportunities.json")
        pool["candidates"].extend([
            {"id": "other_optics", "title": "Other Optics Funding", "source_url": "https://other.example/optics"},
            {"id": "unrelated", "title": "Biology Fellowship", "source_url": "https://third.example/biology"}])
        write_json(self.root / "data/public_opportunities.json", pool)

    def test_selected_program_limits_new_workflow_catalog_results(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "program", "program_id": "family", "name": "Optics Grant"}])
        state = self.workspace.search("catalog")
        self.assertEqual([item["id"] for item in state["results"]], ["family"])

    def test_selected_track_limits_results_to_its_program(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "program", "program_id": "track_b", "name": "Optics B"}])
        state = self.workspace.search("catalog")
        self.assertEqual([item["id"] for item in state["results"]], ["family"])

    def test_selected_program_is_included_without_model_or_keyword_match(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "program", "program_id": "unrelated", "name": "Biology Fellowship"}])
        state = self.workspace.search("catalog")
        self.assertEqual([item["id"] for item in state["results"]], ["unrelated"])
        self.assertTrue(state["results"][0]["known"])
        self.assertFalse(state["results"][0]["recommended"])
        self.assertEqual(state["results"][0]["matched_keywords"], [])

    def test_selected_site_limits_catalog_results_to_that_source(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "site", "source_id": "foundation", "name": "Foundation",
                                    "url": "https://foundation.example/"}])
        state = self.workspace.search("catalog")
        self.assertEqual([item["id"] for item in state["results"]], ["family"])
        self.assertTrue(state["results"][0]["known_source"])

    def test_selected_site_includes_nonmatching_program_from_its_domain(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "site", "name": "Third Foundation", "url": "https://third.example/"}])
        state = self.workspace.search("catalog")
        self.assertEqual([item["id"] for item in state["results"]], ["unrelated"])
        self.assertTrue(state["results"][0]["known_source"])
        self.assertFalse(state["results"][0]["recommended"])
        self.assertEqual(state["results"][0]["matched_keywords"], [])

    def test_manually_named_program_does_not_create_or_rename_catalog_groups(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "program", "name": "A New User Program"}])
        state = self.workspace.search("catalog")
        self.assertEqual(state["results"], [])
        self.assertEqual(state["catalog"]["program_count"], 3)
        prompt = json.loads(self.client.messages[-1][1]["content"])
        self.assertIn({"kind": "program", "name": "A New User Program"}, prompt["known"])
        self.assertNotIn("A New User Program", [item["name"] for item in state["known_options"]["programs"]])

    def test_no_selection_retains_information_based_results(self):
        self.workspace.prepare_selection()
        state = self.workspace.search("catalog")
        self.assertEqual({item["id"] for item in state["results"]}, {"family", "other_optics"})
        self.assertTrue(all(item["recommended"] or item["matched_keywords"] for item in state["results"]))

    def test_legacy_workflow_keeps_existing_catalog_behavior(self):
        self.workspace.save_known([{"kind": "program", "program_id": "family", "name": "Optics Grant"}])
        state = self.workspace.search("catalog")
        self.assertIsNone(state["selection_plan"])
        self.assertEqual({item["id"] for item in state["results"]}, {"family", "other_optics"})

    def test_web_discoveries_remain_available_outside_selected_catalog(self):
        self.workspace.prepare_selection()
        self.workspace.save_known([{"kind": "program", "program_id": "family", "name": "Optics Grant"}])
        with patch("grantfinder.workspace.collect_site_notices", return_value=([], [])), patch(
                "grantfinder.workspace.search_web", return_value=([
                    {"title": "New Research Support", "url": "https://discovery.example/open-call"}], [])):
            state = self.workspace.search("all")
        self.assertEqual([item["id"] for item in state["results"] if item["origin"] == "catalog"], ["family"])
        self.assertEqual(len([item for item in state["results"] if item["origin"] == "web"]), 1)


class SelectionEndpointTests(SelectionFixture):
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

    def post(self):
        request = urllib.request.Request(self.address + "/api/selection", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_post_returns_workspace_with_selection_plan(self):
        status, state = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(state["selection_plan"]["recommendations"][0]["id"], "family")
        self.assertEqual(state["known"], [])
        self.assertIn("job", state)

    def test_model_failure_is_reported_without_a_successful_plan(self):
        self.client.value = RuntimeError("synthetic model failure")
        status, state = self.post()
        self.assertEqual(status, 503)
        self.assertIn("synthetic model failure", state["error"])
        self.assertIsNone(self.workspace.get()["selection_plan"])


if __name__ == "__main__":
    unittest.main()
