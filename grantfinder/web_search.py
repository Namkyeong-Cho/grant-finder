"""Public web search for opportunities outside the registered source list."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .storage import read_json, write_json

MAX_QUERIES = 24
MAX_RESULTS = 200
CACHE_TTL = timedelta(hours=24)
PROVIDER_VERSION = "ddgs-text-general-v2"
SEARCH_BACKENDS = ("duckduckgo", "brave", "yahoo")
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "yclid", "_ga", "_gl"}


def _now():
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    """Canonicalize public HTTP URLs while retaining notice-identifying queries."""
    if not isinstance(url, str) or len(url) > 4000:
        return ""
    try:
        value = url.strip()
        if any(ord(char) < 32 for char in value) or "\\" in value:
            return ""
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
        if (parts.scheme.lower() not in {"http", "https"} or not host
                or parts.username is not None or parts.password is not None
                or host in {"localhost", "metadata.google.internal"}
                or host.endswith((".localhost", ".local", ".internal"))):
            return ""
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return ""
            authority = f"[{host}]" if address.version == 6 else host
        except ValueError:
            # Search results should be public DNS names, not single-label hosts,
            # legacy integer IPv4 forms, or invalid hostnames.
            authority = host.encode("idna").decode("ascii")
            if ("." not in authority or re.fullmatch(r"[0-9.]+", authority)
                    or not re.fullmatch(r"[a-z0-9.-]+", authority)
                    or any(not label or label.startswith("-") or label.endswith("-")
                           for label in authority.split("."))):
                return ""
        if port is not None and not ((parts.scheme == "https" and port == 443)
                                     or (parts.scheme == "http" and port == 80)):
            # Arbitrary service ports are not opportunity pages.
            return ""
        queries = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                   if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_KEYS]
        queries.sort()
        return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", urlencode(queries), ""))
    except (ValueError, UnicodeError):
        return ""


class SearchProviderError(RuntimeError):
    """A short user message with separate backend diagnostics."""

    def __init__(self, attempts):
        super().__init__("웹 검색을 완료하지 못했습니다. 잠시 뒤 다시 검색해 주세요.")
        self.detail = "; ".join(f"{item['backend']}: {item['error']}" for item in attempts if item.get("error"))


class DDGSSearchProvider:
    """A metasearch adapter; no hosted language model or API key is involved."""

    def search(self, query: str, max_results: int) -> list[dict]:
        self.last_attempts = []
        try:
            from ddgs import DDGS
        except ImportError as error:
            raise RuntimeError("웹 검색 패키지가 없습니다. python3 -m pip install -r requirements-search.txt 를 실행하세요.") from error
        # `auto` includes encyclopedias; Wikipedia interprets wt-wt as language wt.
        # Explicit general-web engines keep this search focused on public notices.
        client = DDGS(timeout=10)
        region = "kr-ko" if re.search(r"[가-힣]", query) else "us-en"
        for backend in SEARCH_BACKENDS:
            try:
                results = client.text(query, region=region, max_results=max_results, backend=backend)
                if not isinstance(results, list):
                    raise ValueError("검색 공급자의 응답 형식이 목록이 아닙니다.")
                self.last_attempts.append({"backend": backend, "status": "ok" if results else "empty"})
                if results:
                    return results
            except Exception as error:
                # DDGS uses this exact exception text for a completed empty search.
                if str(error).strip() == "No results found.":
                    self.last_attempts.append({"backend": backend, "status": "empty"})
                else:
                    self.last_attempts.append({"backend": backend, "status": "error", "error": _clean(error, 500)})
        if any(item["status"] == "error" for item in self.last_attempts):
            raise SearchProviderError(self.last_attempts)
        return []


def _clean(value, maximum):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _timestamp(value):
    try:
        moment = datetime.fromisoformat(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def search_web(queries: list[str], cache_dir: Path, refresh: bool = False,
               max_results: int = 4, *, provider=None) -> tuple[list[dict], list[dict]]:
    """Search each query, retaining independent status and shared URL evidence.

    A failed refresh is recorded as an error; cached results are never relabelled
    as a successful fresh search. Existing cache files are retained for retry.
    """
    if not isinstance(queries, list) or any(not isinstance(query, str) for query in queries):
        raise ValueError("검색어는 문자열 목록으로 입력하세요.")
    if any(len(query) > 300 for query in queries):
        raise ValueError("검색어는 각각 300자 이내로 입력하세요.")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10:
        raise ValueError("검색어별 결과 수는 1~10 사이여야 합니다.")
    unique_queries = list(dict.fromkeys(_clean(query, 300) for query in queries if query.strip()))[:MAX_QUERIES]
    cache_dir = Path(cache_dir)
    provider = provider or DDGSSearchProvider()
    merged, statuses = {}, []
    for query in unique_queries:
        now = _now()
        cache_key = hashlib.sha256(f"{PROVIDER_VERSION}\n{max_results}\n{query}".encode()).hexdigest()
        cache_path = cache_dir / f"{cache_key}.json"
        cached = None
        try:
            cached = read_json(cache_path, {})
        except (ValueError, OSError):
            pass
        cached = cached if isinstance(cached, dict) else {}
        checked = _timestamp(cached.get("checked_at"))
        use_cache = (not refresh and checked is not None and timedelta(0) <= now - checked < CACHE_TTL
                     and isinstance(cached.get("results"), list))
        try:
            raw = cached["results"] if use_cache else provider.search(query, max_results)
            attempts = (cached.get("backend_attempts", []) if use_cache
                        else getattr(provider, "last_attempts", []))
            if not isinstance(raw, list):
                raise ValueError("검색 공급자의 응답 형식이 목록이 아닙니다.")
            clean_results, seen = [], set()
            old = {item.get("url"): item for item in cached.get("results", []) if isinstance(item, dict)}
            checked_at = cached["checked_at"] if use_cache else now.isoformat(timespec="seconds")
            for item in raw[:max_results * 3]:
                if not isinstance(item, dict):
                    continue
                url = normalize_url(item.get("url") or item.get("href") or "")
                title = _clean(item.get("title"), 400)
                if not url or not title or url in seen:
                    continue
                seen.add(url)
                before = old.get(url, {})
                clean_results.append({
                    "id": "web_" + hashlib.sha256(url.encode()).hexdigest()[:16],
                    "title": title, "url": url,
                    "snippet": _clean(item.get("snippet") or item.get("body"), 1600),
                    "first_seen_at": before.get("first_seen_at") or checked_at,
                    "last_seen_at": checked_at,
                })
                if len(clean_results) >= max_results:
                    break
            if not use_cache:
                write_json(cache_path, {"provider": PROVIDER_VERSION, "query": query,
                                       "checked_at": checked_at, "results": clean_results,
                                       "backend_attempts": attempts})
            for result in clean_results:
                url = result["url"]
                if url not in merged and len(merged) < MAX_RESULTS:
                    merged[url] = dict(result, queries=[query])
                elif url in merged:
                    candidate = merged[url]
                    if query not in candidate["queries"]:
                        candidate["queries"].append(query)
                    candidate["first_seen_at"] = min(candidate["first_seen_at"], result["first_seen_at"])
                    candidate["last_seen_at"] = max(candidate["last_seen_at"], result["last_seen_at"])
            statuses.append({"query": query, "status": "ok" if clean_results else "empty",
                             "found": len(clean_results), "cache_hit": use_cache, "checked_at": checked_at})
            if attempts:
                statuses[-1]["backend_attempts"] = attempts
        except Exception as error:
            statuses.append({"query": query, "status": "error", "found": 0, "cache_hit": False,
                             "checked_at": now.isoformat(timespec="seconds"), "error": _clean(error, 1000)})
            if isinstance(error, SearchProviderError):
                statuses[-1]["error_detail"] = _clean(error.detail, 2000)
                statuses[-1]["backend_attempts"] = list(getattr(provider, "last_attempts", []))
    return list(merged.values()), statuses
