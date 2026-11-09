"""A generic CV workspace never inherits another researcher's recommendations."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.storage import read_json, write_json
from grantfinder.workspace import Workspace, _stage


class LocalClient:
    model = "synthetic-local-model"

    def __init__(self):
        self.calls = []
        self.plan = {"queries": ["optics faculty research grants"], "recommendations": [
            {"id": "family", "reason": "광학 연구를 제안할 수 있는 분야를 검토합니다.", "matched_keywords": ["optics"]}]}

    def chat(self, messages, schema=None):
        self.calls.append((copy.deepcopy(messages), schema))
        if isinstance(self.plan, Exception):
            raise self.plan
        return {"message": {"content": json.dumps(self.plan, ensure_ascii=False)}}


class WorkspaceTests(unittest.TestCase):
    def test_registered_and_manual_school_sites_both_constrain_search(self):
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "school", "name": "School research office", "url": "https://school.example/notices/", "domains": ["school.example"]}], "pages": []})
        self.upload()
        self.workspace.save_known([{"kind": "site", "name": "School research office", "source_id": "school", "url": "https://school.example/notices/"}])
        self.workspace.add_source({"name": "Second school", "url": "https://second.example/research/"})
        queries = self.workspace._custom_site_queries(self.workspace.get(), ["optics grants"])
        self.assertTrue(any("site:school.example/notices" in query for query in queries))
        self.assertTrue(any("site:second.example/research" in query for query in queries))
        self.assertIn("optics grants", queries)

    def test_direct_school_collection_survives_search_failure_and_reaches_reviewer(self):
        self.upload()
        self.workspace.add_source({"name": "School research office", "url": "https://school.example/notices/"})
        notice = {"id": "school_notice", "title": "학교 신진 연구비 공모", "url": "https://school.example/notices/123",
                  "source_id": "school", "source_name": "School research office", "discovered_from": "https://school.example/notices/",
                  "last_seen_at": "2026-11-08T00:00:00Z", "snippet": "공개 목록에서 발견한 공고"}
        self.site_collector.return_value = ([notice], [{"status": "ok", "found": 1, "source_id": "school"}])
        reviewed = []
        def reviewer(profile, results, client, **kwargs):
            reviewed.extend(copy.deepcopy(results))
            return results
        with patch("grantfinder.workspace.search_web", return_value=([], [{"status": "error", "error": "Search unavailable"}])):
            state = self.workspace.search("web", reviewer=reviewer)
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(reviewed[0]["tracks"][0]["discovered_from"], notice["discovered_from"])
        self.assertTrue(state["results"][0]["known_source"])
        self.assertTrue(any(item["status"] == "error" for item in state["search_statuses"]))
        self.assertEqual(self.site_collector.call_args.args[0][0]["name"], "School research office")

    def test_direct_and_search_notices_merge_without_losing_discovery_source(self):
        self.upload()
        notice = {"title": "School research funding", "url": "https://school.example/call/1", "discovered_from": "https://school.example/notices/", "source_name": "School"}
        self.site_collector.return_value = ([notice], [{"status": "ok", "found": 1}])
        with patch("grantfinder.workspace.search_web", return_value=([dict(notice, discovered_from="")], [{"status": "ok", "found": 1}])):
            state = self.workspace.search("web")
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(len(state["results"][0]["tracks"]), 1)
        self.assertEqual(state["results"][0]["tracks"][0]["discovered_from"], notice["discovered_from"])

    def test_hibrain_is_search_only_for_registered_and_manually_entered_urls(self):
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "hibrain", "name": "Hibrain", "url": "https://www.hibrain.net/", "domains": ["hibrain.net"], "collection_mode": "search_only"}], "pages": []})
        self.upload()
        self.workspace.add_source({"name": "Hibrain board", "url": "https://www.hibrain.net/research/"})
        sites = self.workspace._selected_sites(self.workspace.get())
        self.assertEqual(sites[0]["collection_mode"], "search_only")
        with patch("grantfinder.workspace.search_web", return_value=([
                {"title": "Research funding call", "url": "https://www.hibrain.net/research/123", "snippet": "Search result"}], [{"status": "ok", "found": 1}])):
            state = self.workspace.search("web")
        self.assertEqual(state["results"][0]["tracks"][0]["collection_mode"], "search_only")

    def test_unselected_sites_are_not_collected(self):
        self.upload()
        self.workspace.add_source({"name": "School", "url": "https://school.example/notices/"})
        self.workspace.save_known([])
        self.assertEqual(self.workspace._selected_sites(self.workspace.get()), [])

    def test_json_summary_saves_conditions_and_source_without_model_reanalysis(self):
        state = self.upload()
        raw = copy.deepcopy(state["profile"])
        raw.update(name="Edited researcher", research_summary="Optics and PDE research", constraints="장비 지원 제외", conditions_text="방문연구를 우선 검토합니다.")
        raw["cv_import"] = {"method": "injected", "filename": "other.pdf"}
        calls = len(self.client.calls)
        saved = self.workspace.save_profile(raw)
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(saved["profile"]["name"], "Edited researcher")
        self.assertEqual(saved["profile"]["conditions_text"], raw["conditions_text"])
        self.assertEqual(saved["profile"]["constraints"], raw["constraints"])
        self.assertEqual(saved["profile"]["cv_import"], state["profile"]["cv_import"])
        self.assertEqual(Workspace(self.root).get()["profile"]["research_summary"], raw["research_summary"])

    def test_manually_named_program_is_an_explicit_search_query(self):
        self.upload()
        self.workspace.save_known([{"kind": "program", "name": "Synthetic Foundation Seed Fund"}])
        queries = self.workspace._custom_site_queries(self.workspace.get(), ["optics grants"])
        self.assertTrue(any("Synthetic Foundation Seed Fund" in query for query in queries))
        self.assertIn("optics grants", queries)

    def test_deadlines_without_a_timezone_do_not_invent_a_stage(self):
        self.assertEqual(_stage({"deadline": "2000-01-01"}), "조건 확인")
        self.assertEqual(_stage({"deadline": "2000-01-01T12:00:00"}), "조건 확인")
        self.assertEqual(_stage({"deadline": "2000-01-01T12:00:00+09:00"}), "최근 회차 마감")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "track_a", "title": "Shared Grant A", "sponsor": "Foundation", "support_type": "research_grant",
             "eligibility_summary": "Faculty optics research", "application_timing": "매년 4월", "deadline": None,
             "source_url": "https://foundation.example/program-a", "checked_on": "2026-11-01",
             "fit": "Previous Synthetic Researcher only", "personal_constraints": ["Previous person's excluded status"], "reason": "Previous-researcher-specific"},
            {"id": "track_b", "title": "Shared Grant B", "sponsor": "Foundation", "support_type": "research_grant",
             "source_url": "https://foundation.example/program-b", "eligibility_summary": "Faculty research"},
            {"id": "separate", "title": "Different Grant", "sponsor": "Foundation", "support_type": "fellowship",
             "source_url": "https://foundation.example/different", "eligibility_summary": "Research visitors"},
        ]})
        write_json(self.root / "data/program_groups.json", {"groups": [
            {"id": "family", "title": "Shared Grant", "candidate_ids": ["track_a", "track_b"],
             "track_labels": {"track_a": "유형 A", "track_b": "유형 B"}}
        ]})
        write_json(self.root / "outputs/funding_catalog.json", {"profile_id": "previous-synthetic-researcher", "candidates": [
            {"id": "old_person", "reason": "Previous Synthetic Researcher excluded"}]})
        self.client = LocalClient()
        self.workspace = Workspace(self.root, client=self.client)
        collector = patch("grantfinder.workspace.collect_site_notices", return_value=([], []))
        self.site_collector = collector.start()
        self.addCleanup(collector.stop)

    def upload(self, name="First Researcher", keywords=None):
        return self.workspace.upload_cv("profile.json", json.dumps(
            {"name": name, "keywords": keywords or ["optics"], "phd_date": "2020-06"}).encode())

    def test_fresh_workspace_is_blank_and_counts_shared_program_families(self):
        state = self.workspace.get()
        self.assertIsNone(state["profile"])
        self.assertEqual(state["known"], [])
        self.assertEqual(state["results"], [])
        self.assertEqual(state["catalog"], {"program_count": 2, "track_count": 3})

    def test_profiles_have_separate_known_lists_and_results(self):
        first = self.upload()
        self.workspace.save_known([{"kind": "program", "name": "Shared Grant"}])
        self.workspace.search("catalog")
        second = self.upload("Second Researcher", ["biology"])
        self.assertNotEqual(first["profile"]["id"], second["profile"]["id"])
        self.assertEqual(second["known"], [])
        self.assertEqual(second["results"], [])
        restored = self.upload()
        self.assertEqual(restored["known"][0]["name"], "Shared Grant")
        self.assertEqual(len(restored["results"]), 1)

    def test_site_is_known_source_but_does_not_make_unknown_program_known(self):
        self.upload()
        self.workspace.save_known([{"kind": "site", "name": "Foundation", "url": "https://foundation.example"}])
        state = self.workspace.search("catalog")
        self.assertTrue(all(result["known_source"] for result in state["results"]))
        self.assertTrue(all(not result["known"] for result in state["results"]))
        self.workspace.save_known([{"kind": "program", "name": "Shared Grant"}])
        results = self.workspace.search("catalog")["results"]
        self.assertTrue(results[0]["known"])

    def test_common_pool_and_prompt_do_not_reuse_old_personal_fit_or_full_cv(self):
        self.upload("New Person", ["optics"])
        state = self.workspace.search("catalog")
        prompt = self.client.calls[0][0][1]["content"]
        output = json.dumps(state["results"], ensure_ascii=False)
        for text in (prompt, output):
            self.assertNotIn("Previous Synthetic Researcher", text)
            self.assertNotIn("Previous-researcher-specific", text)
            self.assertNotIn("Previous person's", text)
            self.assertNotIn("New Person", text)
        self.assertEqual(state["results"][0]["match_reason"], self.client.plan["recommendations"][0]["reason"])
        self.assertEqual([track["stage"] for track in state["results"][0]["tracks"]], ["조건 확인", "조건 확인"])

    def test_tracks_are_grouped_without_reusing_personal_hanwoomul_label(self):
        definitions = read_json(self.root / "data/program_groups.json")
        definitions["groups"][0]["candidate_ids"].append("gov_nrf_hanwoomul_replacement")
        definitions["groups"][0]["track_labels"]["gov_nrf_hanwoomul_replacement"] = "한우물파기 · 기존과제 교체"
        pool = read_json(self.root / "data/public_opportunities.json")
        pool["candidates"].append({"id": "gov_nrf_hanwoomul_replacement", "title": "한우물파기", "source_url": "https://gov.example/call"})
        write_json(self.root / "data/program_groups.json", definitions)
        write_json(self.root / "data/public_opportunities.json", pool)
        self.upload()
        results = self.workspace.search("catalog")["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["tracks"]), 3)
        self.assertEqual(results[0]["tracks"][2]["title"], "한우물파기")

    def test_mocked_web_provider_merges_same_notice_but_not_entire_domain(self):
        self.upload()
        web = [
            {"id": "same", "title": "Shared Grant official page", "url": "https://foundation.example/program-a", "last_seen_at": "2026-11-07T00:00:00Z"},
            {"id": "new_one", "title": "Unknown Optics Opportunity", "url": "https://foundation.example/unknown-one", "snippet": "Funding", "last_seen_at": "2026-11-07T00:00:00Z"},
            {"id": "new_two", "title": "Independent Travel Program", "url": "https://foundation.example/unknown-two", "last_seen_at": "2026-11-07T00:00:00Z"},
        ]
        statuses = [{"query": "optics faculty research grants", "status": "ok", "found": 3, "cache_hit": False}]
        with patch("grantfinder.workspace.search_web", return_value=(web, statuses)) as search:
            state = self.workspace.search("all")
        self.assertEqual(search.call_args.kwargs["max_results"], 4)
        self.assertEqual(len(state["results"]), 3)
        self.assertEqual(len(state["results"][0]["tracks"]), 2)
        self.assertTrue(state["results"][0]["web_discovered"])
        self.assertEqual(sum(item["origin"] == "web" for item in state["results"]), 2)
        self.assertEqual(state["search_statuses"], statuses)
        self.assertTrue(state["searched_at"])

    def test_manual_profile_works_without_a_cv_upload(self):
        state = self.workspace.save_profile({"name": "Manual Researcher", "keywords": ["optics"], "constraints": "과제 없음"})
        self.assertTrue(state["profile"]["id"].startswith("cv_"))
        self.assertFalse(state["profile"]["documents"]["cv"])
        self.assertEqual(state["profile"]["cv_import"]["method"], "manual")
        self.assertIsNone(state["profile"]["cv_import"]["filename"])
        identifier = state["profile"]["id"]
        self.assertEqual(self.workspace.save_profile(dict(state["profile"], keywords=["biology"]))["profile"]["id"], identifier)

    def test_web_only_returns_only_web_findings_and_groups_found_tracks(self):
        self.upload()
        found = [{"id": "a", "title": "Shared Grant A", "url": "https://foundation.example/program-a"},
                 {"id": "b", "title": "Shared Grant B", "url": "https://foundation.example/program-b"}]
        with patch("grantfinder.workspace.search_web", return_value=(found, [])):
            state = self.workspace.search("web")
        self.assertEqual(len(state["results"]), 1)
        self.assertEqual(state["results"][0]["title"], "Shared Grant")
        self.assertEqual(state["results"][0]["origin"], "web")
        self.assertEqual(len(state["results"][0]["tracks"]), 2)

    def test_no_recommendations_or_keyword_links_produces_no_personalized_pool(self):
        self.upload("Biology Researcher", ["biology"])
        self.client.plan["recommendations"] = []
        state = self.workspace.search("catalog")
        self.assertEqual(state["catalog"]["program_count"], 2)
        self.assertEqual(state["results"], [])

    def test_short_ai_keyword_does_not_match_json_keys_urls_or_substrings(self):
        pool = read_json(self.root / "data/public_opportunities.json")
        for item in pool["candidates"]:
            item["source_url"] = "https://unrelated.example/ai.html"
            item["eligibility_summary"] = "Paid faculty training"
        write_json(self.root / "data/public_opportunities.json", pool)
        self.upload("AI Researcher", ["AI", "ML"])
        self.client.plan["recommendations"] = []
        self.assertEqual(self.workspace.search("catalog")["results"], [])
        pool["candidates"][2]["eligibility_summary"] = "Faculty AI research"
        write_json(self.root / "data/public_opportunities.json", pool)
        results = self.workspace.search("catalog")["results"]
        self.assertEqual([item["id"] for item in results], ["separate"])
        self.assertEqual(results[0]["matched_keywords"], ["AI"])

    def test_track_stage_uses_deadline_facts_and_errors_are_visible(self):
        pool = read_json(self.root / "data/public_opportunities.json")
        pool["candidates"][0]["deadline"] = "2000-01-01T12:00:00+09:00"
        pool["candidates"][1]["deadline"] = "2099-01-01T12:00:00+09:00"
        write_json(self.root / "data/public_opportunities.json", pool)
        self.upload()
        errors = [{"query": "optics grants", "status": "error", "error": "search failed"},
                  {"query": "other grant", "status": "empty", "found": 0}]
        with patch("grantfinder.workspace.search_web", return_value=([], errors)):
            state = self.workspace.search("all")
        self.assertEqual([track["stage"] for track in state["results"][0]["tracks"]], ["최근 회차 마감", "공고 확인"])
        self.assertEqual(state["errors"], ["search failed"])

    def test_editing_conditions_or_known_list_marks_stale_without_deleting_knowledge(self):
        self.upload()
        self.workspace.search("catalog")
        profile = self.workspace.get()["profile"]
        profile["keywords"] = ["biology"]
        edited = self.workspace.save_profile(profile)
        self.assertEqual(len(edited["results"]), 1)
        self.assertTrue(edited["results_stale"])
        self.assertTrue(edited["searched_at"])
        self.assertTrue(edited["memory"]["programs"])
        self.workspace.search("catalog")
        changed = self.workspace.save_known([{"kind": "site", "name": "A", "url": "https://a.example"}])
        self.assertTrue(changed["results"])
        self.assertTrue(changed["queries"])
        self.assertTrue(changed["results_stale"])

    def test_model_failure_preserves_successful_results_and_records_error(self):
        self.upload()
        self.workspace.search("catalog")
        self.client.plan = RuntimeError("model unavailable")
        with self.assertRaisesRegex(RuntimeError, "연구지원 탐색"):
            self.workspace.search("catalog")
        state = self.workspace.get()
        self.assertTrue(state["results"])
        self.assertTrue(state["results_stale"])
        self.assertTrue(state["memory"]["programs"])
        self.assertEqual(state["search_statuses"][0]["stage"], "model")

    def test_names_and_email_are_removed_from_search_queries(self):
        self.upload("New Person")
        self.client.plan["queries"] = ["New Person grants", "contact person@example.org", "optics research grant"]
        self.assertEqual(self.workspace.search("catalog")["queries"], ["optics research grant"])

    def test_model_failure_does_not_switch_back_to_cv_replaced_during_search(self):
        first = self.upload("First Researcher")

        def replace_cv_then_fail(*args, **kwargs):
            self.upload("Second Researcher", ["biology"])
            raise RuntimeError("model unavailable")

        self.client.chat = replace_cv_then_fail
        with self.assertRaises(RuntimeError):
            self.workspace.search("catalog")
        current = self.workspace.get()
        self.assertEqual(current["profile"]["name"], "Second Researcher")
        self.assertNotEqual(current["profile"]["id"], first["profile"]["id"])

    def test_known_input_rejects_dangerous_url_and_does_not_rewrite_sources(self):
        self.upload()
        for url in ("javascript:alert(1)", "file:///tmp/cv", "http://127.0.0.1:11434"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.workspace.save_known([{"kind": "site", "name": "A", "url": url}])
        self.workspace.save_known([{"kind": "site", "name": "A", "url": "https://a.example"}])
        self.assertFalse((self.root / "data/sources.json").exists())

    def test_old_workspace_migrates_persistent_fields_and_deep_defaults(self):
        state = self.upload()
        path = self.root / "state/workspaces" / (state["profile"]["id"] + ".json")
        old = read_json(path)
        old.pop("applications")
        old["automation"] = {"enabled": True}
        old["memory"] = {"programs": []}
        write_json(path, old)
        migrated = self.workspace.get()
        self.assertEqual(migrated["applications"], [])
        self.assertEqual(migrated["automation"]["interval_hours"], 24)
        self.assertEqual(migrated["memory"]["sources"], [])
        self.assertEqual(migrated["memory"]["search_runs"], [])

    def test_memory_accumulates_programs_sources_and_bounded_search_history(self):
        self.upload()
        first = self.workspace.search("catalog")
        self.assertEqual(len(first["memory"]["programs"]), 1)
        self.assertEqual(len(first["memory"]["sources"]), 2)
        first_seen = first["memory"]["programs"][0]["first_seen_at"]
        self.client.plan["recommendations"].append({"id": "separate", "reason": "교류 경로 검토", "matched_keywords": []})
        for _ in range(31):
            state = self.workspace.search("catalog")
        self.assertEqual(len(state["memory"]["programs"]), 2)
        self.assertEqual(len(state["memory"]["sources"]), 3)
        self.assertEqual(len(state["memory"]["programs"][0]["tracks"]), 2)
        self.assertEqual(state["memory"]["programs"][0]["first_seen_at"], first_seen)
        self.assertEqual(len(state["memory"]["search_runs"]), 30)
        self.assertFalse((self.root / "data/sources.json").exists())

    def test_application_selection_is_persistent_and_duplicate_tracks_merge(self):
        first = self.upload()
        self.workspace.search("catalog")
        selected = self.workspace.add_application({"program_id": "family", "track_ids": ["track_a"]})
        application = selected["applications"][0]
        self.assertEqual(application["status"], "interested")
        self.assertIsNone(application["tasks"][0]["due_date"])
        done = dict(application["tasks"][0], done=True)
        self.workspace.update_application(application["id"], {"status": "preparing", "priority": "high", "notes": "초안 작성", "tasks": [done]})
        selected = self.workspace.add_application({"program_id": "family", "track_ids": ["track_b"]})
        self.assertEqual(len(selected["applications"]), 1)
        application = selected["applications"][0]
        self.assertEqual(application["selected_track_ids"], ["track_a", "track_b"])
        self.assertEqual(len(application["tasks"]), 1)
        self.assertTrue(application["tasks"][0]["done"])
        self.assertEqual(application["status"], "preparing")
        self.assertEqual(application["notes"], "초안 작성")
        self.upload("Second Researcher", ["biology"])
        self.assertEqual(self.workspace.get()["applications"], [])
        restored = self.upload()
        self.assertEqual(restored["profile"]["id"], first["profile"]["id"])
        self.assertEqual(restored["applications"][0]["id"], application["id"])
        self.assertEqual(self.workspace.delete_application(application["id"])["applications"], [])

    def test_application_selection_can_use_memory_and_rejects_invalid_tracks(self):
        self.upload()
        self.workspace.search("catalog")
        self.client.plan["recommendations"] = []
        profile = self.workspace.get()["profile"]
        self.workspace.save_profile(dict(profile, keywords=["biology"]))
        self.workspace.search("catalog")
        self.assertEqual(self.workspace.get()["results"], [])
        with self.assertRaises(ValueError):
            self.workspace.add_application({"program_id": "family", "track_ids": ["separate"]})
        with self.assertRaises(ValueError):
            self.workspace.add_application({"program_id": "family", "track_ids": []})
        state = self.workspace.add_application({"program_id": "family"})
        self.assertEqual(len(state["applications"][0]["tracks"]), 2)
        application = state["applications"][0]
        for invalid in ({"status": "auto_submitted"}, {"priority": []},
                        {"tasks": [{"id": "task1", "title": "초안", "done": False, "due_date": "2026-11"}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.workspace.update_application(application["id"], invalid)

    def test_automation_validates_intervals_and_schedules_next_success(self):
        self.upload()
        state = self.workspace.save_automation({"enabled": True, "interval_hours": 6})
        next_run = datetime.fromisoformat(state["automation"]["next_run_at"])
        self.assertLess(abs((next_run - datetime.now(timezone.utc)).total_seconds()), 3)
        state = self.workspace.search("catalog")
        next_run = datetime.fromisoformat(state["automation"]["next_run_at"])
        self.assertGreater((next_run - datetime.now(timezone.utc)).total_seconds(), 6 * 3600 - 3)
        self.assertIsNone(self.workspace.save_automation({"enabled": False})["automation"]["next_run_at"])
        for invalid in ({"enabled": "yes"}, {"interval_hours": 7}, {"interval_hours": 6.0}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.workspace.save_automation(invalid)

    def test_background_commit_preserves_plans_settings_and_active_cv_switch(self):
        state = self.upload()
        identifier = state["profile"]["id"]
        self.workspace.search("catalog")
        worker = Workspace(self.root, client=self.client, profile_id=identifier)

        def network(*args, **kwargs):
            self.workspace.add_application({"program_id": "family", "track_ids": ["track_a"]})
            self.workspace.save_automation({"enabled": True, "interval_hours": 12})
            self.upload("Second Researcher", ["biology"])
            return [], [{"query": "optics grants", "status": "empty", "found": 0}]

        with patch("grantfinder.workspace.search_web", side_effect=network):
            completed = worker.search("all", refresh=True)
        self.assertEqual(self.workspace.get()["profile"]["name"], "Second Researcher")
        self.assertEqual(completed["profile"]["id"], identifier)
        self.assertEqual(len(completed["applications"]), 1)
        self.assertEqual(completed["automation"]["interval_hours"], 12)
        self.assertTrue(completed["automation"]["enabled"])

    def test_changed_conditions_abort_async_commit_without_losing_memory(self):
        self.upload()
        self.workspace.search("catalog")

        def network(*args, **kwargs):
            profile = self.workspace.get()["profile"]
            self.workspace.save_profile(dict(profile, keywords=["biology"]))
            return [], [{"query": "optics grants", "status": "empty", "found": 0}]

        with patch("grantfinder.workspace.search_web", side_effect=network):
            with self.assertRaisesRegex(RuntimeError, "조건이 바뀌었습니다"):
                self.workspace.search("all")
        state = self.workspace.get()
        self.assertEqual(state["profile"]["keywords"], ["biology"])
        self.assertTrue(state["results_stale"])
        self.assertEqual(len(state["memory"]["programs"]), 1)

    def test_reviewer_progress_adapter_and_reviewed_memory_are_saved(self):
        self.upload()
        stages = []

        def reviewer(profile, results, client, progress=None, cache_dir=None):
            progress("원문 확인 중")
            results[0]["tracks"][0]["next_action"] = "추천서 요청"
            results[0]["tracks"][0]["reviewed"] = True
            return results

        state = self.workspace.search("catalog", reviewer=reviewer, progress=lambda stage, message: stages.append(stage))
        self.assertIn("planning", stages)
        self.assertIn("reviewing", stages)
        self.assertIn("saving", stages)
        self.assertTrue(state["memory"]["programs"][0]["tracks"][0]["reviewed"])
        application = self.workspace.add_application({"program_id": "family", "track_ids": ["track_a"]})["applications"][0]
        self.assertEqual(application["tasks"][0]["title"], "추천서 요청")

    def test_known_picker_ids_are_validated_and_preserved(self):
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "foundation", "name": "Foundation", "domains": ["foundation.example"], "url": "https://foundation.example/"}], "pages": []})
        self.upload()
        self.workspace.save_known([{"kind": "program", "program_id": "family", "name": "표시 이름"},
                                   {"kind": "site", "source_id": "foundation", "name": "사이트"}])
        state = self.workspace.search("catalog")
        self.assertTrue(state["results"][0]["known"])
        self.assertTrue(state["results"][0]["known_source"])
        self.assertEqual(state["known"][0]["program_id"], "family")
        self.assertEqual(state["known_options"]["sites"][0]["id"], "foundation")
        with self.assertRaises(ValueError):
            self.workspace.save_known([{"kind": "program", "name": "없음", "program_id": "missing"}])

    def test_selected_web_program_is_rechecked_when_absent_from_next_search(self):
        self.upload()
        found = [{"id": "web_track", "title": "Unknown Optics Grant", "url": "https://new.example/optics", "snippet": "Faculty grant"}]
        with patch("grantfinder.workspace.search_web", return_value=(found, [{"status": "ok", "found": 1}])):
            state = self.workspace.search("all")
        web_group = next(item for item in state["results"] if item["origin"] == "web")
        self.workspace.add_application({"program_id": web_group["id"], "track_ids": ["web_track"]})
        seen = []

        def reviewer(profile, results, client, progress=None, cache_dir=None):
            seen.extend(copy.deepcopy(results))
            return results

        with patch("grantfinder.workspace.search_web", return_value=([], [{"status": "empty", "found": 0}])):
            state = self.workspace.search("all", reviewer=reviewer)
        self.assertEqual(seen[0]["id"], web_group["id"])
        self.assertTrue(seen[0]["watch"])
        self.assertTrue(seen[0]["tracks"][0]["watch"])
        self.assertEqual(state["applications"][0]["program_id"], web_group["id"])
        self.upload("Another Researcher", ["biology"])
        self.assertEqual(self.workspace.get()["memory"]["programs"], [])

    def test_fresh_track_updates_selected_snapshot_without_changing_manual_plan(self):
        self.upload()
        self.workspace.search("catalog")
        selected = self.workspace.add_application({"program_id": "family", "track_ids": ["track_a"]})
        application = selected["applications"][0]
        task = dict(application["tasks"][0], done=True)
        self.workspace.update_application(application["id"], {"status": "preparing", "priority": "high", "notes": "수정 금지 메모", "tasks": [task]})
        pool = read_json(self.root / "data/public_opportunities.json")
        pool["candidates"][0]["deadline"] = "2099-03-01T18:00:00+09:00"
        pool["candidates"][0]["eligibility_summary"] = "Updated faculty requirement"
        write_json(self.root / "data/public_opportunities.json", pool)
        state = self.workspace.search("catalog")
        application = state["applications"][0]
        self.assertEqual(application["tracks"][0]["deadline"], "2099-03-01T18:00:00+09:00")
        self.assertEqual(application["tracks"][0]["eligibility_summary"], "Updated faculty requirement")
        self.assertEqual(application["status"], "preparing")
        self.assertEqual(application["priority"], "high")
        self.assertEqual(application["notes"], "수정 금지 메모")
        self.assertEqual(application["tasks"], [task])

    def test_unselected_prior_web_discoveries_remain_limited_memory_candidates(self):
        self.upload()
        results = [{"id": "past_" + str(index), "title": "Past program " + str(index), "origin": "web",
                    "tracks": [{"id": "t_" + str(index), "title": "Track", "url": f"https://past.example/{index}"}],
                    "matched_keywords": [], "rank": 900} for index in range(12)]
        raw = self.workspace._load()
        raw["memory"]["programs"] = results
        self.workspace._save(raw)
        state = self.workspace.search("catalog")
        previous = [item for item in state["results"] if item["id"].startswith("past_")]
        self.assertEqual(len(previous), 10)
        self.assertTrue(all(not item["watch"] for item in previous))

    def test_real_client_plan_cache_reuses_only_identical_inputs_and_model(self):
        workspace = Workspace(self.root, model="installed-local-model")
        workspace.save_profile({"name": "Cache Researcher", "keywords": ["optics"]})
        local = LocalClient()
        with patch("grantfinder.workspace.OllamaClient", return_value=local) as factory:
            workspace.search("catalog")
            workspace.search("catalog")
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(len(local.calls), 1)
            factory.assert_called_with("installed-local-model", timeout=300)
            self.assertTrue(workspace.plan_cache_hit)
            profile = workspace.get()["profile"]
            workspace.save_profile(dict(profile, keywords=["biology"]))
            workspace.search("catalog")
            self.assertEqual(factory.call_count, 2)
            workspace.save_known([{"kind": "program", "name": "Shared Grant"}])
            workspace.search("catalog")
            self.assertEqual(factory.call_count, 3)
            pool = read_json(self.root / "data/public_opportunities.json")
            pool["candidates"][0]["eligibility_summary"] = "Changed official condition"
            write_json(self.root / "data/public_opportunities.json", pool)
            workspace.search("catalog")
            self.assertEqual(factory.call_count, 4)
            Workspace(self.root, model="different-installed-model").search("catalog")
            self.assertEqual(factory.call_count, 5)
        cached = list((self.root / "cache/discovery_plans").glob("*.json"))
        self.assertEqual(len(cached), 5)
        plan = read_json(cached[0])["plan"]
        self.assertEqual(set(plan), {"queries", "recommendations"})
        self.assertNotIn("conditions", plan)

    def test_planning_payload_bounds_conditions_but_keeps_all_track_titles(self):
        self.upload()
        workspace = self.workspace._load()
        groups = [{"id": "family", "title": "Long group " * 100, "items": [
            {"id": f"track_{index}", "title": f"Track {index} " + "name " * 100,
             "eligibility_summary": "condition " * 1000, "support_type": "research_grant"}
            for index in range(4)]}]
        self.workspace._plan(workspace, groups)
        payload = json.loads(self.client.calls[-1][0][1]["content"])
        common = payload["common_programs"][0]
        self.assertEqual(len(common["conditions"]), 2)
        self.assertTrue(all(len(value) <= 100 for value in common["conditions"]))
        self.assertEqual(len(common["tracks"]), 4)
        self.assertTrue(all(len(value) <= 100 for value in common["tracks"]))
        self.assertLess(len(self.client.calls[-1][0][1]["content"]), 2000)

    def homepage_profile(self, identifier="cv_0123456789abcdef", keywords=None, checked_at="2026-11-07T00:00:00Z"):
        profile = self.workspace.save_profile({"name": "Homepage Researcher", "keywords": keywords or ["optics"]})["profile"]
        profile["id"] = identifier
        profile["cv_import"] = {"method": "homepage_ollama_local", "url": "https://researcher.example/", "checked_at": checked_at}
        profile["documents"]["cv"] = False
        profile.pop("constraints", None)
        return profile

    def test_homepage_profile_import_reload_and_edit_keep_cv_not_uploaded(self):
        profile = self.homepage_profile()
        parser = unittest.mock.Mock(return_value=profile)
        module = SimpleNamespace(parse_homepage_profile=parser)
        with patch.dict(sys.modules, {"grantfinder.homepage_profile": module}):
            state = self.workspace.import_homepage("https://researcher.example/")
        parser.assert_called_once_with("https://researcher.example/", client=self.client, cache_dir=self.root / "cache/homepages")
        self.assertEqual(state["profile"]["id"], profile["id"])
        self.assertFalse(state["profile"]["documents"]["cv"])
        reloaded = Workspace(self.root, client=self.client).get()
        self.assertEqual(reloaded["profile"]["cv_import"]["method"], "homepage_ollama_local")
        edited = self.workspace.save_profile(dict(state["profile"], constraints="방문연구 불가"))
        self.assertFalse(edited["profile"]["documents"]["cv"])

    def test_same_homepage_refresh_preserves_plans_known_automation_and_constraints(self):
        profile = self.homepage_profile()
        module = SimpleNamespace(parse_homepage_profile=lambda *args, **kwargs: copy.deepcopy(profile))
        with patch.dict(sys.modules, {"grantfinder.homepage_profile": module}):
            state = self.workspace.import_homepage("https://researcher.example/")
            self.workspace.save_known([{"kind": "program", "name": "Shared Grant"}])
            self.workspace.save_profile(dict(state["profile"], constraints="수동 조건 보존"))
            self.workspace.save_automation({"enabled": True, "interval_hours": 12})
            self.workspace.search("catalog")
            self.workspace.add_application({"program_id": "family", "track_ids": ["track_a"]})
            profile["cv_import"]["checked_at"] = "2026-11-08T00:00:00Z"
            same = self.workspace.import_homepage("https://researcher.example/")
            self.assertFalse(same["results_stale"])
            self.assertEqual(same["profile"]["constraints"], "수동 조건 보존")
            self.assertEqual(len(same["applications"]), 1)
            self.assertEqual(len(same["known"]), 1)
            self.assertTrue(same["memory"]["programs"])
            self.assertTrue(same["automation"]["enabled"])
            profile["keywords"] = ["biology"]
            changed = self.workspace.import_homepage("https://researcher.example/")
            self.assertTrue(changed["results_stale"])
            self.assertEqual(len(changed["applications"]), 1)
            self.assertEqual(changed["profile"]["keywords"], ["biology"])

    def test_different_homepage_and_cv_have_isolated_saved_state(self):
        first = self.homepage_profile()
        module = SimpleNamespace(parse_homepage_profile=lambda *args, **kwargs: copy.deepcopy(first))
        with patch.dict(sys.modules, {"grantfinder.homepage_profile": module}):
            self.workspace.import_homepage("https://researcher.example/")
            self.workspace.save_known([{"kind": "program", "name": "Shared Grant"}])
            first["id"] = "cv_fedcba9876543210"
            first["cv_import"]["url"] = "https://second.example/"
            different = self.workspace.import_homepage("https://second.example/")
        self.assertEqual(different["known"], [])
        self.assertEqual(different["memory"]["programs"], [])
        self.assertEqual(different["applications"], [])
        cv = self.upload("CV Researcher", ["biology"])
        self.assertNotEqual(cv["profile"]["id"], different["profile"]["id"])
        self.assertEqual(cv["known"], [])

    def test_failed_homepage_fetch_preserves_active_workspace(self):
        before = self.upload()
        module = SimpleNamespace(parse_homepage_profile=unittest.mock.Mock(side_effect=RuntimeError("page unavailable")))
        with patch.dict(sys.modules, {"grantfinder.homepage_profile": module}):
            with self.assertRaisesRegex(RuntimeError, "page unavailable"):
                self.workspace.import_homepage("https://unavailable.example/")
        self.assertEqual(self.workspace.get()["profile"], before["profile"])

    def test_bulk_all_158_known_items_round_trip_and_plan_stays_compact(self):
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": f"program_{index}", "title": f"Program {index}", "source_url": f"https://programs.example/{index}"}
            for index in range(82)]})
        write_json(self.root / "data/program_groups.json", {"groups": []})
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": f"source_{index}", "name": f"Source {index}", "url": f"https://source{index}.example/", "domains": [f"source{index}.example"]}
            for index in range(76)], "pages": []})
        self.upload()
        options = self.workspace.get()["known_options"]
        items = [{"kind": "program", "program_id": item["id"], "name": item["name"], "url": item["url"]} for item in options["programs"]]
        items += [{"kind": "site", "source_id": item["id"], "name": item["name"], "url": item["url"]} for item in options["sites"]]
        state = self.workspace.save_known(items)
        self.assertEqual(len(state["known"]), 158)
        self.assertEqual(len(Workspace(self.root, client=self.client).get()["known"]), 158)
        self.workspace.search("catalog")
        known = json.loads(self.client.calls[-1][0][1]["content"])["known"]
        self.assertLess(len(json.dumps(known, ensure_ascii=False)), 12000)
        self.assertTrue(all("name" not in item for item in known if item["kind"] == "program"))

    def test_custom_source_add_dedup_notes_reload_and_profile_isolation(self):
        self.upload()
        state = self.workspace.add_source({"name": "Outside Fund", "url": "outside.example/funding/", "notes": "수동 확인 메모"})
        identifier = state["custom_sources"][0]["id"]
        created = state["custom_sources"][0]["created_at"]
        repeated = self.workspace.add_source({"name": "Updated Fund", "url": "https://outside.example/funding?utm_source=copy#intro", "notes": ""})
        self.assertEqual(len(repeated["custom_sources"]), 1)
        self.assertEqual(len(repeated["known"]), 1)
        self.assertEqual(len(repeated["memory"]["sources"]), 1)
        self.assertEqual(repeated["custom_sources"][0]["id"], identifier)
        self.assertEqual(repeated["custom_sources"][0]["created_at"], created)
        self.assertEqual(repeated["known"][0]["notes"], "수동 확인 메모")
        self.assertEqual(repeated["custom_sources"][0]["name"], "Updated Fund")
        self.assertEqual(repeated["known_options"]["sites"][0]["origin"], "custom")
        self.assertEqual(len(Workspace(self.root, client=self.client).get()["custom_sources"]), 1)
        self.assertFalse((self.root / "data/sources.json").exists())
        self.upload("Other CV", ["biology"])
        self.assertEqual(self.workspace.get()["custom_sources"], [])
        with self.assertRaises(ValueError):
            self.workspace.save_known([{"kind": "site", "name": "Injected", "source_id": identifier}])

    def test_manual_site_promotes_but_name_only_site_does_not(self):
        self.upload()
        state = self.workspace.save_known([{"kind": "site", "name": "Outside", "url": "outside.example/calls"},
                                           {"kind": "site", "name": "Known name only"}])
        self.assertEqual(len(state["custom_sources"]), 1)
        self.assertEqual(state["known"][0]["source_id"], state["custom_sources"][0]["id"])
        self.assertNotIn("source_id", state["known"][1])
        with self.assertRaises(ValueError):
            self.workspace.add_source({"name": "Bad", "url": "javascript:alert(1)"})
        self.assertEqual(len(self.workspace.get()["custom_sources"]), 1)

    def test_existing_registered_source_url_is_not_duplicated_as_custom(self):
        write_json(self.root / "data/sources.json", {"sources": [
            {"id": "registered", "name": "Registered Fund", "url": "https://registered.example/funding/", "domains": ["registered.example"]}], "pages": []})
        self.upload()
        state = self.workspace.add_source({"name": "Same Fund", "url": "registered.example/funding"})
        self.assertEqual(state["custom_sources"], [])
        self.assertEqual(state["known"][0]["source_id"], "registered")
        self.assertEqual(len(state["known_options"]["sites"]), 1)
        self.assertEqual(state["known_options"]["sites"][0]["origin"], "registered")

    def test_selected_custom_site_queries_are_added_and_unchecked_sites_stop(self):
        self.upload()
        self.workspace.add_source({"name": "Outside Fund", "url": "outside.example/funding"})
        with patch("grantfinder.workspace.search_web", return_value=([], [{"status": "empty", "found": 0}])) as search:
            self.workspace.search("all")
        queries = search.call_args.args[0]
        self.assertTrue(any(query.startswith("site:outside.example/funding ") and "optics" in query for query in queries))
        self.workspace.save_known([])
        self.assertEqual(len(self.workspace.get()["custom_sources"]), 1)
        with patch("grantfinder.workspace.search_web", return_value=([], [{"status": "empty", "found": 0}])) as search:
            self.workspace.search("all")
        self.assertFalse(any(query.startswith("site:outside.example") for query in search.call_args.args[0]))

    def test_known_bulk_dedup_preserves_manual_notes_and_explicit_id_errors(self):
        self.upload()
        state = self.workspace.save_known([{"kind": "program", "program_id": "family", "name": "Shared", "notes": "메모1"},
                                           {"kind": "program", "program_id": "family", "name": "Shared", "notes": "메모2"}])
        self.assertEqual(len(state["known"]), 1)
        self.assertIn("메모1", state["known"][0]["notes"])
        self.assertIn("메모2", state["known"][0]["notes"])
        with self.assertRaises(ValueError):
            self.workspace.save_known([{"id": "same", "kind": "program", "name": "One"},
                                       {"id": "same", "kind": "program", "name": "Two"}])


if __name__ == "__main__":
    unittest.main()
