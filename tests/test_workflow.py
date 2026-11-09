"""Regression cases for deadline, evidence, local inference, and progress handling.

All notice/profile fixtures below are synthetic test data.
"""

import copy
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grantfinder.llm import OllamaClient, validate_local_host
from grantfinder.agent import answer_question
from grantfinder import cli
from grantfinder.evaluate import evaluate_notice
from grantfinder.report import render_report
from grantfinder.schema import crosscheck_reviewed, unpack_response, validate_extraction
from grantfinder.storage import changes_since


KST = ZoneInfo("Asia/Seoul")


def event(kind, value, *, precision="datetime", timezone="Asia/Seoul", status="announced"):
    return {
        "kind": kind,
        "value": value,
        "precision": precision,
        "timezone": timezone,
        "status": status,
        "label": kind,
        "evidence_quote": "단위 검증용 가상 공고의 일정입니다.",
    }


def notice():
    return {
        "id": "synthetic-grant",
        "source_id": "synthetic-source",
        "title": "단위 검증용 가상 연구지원",
        "sponsor": "가상 재단",
        "year": 2027,
        "track": "test",
        "source_url": "https://example.org/synthetic-notice",
        "evidence_status": "official_snapshot",
        "application_mode": "window",
        "events": [
            event("application_start", "2026-11-25T14:00:00+09:00"),
            event("researcher_deadline", "2026-12-07T18:00:00+09:00"),
            event("institution_deadline", "2026-12-09T18:00:00+09:00"),
        ],
        "eligibility": [{
            "field": "employment",
            "operator": "eq",
            "value": "full_time",
            "label": "전임교원",
            "evidence_quote": "지원대상은 전임교원입니다.",
        }],
        "required_documents": ["CV"],
        "keywords": ["수학"],
        "notes": ["단위 검증용 가상 자료입니다."],
        "missing_information": [],
    }


def profile():
    return {
        "id": "synthetic-professor",
        "name": "가상 교수",
        "is_demo": True,
        "affiliation_type": "university",
        "employment": "full_time",
        "nationality": None,
        "phd_date": None,
        "first_faculty_date": None,
        "birth_date": None,
        "keywords": ["수학"],
        "active_projects": None,
        "documents": {"cv": False},
        "internal_deadlines": {},
    }


class LocalInferenceTests(unittest.TestCase):
    def test_accepts_only_explicit_loopback_http_addresses(self):
        for host in ["http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"]:
            with self.subTest(host=host):
                self.assertEqual(validate_local_host(host), host.rstrip("/"))

    def test_external_hosts_and_ambiguous_local_urls_are_rejected(self):
        invalid = [
            "https://localhost:11434",
            "http://ollama.com",
            "http://192.168.0.2:11434",
            "http://127.0.0.1.example.org:11434",
            "http://user@localhost:11434",
            "http://localhost:11434/api",
            "http://localhost:11434/?target=remote",
            "http://localhost:11434/#remote",
        ]
        for host in invalid:
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_local_host(host)

    def test_cloud_model_name_is_rejected_before_request(self):
        client = OllamaClient(model="synthetic:cloud")
        with patch.object(client, "request") as request:
            with self.assertRaises(ValueError):
                client.ensure_local_model()
            request.assert_not_called()

    def test_installed_remote_model_metadata_is_rejected(self):
        client = OllamaClient(model="synthetic:7b")
        with patch.object(client, "request", side_effect=[
            {"models": [{"name": "synthetic:7b"}]},
            {"remote_host": "https://remote.example.org"},
        ]):
            with self.assertRaises(ValueError):
                client.ensure_local_model()


class EvidenceTests(unittest.TestCase):
    def test_invented_midnight_is_removed_from_a_date_only_source(self):
        quote = "세부 신청요강은 2026.11.16.(월) 공지 예정입니다."
        for precision in ("date", "datetime"):
            with self.subTest(precision=precision):
                raw = {"guidelines_release": {
                    "label": "공개 예정", "value": "2026-11-16T00:00:00+09:00",
                    "precision": precision, "timezone": "Asia/Seoul", "evidence_quote": quote,
                }}
                result = validate_extraction(unpack_response(raw), quote, notice())
                self.assertEqual(result["events"][0]["value"], "2026-11-16")
                self.assertEqual(result["events"][0]["precision"], "date")

    def test_posting_date_is_not_a_guidelines_release_date(self):
        quote = "작성일 2026.03.12"
        raw = {"guidelines_release": {
            "label": "서식 공개", "value": "2026-03-12", "precision": "date",
            "timezone": "Asia/Seoul", "evidence_quote": quote,
        }}
        result = validate_extraction(unpack_response(raw), quote, notice())
        self.assertIsNone(result["events"][0]["value"])

    def test_plain_date_in_datetime_field_keeps_the_source_date_precision(self):
        quote = "세부 신청요강은 2026.11.16. 공개 예정입니다."
        raw = {"guidelines_release": {
            "label": "guidelines_release", "value": "2026.11.16.",
            "precision": "datetime", "timezone": "Asia/Seoul", "evidence_quote": quote,
        }}
        result = validate_extraction(unpack_response(raw), quote, notice())
        value = result["events"][0]
        self.assertEqual(value["kind"], "guidelines_release")
        self.assertEqual(value["value"], "2026-11-16")
        self.assertEqual(value["precision"], "date")
        self.assertEqual(value["status"], "planned")

    def test_explicit_deadline_and_opening_wording_corrects_misassigned_fields(self):
        deadline_quote = "The deadline for submissions is November 11, 2026 at 11:59PM Pacific Time."
        opening_quote = "Starting October 1, 2026, we will open the call."
        deadline = {
            "label": "application_start", "value": "2026-11-11T23:59:00-08:00",
            "precision": "datetime", "timezone": "America/Los_Angeles", "evidence_quote": deadline_quote,
        }
        opening = {
            "label": "institution_deadline", "value": "2026-10-01",
            "precision": "date", "timezone": "America/Los_Angeles", "evidence_quote": opening_quote,
        }
        raw = {"application_start": deadline, "researcher_deadline": copy.deepcopy(deadline),
               "institution_deadline": opening}
        result = validate_extraction(unpack_response(raw), opening_quote + "\n" + deadline_quote, notice())
        self.assertEqual(len(result["events"]), 2)
        by_kind = {value["kind"]: value for value in result["events"]}
        self.assertEqual(set(by_kind), {"researcher_deadline", "application_start"})
        self.assertEqual(by_kind["researcher_deadline"]["value"], "2026-11-11T23:59:00-08:00")
        self.assertEqual(by_kind["application_start"]["value"], "2026-10-01")

    def test_approximate_schedule_does_not_display_a_model_invented_day(self):
        cases = [
            ("relative", "We notify applicants approximately 3 months after the deadline."),
            ("month", "선정결과는 2026년 6월 하순 안내 예정입니다."),
            ("week", "최종 발표는 2026년 9월 2주 예정입니다."),
        ]
        for precision, quote in cases:
            with self.subTest(precision=precision):
                raw = {"result_expected": [{
                    "label": "심사결과", "value": "2027-02-11",
                    "precision": precision, "timezone": "Asia/Seoul", "evidence_quote": quote,
                }]}
                result = validate_extraction(unpack_response(raw), quote, notice())
                value = result["events"][0]
                self.assertEqual(value["value"], quote)
                self.assertEqual(value["precision"], precision)
                self.assertEqual(value["status"], "planned")

    def test_unverifiable_quote_does_not_become_a_deadline(self):
        raw = {"events": [event("researcher_deadline", "2026-12-07T18:00:00+09:00")]}
        result = validate_extraction(raw, "이 원문에는 제출 마감이 없습니다.", notice())
        self.assertIsNone(result["events"][0]["value"])
        self.assertEqual(result["events"][0]["precision"], "unknown")
        self.assertTrue(result["missing_information"])

    def test_invalid_calendar_date_is_not_retained(self):
        raw = {"events": [event("researcher_deadline", "2026-02-30T18:00:00+09:00")]}
        result = validate_extraction(raw, "단위 검증용 가상 공고의 일정입니다.", notice())
        self.assertIsNone(result["events"][0]["value"])

    def test_unverified_eligibility_is_kept_for_manual_review(self):
        raw = {"eligibility": notice()["eligibility"]}
        result = validate_extraction(raw, "자격 정보가 없는 원문입니다.", notice())
        self.assertEqual(result["eligibility"][0]["operator"], "manual")

    def test_whitespace_normalization_preserves_a_real_quote(self):
        value = event("researcher_deadline", "2026-12-07T18:00:00+09:00")
        value["evidence_quote"] = "연구자 제출 마감: 2026-12-07 18:00"
        raw = {"events": [value]}
        result = validate_extraction(raw, "연구자 제출\n마감: 2026-12-07\n18:00", notice())
        self.assertEqual(result["events"][0]["value"], "2026-12-07T18:00:00+09:00")

    def test_existing_quote_does_not_validate_a_different_date_or_time(self):
        quote = "연구자 제출 마감: 2026-12-07 18:00"
        for wrong in ["2099-12-31T18:00:00+09:00", "2026-12-08T18:00:00+09:00", "2026-12-07T19:00:00+09:00"]:
            with self.subTest(value=wrong):
                value = event("researcher_deadline", wrong)
                value["evidence_quote"] = quote
                result = validate_extraction({"events": [value]}, quote, notice())
                self.assertIsNone(result["events"][0]["value"])
                self.assertEqual(result["events"][0]["status"], "unknown")

    def test_korean_date_without_time_retains_date_precision(self):
        quote = "연구자 제출 마감: 2026년 12월 7일"
        value = event("researcher_deadline", "2026-12-07", precision="date")
        value["evidence_quote"] = quote
        result = validate_extraction({"events": [value]}, quote, notice())
        self.assertEqual(result["events"][0]["value"], "2026-12-07")
        self.assertEqual(result["events"][0]["precision"], "date")

    def test_english_month_and_pm_are_compared_in_source_timezone(self):
        quote = "The deadline is November 11, 2026 at 11:59PM Pacific Time."
        value = event("researcher_deadline", "2026-11-11T23:59:00-08:00", timezone="America/Los_Angeles")
        value["evidence_quote"] = quote
        result = validate_extraction({"events": [value]}, quote, notice())
        self.assertEqual(result["events"][0]["value"], "2026-11-11T23:59:00-08:00")

    def test_short_year_date_notation_keeps_month_and_day(self):
        quote = "과제 접수 ~‘26.12.11(금) 10:00"
        value = event("researcher_deadline", "2026-12-11T10:00:00+09:00")
        value["evidence_quote"] = quote
        result = validate_extraction({"events": [value]}, quote, notice())
        self.assertEqual(result["events"][0]["value"], "2026-12-11T10:00:00+09:00")

    def test_omitted_year_requires_matching_year_elsewhere_in_source(self):
        quote = "연구자 제출 마감: 12월 7일 18:00"
        text = "2026년도 연구지원\n" + quote
        value = event("researcher_deadline", "2026-12-07T18:00:00+09:00")
        value["evidence_quote"] = quote
        result = validate_extraction({"events": [value]}, text, notice())
        self.assertEqual(result["events"][0]["value"], "2026-12-07T18:00:00+09:00")
        value["value"] = "2027-12-07T18:00:00+09:00"
        rejected = validate_extraction({"events": [value]}, text, notice())
        self.assertIsNone(rejected["events"][0]["value"])

    def test_review_disagreement_is_flagged_without_replacing_model_output(self):
        reference = notice()
        extracted = copy.deepcopy(reference)
        extracted["events"][1]["value"] = "2026-12-09T18:00:00+09:00"
        result = crosscheck_reviewed(extracted, reference)
        self.assertEqual(result["events"][1]["value"], "2026-12-09T18:00:00+09:00")
        self.assertTrue(result["events"][1]["source_conflict"])
        self.assertTrue(result["missing_information"])

    def test_equal_instants_in_different_timezones_are_not_a_conflict(self):
        reference = notice()
        extracted = copy.deepcopy(reference)
        extracted["events"][1]["value"] = "2026-12-07T09:00:00+00:00"
        result = crosscheck_reviewed(extracted, reference)
        self.assertFalse(result["events"][1].get("source_conflict", False))


class ChangeTrackingTests(unittest.TestCase):
    def test_runtime_metadata_does_not_trigger_a_false_notice_change(self):
        before = notice()
        after = copy.deepcopy(before)
        after["extraction_metadata"] = {"seconds": 10, "model": "synthetic"}
        self.assertEqual(changes_since([before], [after]), [])

    def test_failed_or_limited_collection_is_not_treated_as_cancellation(self):
        self.assertEqual(changes_since([notice()], []), [])

    def test_changed_deadline_is_reported(self):
        before = notice()
        after = copy.deepcopy(before)
        after["events"][1]["value"] = "2026-12-08T18:00:00+09:00"
        changes = changes_since([before], [after])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "updated")
        self.assertIn("events", changes[0]["fields"])


class DeadlineAndEligibilityTests(unittest.TestCase):
    def test_institution_deadline_does_not_extend_researcher_submission(self):
        result = evaluate_notice(notice(), profile(), datetime(2026, 12, 8, 12, tzinfo=KST))
        self.assertEqual(result["status"], "closed")
        self.assertFalse(any(task["kind"] == "preparation" for task in result["tasks"]))

    def test_future_application_start_is_upcoming(self):
        result = evaluate_notice(notice(), profile(), datetime(2026, 11, 6, 12, tzinfo=KST))
        self.assertEqual(result["status"], "upcoming")

    def test_pacific_deadline_is_compared_at_the_same_instant_in_korea(self):
        record = notice()
        record["events"] = [
            event("application_start", "2026-10-01", precision="date", timezone="America/Los_Angeles"),
            event("researcher_deadline", "2026-11-11T23:59:00", timezone="America/Los_Angeles"),
        ]
        before = evaluate_notice(record, profile(), datetime(2026, 11, 12, 16, 58, tzinfo=KST))
        after = evaluate_notice(record, profile(), datetime(2026, 11, 12, 17, 0, tzinfo=KST))
        self.assertEqual(before["status"], "open")
        self.assertEqual(after["status"], "closed")

    def test_institution_only_deadline_stays_unknown(self):
        record = notice()
        record["events"] = [record["events"][2]]
        result = evaluate_notice(record, profile(), datetime(2026, 12, 8, 12, tzinfo=KST))
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["warnings"])

    def test_month_precision_is_not_converted_to_an_exact_deadline(self):
        record = notice()
        record["events"][1] = event("researcher_deadline", "2026-12", precision="month")
        result = evaluate_notice(record, profile(), datetime(2026, 12, 8, 12, tzinfo=KST))
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(any(task["kind"] == "official" for task in result["tasks"]))
        self.assertEqual(result["events"][1]["value"], "2026-12")

    def test_date_only_deadline_has_no_invented_time(self):
        record = notice()
        record["events"][1] = event("researcher_deadline", "2026-12-07", precision="date")
        same_day = evaluate_notice(record, profile(), datetime(2026, 12, 7, 12, tzinfo=KST))
        next_day = evaluate_notice(record, profile(), datetime(2026, 12, 8, 0, tzinfo=KST))
        self.assertEqual(same_day["status"], "open")
        self.assertEqual(next_day["status"], "closed")
        self.assertEqual(same_day["events"][1]["value"], "2026-12-07")

    def test_unknown_personal_condition_is_not_eligible(self):
        record = notice()
        record["eligibility"].append({
            "field": "nationality", "operator": "eq", "value": "KR",
            "label": "대한민국 국적", "evidence_quote": "대한민국 국적자만 지원합니다.",
        })
        result = evaluate_notice(record, profile(), datetime(2026, 12, 1, 12, tzinfo=KST))
        self.assertEqual(result["eligibility"], "needs_review")
        self.assertEqual(result["conditions"][1]["status"], "needs_review")

    def test_explicitly_failed_condition_takes_precedence_over_unknown(self):
        record = notice()
        record["eligibility"].append({
            "field": "career", "operator": "manual", "value": None,
            "label": "경력과 예외", "evidence_quote": "경력 조건과 예외를 따릅니다.",
        })
        person = profile()
        person["employment"] = "part_time"
        result = evaluate_notice(record, person, datetime(2026, 12, 1, 12, tzinfo=KST))
        self.assertEqual(result["eligibility"], "ineligible")
        self.assertFalse(any(task["kind"] == "preparation" for task in result["tasks"]))

    def test_empty_eligibility_does_not_mean_no_restrictions(self):
        record = notice()
        record["eligibility"] = []
        result = evaluate_notice(record, profile(), datetime(2026, 12, 1, 12, tzinfo=KST))
        self.assertEqual(result["eligibility"], "needs_review")

    def test_evidence_disagreement_blocks_automatic_submission_deadline(self):
        reference = notice()
        extracted = copy.deepcopy(reference)
        extracted["events"][1]["value"] = "2026-12-09T18:00:00+09:00"
        crosscheck_reviewed(extracted, reference)
        result = evaluate_notice(extracted, profile(), datetime(2026, 12, 1, 12, tzinfo=KST))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["eligibility"], "needs_review")
        self.assertFalse(any(task["kind"] == "official" for task in result["tasks"]))

    def test_submitted_application_moves_to_institution_approval(self):
        record = notice()
        record["application_status"] = "submitted"
        result = evaluate_notice(record, profile(), datetime(2026, 12, 1, 12, tzinfo=KST))
        self.assertFalse(any(task["kind"] == "preparation" for task in result["tasks"]))
        self.assertTrue(any("기관" in task["title"] and "승인" in task["title"] for task in result["tasks"]))
        self.assertFalse(any(task["title"] == "연구자 제출 완료 확인" for task in result["tasks"]))


class ProgressReportTests(unittest.TestCase):
    def test_progress_only_report_does_not_claim_a_new_inference(self):
        record = notice()
        record["extraction_metadata"] = {
            "method": "ollama_local", "model": "synthetic-protocol-fixture",
            "cache_hit": False, "analyzed_at": "2026-11-06T12:00:00+09:00",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(cli, "ROOT", root), patch.object(cli, "load_progress", return_value={}), contextlib.redirect_stdout(io.StringIO()):
                summary = cli.make_report([record], profile(), datetime(2026, 11, 6, 12, tzinfo=KST),
                                          [], "local_model", progress_only=True)
            self.assertEqual(summary["model_calls"], 0)
            self.assertEqual(summary["cache_hits"], 0)
            self.assertTrue(summary["progress_only"])
            self.assertIn("모델 추론은 실행하지 않았습니다", (root / "outputs/latest_report.md").read_text())
            self.assertEqual(record["extraction_metadata"]["analyzed_at"], "2026-11-06T12:00:00+09:00")

    def test_completed_task_is_hidden_and_unfinished_task_remains(self):
        record = notice()
        result = evaluate_notice(record, profile(), datetime(2026, 12, 1, 12, tzinfo=KST))
        completed = {
            "id": "test-completed", "notice_id": record["id"],
            "title": "UNIQUE_COMPLETED_ACTION", "due": None,
            "kind": "verification", "reason": "수업용 확인",
        }
        pending = dict(completed, id="test-pending", title="UNIQUE_PENDING_ACTION")
        result["tasks"] = [completed, pending]
        text = render_report([record], [result], profile(), datetime(2026, 12, 1, 12, tzinfo=KST),
                             [], [], {"completed_tasks": [completed["id"]]})
        self.assertNotIn("UNIQUE_COMPLETED_ACTION", text)
        self.assertIn("UNIQUE_PENDING_ACTION", text)

    def test_reordering_documents_does_not_reassign_completed_task_ids(self):
        record = notice()
        record["required_documents"] = ["CV", "연구계획서"]
        reordered = copy.deepcopy(record)
        reordered["required_documents"].reverse()
        as_of = datetime(2026, 12, 1, 12, tzinfo=KST)
        first = evaluate_notice(record, profile(), as_of)
        second = evaluate_notice(reordered, profile(), as_of)
        first_ids = {task["title"]: task["id"] for task in first["tasks"]}
        second_ids = {task["title"]: task["id"] for task in second["tasks"]}
        self.assertEqual(first_ids, second_ids)

    def test_changed_submission_deadline_creates_a_new_official_task(self):
        record = notice()
        changed = copy.deepcopy(record)
        changed["events"][1]["value"] = "2026-12-08T18:00:00+09:00"
        as_of = datetime(2026, 12, 1, 12, tzinfo=KST)
        first = evaluate_notice(record, profile(), as_of)
        second = evaluate_notice(changed, profile(), as_of)
        first_task = next(task for task in first["tasks"] if task["title"] == "연구자 제출 완료 확인")
        second_task = next(task for task in second["tasks"] if task["title"] == "연구자 제출 완료 확인")
        self.assertNotEqual(first_task["id"], second_task["id"])


class ScriptedClient:
    """Protocol fixture; does not call a model or count as a model execution."""

    model = "synthetic-protocol-fixture"

    def __init__(self, responses):
        self.responses = responses
        self.index = 0

    def chat(self, *args, **kwargs):
        response = copy.deepcopy(self.responses[min(self.index, len(self.responses) - 1)])
        self.index += 1
        return response


class AgentEvidenceTests(unittest.TestCase):
    @staticmethod
    def tool_response(name, arguments):
        return {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": name, "arguments": arguments}},
        ]}}

    @staticmethod
    def final_response():
        return {"message": {"role": "assistant", "content": "단위 검증용 답변입니다."}}

    def query(self, client):
        return answer_question(client, "지원 마감을 알려주세요.", [notice()], [], profile(), {},
                               datetime(2026, 11, 6, 12, tzinfo=KST))

    def test_failed_notice_lookup_is_not_evidence_for_a_final_answer(self):
        client = ScriptedClient([
            self.tool_response("read_notice", {"notice_id": "does-not-exist"}),
            self.final_response(),
        ])
        with self.assertRaises(RuntimeError):
            self.query(client)

    def test_search_results_alone_do_not_support_deadline_answer(self):
        client = ScriptedClient([
            self.tool_response("search_notices", {"query": ""}),
            self.final_response(),
        ])
        with self.assertRaises(RuntimeError):
            self.query(client)

    def test_unknown_tool_does_not_count_as_evidence(self):
        client = ScriptedClient([
            self.tool_response("invented_tool", {}),
            self.final_response(),
        ])
        with self.assertRaises(RuntimeError):
            self.query(client)

    def test_successful_notice_read_allows_grounded_response(self):
        client = ScriptedClient([
            self.tool_response("read_notice", {"notice_id": notice()["id"]}),
            self.final_response(),
        ])
        result = self.query(client)
        self.assertEqual(result["notice_ids"], [notice()["id"]])
        self.assertEqual(result["tool_calls"][0]["tool"], "read_notice")


if __name__ == "__main__":
    unittest.main()
