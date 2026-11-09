"""CV uploads keep user profiles separate and use only the local model client."""
import hashlib
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grantfinder.cv_profile import extract_cv_text, normalize_profile, parse_cv_profile


class LocalClient:
    model = "synthetic-local-model"

    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def chat(self, messages, schema=None):
        self.calls.append((messages, schema))
        return {"message": {"content": json.dumps(self.raw, ensure_ascii=False)}}


class CVProfileTests(unittest.TestCase):
    def test_readable_cv_with_no_extracted_identity_keeps_missing_values_and_source(self):
        raw = {"name": None, "keywords": [], "research_summary": None,
               "research_tracks": [], "position": None, "active_projects": [],
               "unknown_information": ["기관 내부 승인 여부"]}
        client = LocalClient(raw)
        payload = b"Office hours and contact information. Academic appointments are listed separately."
        profile = parse_cv_profile("contact-notes.txt", payload, client)
        for field in ("name", "research_summary", "position", "nationality", "phd_date"):
            self.assertIsNone(profile[field])
        for field in ("keywords", "research_tracks", "active_projects"):
            self.assertEqual(profile[field], [])
        self.assertEqual(profile["id"], "cv_" + hashlib.sha256(payload).hexdigest()[:16])
        self.assertEqual(profile["cv_import"], {"method": "ollama_local", "model": client.model,
                                                "filename": "contact-notes.txt"})
        self.assertTrue(profile["documents"]["cv"])
        self.assertIn("기관 내부 승인 여부", profile["unknown_information"])
        self.assertIn("박사학위 취득 시기", profile["unknown_information"])
        self.assertEqual(len(client.calls), 1)
        self.assertIn(payload.decode(), client.calls[0][0][1]["content"])

    def test_empty_profile_object_and_legacy_allow_empty_flag_keep_null_and_empty_lists(self):
        for options in ({}, {"allow_empty": False}, {"allow_empty": True}):
            with self.subTest(options=options):
                profile = normalize_profile({}, "test", **options)
                self.assertIsNone(profile["name"])
                self.assertIsNone(profile["research_summary"])
                self.assertIsNone(profile["position"])
                self.assertEqual(profile["keywords"], [])
                self.assertEqual(profile["research_tracks"], [])
                self.assertEqual(profile["active_projects"], [])
                self.assertIn("전임 여부", profile["unknown_information"])

    def test_profile_json_without_identity_does_not_call_model(self):
        payload = b'{"name":null,"keywords":[],"research_summary":null,"research_tracks":[]}'
        with patch("grantfinder.cv_profile.OllamaClient", side_effect=AssertionError("no model needed")):
            profile = parse_cv_profile("unfilled-profile.json", payload)
        self.assertIsNone(profile["name"])
        self.assertIsNone(profile["research_summary"])
        self.assertEqual(profile["keywords"], [])
        self.assertEqual(profile["research_tracks"], [])
        self.assertEqual(profile["cv_import"], {"method": "profile_json", "filename": "unfilled-profile.json"})
        self.assertEqual(profile["id"], "cv_" + hashlib.sha256(payload).hexdigest()[:16])

    def test_missing_identity_does_not_bypass_json_or_date_validation(self):
        for payload in (b'{"name":', b'[]', b'{"phd_date":"2020-13"}'):
            with self.subTest(payload=payload), \
                    patch("grantfinder.cv_profile.OllamaClient", side_effect=AssertionError("no model needed")):
                with self.assertRaises(ValueError):
                    parse_cv_profile("profile.json", payload)

    def test_text_upload_is_data_for_local_model_and_profile_is_generic(self):
        client = LocalClient({"name": "Test Researcher", "keywords": ["optics", "optics"],
                              "phd_date": "2020-06", "country": "kr"})
        profile = parse_cv_profile("cv.txt", b"Test Researcher\nOptics research\nPhD June 2020", client)
        self.assertTrue(profile["id"].startswith("cv_"))
        self.assertEqual(profile["name"], "Test Researcher")
        self.assertEqual(profile["keywords"], ["optics"])
        self.assertEqual(profile["phd_date"], "2020-06")
        self.assertEqual(profile["country"], "KR")
        self.assertIsNone(profile["nationality"])
        self.assertIsNone(profile["employment"])
        self.assertEqual(profile["cv_import"]["method"], "ollama_local")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("<cv_document>", client.calls[0][0][1]["content"])
        self.assertFalse(client.calls[0][1]["additionalProperties"])

    def test_profile_json_import_drops_paths_settings_and_claimed_identity(self):
        raw = {"id": "previous-synthetic-researcher", "name": "New Researcher", "keywords": ["biology"],
               "active_profile": "/secret", "OLLAMA_HOST": "https://remote.example", "evidence": [{"path": "/secret"}]}
        with patch("grantfinder.cv_profile.OllamaClient", side_effect=AssertionError("no model needed")):
            profile = parse_cv_profile("profile.json", json.dumps(raw).encode())
        self.assertNotEqual(profile["id"], "previous-synthetic-researcher")
        self.assertNotIn("active_profile", profile)
        self.assertNotIn("evidence", profile)
        self.assertNotIn("OLLAMA_HOST", profile)

    def test_different_uploads_have_different_ids(self):
        first = parse_cv_profile("one.json", b'{"name":"First"}')
        second = parse_cv_profile("two.json", b'{"name":"Second"}')
        self.assertNotEqual(first["id"], second["id"])

    def test_docx_reads_paragraphs_without_extracting_files(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Researcher</w:t></w:r></w:p><w:p><w:r><w:t>Biology</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(extract_cv_text("cv.docx", stream.getvalue()), "Researcher\nBiology")

    def test_docx_rejects_expansion_limit_and_broken_file(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", "x" * 50)
        with patch("grantfinder.cv_profile.MAX_DOCX_EXPANDED_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "20MB"):
                extract_cv_text("cv.docx", stream.getvalue())
        with self.assertRaisesRegex(ValueError, "DOCX"):
            extract_cv_text("cv.docx", b"broken")

    def test_pdf_checks_real_text_and_keeps_scan_empty(self):
        class Document:
            needs_pass = False

            def __init__(self, text):
                self.text = text

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __len__(self):
                return 1

            def __iter__(self):
                return iter([SimpleNamespace(get_text=lambda: self.text)])

        reader = SimpleNamespace(open=lambda **kwargs: Document("Researcher name\nOptics and computational imaging research"))
        with patch.dict(sys.modules, {"pymupdf": reader}):
            self.assertIn("Optics", extract_cv_text("cv.pdf", b"pdf payload"))
        reader.open = lambda **kwargs: Document("  \n  ")
        with patch.dict(sys.modules, {"pymupdf": reader}):
            with self.assertRaisesRegex(ValueError, "OCR"):
                extract_cv_text("cv.pdf", b"scanned pdf")

    def test_pdf_missing_dependency_has_install_instruction(self):
        with patch.dict(sys.modules, {"pymupdf": None}):
            with self.assertRaisesRegex(ValueError, "requirements-pdf.txt"):
                extract_cv_text("cv.pdf", b"pdf payload")

    def test_upload_rejects_empty_unsupported_and_oversized_payloads(self):
        for filename, payload in (("cv.exe", b"body"), ("cv.txt", b""), ("cv.txt", "not bytes")):
            with self.subTest(filename=filename, payload=payload):
                with self.assertRaises(ValueError):
                    extract_cv_text(filename, payload)
        with patch("grantfinder.cv_profile.MAX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "10MB"):
                extract_cv_text("cv.txt", b"123456")
        with patch("grantfinder.cv_profile.MAX_TEXT_CHARACTERS", 5):
            with self.assertRaisesRegex(ValueError, "60,000"):
                extract_cv_text("cv.txt", b"123456")

    def test_dates_preserve_precision_and_reject_impossible_calendar_dates(self):
        profile = normalize_profile({"name": "X", "phd_date": "2020", "first_faculty_date": "2024-09"}, "test")
        self.assertEqual(profile["phd_date"], "2020")
        self.assertEqual(profile["first_faculty_date"], "2024-09")
        for value in ("2020-13", "2020-02-31", "June 2020", "2020-01-01T00:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_profile({"name": "X", "phd_date": value}, "test")

    def test_malformed_model_profile_and_non_json_are_rejected(self):
        for raw in ([], {"name": ["X"]}, {"name": "X", "keywords": "AI"},
                    {"name": "X", "country": "Korea"}, {"name": "X", "active_projects": ["grant"]}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_profile(raw, "test")
        client = LocalClient({"name": "X"})
        client.chat = lambda *args, **kwargs: {"message": {"content": "unstructured output"}}
        with self.assertRaisesRegex(ValueError, "JSON"):
            parse_cv_profile("cv.txt", b"Researcher", client)


if __name__ == "__main__":
    unittest.main()
