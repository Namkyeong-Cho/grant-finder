"""Program grouping preserves separate tracks and their application evidence."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.program_groups import group_records, load_program_groups


def definitions():
    return {"groups": [{"id": "sample-program", "title": "가상 지원사업",
                        "candidate_ids": ["track-a", "track-b"],
                        "notice_ids": ["notice-a", "notice-b"],
                        "track_labels": {"track-a": "일반 전형", "track-b": "신진 전형"}}]}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ProgramGroupTests(unittest.TestCase):
    def test_explicit_family_keeps_each_tracks_deadline_and_eligibility(self):
        records = [
            {"id": "track-a", "title": "일반", "deadline": "2026-12-01",
             "fit": "excluded", "eligibility_summary": "기존 과제 수행자는 제외"},
            {"id": "track-b", "title": "신진", "deadline": "2027-03-01",
             "fit": "high", "eligibility_summary": "장비 미수혜자 신청"},
        ]
        before = copy.deepcopy(records)
        result = group_records(records, "candidate", definitions())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "가상 지원사업")
        self.assertEqual(result[0]["track_labels"]["track-b"], "신진 전형")
        self.assertEqual({item["id"]: item for item in result[0]["items"]},
                         {item["id"]: item for item in before})
        self.assertEqual(records, before)

    def test_unlisted_records_stay_separate_despite_same_site_or_sponsor(self):
        records = [{"id": identifier, "title": title, "sponsor": "같은 기관",
                    "source_url": "https://example.org/all-programs"}
                   for identifier, title in (("different-a", "별도 사업 A"),
                                             ("different-b", "별도 사업 B"))]
        with tempfile.TemporaryDirectory() as directory:
            empty = load_program_groups(Path(directory) / "missing.json")
        self.assertEqual(empty, {"groups": []})
        groups = group_records(records, "candidate", empty)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(group["items"]) == 1 for group in groups))
        self.assertEqual({group["title"] for group in groups}, {"별도 사업 A", "별도 사업 B"})

    def test_invalid_group_or_member_duplicates_fail_but_kind_namespaces_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            duplicate_group = definitions()
            duplicate_group["groups"].append(copy.deepcopy(duplicate_group["groups"][0]))
            duplicate_member = definitions()
            duplicate_member["groups"].append({"id": "other-program", "title": "다른 사업",
                                               "candidate_ids": ["track-a"], "notice_ids": []})
            within_group = definitions()
            within_group["groups"][0]["candidate_ids"].append("track-a")
            for name, data in (("group", duplicate_group), ("member", duplicate_member),
                               ("within-group", within_group)):
                with self.subTest(duplicate=name):
                    write_json(path, data)
                    with self.assertRaises(ValueError):
                        load_program_groups(path)
            separate_kinds = definitions()
            separate_kinds["groups"][0]["notice_ids"] = ["track-a"]
            write_json(path, separate_kinds)
            self.assertEqual(load_program_groups(path)["groups"][0]["notice_ids"], ["track-a"])

    def test_partial_data_does_not_create_empty_or_phantom_track_cards(self):
        metadata = definitions()
        metadata["groups"].append({"id": "absent-program", "title": "미포함 사업",
                                    "candidate_ids": ["absent"], "notice_ids": []})
        groups = group_records([{"id": "track-b", "title": "신진"}], "candidate", metadata)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["title"], "가상 지원사업")
        self.assertEqual([item["id"] for item in groups[0]["items"]], ["track-b"])

    def test_real_catalog_keeps_all_96_tracks_and_groups_samsung_and_amazon(self):
        records = []
        for path in (ROOT / "data/research").glob("*_candidates.json"):
            records.extend(json.loads(path.read_text(encoding="utf-8"))["candidates"])
        before = copy.deepcopy(records)
        groups = group_records(records, "candidate", load_program_groups(ROOT / "data/program_groups.json"))
        flattened = [item for group in groups for item in group["items"]]
        self.assertEqual(len(flattened), 96)
        self.assertEqual({item["id"] for item in flattened}, {item["id"] for item in records})
        self.assertEqual(records, before)
        member_sets = [{item["id"] for item in group["items"]} for group in groups]
        self.assertIn({"samsung_science_2027", "samsung_technology_2027"}, member_sets)
        self.assertIn({"amazon_ara_agentic_ai_fall_2026", "amazon_ara_automated_reasoning_fall_2026",
                       "amazon_build_trainium_fall_2026"}, member_sets)

    def test_six_detailed_notices_become_four_programs_without_losing_tracks(self):
        records = json.loads((ROOT / "data/seeds.json").read_text(encoding="utf-8"))["notices"]
        groups = group_records(records, "notice", load_program_groups(ROOT / "data/program_groups.json"))
        self.assertEqual(len(records), 6)
        self.assertEqual(len(groups), 4)
        self.assertEqual(sorted(len(group["items"]) for group in groups), [1, 1, 2, 2])
        member_sets = [{item["id"] for item in group["items"]} for group in groups]
        self.assertIn({"msit_2027_core_a", "msit_2027_young_a"}, member_sets)
        self.assertIn({"samsung_2027_h1", "samsung_2027_h2"}, member_sets)
        self.assertEqual({item["id"] for group in groups for item in group["items"]},
                         {item["id"] for item in records})


if __name__ == "__main__":
    unittest.main()
