"""Domain and evidence checks for the standalone HTML report.

Fixtures below are synthetic and contain no actual researcher information.
"""

import copy
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grantfinder.html_report import render_html_report
from grantfinder import cli


AS_OF = datetime(2026, 11, 6, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.visible = []
        self.sections = {}
        self._sections = []
        self._hidden_depth = 0
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "section":
            self._sections.append(attributes.get("id", ""))
        if tag in ("script", "style"):
            self._hidden_depth += 1

    def handle_endtag(self, tag):
        if tag == "section" and self._sections:
            self._sections.pop()
        if tag in ("script", "style"):
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data):
        if not self._hidden_depth:
            self.visible.append(data)
            for section in self._sections:
                self.sections.setdefault(section, []).append(data)

    @property
    def text(self):
        return " ".join(self.visible)

    @property
    def normalized_dates(self):
        return re.sub(r"\b(\d{4})[.](\d{2})[.](\d{2})\b", r"\1-\2-\3", self.text)


def fixture():
    notice = {
        "id": "synthetic", "title": "검증용 가상 연구지원", "sponsor": "가상 지원기관",
        "year": 2027, "track": "검증 유형", "source_url": "https://example.org/grant",
        "evidence_status": "official_snapshot", "application_mode": "window",
        "events": [
            {"kind": "researcher_deadline", "label": "연구자 제출 마감",
             "value": "2026-12-07T18:00:00+09:00", "precision": "datetime",
             "timezone": "Asia/Seoul", "status": "announced",
             "evidence_quote": "연구자 제출은 2026-12-07 18:00까지입니다."},
            {"kind": "institution_deadline", "label": "기관승인 마감",
             "value": "2026-12-09T18:00:00+09:00", "precision": "datetime",
             "timezone": "Asia/Seoul", "status": "announced",
             "evidence_quote": "기관승인은 2026-12-09 18:00까지입니다."},
        ],
        "eligibility": [], "required_documents": ["연구계획서"], "keywords": ["수학"],
        "notes": [], "missing_information": [],
    }
    evaluation = {
        "notice_id": "synthetic", "status": "open", "eligibility": "needs_review",
        "topic_matches": ["수학"], "events": copy.deepcopy(notice["events"]),
        "conditions": [{"label": "경력 기준 확인", "status": "needs_review",
                        "reason": "가상 프로필에 임용일이 없습니다.", "evidence_quote": "경력 기준은 해당 요강을 확인합니다."}],
        "tasks": [
            {"id": "finished-task", "notice_id": "synthetic", "title": "완료된 확인 작업",
             "due": None, "kind": "verification", "reason": "이미 완료했습니다.",
             "source_url": "https://example.org/grant"},
            {"id": "pending-task", "notice_id": "synthetic", "title": "남은 계획서 준비 작업",
             "due": "2026-12-07T18:00:00+09:00", "kind": "preparation",
             "reason": "서류를 준비합니다.", "source_url": "https://example.org/grant"},
        ], "warnings": [],
    }
    profile = {"id": "synthetic-professor", "name": "가상 교수", "is_demo": True,
               "keywords": ["수학"], "internal_deadlines": {}}
    return notice, evaluation, profile


def render(notice=None, evaluation=None, profile=None, **kwargs):
    default_notice, default_evaluation, default_profile = fixture()
    return render_html_report(
        [notice or default_notice], [evaluation or default_evaluation], profile or default_profile,
        AS_OF, [], [{"source_id": "synthetic", "url": "https://example.org/grant", "status": "snapshot",
                     "fetched_at": "2026-11-06T10:00:00+09:00"}], **kwargs,
    )


class HtmlReportTests(unittest.TestCase):
    def test_researcher_and_institution_deadlines_stay_separate(self):
        document = Document(render())
        self.assertIn("2026-12-07 18:00", document.normalized_dates)
        self.assertIn("2026-12-09 18:00", document.normalized_dates)
        self.assertIn("연구자 제출", document.text)
        self.assertIn("기관승인", document.text)

    def test_unknown_and_month_precision_do_not_gain_an_exact_date(self):
        notice, evaluation, _ = fixture()
        notice["events"] = [
            {"kind": "researcher_deadline", "label": "연구자 제출 마감", "value": None,
             "precision": "unknown", "timezone": "Asia/Seoul", "status": "unknown", "evidence_quote": ""},
            {"kind": "result_expected", "label": "결과 발표 예정", "value": "2027년 6월 하순",
             "precision": "month", "timezone": "Asia/Seoul", "status": "planned",
             "evidence_quote": "결과는 2027년 6월 하순 안내 예정입니다."},
        ]
        evaluation.update(events=copy.deepcopy(notice["events"]), tasks=[], status="unknown")
        document = Document(render(notice, evaluation))
        self.assertIn("2027년 6월 하순", document.text)
        self.assertNotRegex(document.normalized_dates, r"2027-06-\d{2}")
        self.assertNotIn("None", document.text)
        self.assertNotIn("00:00", " ".join(document.sections.get("calendar", [])))

    def test_pacific_time_is_displayed_in_korean_time(self):
        notice, evaluation, _ = fixture()
        notice["events"] = [{
            "kind": "researcher_deadline", "label": "해외 공고 마감",
            "value": "2026-11-11T23:59:00-08:00", "precision": "datetime",
            "timezone": "America/Los_Angeles", "status": "announced",
            "evidence_quote": "November 11, 2026 at 11:59PM Pacific Time",
        }]
        evaluation.update(events=copy.deepcopy(notice["events"]), tasks=[])
        document = Document(render(notice, evaluation))
        self.assertIn("2026-11-12 16:59", document.normalized_dates)

    def test_completed_task_is_not_presented_as_unfinished(self):
        document = Document(render(progress={"completed_tasks": ["finished-task"], "applications": {}}))
        self.assertNotIn("완료된 확인 작업", document.text)
        self.assertIn("남은 계획서 준비 작업", document.text)

    def test_demo_profile_and_evidence_origin_remain_visible(self):
        notice, evaluation, profile = fixture()
        other = copy.deepcopy(notice)
        other.update(id="live-notice", title="웹에서 수집한 검증 공고", evidence_status="live")
        other_evaluation = copy.deepcopy(evaluation)
        other_evaluation.update(notice_id="live-notice", tasks=[])
        markup = render_html_report([notice, other], [evaluation, other_evaluation], profile,
                                    AS_OF, [], [], summary={"mode": "demo", "model_calls": 0, "cache_hits": 0})
        document = Document(markup)
        self.assertIn("수업용 가상", document.text)
        self.assertTrue("데모" in document.text or "검토자료" in document.text)
        self.assertIn("저장본", document.text)
        self.assertIn("웹 수집", document.text)

    def test_untrusted_values_cannot_create_executable_markup(self):
        payload = '</script><img id="GF_XSS" src=x onerror="alert(1)"><script id="GF_XSS">alert(1)</script>'
        notice, evaluation, profile = fixture()
        notice.update(title=payload, sponsor=payload, source_url="javascript:alert(1)",
                      notes=[payload], required_documents=[payload])
        notice["events"][0]["evidence_quote"] = payload
        profile["name"] = payload
        evaluation["conditions"][0].update(label=payload, reason=payload, evidence_quote=payload)
        evaluation["tasks"][0].update(title=payload, source_url="javascript:alert(1)")
        markup = render(notice, evaluation, profile,
                        seasons={"basis": payload, "items": [{"program": payload, "timing": payload,
                                 "evidence_type": payload, "action": payload, "source_url": "javascript:alert(1)"}]},
                        discovery={"checked_at": "2026-11-06", "candidates": [
                            {"title": payload, "url": "javascript:alert(1)", "source_id": "synthetic"}]})
        document = Document(markup)
        for tag, attrs in document.tags:
            self.assertNotEqual(attrs.get("id"), "GF_XSS")
            for key, value in attrs.items():
                if key.startswith("on"):
                    self.assertNotIn("GF_XSS", value or "")
                if key in ("href", "src") and value:
                    self.assertFalse(re.match(r"\s*(?:javascript|vbscript|data):", value, re.I), (tag, key, value))

    def test_report_loads_without_external_scripts_or_stylesheets(self):
        document = Document(render())
        for tag, attrs in document.tags:
            if tag == "script":
                self.assertFalse(attrs.get("src"))
            if tag == "link" and "stylesheet" in attrs.get("rel", ""):
                self.assertFalse(attrs.get("href", "").startswith(("http://", "https://", "//")))

    def test_rendering_does_not_mutate_analysis_or_progress(self):
        notice, evaluation, profile = fixture()
        progress = {"completed_tasks": ["finished-task"], "applications": {"synthetic": "submitted"}}
        before = copy.deepcopy((notice, evaluation, profile, progress))
        render(notice, evaluation, profile, progress=progress)
        self.assertEqual((notice, evaluation, profile, progress), before)


class HtmlExportCommandTests(unittest.TestCase):
    def test_existing_analysis_export_does_not_call_model_web_or_change_json(self):
        notice, evaluation, profile = fixture()
        summary = {"mode": "local_model", "as_of": AS_OF.isoformat(),
                   "created_at": "2026-11-06T12:01:00+09:00", "notices": 1,
                   "collection_status": [], "changes": [], "errors": [],
                   "model_calls": 1, "cache_hits": 0, "profile_is_demo": True}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "outputs"
            output.mkdir()
            records = {"notices.json": [notice], "evaluations.json": [evaluation],
                       "profile_used.json": profile, "run_summary.json": summary}
            for filename, value in records.items():
                (output / filename).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            before = {filename: (output / filename).read_bytes() for filename in records}
            with (
                patch.object(cli, "ROOT", root), patch.object(cli, "OllamaClient") as model,
                patch("grantfinder.collectors.collect_source") as collect,
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(cli.main(["report"]), 0)
                model.assert_not_called()
                collect.assert_not_called()
            for filename in records:
                self.assertEqual((output / filename).read_bytes(), before[filename])
            self.assertTrue((output / "latest_report.html").is_file())
            self.assertTrue((output / "latest_report.md").is_file())
            self.assertFalse((root / "state").exists())


if __name__ == "__main__":
    unittest.main()
