"""Central URL ownership and reference integrity, without network access."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.collectors import load_sources
from grantfinder.funding_catalog import load_catalog
from grantfinder.source_registry import load_registry, resolve_links


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def registry_fixture():
    return {
        "schema_version": 2,
        "sources": [{"id": "example", "name": "가상 지원기관", "kind": "notice",
                     "enabled": True, "domains": ["example.org"], "url_ref": "call",
                     "discovery_url_ref": "listing", "attachment_url_ref": "rules",
                     "supporting_sources": [{"url_ref": "faq", "content_class": "body"}]}],
        "pages": [
            {"id": "call", "source_id": "example", "title": "가상 공고", "url": "https://example.org/call"},
            {"id": "listing", "source_id": "example", "title": "가상 목록", "url": "https://example.org/list"},
            {"id": "rules", "source_id": "example", "title": "가상 규정", "url": "https://example.org/rules.pdf"},
            {"id": "faq", "source_id": "example", "title": "가상 FAQ", "url": "https://example.org/faq"},
        ],
    }


def candidate_fixture(**changes):
    item = {"id": "synthetic", "title": "가상 연구비", "sponsor": "가상 지원기관",
            "category": "foundation", "support_type": "research_grant", "route": "direct",
            "availability": "rolling", "fit": "conditional", "fit_reason": "합성 검증",
            "eligibility_summary": "가상 자격", "application_timing": "상시 접수",
            "next_action": "가상 신청서 확인", "checked_on": "2026-11-07",
            "source_ref": "call", "additional_source_refs": ["rules", "faq"]}
    item.update(changes)
    return item


class SourceRegistryTests(unittest.TestCase):
    def test_collector_resolves_all_configured_url_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            write_json(path, registry_fixture())
            source = load_sources(path)[0]
            self.assertEqual(source["url"], "https://example.org/call")
            self.assertEqual(source["discovery_url"], "https://example.org/list")
            self.assertEqual(source["attachment_url"], "https://example.org/rules.pdf")
            self.assertEqual(source["supporting_sources"][0]["url"], "https://example.org/faq")
            self.assertEqual(source["supporting_sources"][0]["content_class"], "body")
            self.assertNotIn("url", json.loads(path.read_text())["sources"][0])

    def test_recursive_resolution_preserves_the_input_and_shared_programs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            write_json(path, registry_fixture())
            registry = load_registry(path)
            original = {"candidates": [candidate_fixture(), candidate_fixture(id="different-program")]}
            before = copy.deepcopy(original)
            resolved = resolve_links(original, registry)
            self.assertEqual(original, before)
            self.assertEqual([item["id"] for item in resolved["candidates"]],
                             ["synthetic", "different-program"])
            for item in resolved["candidates"]:
                self.assertEqual(item["source_url"], "https://example.org/call")
                self.assertEqual(item["additional_sources"],
                                 ["https://example.org/rules.pdf", "https://example.org/faq"])
            resolved["candidates"][0]["title"] = "changed"
            self.assertEqual(original, before)

    def test_unresolved_links_fail_instead_of_silently_omitting_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            data = registry_fixture()
            data["sources"][0]["url_ref"] = "missing-page"
            write_json(path, data)
            with self.assertRaises(ValueError):
                load_registry(path)
            write_json(path, registry_fixture())
            registry = load_registry(path)
            for changes in ({"source_ref": "missing"}, {"additional_source_refs": ["missing"]}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    resolve_links(candidate_fixture(**changes), registry)

    def test_duplicate_ids_urls_and_orphan_pages_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            variants = {}
            data = registry_fixture()
            data["sources"].append(copy.deepcopy(data["sources"][0]))
            variants["duplicate-source-id"] = data
            data = registry_fixture()
            data["pages"][1]["id"] = data["pages"][0]["id"]
            variants["duplicate-page-id"] = data
            data = registry_fixture()
            data["pages"][1]["url"] = data["pages"][0]["url"]
            variants["duplicate-page-url"] = data
            data = registry_fixture()
            data["pages"][0]["source_id"] = "missing-source"
            variants["orphan-page"] = data
            for name, data in variants.items():
                with self.subTest(name=name):
                    write_json(path, data)
                    with self.assertRaises(ValueError):
                        load_registry(path)

    def test_registry_rejects_non_https_and_credential_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            for url in ("http://example.org/call", "javascript:alert(1)",
                        "file:///tmp/private", "https://user:password@example.org/call", "https:///call"):
                with self.subTest(url=url):
                    data = registry_fixture()
                    data["pages"][0]["url"] = url
                    write_json(path, data)
                    with self.assertRaises(ValueError):
                        load_registry(path)

    def test_legacy_source_dict_and_list_remain_usable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            sources = [{"id": "legacy", "name": "가상 기존 채널", "url": "https://example.org/",
                        "enabled": False, "kind": "listing", "domains": ["example.org"]}]
            for data in ({"sources": sources}, sources):
                with self.subTest(container=type(data).__name__):
                    write_json(path, data)
                    self.assertEqual(load_registry(path)["sources"], sources)
                    self.assertEqual(load_sources(path), sources)

    def test_catalog_uses_one_registry_for_updates_and_supports_raw_fixtures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            research = root / "research"
            path = root / "sources.json"
            write_json(path, registry_fixture())
            candidate_path = research / "synthetic_candidates.json"
            write_json(candidate_path, {"candidates": [candidate_fixture()]})
            profile = {"id": "synthetic-professor"}
            self.assertEqual(load_catalog(research, profile)["candidates"][0]["source_url"],
                             "https://example.org/call")
            changed = registry_fixture()
            changed["pages"][0]["url"] = "https://example.org/new-call"
            write_json(path, changed)
            self.assertEqual(load_catalog(research, profile)["candidates"][0]["source_url"],
                             "https://example.org/new-call")
            self.assertNotIn("source_url", json.loads(candidate_path.read_text())["candidates"][0])
            alternate = root / "alternate" / "sites.json"
            write_json(alternate, registry_fixture())
            self.assertEqual(load_catalog(research, profile, registry_path=alternate)["candidates"][0]["source_url"],
                             "https://example.org/call")
            raw = candidate_fixture()
            raw.pop("source_ref")
            raw.pop("additional_source_refs")
            raw.update(source_url="https://example.org/raw", additional_sources=[])
            raw_research = root / "isolated" / "research"
            write_json(raw_research / "raw_candidates.json", {"candidates": [raw]})
            self.assertEqual(load_catalog(raw_research, profile)["candidates"][0]["source_url"],
                             "https://example.org/raw")

    def test_actual_96_candidates_resolve_and_four_collectors_stay_enabled(self):
        path = ROOT / "data/sources.json"
        registry = load_registry(path)
        sources = registry["sources"]
        self.assertEqual({source["id"] for source in sources if source.get("enabled")},
                         {"msit", "samsung_science", "posco", "amazon"})
        raw = []
        for file in sorted((ROOT / "data/research").glob("*_candidates.json")):
            raw.extend(json.loads(file.read_text(encoding="utf-8"))["candidates"])
        self.assertEqual(len(raw), 96)
        self.assertTrue(all(item.get("source_ref") and "source_url" not in item for item in raw))
        profile = json.loads((ROOT / "data/profile.example.json").read_text(encoding="utf-8"))
        catalog = load_catalog(ROOT / "data/research", profile)
        self.assertEqual(len(catalog["candidates"]), 96)
        self.assertEqual(catalog["counts"]["excluded"], 0)
        self.assertTrue(all(item["source_url"].startswith("https://") for item in catalog["candidates"]))


if __name__ == "__main__":
    unittest.main()
