"""Free research conditions retain details outside the structured CV fields."""

import copy
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.cv_profile import parse_conditions_text
from grantfinder.opportunity_review import review_opportunities
from grantfinder.server import create_server
from grantfinder.storage import write_json
from grantfinder.workspace import Workspace


TEXT = "국내에서 수행할 수 있는 연구비를 찾습니다.\n예산은 3천만 원 이상, 해외 장기방문은 제외합니다.\n2027년 3월 이후 시작하고 GPU를 구입할 수 있으면 좋겠습니다."


class LocalClient:
    model = "test-local-conditions"

    def __init__(self, raw=None):
        self.raw = raw if raw is not None else {}
        self.calls = []

    def chat(self, messages, schema=None):
        self.calls.append((copy.deepcopy(messages), schema))
        if isinstance(self.raw, Exception):
            raise self.raw
        return {"message": {"content": json.dumps(self.raw, ensure_ascii=False)}}


class ConditionsTextTests(unittest.TestCase):
    def test_conditions_without_name_or_research_fields_are_allowed_and_preserved(self):
        client = LocalClient({})
        result = parse_conditions_text("  " + TEXT + "\n", "cv_0123456789abcdef", client=client)
        self.assertEqual(result["conditions_text"], TEXT)
        self.assertIsNone(result["name"])
        self.assertEqual(result["keywords"], [])
        self.assertIn(TEXT, client.calls[0][0][1]["content"])
        self.assertFalse(client.calls[0][1]["additionalProperties"])

    def test_invalid_text_is_rejected_before_model_call(self):
        for value in (None, 12, [], "", "  \n\t", "가" * 12001):
            with self.subTest(value_type=type(value).__name__):
                client = LocalClient()
                with self.assertRaises(ValueError):
                    parse_conditions_text(value, "cv_0123456789abcdef", client=client)
                self.assertEqual(client.calls, [])

    def test_dates_from_revised_text_keep_month_precision(self):
        client = LocalClient({"phd_date": "2020-06", "first_faculty_date": "2024-03"})
        result = parse_conditions_text("박사 취득은 2020년 6월, 최초 임용은 2024년 3월입니다.",
                                       "cv_0123456789abcdef", client=client)
        self.assertEqual(result["phd_date"], "2020-06")
        self.assertEqual(result["first_faculty_date"], "2024-03")

    def test_arbitrary_conditions_are_sent_to_official_opportunity_review(self):
        client = LocalClient({"reviews": []})
        group = {"id": "g", "title": "GPU Research Grant", "origin": "catalog", "tracks": [
            {"id": "t", "title": "Main", "url": "https://example.org/grant",
             "eligibility_summary": "University researchers may apply for research equipment funding."}]}
        review_opportunities({"conditions_text": TEXT}, [group], client)
        request = json.loads(client.calls[0][0][1]["content"])
        self.assertEqual(request["cv_conditions"]["conditions_text"], TEXT)


class ConditionsWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        write_json(self.root / "data/public_opportunities.json", {"candidates": [
            {"id": "t", "title": "Optics Grant", "sponsor": "Foundation",
             "support_type": "research_grant", "source_url": "https://example.org/grant",
             "eligibility_summary": "Faculty optics research funding"}]})
        write_json(self.root / "data/program_groups.json", {"groups": [
            {"id": "g", "title": "Optics Grant", "candidate_ids": ["t"]}]})
        self.client = LocalClient()
        self.workspace = Workspace(self.root, client=self.client)

    def plan(self):
        self.client.raw = {"queries": ["optics equipment faculty research grant"],
                           "recommendations": [{"id": "g", "reason": "광학 장비 연구지원",
                                                "matched_keywords": ["optics"]}]}

    def test_text_only_profile_persists_and_full_text_reaches_discovery(self):
        result = self.workspace.save_conditions(TEXT)
        self.assertFalse(result["profile"]["documents"]["cv"])
        self.assertEqual(result["profile"]["cv_import"]["method"], "manual")
        identifier = result["profile"]["id"]
        self.assertRegex(identifier, r"^cv_[a-f0-9]{16}$")
        reloaded = Workspace(self.root, client=self.client).get()
        self.assertEqual(reloaded["profile"]["id"], identifier)
        self.assertEqual(reloaded["profile"]["conditions_text"], TEXT)
        self.plan()
        self.workspace.search("catalog")
        packet = json.loads(self.client.calls[-1][0][1]["content"])
        self.assertEqual(packet["conditions"]["conditions_text"], TEXT)

    def test_edit_keeps_cv_identity_known_list_and_application_notes(self):
        initial = self.workspace.upload_cv("profile.json", json.dumps({
            "name": "Researcher", "keywords": ["optics"], "phd_date": "2020-06"}).encode())
        self.workspace.save_known([{"kind": "program", "program_id": "g", "name": "Optics Grant"}])
        self.plan()
        self.workspace.search("catalog")
        state = self.workspace.add_application({"program_id": "g", "track_ids": ["t"]})
        state = self.workspace.update_application(state["applications"][0]["id"],
                                                  {"notes": "장비 견적서를 준비합니다.", "status": "preparing"})
        applications = copy.deepcopy(state["applications"])
        known = copy.deepcopy(state["known"])
        self.client.raw = {"name": "Researcher", "keywords": ["optics"], "phd_date": "2020-08"}
        state = self.workspace.save_conditions("Researcher, optics. 박사 취득일은 2021년 2월입니다.\n" + TEXT)
        self.assertEqual(state["profile"]["id"], initial["profile"]["id"])
        self.assertEqual(state["profile"]["cv_import"], initial["profile"]["cv_import"])
        self.assertTrue(state["profile"]["documents"]["cv"])
        self.assertEqual(state["profile"]["phd_date"], "2020-08")
        self.assertEqual(state["known"], known)
        self.assertEqual(state["applications"], applications)
        self.assertTrue(state["results_stale"])

    def test_model_failure_keeps_stored_workspace_and_active_profile(self):
        state = self.workspace.upload_cv("profile.json", b'{"name":"Researcher","keywords":["optics"]}')
        path = self.root / "state/workspaces" / (state["profile"]["id"] + ".json")
        before = path.read_bytes()
        active = (self.root / "state/workspace_active.json").read_bytes()
        self.client.raw = RuntimeError("local model unavailable")
        with self.assertRaisesRegex(RuntimeError, "local model unavailable"):
            self.workspace.save_conditions(TEXT)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.root / "state/workspace_active.json").read_bytes(), active)


class ConditionsHTTPTests(unittest.TestCase):
    def test_text_save_and_legacy_profile_save_share_the_existing_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            client = LocalClient({})
            workspace = Workspace(Path(directory), client=client)
            server = create_server(Path(directory), port=0, workspace=workspace)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def put(value):
                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/profile",
                                                 data=json.dumps(value).encode(), method="PUT",
                                                 headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=3) as response:
                    return response.status, json.load(response)

            try:
                status, state = put({"text": TEXT})
                self.assertEqual(status, 200)
                self.assertEqual(state["profile"]["conditions_text"], TEXT)
                status, state = put({"profile": {"name": "Researcher", "keywords": ["optics"]}})
                self.assertEqual(status, 200)
                self.assertEqual(state["profile"]["name"], "Researcher")
            finally:
                server.discovery_jobs.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
