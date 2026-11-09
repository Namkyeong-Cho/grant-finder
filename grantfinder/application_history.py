"""Human-maintained application records, independent of eligibility and progress."""

import json
import re
from datetime import date
from html import escape
from pathlib import Path

from .report import PROGRESS, _cell

STATUSES = dict(PROGRESS, withdrawn="철회", unknown="미확인")


def load_history(path: Path, profile_id):
    if not profile_id or not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("application_history.json의 entries는 목록이어야 합니다.")
    entries, seen = [], set()
    for item in data["entries"]:
        if not isinstance(item, dict):
            raise ValueError("지원 이력의 각 항목은 JSON 객체여야 합니다.")
        if item.get("profile_id") != profile_id:
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
            raise ValueError(f"지원 이력 ID가 없거나 중복됩니다: {identifier}")
        if not isinstance(item.get("program"), str) or not item["program"].strip():
            raise ValueError(f"지원 이력의 program을 입력하세요: {identifier}")
        if not isinstance(item.get("status", "unknown"), str) or item.get("status", "unknown") not in STATUSES:
            raise ValueError(f"지원 이력 status를 확인하세요: {identifier}")
        for field in ("submitted_on", "decided_on"):
            value = item.get(field)
            if value is None:
                continue
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"지원 이력 {identifier}의 {field}: YYYY-MM-DD 또는 null로 입력하세요.") from None
        entries.append(dict(item))
        seen.add(identifier)
    return entries


def _values(item):
    return [item["program"], item.get("round") or "미기재", item.get("submitted_on") or "미기재",
            item.get("decided_on") or "미기재", STATUSES.get(item.get("status", "unknown"), "미확인"),
            item.get("notes") or "—"]


def history_markdown(entries):
    lines = ["", "## 지원 이력", "", "사람이 직접 입력한 지원·결과 기록입니다.", ""]
    if entries:
        lines.extend(["| 지원사업 | 연도·회차 | 지원일 | 결과 확인일 | 진행·결과 | 메모 |",
                      "| --- | --- | --- | --- | --- | --- |"])
        lines.extend("| " + " | ".join(_cell(value).replace("[", r"\[").replace("]", r"\]") for value in _values(item)) + " |" for item in entries)
    else:
        lines.append("등록한 지원 이력이 없습니다.")
    return "\n".join(lines) + "\n"


def render_history_html(entries):
    if entries:
        rows = "".join('<tr>' + ''.join('<td>' + escape(str(value)) + '</td>' for value in _values(item)) + '</tr>' for item in entries)
        body = '<div class="table-wrap"><table><thead><tr>' + ''.join('<th scope="col">' + label + '</th>' for label in
               ("지원사업", "연도·회차", "지원일", "결과 확인일", "진행·결과", "메모")) + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
    else:
        body = '<p class="empty">등록한 지원 이력이 없습니다.</p>'
    return ('<section id="application-history" aria-labelledby="application-history-title">'
            '<div class="section-heading"><div><p class="eyebrow">APPLICATION HISTORY</p>'
            '<h2 id="application-history-title">지원 이력</h2></div><span class="section-tag">' + str(len(entries)) + '건</span></div>'
            '<p class="section-description">사람이 직접 입력한 지원·결과 기록입니다. 연도와 회차별로 메모를 남길 수 있습니다.</p>'
            + body + '</section>')
