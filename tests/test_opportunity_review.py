"""Local evidence review must not turn missing facts into eligibility or dates."""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.opportunity_review import _condition_status, _page_text, review_opportunities


def group(identifier="p1", origin="catalog", text="Applicants must work at a university in Korea."):
    return {"id": identifier, "title": "Research opportunity", "origin": origin,
            "tracks": [{"id": identifier + "_t", "title": "Main call", "url": "https://example.org/" + identifier,
                        "eligibility_summary": text, "application_timing": "Deadline 2026-12-01.",
                        "deadline": "2026-12-01", "stage": "공고 확인", "next_action": "원문 확인",
                        **({"snippet": "Search result description"} if origin == "web" else {})}]}


def check(field="country", value="KR", quote="Applicants must work at a university in Korea."):
    return {"condition": "지원 대상 소재국", "field": field, "operator": "eq", "value": value, "quote": quote}


def review(identifier="p1", checks=None, complete=True, relevant=True, relevance_quote=""):
    return {"program_id": identifier, "relevant": relevant, "relevance_quote": relevance_quote,
            "track_updates": [{"track_id": identifier + "_t", "conditions_complete": complete,
                               "condition_checks": checks if checks is not None else [check()],
                               "next_action": "소속 기관의 신청 절차 확인"}]}


class Client:
    def __init__(self, reviews=None, error=None):
        self.reviews = reviews or []
        self.error = error
        self.calls = []

    def chat(self, messages, schema):
        self.calls.append(messages)
        if self.error:
            raise self.error
        ids = {item["program_id"] for item in json.loads(messages[1]["content"])["opportunities"]}
        return {"message": {"content": json.dumps({"reviews": [item for item in self.reviews if item["program_id"] in ids]})}}


class OpportunityReviewTests(unittest.TestCase):
    def test_search_only_notice_is_not_fetched_or_used_as_eligibility_evidence(self):
        item = group(origin="web")
        item["tracks"][0]["collection_mode"] = "search_only"
        client = Client([review()])
        with patch("grantfinder.opportunity_review._page_text", side_effect=AssertionError("Do not fetch restricted sources")):
            result = review_opportunities({"country": "KR"}, [item], client)
        self.assertEqual(result[0]["tracks"][0]["review_evidence_status"], "search_only")
        self.assertEqual(result[0]["eligibility_status"], "needs_review")
        packet = json.loads(client.calls[0][1]["content"])["opportunities"][0]["tracks"][0]
        self.assertEqual(packet["source_text"], "")

    def test_complete_simple_conditions_have_source_and_profile_evidence(self):
        checks = [check(), check("affiliation_type", "university")]
        result = review_opportunities({"country": "KR", "affiliation_type": "university"},
                                      [group()], Client([review(checks=checks)]))
        self.assertEqual(result[0]["eligibility_status"], "eligible")
        self.assertTrue(all(item["status"] == "met" for item in result[0]["condition_checks"]))

    def test_unknown_nationality_stays_unknown_even_when_conditions_complete(self):
        quote = "Applicants must have Korean nationality."
        item = group(text=quote)
        result = review_opportunities({"nationality": None}, [item],
                                      Client([review(checks=[check("nationality", "KR", quote)])]))
        self.assertEqual(result[0]["eligibility_status"], "needs_review")
        self.assertEqual(result[0]["condition_checks"][0]["status"], "unknown")

    def test_explicit_unrestricted_nationality_does_not_require_profile_nationality(self):
        for quote in ("국적 무관", "any nationality", "all nationalities", "regardless of nationality."):
            for field in ("nationality", "manual"):
                with self.subTest(quote=quote, field=field):
                    raw = dict(check(field, "", quote), operator="manual")
                    self.assertEqual(_condition_status(raw, {"nationality": None}, quote), "met")

    def test_unrestricted_nationality_does_not_establish_a_country_condition(self):
        raw = check("country", "KR", "국적 무관")
        self.assertEqual(_condition_status(raw, {"country": "JP"}, "국적 무관"), "unknown")

    def test_unverified_or_compound_nationality_clause_is_not_automatically_met(self):
        raw = dict(check("manual", "", "국적 무관"), operator="manual")
        self.assertEqual(_condition_status(raw, {}, "대학 전임교원 대상"), "unknown")
        compound = "국적 무관. 국내 대학 전임교원만 지원합니다."
        raw["quote"] = compound
        self.assertEqual(_condition_status(raw, {}, compound), "unknown")

    def test_verified_country_mismatch_is_ineligible(self):
        result = review_opportunities({"country": "JP"}, [group()], Client([review()]))
        self.assertEqual(result[0]["eligibility_status"], "ineligible")
        self.assertEqual(result[0]["condition_checks"][0]["status"], "not_met")

    def test_fabricated_quote_or_criterion_cannot_fail_a_researcher(self):
        for raw_check in (check(quote="Only United States citizens may apply."), check(value="US")):
            with self.subTest(raw_check=raw_check):
                result = review_opportunities({"country": "JP"}, [group()],
                                              Client([review(checks=[raw_check])]))
                self.assertEqual(result[0]["eligibility_status"], "needs_review")
                self.assertEqual(result[0]["condition_checks"][0]["status"], "unknown")

    def test_exceptions_and_nationality_are_not_country_restrictions(self):
        for quote in ("Applicants may work in Korea or Japan.", "Korean nationals worldwide may apply.",
                      "국내외 대학 연구자가 신청할 수 있습니다."):
            result = review_opportunities({"country": "JP"}, [group(text=quote)],
                                          Client([review(checks=[check(quote=quote)])]))
            self.assertEqual(result[0]["eligibility_status"], "needs_review")

    def test_manual_and_month_precision_dates_need_review(self):
        quote = "PhD after 2020-06-01; duplicate grants require manual review."
        checks = [dict(check("phd_date", "2020-06-01", quote), operator="after"),
                  dict(check("manual", "", quote), operator="manual")]
        result = review_opportunities({"phd_date": "2020-08"}, [group(text=quote)],
                                      Client([review(checks=checks)]))
        self.assertEqual(result[0]["eligibility_status"], "needs_review")
        self.assertEqual([check["status"] for check in result[0]["condition_checks"]], ["unknown", "unknown"])

    def test_web_fetch_failure_preserves_candidate_and_existing_dates(self):
        original = group(origin="web")
        before = copy.deepcopy(original)
        with patch("grantfinder.opportunity_review._page_text", side_effect=RuntimeError("page unavailable")):
            result = review_opportunities({"country": "KR"}, [original], Client([review()]))
        self.assertEqual(original, before)
        self.assertEqual(result[0]["eligibility_status"], "needs_review")
        track = result[0]["tracks"][0]
        self.assertEqual(track["deadline"], "2026-12-01")
        self.assertEqual(track["stage"], "공고 확인")
        self.assertIn("page unavailable", track["review_error"])

    def test_watched_catalog_program_or_track_reads_its_original_page(self):
        for watch_on_group in (True, False):
            item = group()
            (item if watch_on_group else item["tracks"][0])["watch"] = True
            with patch("grantfinder.opportunity_review._page_text", return_value="Applicants must work at a university in Korea.") as fetch:
                result = review_opportunities({"country": "KR"}, [item], Client([review()]))
            fetch.assert_called_once_with("https://example.org/p1", None)
            self.assertEqual(result[0]["tracks"][0]["review_evidence_status"], "page_text")

    def test_watched_catalog_fetch_failure_labels_previous_snapshot(self):
        item = group()
        item["watch"] = True
        with patch("grantfinder.opportunity_review._page_text", side_effect=RuntimeError("source unavailable")):
            result = review_opportunities({"country": "KR"}, [item], Client([review()]))
        track = result[0]["tracks"][0]
        self.assertEqual(track["review_evidence_status"], "previous_snapshot")
        self.assertEqual(track["deadline"], item["tracks"][0]["deadline"])
        self.assertIn("source unavailable", track["review_error"])

    def test_pdf_page_markers_do_not_count_as_scanned_document_text(self):
        markers = "\n".join(f"[PDF page {page}]" for page in range(1, 20))
        with patch("grantfinder.opportunity_review.socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443))]), \
                patch("grantfinder.opportunity_review._download", return_value=(b"%PDF-scanned", "application/pdf", "https://example.org/call.pdf")), \
                patch("grantfinder.opportunity_review._pdf_text", return_value=markers):
            with self.assertRaisesRegex(ValueError, "OCR"):
                _page_text("https://example.org/call.pdf", None)
        with patch("grantfinder.opportunity_review._page_text", side_effect=ValueError("스캔 공고 OCR 필요")):
            result = review_opportunities({}, [group(origin="web")], Client([review()]))
        self.assertIn("OCR", result[0]["tracks"][0]["next_action"])

    def test_only_source_evidenced_irrelevant_web_results_are_filtered(self):
        quote = "Enroll in our introductory course and pay the tuition fee."
        reviews = [review("web", relevant=False, relevance_quote=quote),
                   review("pool", relevant=False, relevance_quote=quote),
                   review("unknown", relevant=False, relevance_quote="invented source")]
        with patch("grantfinder.opportunity_review._page_text", return_value=quote):
            result = review_opportunities({}, [group("web", "web"), group("pool", text=quote),
                                               group("unknown", "web")], Client(reviews))
        self.assertEqual([item["id"] for item in result], ["pool", "unknown"])

    def test_one_failed_track_does_not_exclude_an_entire_program(self):
        item = group()
        other = copy.deepcopy(item["tracks"][0])
        other["id"] = "second"
        item["tracks"].append(other)
        result = review_opportunities({"country": "JP"}, [item], Client([review()]))
        self.assertEqual(result[0]["tracks"][0]["eligibility_status"], "ineligible")
        self.assertEqual(result[0]["tracks"][1]["eligibility_status"], "needs_review")
        self.assertEqual(result[0]["eligibility_status"], "needs_review")

    def test_work_is_bounded_and_does_not_send_profile_identity(self):
        profile = {"name": "Private Researcher", "email": "private@example.org", "country": "KR"}
        client = Client([review("p" + str(index)) for index in range(30)])
        with patch("grantfinder.opportunity_review._page_text", return_value="Public opportunity page with a faculty grant and conditions.") as fetch:
            result = review_opportunities(profile, [group("p" + str(index), "web") for index in range(30)], client)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(fetch.call_count, 8)
        self.assertEqual(len(result), 30)
        self.assertEqual(result[-1]["review_status"], "pending")
        self.assertNotIn("Private Researcher", json.dumps(client.calls))
        self.assertNotIn("private@example.org", json.dumps(client.calls))

    def test_local_model_failure_retains_candidates_with_error(self):
        result = review_opportunities({}, [group()], Client(error=RuntimeError("Ollama unavailable")))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["review_status"], "error")
        self.assertEqual(result[0]["eligibility_status"], "needs_review")
        self.assertIn("Ollama unavailable", result[0]["review_error"])


if __name__ == "__main__":
    unittest.main()
