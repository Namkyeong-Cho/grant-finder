"""Personalization regressions and minimal synthetic catalog scenarios.

Fictional researchers and grants exercise restrictions in public call snapshots.
All profiles and example.org catalog programs below are synthetic test data.
"""

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.catalog_html import render_catalog
from grantfinder import cli
from grantfinder.evaluate import evaluate_notice
from grantfinder.funding_catalog import load_catalog
from grantfinder.html_report import render_html_report

AS_OF = datetime(2026, 11, 6, 12, tzinfo=ZoneInfo("Asia/Seoul"))


def fictional_funded_profile():
    """Invented grant and career dates, unrelated to any stored personal CV."""
    return {
        "id": "synthetic-funded-researcher", "name": "가상 연구자", "is_demo": False,
        "affiliation": "가상대학교", "affiliation_type": "university", "employment": "full_time",
        "country": "KR", "nationality": None, "keywords": ["optics"],
        "phd_date": "2020-08", "phd_date_precision": "month",
        "first_faculty_date": "2024-09", "first_faculty_date_precision": "month",
        "active_projects": [{
            "title": "Synthetic Optics Regression Project", "program": "가상 개인기초연구 과제",
            "category": "nrf_individual_basic", "grant_number": "SYNTHETIC-GRANT-001", "role": "PI",
            "start_date": "2025-09-01", "end_date": "2029-08-31",
            "annual_budget_krw": 80000000, "total_budget_krw": 320000000,
        }],
        "other_active_or_pending_projects": [], "infrastructure_grant_awarded": False,
        "user_confirmations": [{"statement": "가상 테스트 설정: 신진인프라 수혜 이력 없음"}],
    }


def public_calls():
    data = json.loads((ROOT / "data/reviewed_notices.json").read_text(encoding="utf-8"))
    return [n for n in data["notices"]
            if n["id"] in {"msit_2027_core_a", "msit_2027_young_a"}]


def synthetic_profile():
    return {"id": "synthetic-catalog-reader", "name": "가상 연구자", "is_demo": False,
            "affiliation": "가상 대학", "phd_date": "2020-08",
            "first_faculty_date": "2024-09", "active_projects": [], "keywords": []}


def program(identifier, **changes):
    item = {"id": identifier, "title": f"가상 프로그램 {identifier}", "sponsor": "가상 기관",
            "category": "government", "support_type": "research_grant", "route": "direct",
            "availability": "open", "fit": "high", "fit_reason": "가상 연구주제와 연결",
            "eligibility_summary": "가상 연구자 신청요건 확인",
            "personal_constraints": [], "application_timing": "2026년11월20일18시 연구자 마감",
            "deadline": "2026-11-20T18:00:00+09:00", "deadline_timezone": "Asia/Seoul",
            "next_action": "가상 신청서 준비", "source_url": "https://example.org/" + identifier,
            "additional_sources": [], "evidence_note": "합성 테스트 자료",
            "checked_on": "2026-11-06"}
    item.update(changes)
    return item


def catalog_fixture():
    return [
        program("direct-current"),
        program("foreign-host", title="가상 호스트 방문연구", category="international",
                support_type="visit", route="host_required", availability="future_watch",
                fit="conditional", deadline=None, application_timing="다음 공모일 미확인",
                personal_constraints=["해외 호스트와 현지체류 필요"]),
        program("excluded", title="가상 중복수행 제외", availability="excluded", fit="excluded",
                deadline=None, personal_constraints=["현 과제와 동시수행 불가"]),
        program("replacement", title="가상 기존과제 교체 경로", availability="announced",
                fit="conditional", application_timing="연구자11월20일18시,기관11월23일18시",
                personal_constraints=["선정 시 기존과제를2027년12월31일까지 종료해야 합니다.",
                                      "현 과제와 병행하는 추가 연구비가 아닙니다."]),
    ]


def write_candidates(directory, candidates, name="synthetic_candidates.json"):
    (Path(directory) / name).write_text(
        json.dumps({"checked_on": "2026-11-06", "category": "government",
                    "candidates": candidates}, ensure_ascii=False), encoding="utf-8")


class Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags, self.text = [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, value):
        self.text.append(value)

    def attrs_for_id(self, identifier):
        return next(attrs for _, attrs in self.tags if attrs.get("id") == identifier)


class FictionalFundedProfileTests(unittest.TestCase):
    def test_current_nrf_pi_ending_2029_excludes_both_2027_ordinary_tracks(self):
        calls, profile = public_calls(), fictional_funded_profile()
        self.assertEqual({n["id"] for n in calls}, {"msit_2027_core_a", "msit_2027_young_a"})
        for notice in calls:
            with self.subTest(track=notice["id"]):
                evaluation = evaluate_notice(notice, profile, AS_OF)
                self.assertEqual(evaluation["eligibility"], "ineligible")
                self.assertTrue(any(c["status"] == "not_met" and "2029-08-31" in c["reason"]
                                    for c in evaluation["conditions"]))
                self.assertFalse(any(t["kind"] == "preparation" or "제출 완료" in t["title"]
                                     for t in evaluation["tasks"]))

    def test_no_project_other_program_copi_or_2027_end_do_not_trigger_blanket_exclusion(self):
        cases = {
            "projects_unknown": None,
            "no_projects": [],
            "different_program": [dict(fictional_funded_profile()["active_projects"][0], category="iitp_collaborative")],
            "not_pi": [dict(fictional_funded_profile()["active_projects"][0], role="co_investigator")],
            "ends_2027": [dict(fictional_funded_profile()["active_projects"][0], end_date="2027-12-31")],
            "future_project": [dict(fictional_funded_profile()["active_projects"][0], start_date="2027-03-01")],
        }
        for case, projects in cases.items():
            for notice in public_calls():
                with self.subTest(case=case, track=notice["id"]):
                    profile = fictional_funded_profile()
                    profile["active_projects"] = projects
                    self.assertNotEqual(evaluate_notice(notice, profile, AS_OF)["eligibility"], "ineligible")

    def test_unknown_project_end_does_not_become_a_confirmed_ineligibility(self):
        profile = fictional_funded_profile()
        profile["active_projects"][0]["end_date"] = None
        for notice in public_calls():
            with self.subTest(track=notice["id"]):
                self.assertEqual(evaluate_notice(notice, profile, AS_OF)["eligibility"], "needs_review")

    def test_verified_restriction_does_not_spread_to_an_unrelated_program(self):
        notice = copy.deepcopy(public_calls()[0])
        notice["id"] = "synthetic-unrelated-program"
        self.assertNotEqual(evaluate_notice(notice, fictional_funded_profile(), AS_OF)["eligibility"], "ineligible")


class CatalogValidationTests(unittest.TestCase):
    def test_minimal_routes_and_exclusion_are_kept_in_the_personal_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            write_candidates(directory, catalog_fixture())
            catalog = load_catalog(Path(directory), synthetic_profile())
        self.assertEqual(catalog["profile_id"], "synthetic-catalog-reader")
        self.assertEqual(catalog["counts"], {"total": 4, "current": 2, "future": 1, "excluded": 1})
        by_id = {item["id"]: item for item in catalog["candidates"]}
        self.assertEqual(by_id["foreign-host"]["route"], "host_required")
        self.assertEqual(by_id["excluded"]["fit"], "excluded")
        self.assertEqual(by_id["replacement"]["fit"], "conditional")

    def test_duplicate_id_across_source_files_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            write_candidates(directory, [program("same-id")], "first_candidates.json")
            write_candidates(directory, [program("same-id")], "second_candidates.json")
            with self.assertRaisesRegex(ValueError, "중복 후보 ID"):
                load_catalog(Path(directory), synthetic_profile())

    def test_unsafe_primary_source_urls_are_rejected(self):
        unsafe_urls = ["javascript:alert(1)", "data:text/html,unsafe", "file:///tmp/source",
                       "https://user:password@example.org/source", "//example.org/source", "https://[broken"]
        for url in unsafe_urls:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                write_candidates(directory, [program("unsafe", source_url=url)])
                with self.assertRaisesRegex(ValueError, "URL"):
                    load_catalog(Path(directory), synthetic_profile())

    def test_excluded_open_entry_is_not_counted_as_a_current_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            write_candidates(directory, [program("blocked-open", fit="excluded", availability="open")])
            catalog = load_catalog(Path(directory), synthetic_profile())
        self.assertEqual(catalog["counts"]["current"], 0)
        self.assertEqual(catalog["counts"]["excluded"], 1)


class PersonalCatalogHtmlTests(unittest.TestCase):
    def setUp(self):
        self.profile = synthetic_profile()
        with tempfile.TemporaryDirectory() as directory:
            write_candidates(directory, catalog_fixture())
            self.catalog = load_catalog(Path(directory), self.profile)

    def test_mismatched_profile_does_not_show_the_saved_personal_catalog(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["profile_id"] = "different-person"
        catalog["profile_name"] = "PRIVATE OTHER RESEARCHER"
        catalog["candidates"][0]["title"] = "PRIVATE PERSONAL OPPORTUNITY"
        html = render_html_report([], [], self.profile, AS_OF, [], [], catalog=catalog)
        self.assertNotIn("PRIVATE OTHER RESEARCHER", html)
        self.assertNotIn("PRIVATE PERSONAL OPPORTUNITY", html)
        self.assertNotIn('id="funding-catalog"', html)

    def test_excluded_candidate_is_marked_separately_and_not_highlighted(self):
        document = Markup(render_catalog(self.catalog, self.profile))
        self.assertEqual(document.attrs_for_id("candidate-excluded")["data-catalog-excluded"], "yes")
        self.assertEqual(document.attrs_for_id("candidate-direct-current")["data-catalog-excluded"], "no")
        highlights = [a["href"] for tag, a in document.tags
                      if tag == "a" and a.get("class") == "catalog-highlight"]
        self.assertIn("#program-direct-current", highlights)
        self.assertNotIn("#program-excluded", highlights)
        self.assertIn(("option", {"value": "excluded"}), document.tags)

    def test_host_and_replacement_conditions_do_not_become_ordinary_direct_highlights(self):
        html = render_catalog(self.catalog, self.profile)
        document = Markup(html)
        self.assertEqual(document.attrs_for_id("candidate-foreign-host")["data-catalog-route"], "host_required")
        self.assertIn("해외 호스트와 현지체류 필요", " ".join(document.text))
        self.assertIn("기존과제를2027년12월31일까지 종료", " ".join(document.text))
        self.assertIn("병행하는 추가 연구비가 아닙니다", " ".join(document.text))
        self.assertIn("연구자11월20일18시,기관11월23일18시", " ".join(document.text))
        highlights = [a["href"] for tag, a in document.tags
                      if tag == "a" and a.get("class") == "catalog-highlight"]
        self.assertNotIn("#candidate-foreign-host", highlights)
        self.assertNotIn("#candidate-replacement", highlights)

    def test_additional_unsafe_source_is_never_rendered_as_a_link(self):
        self.catalog["candidates"][0]["additional_sources"] = ["javascript:alert('x')", "https://example.org/official"]
        document = Markup(render_catalog(self.catalog, self.profile))
        hrefs = [attrs["href"] for tag, attrs in document.tags if tag == "a" and "href" in attrs]
        self.assertIn("https://example.org/official", hrefs)
        self.assertFalse(any(url.startswith("javascript:") for url in hrefs))


class PersonalizeCliTests(unittest.TestCase):
    def test_personalize_retains_fictional_profile_details_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/research").mkdir(parents=True)
            write_candidates(root / "data/research", catalog_fixture())
            profile = fictional_funded_profile()
            profile_path = root / "data/synthetic-profile.json"
            profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
            original_profile = profile_path.read_bytes()
            with patch.object(cli, "ROOT", root), \
                    patch.object(cli, "OllamaClient", side_effect=AssertionError("personalize must stay offline")), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["personalize", "--profile", str(profile_path), "--as-of", "2026-11-06"])
            self.assertEqual(result, 0)
            self.assertEqual(profile_path.read_bytes(), original_profile)
            saved = json.loads((root / "outputs/profile_used.json").read_text(encoding="utf-8"))
            self.assertEqual((saved["phd_date"], saved["phd_date_precision"]), ("2020-08", "month"))
            self.assertEqual((saved["first_faculty_date"], saved["first_faculty_date_precision"]),
                             ("2024-09", "month"))
            self.assertIs(saved["infrastructure_grant_awarded"], False)
            self.assertEqual(saved["other_active_or_pending_projects"], [])
            self.assertEqual(saved["user_confirmations"], profile["user_confirmations"])
            self.assertIsNone(saved["nationality"])
            catalog = json.loads((root / "outputs/funding_catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["profile_id"], profile["id"])
            self.assertEqual(catalog["counts"]["total"], 4)
            summary = json.loads((root / "outputs/run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["mode"], "cv_review")
            self.assertEqual(summary["model_calls"], 0)
            self.assertTrue(summary["review_only"])
            html = (root / "outputs/latest_report.html").read_text(encoding="utf-8")
            self.assertIn('id="funding-catalog"', html)
            self.assertIn("가상 기존과제 교체 경로", html)
            self.assertIn("기존과제를2027년12월31일까지 종료", html)


if __name__ == "__main__":
    unittest.main()
