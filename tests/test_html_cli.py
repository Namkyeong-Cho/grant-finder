"""The HTML export must not silently recollect, infer, or change analysis history."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder import cli
from grantfinder.storage import write_json


class SavedHtmlReportTests(unittest.TestCase):
    def prepare(self, root):
        output = root / "outputs"
        summary = {"mode": "local_model", "as_of": "2026-11-06T12:00:00+09:00",
                   "created_at": "2026-11-06T12:10:00+09:00", "model_calls": 2,
                   "cache_hits": 4, "changes": [], "collection_status": []}
        values = {"notices": [], "evaluations": [], "profile_used": {"name": "가상 교수", "is_demo": True},
                  "run_summary": summary}
        for name, value in values.items():
            write_json(output / (name + ".json"), value)
        write_json(root / "state/history/local_model.json", [{"id": "unchanged"}])
        return {path: path.read_bytes() for path in root.rglob("*.json")}

    def test_saved_report_preserves_analysis_and_does_not_call_a_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = self.prepare(root)
            with patch.object(cli, "ROOT", root), patch.object(cli, "OllamaClient") as client:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["report"]), 0)
                client.assert_not_called()
            self.assertTrue((root / "outputs/latest_report.html").is_file())
            self.assertTrue((root / "outputs/latest_report.md").is_file())
            for path, content in previous.items():
                self.assertEqual(path.read_bytes(), content)

    def test_open_uses_the_generated_local_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            with patch.object(cli, "ROOT", root), patch("webbrowser.open", return_value=True) as opener:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["report", "--open"]), 0)
                opener.assert_called_once_with((root / "outputs/latest_report.html").resolve().as_uri())

    def test_missing_analysis_requires_a_previous_demo_or_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(cli, "ROOT", Path(temporary)):
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(cli.main(["report"]), 1)
                    self.assertIn("demo", error.getvalue())


if __name__ == "__main__":
    unittest.main()
