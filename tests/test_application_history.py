"""Human-maintained application records stay separate from analysis and progress."""

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder import cli
from grantfinder.application_history import load_history, render_history_html, history_markdown
from grantfinder.storage import write_json


def entry(**changes):
    value = {"id": "test-application-2026", "profile_id": "test-professor",
             "candidate_id": None, "program": "합성 검사용 연구지원",
             "round": "2026년 하반기", "status": "submitted", "submitted_on": "2026-11-07",
             "decided_on": None, "notes": "실제 지원 이력이 아닌 테스트 자료"}
    value.update(changes)
    return value


class ApplicationHistoryTests(unittest.TestCase):
    def test_missing_empty_and_template_do_not_create_real_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "application_history.json"
            self.assertEqual(load_history(path, "test-professor"), [])
            write_json(path, {"schema_version": 1, "entries": [], "entry_template": entry()})
            self.assertEqual(load_history(path, "test-professor"), [])
            write_json(path, {"entries": [entry()]})
            for identifier in (None, ""):
                with self.subTest(profile_id=identifier):
                    self.assertEqual(load_history(path, identifier), [])

    def test_profiles_are_exact_and_repeated_programs_keep_distinct_rounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "application_history.json"
            rows = [entry(candidate_id="shared-program"),
                    entry(id="test-application-2027", round="2027년 상반기", candidate_id="shared-program"),
                    entry(profile_id="another-professor"),
                    entry(id="case-mismatch", profile_id="Test-professor")]
            write_json(path, {"entries": rows})
            result = load_history(path, "test-professor")
            self.assertEqual({row["id"] for row in result},
                             {"test-application-2026", "test-application-2027"})
            self.assertEqual(len(result), 2)

    def test_duplicate_ids_within_one_profile_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "application_history.json"
            write_json(path, {"entries": [entry(), entry(round="2027년 상반기")]})
            with self.assertRaises(ValueError):
                load_history(path, "test-professor")

    def test_statuses_and_calendar_dates_are_validated_without_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "application_history.json"
            for status in ("preparing", "submitted", "selected", "rejected", "withdrawn", "unknown"):
                with self.subTest(status=status):
                    write_json(path, {"entries": [entry(status=status, submitted_on=None,
                                                       decided_on="2026-11-07")]})
                    result = load_history(path, "test-professor")
                    self.assertEqual(result[0]["status"], status)
                    self.assertIsNone(result[0]["submitted_on"])
            invalid = [{"status": "invented-result"}]
            for field in ("submitted_on", "decided_on"):
                invalid.extend({field: value} for value in
                               ("2026-02-29", "2026-11", "20261107", "2026-11-07T12:00:00", 20261107))
            for changes in invalid:
                with self.subTest(changes=changes):
                    write_json(path, {"entries": [entry(**changes)]})
                    with self.assertRaises(ValueError):
                        load_history(path, "test-professor")

    def test_html_and_markdown_escape_human_input(self):
        payload = "<script>alert('x')</script> | [click](javascript:alert(1))"
        rows = [entry(program=payload, notes=payload + "\n다음 줄")]
        original = copy.deepcopy(rows)
        html = render_history_html(rows)
        markdown = history_markdown(rows)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("2026년 하반기", html)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[click](javascript:", markdown)
        self.assertIn("\\|", markdown)
        self.assertEqual(rows, original)

    def test_report_reads_history_without_changing_history_or_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "outputs"
            values = {"notices": [], "evaluations": [],
                      "profile_used": {"id": "test-professor", "name": "가상 교수", "is_demo": False},
                      "run_summary": {"mode": "local_model", "as_of": "2026-11-07T12:00:00+09:00",
                                      "created_at": "2026-11-07T12:10:00+09:00",
                                      "model_calls": 0, "cache_hits": 0, "changes": [],
                                      "collection_status": []}}
            for name, value in values.items():
                write_json(output / (name + ".json"), value)
            write_json(root / "data/application_history.json", {"entries": [entry()]})
            write_json(root / "data/progress.json", {"completed_tasks": [], "applications": {}})
            write_json(root / "state/history/local_model.json", [{"id": "unchanged"}])
            previous = {path: path.read_bytes() for path in root.rglob("*.json")}
            with patch.object(cli, "ROOT", root), patch.object(cli, "OllamaClient") as model:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["report"]), 0)
                model.assert_not_called()
            for path, data in previous.items():
                self.assertEqual(path.read_bytes(), data)
            for suffix in ("html", "md"):
                self.assertIn("합성 검사용 연구지원", (output / ("latest_report." + suffix)).read_text())


if __name__ == "__main__":
    unittest.main()
