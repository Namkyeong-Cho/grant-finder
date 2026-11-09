"""공식 공개 웹페이지와 첨부를 수집합니다. 외부 LLM/API 키는 사용하지 않습니다."""
from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
USER_AGENT = "GrantFinder/0.1 (educational public-notice reader) Mozilla/5.0"


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_sources(path: Path) -> list[dict]:
    from .source_registry import load_registry
    return load_registry(path)["sources"]


def _allowed(url: str, domains: list[str]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and any(host == domain or host.endswith("." + domain) for domain in domains)
    )


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, domains: list[str]):
        super().__init__()
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl, self.domains):
            raise ValueError("허용하지 않은 호스트로 리다이렉트되었습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, domains: list[str], timeout: int = 25) -> tuple[bytes, str, str]:
    if not _allowed(url, domains):
        raise ValueError("등록된 공식 HTTPS 호스트만 수집할 수 있습니다.")
    # urllib requires non-ASCII URL paths to be percent-encoded.
    parts = urllib.parse.urlsplit(url)
    safe_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%:@"), parts.query, ""))
    request = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_OfficialRedirects(domains))
    with opener.open(request, timeout=timeout) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise ValueError("첨부파일이 수집 크기 제한 12MB를 초과했습니다.")
        return payload, response.headers.get("Content-Type", ""), response.url


class _HTMLText(HTMLParser):
    BLOCKS = {"p", "div", "section", "article", "main", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, base_url: str, content_class: str | None = None):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.content_class = content_class
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.links: list[dict] = []
        self.images: list[str] = []
        self.anchor: dict | None = None
        self.focus_found = not content_class

    def _visible(self) -> bool:
        selected = not self.content_class or any(focus for _, focus in self.stack)
        return selected and not any(tag in self.SKIP for tag, _ in self.stack)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        focus = bool(self.content_class and self.content_class in attributes.get("class", "").split())
        self.focus_found |= focus
        if tag not in self.VOID:
            self.stack.append((tag, focus))
        if tag == "a":
            self.anchor = {"url": urllib.parse.urljoin(self.base_url, attributes.get("href", "")), "parts": []}
        if self._visible():
            if tag in self.BLOCKS or tag in {"br", "hr"}:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append(" | ")
            elif tag == "img" and attributes.get("src"):
                self.images.append(urllib.parse.urljoin(self.base_url, attributes["src"]))
                if attributes.get("alt", "").strip():
                    self.parts.append("[이미지: " + attributes["alt"].strip() + "]")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self._visible() and tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self.anchor is not None:
            title = re.sub(r"\s+", " ", "".join(self.anchor["parts"])).strip()
            if title:
                self.links.append({"title": title, "url": self.anchor["url"]})
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.anchor is not None and not any(tag in self.SKIP for tag, _ in self.stack):
            self.anchor["parts"].append(data)
        if self._visible():
            # HTML source indentation is not a semantic newline. Block tags supply those.
            self.parts.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        if not self.focus_found:
            raise ValueError("설정된 본문 영역을 찾지 못했습니다. 사이트 구조를 확인하세요.")
        return "\n".join(line for part in "".join(self.parts).splitlines()
                         if (line := re.sub(r"[\t\r\f\v ]+", " ", part).strip()))


def _decode(payload: bytes, content_type: str) -> str:
    charset = re.search(r"charset=([^;\s]+)", content_type, re.I)
    candidates = [charset.group(1).strip('\"\'')] if charset else []
    candidates += ["utf-8-sig", "cp949"]
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    raise ValueError("문자 인코딩을 판별하지 못했습니다.")


def _msit_links(html: str, base_url: str) -> list[dict]:
    """MSIT가 서버 HTML의 JS 문자열로 제공하는 제목을 읽습니다. JS 실행 없음."""
    links = []
    for block in html.split("var sHtml = replyHrml;")[1:]:
        identifier = re.search(r"fn_detail\((\d+)\)", block)
        title = re.search(r"sHtml\s*\+=\s*unescape\('((?:\\.|[^'])*)'\)", block)
        if identifier and title:
            raw_title = title.group(1).replace("\\'", "'").replace('\\"', '"')
            raw_title = re.sub(r"%u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), raw_title)
            text = re.sub("<[^>]+>", "", unescape(urllib.parse.unquote(raw_title))).strip()
            links.append({"title": text, "url": urllib.parse.urljoin(base_url,
                "/bbs/view.do?bbsSeqNo=100&mId=311&mPid=121&nttSeqNo=" + identifier.group(1) + "&sCode=user")})
    return links


def extract_hwpx(payload: bytes) -> str:
    parts = []

    def walk(node):
        name = node.tag.rsplit("}", 1)[-1]
        if name in {"p", "tr"}:
            parts.append("\n")
        elif name in {"tc"}:
            parts.append(" | ")
        elif name in {"lineBreak"}:
            parts.append("\n")
        elif name == "tab":
            parts.append(" ")
        if name == "t" and node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if name == "t" and child.tail:
                parts.append(child.tail)
        if name in {"p", "tr"}:
            parts.append("\n")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        sections = sorted((name for name in archive.namelist()
                           if re.fullmatch(r"Contents/section\d+\.xml", name)),
                          key=lambda name: int(re.search(r"section(\d+)", name)[1]))
        if not sections:
            raise ValueError("HWPX 본문 section XML이 없습니다.")
        for name in sections:
            item = archive.getinfo(name)
            if item.file_size > 8 * MAX_DOWNLOAD_BYTES:
                raise ValueError("HWPX XML의 해제 크기가 너무 큽니다.")
            root = ElementTree.fromstring(archive.read(name))
            # Visit text nodes once, including runs outside nested table paragraphs.
            walk(root)
    lines = [line.strip() for line in "".join(parts).splitlines() if line.strip()]
    if not lines:
        raise ValueError("HWPX에서 텍스트를 추출하지 못했습니다.")
    return "\n".join(lines)


def _pdf_text(payload: bytes) -> str:
    try:
        import pymupdf
        with pymupdf.open(stream=payload, filetype="pdf") as document:
            text = "\n".join(f"[PDF page {i + 1}]\n{page.get_text()}" for i, page in enumerate(document))
    except ImportError:
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise ValueError("PDF 추출에는 선택 패키지 pymupdf 또는 pypdf가 필요합니다.") from error
        text = "\n".join(f"[PDF page {i + 1}]\n{page.extract_text() or ''}"
                         for i, page in enumerate(PdfReader(io.BytesIO(payload)).pages))
    if not re.search(r"[A-Za-z가-힣]", text):
        raise ValueError("PDF 텍스트가 없습니다. 스캔 문서의 OCR이 필요합니다.")
    return text


def collect_source(source: dict, cache_dir: Path, refresh: bool = False) -> dict:
    """성공·실패를 모두 JSON으로 저장합니다. 실패 시 오래된 내용을 최신으로 반환하지 않습니다."""
    cache_dir = Path(cache_dir)
    identifier = str(source["id"])
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", identifier)
    cache_file = cache_dir / (safe_id + ".json")
    previous = {}
    if cache_file.exists():
        previous = json.loads(cache_file.read_text(encoding="utf-8"))
        if not refresh and previous.get("status") == "ok":
            return {**previous, "cache_used": True}
    requested_url = source.get("attachment_url") or source.get("url") or ""
    result = {"source_id": identifier, "url": requested_url, "source_url": source.get("url", ""),
              "fetched_at": _now(), "content_hash": None, "text": "", "status": "error",
              "content_type": "", "links": [], "cache_used": False}
    if previous.get("last_success_at"):
        result["last_success_at"] = previous["last_success_at"]
    try:
        payload, content_type, effective_url = _download(requested_url, source.get("domains", []),
                                                       int(source.get("timeout", 25)))
        cache_dir.mkdir(parents=True, exist_ok=True)
        raw_file = cache_dir / (safe_id + ".raw")
        raw_file.write_bytes(payload)
        result.update({"url": effective_url, "content_type": content_type,
                       "raw_sha256": hashlib.sha256(payload).hexdigest(), "raw_bytes": len(payload),
                       "raw_file": str(raw_file)})
        document_format = source.get("format", "").lower()
        if document_format == "hwpx" or (zipfile.is_zipfile(io.BytesIO(payload)) and b"PK" == payload[:2]):
            text = extract_hwpx(payload)
            result["extraction_method"] = "HWPX XML document-order text"
        elif document_format == "pdf" or payload.startswith(b"%PDF"):
            text = _pdf_text(payload)
            result["extraction_method"] = "PDF text extraction with page markers"
        else:
            html = _decode(payload, content_type)
            parser = _HTMLText(effective_url, source.get("content_class"))
            parser.feed(html)
            text = parser.text()
            if source.get("drop_view_count"):
                text = re.sub(r"(?m)^조회 수\n[\d,]+\n", "", text)
            result["links"] = parser.links
            result["image_urls"] = parser.images
            result["extraction_method"] = "HTMLParser visible text"
            if source.get("parser") == "msit_listing":
                dynamic_links = _msit_links(html, effective_url)
                result["links"] += dynamic_links
                text += "\n" + "\n".join(item["title"] for item in dynamic_links)
                result["extraction_method"] += "; MSIT title string decoding (no JavaScript execution)"
        if len(text.strip()) < 20:
            raise ValueError("공고 본문 텍스트가 너무 짧습니다. 동적 페이지·이미지 본문을 확인하세요.")
        supporting_results = []
        for index, supplement in enumerate(source.get("supporting_sources", []), 1):
            config = {"id": identifier + f"__support_{index}", "domains": source.get("domains", []), **supplement}
            support = collect_source(config, cache_dir, refresh)
            supporting_results.append({key: support.get(key) for key in
                                       ("url", "fetched_at", "content_hash", "status", "error")})
            if support["status"] == "ok":
                text += "\n\n[추가 공식 원문: " + support["url"] + "]\n" + support["text"]
        if supporting_results:
            result["supporting_results"] = supporting_results
        result.update({"status": "ok", "text": text.strip(),
                       "content_hash": hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
                       "last_success_at": result["fetched_at"]})
        if source.get("image_notice"):
            result["warning"] = "주요 모집내용이 이미지입니다. HTML 텍스트만으로 전체 일정·자격을 확인하지 마세요."
    except (OSError, ValueError, urllib.error.URLError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    _write_json(cache_file, result)
    return result


def discover(source: dict, cache_dir: Path, refresh: bool = False) -> list[dict]:
    """등록한 목록 페이지의 관련 공고를 찾습니다. 수집상태는 별도 __discovery.json에 남깁니다."""
    listing_url = source.get("discovery_url") or (source.get("url") if source.get("kind") == "listing" else None)
    if not listing_url:
        return []
    config = {**source, "id": source["id"] + "__discovery", "url": listing_url}
    for key in ("attachment_url", "format", "content_class", "image_notice"):
        config.pop(key, None)
    config["parser"] = source.get("discovery_parser", "")
    result = collect_source(config, cache_dir, refresh)
    if result["status"] != "ok":
        raise ValueError(f"{source['id']} 목록 수집 실패: {result.get('error')}")
    pattern = source.get("discovery_link_pattern")
    keywords = source.get("discovery_keywords", ["연구", "공모", "지원", "Research", "proposal", "award"])
    found = []
    seen = set()
    for item in result["links"]:
        title, url = item["title"], item["url"]
        if not _allowed(url, source.get("domains", [])) or url in seen:
            continue
        if pattern and not re.search(pattern, url):
            continue
        if keywords and not any(keyword.lower() in title.lower() for keyword in keywords):
            continue
        seen.add(url)
        found.append({"source_id": source["id"], "title": title, "url": url,
                      "discovered_at": result["fetched_at"]})
    return found
