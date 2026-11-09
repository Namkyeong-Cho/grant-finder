"""Single editable registry for funding sites, pages, and collector settings."""

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit


def _https_url(value):
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and bool(parts.hostname) and not parts.username
                and not parts.password and parts.port in (None, 443))
    except (TypeError, ValueError):
        return False


def resolve_links(record, registry):
    """Resolve registry references without changing the stored source or evidence."""
    pages = {page["id"]: page for page in registry.get("pages", [])}

    def url(reference):
        if reference not in pages:
            raise ValueError(f"sources.json에 없는 페이지 참조: {reference}")
        return pages[reference]["url"]

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        result = {key: walk(item) for key, item in value.items()}
        for reference, target in (("url_ref", "url"), ("source_ref", "source_url"),
                                  ("discovery_url_ref", "discovery_url"),
                                  ("attachment_url_ref", "attachment_url")):
            if reference in result:
                result[target] = url(result[reference])
        if "additional_source_refs" in result:
            result["additional_sources"] = [url(key) for key in result["additional_source_refs"]]
        return result

    return walk(record)


def load_registry(path: Path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    data = {"sources": raw} if isinstance(raw, list) else raw
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError("sources는 목록이어야 합니다.")
    sources = data["sources"]
    identifiers = [source["id"] for source in sources]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("중복 source id가 있습니다.")
    pages = data.get("pages", [])
    if not isinstance(pages, list):
        raise ValueError("pages는 목록이어야 합니다.")
    page_ids, urls = set(), set()
    for page in pages:
        if not page.get("id") or page["id"] in page_ids:
            raise ValueError(f"중복 또는 누락 page id: {page.get('id')}")
        if page.get("source_id") not in identifiers:
            raise ValueError(f"페이지의 source_id가 없습니다: {page.get('id')}")
        if not _https_url(page.get("url")):
            raise ValueError(f"공식 HTTPS 페이지 URL을 확인하세요: {page.get('id')}")
        if page["url"] in urls:
            raise ValueError(f"중복 페이지 URL: {page['url']}")
        page_ids.add(page["id"])
        urls.add(page["url"])
    result = dict(data, pages=pages)
    result["sources"] = resolve_links(sources, result)
    return result
