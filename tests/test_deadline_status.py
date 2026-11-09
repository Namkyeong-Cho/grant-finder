"""Deadline precision, original timezones, and non-persistent plan metadata."""

import copy
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.deadline_status import application_deadline_statuses, classify_track_deadline
from grantfinder.server import create_server
from grantfinder.storage import write_json
from grantfinder.workspace import Workspace

NOW = datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("Asia/Seoul"))
ESTIMATE_NOW = datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Asia/Seoul"))


class DeadlineStatusTests(unittest.TestCase):
    def test_date_only_includes_its_entire_local_deadline_day(self):
        track = {"deadline": "2026-11-08", "deadline_timezone": "Asia/Seoul"}
        late = datetime(2026, 11, 8, 23, 59, 59, 999999, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(classify_track_deadline(track, late)["status"], "open")
        self.assertEqual(classify_track_deadline(track, late + timedelta(microseconds=1))["status"], "closed")

    def test_date_only_uses_the_source_timezone_not_seoul_calendar_date(self):
        track = {"deadline": "2026-11-07", "deadline_timezone": "America/Los_Angeles"}
        self.assertEqual(classify_track_deadline(track, NOW)["status"], "open")
        self.assertEqual(classify_track_deadline(track, NOW + timedelta(hours=6))["status"], "closed")

    def test_offset_datetimes_compare_exact_instants_including_boundary(self):
        deadline = "2026-11-08T03:00:00Z"
        track = {"deadline": deadline}
        self.assertEqual(classify_track_deadline(track, NOW - timedelta(microseconds=1))["status"], "open")
        self.assertEqual(classify_track_deadline(track, NOW)["status"], "open")
        self.assertEqual(classify_track_deadline(track, NOW + timedelta(microseconds=1))["status"], "closed")
        self.assertEqual(classify_track_deadline({"deadline": "2026-11-07T22:00:00-05:00"}, NOW)["status"], "open")
        self.assertEqual(classify_track_deadline(track, NOW)["deadline"], deadline)

    def test_dst_transition_uses_the_explicit_offset_without_guessing(self):
        now = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(classify_track_deadline({"deadline": "2026-11-01T01:30:00-04:00"}, now)["status"], "closed")
        self.assertEqual(classify_track_deadline({"deadline": "2026-11-01T01:30:00-05:00"}, now)["status"], "open")

    def test_missing_precision_timezone_and_invalid_dates_are_unconfirmed(self):
        cases = [{"deadline": "2026-11-08"}, {"deadline": "2026-11-08", "timezone": "Not/AZone"},
                 {"deadline": "2026-11-08T18:00:00", "timezone": "Asia/Seoul"},
                 {"deadline": "2026-11"}, {"deadline": "2026"}, {"deadline": "unknown"},
                 {"deadline": "2026-02-29", "timezone": "Asia/Seoul"},
                 {"deadline": "2026-13-01T00:00:00Z"}, {"deadline": "2026-11-08T25:00:00Z"},
                 {"deadline": "2026-11-08T12:00:00+09:60"}, {"deadline": 20261108},
                 {"deadline": {"value": "2026-11-08", "timezone": "Asia/Seoul", "precision": "unknown"}},
                 {"deadline": {"value": "2026-11-08T12:00:00Z", "precision": "date"}},
                 {"deadline": {"value": "2026-11-08", "timezone": "Asia/Seoul", "validation_error": "unverified"}}]
        for track in cases:
            with self.subTest(track=track):
                value = classify_track_deadline(track, NOW)
                self.assertEqual(value["status"], "unconfirmed")
                self.assertEqual(value["label"], "마감 미확인")

    def test_deadline_aliases_and_event_structures_preserve_values(self):
        examples = [
            {"researcher_deadline": "2026-11-09", "researcher_deadline_timezone": "Asia/Seoul"},
            {"application_deadline": {"date": "2026-11-09", "timezone": "Asia/Seoul"}},
            {"events": [{"kind": "institution_deadline", "value": "2000-01-01", "timezone": "Asia/Seoul"},
                        {"kind": "researcher_deadline", "value": "2026-11-09", "precision": "date", "timezone": "Asia/Seoul"}]},
            {"events": {"application_deadline": [{"value": None}, {"value": "2026-11-09", "timezone": "Asia/Seoul"}]}},
            {"events": [{"type": "deadline", "date": "2026-11-09"}], "timezone": "Asia/Seoul"},
        ]
        for track in examples:
            before = copy.deepcopy(track)
            with self.subTest(track=track):
                result = classify_track_deadline(track, NOW)
                self.assertEqual(result, {"status": "open", "label": "마감 전", "deadline": "2026-11-09"})
                self.assertEqual(track, before)
        self.assertEqual(classify_track_deadline({"events": [{"kind": "institution_deadline", "value": "2026-11-09"}]}, NOW)["status"], "unconfirmed")

    def test_explicit_rolling_timing_needs_no_invented_deadline(self):
        for timing in ("상시 모집", "수시 접수", "연중 신청", "접수 상시", "Rolling applications", "Applications accepted on a rolling basis"):
            with self.subTest(timing=timing):
                result = classify_track_deadline({"application_timing": timing}, NOW)
                self.assertEqual(result, {"status": "open", "label": "상시 모집", "deadline": None})
        for track in ({"application_mode": "continuous"}, {"stage": "open"}, {"application_timing": "상시 모집 여부 확인 필요"},
                      {"application_timing": "상시 모집이 아닙니다"}, {"application_timing": "Rolling applications suspended"}):
            with self.subTest(track=track):
                self.assertEqual(classify_track_deadline(track, NOW)["status"], "unconfirmed")

    def test_known_past_or_invalid_deadline_is_not_overridden_by_rolling_text(self):
        past = {"deadline": "2020-01-01", "timezone": "Asia/Seoul", "application_timing": "상시 모집"}
        self.assertEqual(classify_track_deadline(past, NOW)["status"], "closed")
        past["deadline"] = "2026-02-30"
        self.assertEqual(classify_track_deadline(past, NOW)["status"], "unconfirmed")

    def test_default_and_naive_now_use_seoul_not_the_host_timezone(self):
        track = {"deadline": "2026-11-08T03:00:00Z"}
        with patch("grantfinder.deadline_status._now", return_value=NOW):
            self.assertEqual(classify_track_deadline(track)["status"], "open")
        self.assertEqual(classify_track_deadline(track, NOW.replace(tzinfo=None))["status"], "open")

    def test_aggregation_uses_only_selected_tracks_and_keeps_unknown_separate(self):
        closed = {"id": "closed", "deadline": "2020-01-01", "timezone": "Asia/Seoul"}
        opened = {"id": "open", "deadline": "2030-01-01", "timezone": "Asia/Seoul"}
        unknown = {"id": "unknown", "deadline": None}
        apps = [
            {"id": "selected_closed", "tracks": [closed, opened], "selected_track_ids": ["closed"]},
            {"id": "any_open", "tracks": [closed, unknown, opened]},
            {"id": "unknown_mix", "tracks": [closed, unknown]},
            {"id": "none", "tracks": []},
        ]
        before = copy.deepcopy(apps)
        metadata = application_deadline_statuses(apps, NOW)
        self.assertEqual([metadata[item["id"]]["status"] for item in apps], ["closed", "open", "unconfirmed", "unconfirmed"])
        self.assertEqual(set(metadata["selected_closed"]["tracks"]), {"closed"})
        self.assertEqual(apps, before)

    def test_workspace_get_adds_only_runtime_metadata_and_never_changes_the_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = Workspace(root)
            state = workspace.upload_cv("test.json", b'{"name":"Temporary Researcher"}')
            identifier = state["profile"]["id"]
            path = root / "state/workspaces" / f"{identifier}.json"
            saved = json.loads(path.read_bytes())
            saved["applications"] = [{"id": "app_test", "program_id": "program_test", "status": "interested", "priority": "normal", "notes": "", "tasks": [],
                                      "selected_track_ids": ["test"], "tracks": [{"id": "test", "deadline": "2026-11-08", "deadline_timezone": "Asia/Seoul"}]}]
            write_json(path, saved)
            before = path.read_bytes()
            with patch("grantfinder.deadline_status._now", return_value=NOW):
                view = workspace.get()
            self.assertEqual(view["application_deadlines"]["app_test"]["status"], "open")
            self.assertEqual(view["applications"], saved["applications"])
            self.assertEqual(path.read_bytes(), before)
            workspace.update_application("app_test", {"notes": "Updated note only"})
            persisted = json.loads(path.read_bytes())
            self.assertNotIn("application_deadlines", persisted)
            self.assertEqual(persisted["applications"][0]["tracks"], saved["applications"][0]["tracks"])


class EstimatedDeadlineStatusTests(unittest.TestCase):
    def assert_estimated(self, track, period, kind, source="application_timing", now=ESTIMATE_NOW):
        before = copy.deepcopy(track)
        value = classify_track_deadline(track, now)
        self.assertEqual(value["status"], "open")
        self.assertEqual(value["label"], "마감 전")
        self.assertIs(value["estimated"], True)
        self.assertEqual(value["expected_period"], period)
        self.assertEqual(value["expected_kind"], kind)
        self.assertEqual(value["expected_source"], source)
        self.assertEqual(track, before)
        return value

    def test_direct_month_and_month_precision_preserve_the_original_deadline(self):
        examples = [
            {"deadline": "2027-01"},
            {"deadline": {"value": "2027-01", "precision": "month"}},
            {"researcher_deadline": "2027-01"},
            {"events": [{"kind": "researcher_deadline", "value": "2027-01", "precision": "month"}]},
        ]
        for track in examples:
            with self.subTest(track=track):
                value = self.assert_estimated(track, "2027-01", "deadline", "deadline")
                self.assertEqual(value["deadline"], "2027-01")

    def test_one_month_is_unconfirmed_but_two_calendar_months_are_open(self):
        for deadline in ("2026-10", "2026-11", "2026-12"):
            with self.subTest(deadline=deadline):
                self.assertEqual(classify_track_deadline({"deadline": deadline}, ESTIMATE_NOW)["status"], "unconfirmed")
        self.assert_estimated({"deadline": "2027-01"}, "2027-01", "deadline", "deadline")

    def test_month_end_and_year_rollover_use_calendar_months_rather_than_sixty_days(self):
        for now, accepted, rejected in (
            (datetime(2026, 11, 30, 23, 59, tzinfo=ZoneInfo("Asia/Seoul")), "2027-01", "2026-12"),
            (datetime(2026, 12, 31, 23, 59, tzinfo=ZoneInfo("Asia/Seoul")), "2027-02", "2027-01"),
            (datetime(2027, 1, 31, 23, 59, tzinfo=ZoneInfo("Asia/Seoul")), "2027-03", "2027-02"),
        ):
            with self.subTest(now=now):
                self.assert_estimated({"deadline": accepted}, accepted, "deadline", "deadline", now)
                self.assertEqual(classify_track_deadline({"deadline": rejected}, now)["status"], "unconfirmed")

    def test_year_only_deadline_keeps_year_precision_without_inventing_a_month(self):
        for record in ("2027", {"value": "2027", "precision": "year"}):
            with self.subTest(record=record):
                value = self.assert_estimated({"deadline": record}, "2027", "deadline", "deadline")
                self.assertEqual(value["deadline"], "2027")

    def test_real_infrastructure_plan_uses_application_month_not_announcement_or_start(self):
        track = {"deadline": None, "deadline_timezone": "Asia/Seoul",
                 "application_timing": "2027년3월 별도공고,4월접수,9월개시 예정. 일자·시각은 아직 미공고."}
        value = self.assert_estimated(track, "2027-04", "application")
        self.assertIsNone(value["deadline"])

    def test_real_matching_plan_inherits_year_for_application_end_month(self):
        track = {"deadline": None, "application_timing": "2027년1월공고,3월까지 접수,12월1일 개시 예정. 확정 마감일은 미공고."}
        self.assert_estimated(track, "2027-03", "deadline")

    def test_royal_round_uses_explicit_end_month_even_when_opening_is_earlier(self):
        track = {"application_timing": "공식 신청 일정은 2027 Round 1 개시 2026년 12월, 종료 2027년 2월 예정. 정확한 일자는 아직 확정하지 않았습니다."}
        self.assert_estimated(track, "2027-02", "deadline")

    def test_scheduled_announcement_or_application_month_needs_no_exact_day(self):
        for text, period, kind in (
            ("2027년 3월 모집공고 예정", "2027-03", "announcement"),
            ("2027년 3월 접수 예정", "2027-03", "application"),
            ("2027년 3~4월 모집 예정", "2027-03", "application"),
            ("2027년3월~4월 공고 예정", "2027-03", "announcement"),
        ):
            with self.subTest(text=text):
                self.assert_estimated({"application_timing": text}, period, kind)

    def test_month_range_uses_its_first_month_not_its_later_end(self):
        self.assert_estimated({"application_timing": "2027년 1~3월 접수 예정"}, "2027-01", "application")
        for text in ("2026년 12월~2027년 2월 접수 예정", "2026년12월~2027년1월 모집공고 예정"):
            with self.subTest(text=text):
                self.assertEqual(classify_track_deadline({"application_timing": text}, ESTIMATE_NOW)["status"], "unconfirmed")

    def test_official_recurring_announcement_uses_next_upcoming_range_start(self):
        timing = "공식 상시 사업안내는 매년 3~4월경 공고라고 명시합니다. 2026 개별 공고는 04-21~05-20 17:00 접수, 06-29 선정 공지였습니다. 차기 접수일은 미확정입니다."
        self.assert_estimated({"application_timing": timing}, "2027-03", "announcement")
        near = datetime(2027, 2, 1, 12, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(classify_track_deadline({"application_timing": timing}, near)["status"], "unconfirmed")

    def test_explicit_annual_call_is_a_schedule_without_requiring_the_word_official(self):
        track = {"application_timing": "매년 4월 공모"}
        before = copy.deepcopy(track)
        value = classify_track_deadline(track, ESTIMATE_NOW)
        self.assertEqual(value["status"], "open")
        self.assertIs(value["estimated"], True)
        self.assertEqual(value["expected_period"], "2027-04")
        self.assertEqual(value["expected_kind"], "announcement")
        self.assertEqual(value["expected_source"], "application_timing")
        self.assertEqual(track, before)

    def test_unrelated_future_events_and_unannounced_calls_are_not_deadlines(self):
        cases = (
            "2027년 2월 결과 발표 예정. 다음 접수 일정은 미발표입니다.",
            "2027년 1월 연구 시작 예정. 다음 공모 일정은 미발표입니다.",
            "2026 공모는 06-22~08-23 접수 종료. 2027년 1월 연구 시작 예정. 다음 공모 미발표.",
            "프로그램 2027-09-13~12-17 확정. 지원 마감·결과일은 미확인.",
            "연구기간 2027년3월~2028년2월. 신청 일정은 미발표.",
            "2026 회차는 3월 공고, 3월30일 초록 마감. 차기 미발표이며 과거 3월 이력으로 감시.",
            "최근 확인 공모는 2025년11월 접수. 2027년3월 결과 예정. 차기 접수일 미확인.",
            "2027 신규공고 및 접수일 미발표 또는 미확인.",
            "교수 프로그램은 일정 미확인. 별도 학생 fellowship은 2027년1분기 발표 예정.",
            "2027년1차 공고가 발표되어 핵심연구12월7일, 신진 등12월8일 연구자마감 예정.",
        )
        for timing in cases:
            before = {"application_timing": timing}
            track = copy.deepcopy(before)
            with self.subTest(timing=timing):
                self.assertEqual(classify_track_deadline(track, ESTIMATE_NOW)["status"], "unconfirmed")
                self.assertEqual(track, before)

    def test_explicit_deadline_remains_authoritative_over_future_timing(self):
        for record, expected in (
            ({"deadline": "2026-10-01", "deadline_timezone": "Asia/Seoul"}, "closed"),
            ({"deadline": "2026-10-01T18:00:00+09:00"}, "closed"),
            ({"deadline": "2026-11-10", "deadline_timezone": "Asia/Seoul"}, "open"),
            ({"deadline": "2027-03-10T18:00:00+09:00"}, "open"),
        ):
            track = dict(record, application_timing="2028년 3월 추가 공모 예정")
            with self.subTest(track=track):
                self.assertEqual(classify_track_deadline(track, ESTIMATE_NOW), {
                    "status": expected, "label": "마감 전" if expected == "open" else "마감됨",
                    "deadline": record["deadline"]})

    def test_far_future_dates_without_timezone_can_be_estimated_without_rewriting_values(self):
        for deadline in ("2027-01-05", "2027-01-05T18:00:00"):
            with self.subTest(deadline=deadline):
                value = self.assert_estimated({"deadline": deadline}, "2027-01", "deadline", "deadline")
                self.assertEqual(value["deadline"], deadline)
        self.assertEqual(classify_track_deadline({"deadline": "2026-12-31"}, ESTIMATE_NOW)["status"], "unconfirmed")

    def test_invalid_or_unverified_dates_never_promote_using_future_timing(self):
        records = (
            "2027-02-30", "2027-13", "2027-01-05T25:00:00", "2027-01-05T18:00:00+09:60",
            {"value": "2027-01", "precision": "date"},
            {"value": "2027-01-05", "precision": "datetime"},
            {"value": "2027-01-05T18:00:00Z", "precision": "date"},
            {"value": "2027-01", "precision": "month", "validation_error": "not verified"},
            {"value": "2027-01", "precision": "month", "status": "unknown"},
        )
        for record in records:
            track = {"deadline": record, "application_timing": "2027년 3월 모집 예정"}
            before = copy.deepcopy(track)
            with self.subTest(record=record):
                self.assertEqual(classify_track_deadline(track, ESTIMATE_NOW)["status"], "unconfirmed")
                self.assertEqual(track, before)

    def test_estimated_track_aggregation_obeys_selected_tracks_without_changing_inputs(self):
        apps = [{"id": "estimated", "selected_track_ids": ["month"], "tracks": [
            {"id": "month", "deadline": "2027-01"},
            {"id": "past", "deadline": "2020-01-01", "timezone": "Asia/Seoul"}]},
            {"id": "unknown", "tracks": [{"id": "results", "application_timing": "2027년 3월 결과 발표 예정"}]}]
        before = copy.deepcopy(apps)
        metadata = application_deadline_statuses(apps, ESTIMATE_NOW)
        self.assertEqual(metadata["estimated"]["status"], "open")
        self.assertEqual(set(metadata["estimated"]["tracks"]), {"month"})
        self.assertIs(metadata["estimated"]["tracks"]["month"]["estimated"], True)
        self.assertEqual(metadata["unknown"]["status"], "unconfirmed")
        self.assertEqual(apps, before)

    def test_get_api_adds_estimates_only_at_runtime_and_preserves_every_saved_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = Workspace(root)
            state = workspace.upload_cv("test.json", b'{"name":"Temporary Researcher"}')
            path = root / "state/workspaces" / f'{state["profile"]["id"]}.json'
            saved = json.loads(path.read_bytes())
            saved["applications"] = [{"id": "app_estimated", "program_id": "program_test", "status": "interested",
                "priority": "normal", "notes": "", "tasks": [], "selected_track_ids": ["month"],
                "tracks": [{"id": "month", "deadline": "2027-01"}]}]
            write_json(path, saved)
            before = {file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()}
            server = create_server(root, port=0, workspace=workspace)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with patch("grantfinder.deadline_status._now", return_value=ESTIMATE_NOW):
                    with opener.open(f"http://127.0.0.1:{server.server_port}/api/workspace", timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        view = json.load(response)
                estimate = view["application_deadlines"]["app_estimated"]["tracks"]["month"]
                self.assertIs(estimate["estimated"], True)
                self.assertEqual(estimate["expected_period"], "2027-01")
                self.assertEqual(view["applications"], saved["applications"])
                after = {file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()}
                self.assertEqual(after, before)
                self.assertNotIn("application_deadlines", json.loads(path.read_bytes()))
            finally:
                server.discovery_jobs.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


    def test_invalid_or_reversed_forecast_ranges_are_not_included(self):
        for timing in ("2027년3월~13월 접수 예정",
                       "2027년3월~2027년2월 접수 예정",
                       "2027년2월1일~2027년2월30일 접수 예정"):
            with self.subTest(timing=timing):
                self.assertEqual(classify_track_deadline(
                    {"application_timing": timing}, ESTIMATE_NOW)["status"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
