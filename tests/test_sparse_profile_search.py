"""Missing identity/research fields do not block explicit or general discovery."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.storage import write_json
from grantfinder.workspace import Workspace, _empty


class SparseProfileSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "alpha", "title": "Alpha Foundation Support", "source_url": "https://alpha.example/call",
             "eligibility_summary": "Faculty research support"},
            {"id": "beta", "title": "Beta Research Fellowship", "source_url": "https://beta.example/call"}]})
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "alpha_source", "name": "Alpha Foundation", "url": "https://alpha.example/",
             "domains": ["alpha.example"]}], "pages": []})
        self.workspace = Workspace(self.root)
        self.profile = {"id": "cv_1234567890abcdef", "name": None, "keywords": [],
                        "research_summary": None, "research_tracks": [], "active_projects": [],
                        "constraints": "", "conditions_text": "", "cv_import": {"method": "profile_json", "filename": "sparse.json"}}
        self.workspace._save(_empty(copy.deepcopy(self.profile)))
        model = patch.object(self.workspace, "_model_client", side_effect=AssertionError("must not require a model"))
        self.model = model.start()
        self.addCleanup(model.stop)
        collector = patch("grantfinder.workspace.collect_site_notices", return_value=([], []))
        self.collector = collector.start()
        self.addCleanup(collector.stop)
        web = patch("grantfinder.workspace.search_web", return_value=([
            {"title": "Public Research Opportunity", "url": "https://public.example/open-call", "snippet": "Funding call"}],
            [{"query": "synthetic", "status": "ok", "found": 1}]))
        self.web = web.start()
        self.addCleanup(web.stop)

    def test_selected_program_is_searchable_without_identity_keywords_or_model(self):
        self.workspace.save_known([{"kind": "program", "program_id": "alpha", "name": "Alpha Foundation Support"}])
        reviewer = Mock(side_effect=AssertionError("must not review unknown personal conditions"))
        state = self.workspace.search("all", reviewer=reviewer)
        self.assertEqual([item["id"] for item in state["results"] if item["origin"] == "catalog"], ["alpha"])
        self.assertTrue(any("Alpha Foundation Support" in query for query in state["queries"]))
        self.assertEqual(state["profile"], self.profile)
        self.assertTrue(all(item["eligibility_status"] == "needs_review" for item in state["results"]))
        self.assertTrue(all(not item["condition_checks"] for item in state["results"]))
        reviewer.assert_not_called()
        self.model.assert_not_called()

    def test_selected_site_still_drives_collection_search_and_catalog_scope(self):
        self.workspace.save_known([{"kind": "site", "source_id": "alpha_source", "name": "Alpha Foundation",
                                    "url": "https://alpha.example/"}])
        state = self.workspace.search("all")
        self.assertEqual([item["id"] for item in state["results"] if item["origin"] == "catalog"], ["alpha"])
        self.assertTrue(any("site:alpha.example" in query for query in state["queries"]))
        self.assertEqual(self.collector.call_args.args[0][0]["id"], "alpha_source")
        self.assertTrue(state["searched_at"])
        self.model.assert_not_called()

    def test_manual_program_name_reaches_web_search_without_creating_a_catalog_program(self):
        self.workspace.save_known([{"kind": "program", "name": "Independent Foundation Seed Award"}])
        state = self.workspace.search("all")
        self.assertTrue(any("Independent Foundation Seed Award" in query for query in self.web.call_args.args[0]))
        self.assertEqual([item for item in state["results"] if item["origin"] == "catalog"], [])
        self.assertEqual(state["catalog"]["program_count"], 2)

    def planning_client(self, queries):
        client = Mock()
        client.model = "synthetic-condition-planner"
        client.chat.return_value = {"message": {"content": json.dumps({"queries": queries, "recommendations": []})}}
        self.model.side_effect = None
        self.model.return_value = client
        return client

    def test_manual_conditions_reach_model_and_reviewer_in_full_without_inventing_profile_fields(self):
        workspace = self.workspace._load()
        conditions = "한국 대학 연구지원. " * 80 + "마지막 조건: 장기 해외방문 제외."
        workspace["profile"].update(constraints="GPU 계산자원 방문연구", conditions_text=conditions)
        before = copy.deepcopy(workspace["profile"])
        self.workspace._save(workspace)
        client = self.planning_client(["GPU 계산자원 방문연구 공고", "한국 대학 연구지원"])
        reviewer = Mock(side_effect=lambda profile, results, model, **kwargs: results)
        state = self.workspace.search("web", reviewer=reviewer)
        packet = json.loads(client.chat.call_args.args[0][1]["content"])
        self.assertEqual(packet["conditions"]["conditions_text"], conditions)
        self.assertEqual(packet["conditions"]["constraints"], before["constraints"])
        self.assertEqual(reviewer.call_args.args[0]["conditions_text"], conditions)
        self.assertIs(reviewer.call_args.args[2], client)
        self.assertTrue(any("GPU 계산자원 방문연구" in query for query in state["queries"]))
        self.assertTrue(any("한국 대학 연구지원" in query for query in state["queries"]))
        self.assertEqual(state["profile"], before)
        self.assertEqual(state["profile"]["keywords"], [])
        self.assertIsNone(state["profile"]["name"])

    def test_either_manual_condition_field_keeps_the_model_planning_path(self):
        for field in ("conditions_text", "constraints"):
            with self.subTest(field=field):
                workspace = _empty(copy.deepcopy(self.profile))
                workspace["profile"][field] = "GPU 계산자원 지원"
                self.workspace._save(workspace)
                client = self.planning_client(["GPU 계산자원 연구지원"])
                self.workspace.search("web")
                packet = json.loads(client.chat.call_args.args[0][1]["content"])
                self.assertEqual(packet["conditions"][field], "GPU 계산자원 지원")

    def test_no_starting_points_uses_general_queries_instead_of_an_error(self):
        state = self.workspace.search("all")
        self.assertEqual(state["queries"], ["연구자 연구비 연구지원 공모", "research funding grants open calls"])
        self.assertEqual(len(state["results"]), 1)
        self.assertFalse(state["results_stale"])
        self.assertEqual(state["profile"], self.profile)

    def test_unknown_profile_can_prepare_selected_targets_without_model(self):
        self.workspace.save_known([{"kind": "program", "program_id": "beta", "name": "Beta Research Fellowship"}])
        state = self.workspace.prepare_selection()
        self.assertEqual(state["selection_plan"]["recommendations"], [
            {"id": "beta", "reason": "선택한 사업·사이트의 공고 확인", "matched_keywords": []}])
        self.assertTrue(any("Beta Research Fellowship" in query for query in state["selection_plan"]["queries"]))
        self.assertEqual(state["results"], [])
        self.model.assert_not_called()

    def test_catalog_only_can_return_empty_without_requiring_a_model_or_network(self):
        state = self.workspace.search("catalog")
        self.assertEqual(state["results"], [])
        self.web.assert_not_called()
        self.collector.assert_not_called()
        self.model.assert_not_called()

    def test_old_personal_eligibility_does_not_survive_an_unknown_profile_search(self):
        workspace = self.workspace._load()
        workspace["memory"]["programs"] = [{"id": "old_web", "title": "Earlier Public Call", "origin": "web",
            "eligibility_status": "eligible", "condition_checks": [{"status": "met"}], "review_status": "reviewed",
            "tracks": [{"id": "old_track", "title": "Earlier Track", "url": "https://older.example/call",
                        "eligibility_status": "eligible", "condition_checks": [{"status": "met"}]}]}]
        self.workspace._save(workspace)
        state = self.workspace.search("catalog")
        old = next(item for item in state["results"] if item["id"] == "old_web")
        self.assertEqual(old["eligibility_status"], "needs_review")
        self.assertEqual(old["tracks"][0]["eligibility_status"], "needs_review")
        self.assertEqual(old["condition_checks"], [])
        self.assertEqual(old["tracks"][0]["condition_checks"], [])

    def test_manual_condition_model_queries_keep_existing_name_and_email_filtering(self):
        workspace = self.workspace._load()
        workspace["profile"].update(name="Synthetic Person", conditions_text="Synthetic Person test@example.org GPU 계산자원")
        self.workspace._save(workspace)
        self.planning_client(["Synthetic Person GPU 계산자원", "test@example.org GPU 계산자원", "GPU 계산자원 연구지원"])
        state = self.workspace.search("web")
        queries = " ".join(state["queries"])
        self.assertIn("GPU 계산자원", queries)
        self.assertNotIn("Synthetic Person", queries)
        self.assertNotIn("test@example.org", queries)


if __name__ == "__main__":
    unittest.main()
