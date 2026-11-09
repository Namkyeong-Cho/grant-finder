"""Discover public funding-notice links without model calls or login sessions."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from .collectors import _decode, _HTMLText
from .homepage_profile import _fetch_page
from .storage import read_json, write_json
from .web_search import normalize_url

MAX_SITES = 12
MAX_PER_SITE = 20
MAX_RESULTS = 120
CACHE_TTL = timedelta(hours=24)
COLLECTOR_VERSION = "public-site-notices-v1"

_FUNDING_KOREAN = re.compile(r"연구비|연구과제|연구지원|학술연구|공모|지원사업|사업공고|과제공고")
_FUNDING_ENGLISH = re.compile(
    r"\b(?:grants?|funding|fellowships?|call\s+for\s+(?:research\s+)?proposals?|"
    r"research\s+(?:support|awards?|grants?))\b", re.I)
_EMPLOYMENT = re.compile(r"채용|초빙|임용|구인|recruit|vacanc|\bhiring\b", re.I)
_LOGIN_PATH = re.compile(r"(?:^|[/_.-])(?:login|logon|signin|sign-in|sso)(?:[/_.;-]|$)", re.I)


def _now():
    return datetime.now(timezone.utc)


def _clean(value, limit=400):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _funding_title(value):
    title = _clean(value)
    return bool(title and not _EMPLOYMENT.search(title) and
                (_FUNDING_KOREAN.search(re.sub(r"\s+", "", title)) or _FUNDING_ENGLISH.search(title)))


def _https_url(value):
    address = normalize_url(value)
    if not address or urlsplit(address).scheme != "https":
        raise ValueError("공개 HTTPS 공고 목록 주소가 필요합니다.")
    return address


def _login_url(value):
    parts = urlsplit(value)
    if _LOGIN_PATH.search(unquote(parts.path)):
        return True
    return any(key.casefold() in {"action", "mode", "page", "view"} and
               re.fullmatch(r"login|logon|signin|sign-in", item, re.I)
               for key, item in parse_qsl(parts.query))


class _PageInfo(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.password = False
        self.title_parts, self.headings = [], []
        self.in_title, self.heading = False, None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and str(attributes.get("type", "")).casefold() == "password":
            self.password = True
        if tag == "title":
            self.in_title = True
        if tag == "h1":
            self.heading = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "h1" and self.heading is not None:
            self.headings.append(_clean(" ".join(self.heading)))
            self.heading = None


def _read_listing(site, address, fetcher):
    if _login_url(address):
        raise ValueError("로그인 페이지입니다. 로그인 없이 읽을 수 있는 공개 공고 주소가 필요합니다.")
    payload, content_type, effective = fetcher(address)
    effective = _https_url(effective)
    if _login_url(effective):
        raise ValueError("로그인 페이지로 이동했습니다. 로그인 없이 읽을 수 있는 공개 공고 주소가 필요합니다.")
    if not isinstance(payload, bytes):
        raise ValueError("공고 목록 응답을 읽지 못했습니다.")
    if payload.startswith(b"%PDF") or (content_type and not any(
            kind in content_type.casefold() for kind in ("text/", "html", "xml"))):
        raise ValueError("HTML 공고 목록 또는 공고 상세 페이지 주소가 필요합니다.")
    html = _decode(payload, content_type)
    info = _PageInfo()
    info.feed(html)
    if info.password:
        raise ValueError("비밀번호 입력이 필요한 로그인 페이지입니다. 공개 공고 주소를 등록해 주세요.")
    parser = _HTMLText(effective)
    parser.feed(html)
    if not parser.text().strip():
        raise ValueError("공고 목록 본문을 읽지 못했습니다. 공개 HTML 페이지인지 확인해 주세요.")

    links = []
    if site.get("kind") == "notice":
        titles = info.headings + [_clean(" ".join(info.title_parts))]
        title = next((title for title in titles if _funding_title(title)), None)
        if title:
            links.append({"title": title, "url": effective, "discovery_method": "public_notice"})
    seen = {item["url"] for item in links}
    host = urlsplit(effective).hostname
    for link in parser.links:
        title = _clean(link.get("title"))
        url = normalize_url(link.get("url") or "")
        if (not _funding_title(title) or not url or url in seen or url == effective
                or urlsplit(url).scheme != "https" or urlsplit(url).hostname != host
                or _login_url(url)):
            continue
        seen.add(url)
        links.append({"title": title, "url": url, "discovery_method": "public_listing"})
        if len(links) >= MAX_PER_SITE:
            break
    return links[:MAX_PER_SITE], effective


def _cache_time(value):
    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def collect_site_notices(sites, cache_dir, refresh=False, *, fetcher=None):
    """Return funding-link candidates and independent per-site collection status.

    A search-only source is never fetched. Cached successful results retain their
    original check time; a failed refresh returns an error and no stale candidates.
    The default fetcher validates public addresses and every redirect.
    """
    if not isinstance(sites, list) or any(not isinstance(site, dict) for site in sites):
        raise ValueError("수집할 사이트는 객체 목록으로 전달하세요.")
    fetcher = fetcher or _fetch_page
    cache_dir = Path(cache_dir)
    candidates, statuses, seen = [], [], set()
    for site in sites[:MAX_SITES]:
        checked_at = _now()
        source_id, name = _clean(site.get("id"), 200), _clean(site.get("name"), 250)
        raw_url = site.get("url") or ""
        status = {"source_id": source_id, "source_name": name, "name": name, "url": raw_url,
                  "status": "error", "found": 0, "checked_at": checked_at.isoformat(timespec="seconds"),
                  "cache_hit": False, "error": ""}
        if site.get("collection_mode") == "search_only":
            status["status"] = "search_only"
            statuses.append(status)
            continue
        try:
            address = _https_url(raw_url)
            status["url"] = address
            key = hashlib.sha256(repr((COLLECTOR_VERSION, source_id, name, address,
                                      site.get("kind", "listing"))).encode()).hexdigest()
            path = cache_dir / f"{key}.json"
            try:
                previous = read_json(path, {})
            except (OSError, ValueError):
                previous = {}
            previous = previous if isinstance(previous, dict) else {}
            cache_time = _cache_time(previous.get("checked_at"))
            cached = (not refresh and cache_time is not None and
                      timedelta(0) <= checked_at - cache_time < CACHE_TTL and
                      previous.get("version") == COLLECTOR_VERSION and
                      isinstance(previous.get("candidates"), list))
            if cached:
                found = previous["candidates"][:MAX_PER_SITE]
                status.update(checked_at=previous["checked_at"], cache_hit=True)
            else:
                links, effective = _read_listing(site, address, fetcher)
                old = {item.get("url"): item for item in previous.get("candidates", []) if isinstance(item, dict)}
                found = []
                for link in links:
                    url = link["url"]
                    found.append({
                        "id": "web_" + hashlib.sha256(url.encode()).hexdigest()[:16],
                        "title": link["title"], "url": url,
                        "snippet": "공개 공고 페이지에서 확인한 제목과 링크입니다. 신청자격·마감은 원문 확인이 필요합니다."
                                   if link["discovery_method"] == "public_notice" else
                                   "공개 목록에서 발견한 공고 제목과 링크입니다. 신청자격·마감은 원문 확인이 필요합니다.",
                        "source_id": source_id, "source_name": name,
                        "discovered_from": effective, "discovery_method": link["discovery_method"],
                        "first_seen_at": old.get(url, {}).get("first_seen_at") or status["checked_at"],
                        "last_seen_at": status["checked_at"],
                    })
                write_json(path, {"version": COLLECTOR_VERSION, "checked_at": status["checked_at"], "candidates": found})
            status.update(status="ok" if found else "empty", found=len(found))
            for item in found:
                if item["url"] not in seen and len(candidates) < MAX_RESULTS:
                    seen.add(item["url"])
                    candidates.append(item)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            status["error"] = _clean(str(error), 500)
        statuses.append(status)
    return candidates, statuses
