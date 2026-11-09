"""Render an auditable Korean Markdown report from stored grant records."""

from __future__ import annotations

from datetime import datetime
import html
import re
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
STATUS = {"open": "접수 중", "upcoming": "접수 예정", "closed": "접수 마감", "unknown": "접수 상태 확인 필요"}
ELIGIBILITY = {"eligible": "추출 조건 충족", "ineligible": "조건 미충족", "needs_review": "추가 확인 필요"}
CONDITION = {"met": "충족", "not_met": "미충족", "needs_review": "확인 필요"}
KINDS = {
    "application_start": "접수 시작", "researcher_deadline": "연구자 제출 마감",
    "institution_deadline": "기관승인 마감", "result_expected": "심사결과 발표 예정",
    "guidelines_release": "세부요강 공개 예정", "followup_deadline": "후속 서류 마감",
}
PROGRESS = {
    "interested": "관심", "reviewing": "검토", "preparing": "준비", "submitted": "제출",
    "institution_approved": "기관승인", "awaiting_result": "결과대기", "shortlisted": "서면통과",
    "document_passed": "서면통과", "selected": "선정", "rejected": "미선정", "hold": "보류", "on_hold": "보류",
}


def _cell(value: object) -> str:
    if value is None or value == "":
        return "미확인"
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _link(label: object, url: object) -> str:
    text = _cell(label).replace("[", "\\[").replace("]", "\\]")
    address = str(url or "")
    if not address.startswith(("https://", "http://")):
        return text
    return f"[{text}](<{address.replace('>', '%3E')}>)"


def _event_value(event: dict) -> str:
    value = event.get("value")
    precision = event.get("precision", "unknown")
    if value is None or value == "":
        return "미기재·확인 필요"
    if precision == "datetime":
        try:
            if not re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", str(value)):
                raise ValueError("시각 미기재")
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(event.get("timezone") or "Asia/Seoul"))
            return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        except (ValueError, TypeError, KeyError):
            return _cell(value) + " (시각 표기 확인 필요)"
    labels = {"date": "날짜 단위·시각 미기재", "month": "월 단위", "week": "주차 단위", "relative": "상대적 예정", "unknown": "정밀도 확인 필요"}
    return f"{_cell(value)} ({labels.get(precision, '원문 표기')})"


def _state(progress: dict, notice_id: str) -> str:
    state = (progress.get("applications") or {}).get(notice_id, "")
    if isinstance(state, dict):
        state = state.get("status", "")
    return PROGRESS.get(str(state), str(state) or "미등록")


def _due(value: object) -> str:
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        except ValueError:
            pass
    return _cell(value) if value else "기한 미확인·먼저 확인"


def _change_text(change: dict) -> str:
    label = change.get("title") or change.get("notice_id") or change.get("source_id") or "공고"
    kind = change.get("kind") or change.get("type") or change.get("change") or "변경"
    kind = {"new": "신규", "added": "신규", "updated": "수정", "changed": "수정", "removed": "삭제·종료 확인 필요", "unchanged": "변경 없음"}.get(str(kind), str(kind))
    detail = change.get("description") or change.get("summary") or change.get("message")
    if not detail:
        fields = change.get("fields") or change.get("changed_fields")
        if fields:
            names = {"title": "공고명", "events": "일정", "eligibility": "신청자격",
                     "required_documents": "제출서류", "missing_information": "확인할 정보"}
            detail = "변경 항목: " + ", ".join(names.get(str(field), str(field)) for field in fields)
        elif "old" in change or "new" in change:
            detail = f"{change.get('old', '미기재')} → {change.get('new', '미기재')}"
    return f"- **{_cell(kind)} · {_cell(label)}**" + (f": {_cell(detail)}" if detail else "")


def render_report(notices: list[dict], evaluations: list[dict], profile: dict,
                  as_of: datetime, changes: list[dict], collection_status: list[dict],
                  progress: dict | None = None) -> str:
    progress = progress or {}
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=KST)
    lookup = {str(item.get("notice_id")): item for item in evaluations}
    by_id = {str(item.get("id")): item for item in notices}
    completed = set(progress.get("completed_tasks") or [])
    lines = ["# Grant Finder 연구지원 보고서", "",
             f"판정 기준: **{as_of.astimezone(KST).strftime('%Y-%m-%d %H:%M KST')}**  ",
             f"연구자: **{_cell(profile.get('name') or profile.get('id') or '이름 미등록')}**"
             + (" · **수업용 가상 프로필**" if profile.get("is_demo") else ""), "",
             "지원자격은 원문에서 추출한 조건을 프로필과 비교한 결과입니다. 연구주제 일치와 신분·경력 조건을 따로 확인합니다.", "",
             "## 신규·변경사항", ""]
    if changes:
        lines.extend(_change_text(change) for change in changes)
    else:
        lines.append("이번 비교에서 새 변경사항이 없습니다. 웹 재수집 여부는 아래 출처 확인 기록을 참고하세요.")
    lines.extend(["", "## 지원 후보", "",
                  "| 공고·유형 | 접수 상태 | 자격 비교 | 일치 키워드 | 사용자 진행 | 근거 상태 |",
                  "| --- | --- | --- | --- | --- | --- |"])
    ordered = sorted(notices, key=lambda item: (
        lookup.get(str(item.get("id")), {}).get("status") == "closed",
        lookup.get(str(item.get("id")), {}).get("eligibility") == "ineligible",
        str(item.get("title", ""))))
    for notice in ordered:
        notice_id = str(notice.get("id"))
        evaluated = lookup.get(notice_id, {})
        title = str(notice.get("title", notice_id))
        if notice.get("track"):
            title += f" · {notice['track']}"
        evidence = {"official_snapshot": "공식 원문 저장본", "live": "웹 수집 원문"}.get(notice.get("evidence_status"), "근거 상태 확인 필요")
        terms = ", ".join(str(term) for term in evaluated.get("topic_matches", [])) or "직접 일치 없음"
        lines.append(f"| {_link(title, notice.get('source_url'))} | {STATUS.get(evaluated.get('status'), '미평가')} | {ELIGIBILITY.get(evaluated.get('eligibility'), '미평가')} | {_cell(terms)} | {_cell(_state(progress, notice_id))} | {evidence} |")
    if not notices:
        lines.append("| 공고가 없습니다 | - | - | - | - | - |")
    lines.extend(["", "## 일정", "",
                  "예정·월·주차 일정은 원문 정밀도를 유지합니다. 결과 발표 예정일이 지났다는 이유로 선정·미선정을 기록하지 않습니다.", "",
                  "| 공고 | 단계 | 일정 | 공지 성격 | 근거 |", "| --- | --- | --- | --- | --- |"])
    count = 0
    for notice in ordered:
        for event in notice.get("events", []):
            count += 1
            status = {"announced": "공식 게시 일정", "planned": "공식 예정", "unknown": "확인 필요"}.get(event.get("status"), "성격 확인 필요")
            if event.get("source_conflict"):
                status = "출처 충돌·확인 필요"
            label = event.get("label") or KINDS.get(event.get("kind"), event.get("kind", "일정"))
            quote = event.get("evidence_quote") or "원문 인용 미첨부"
            lines.append(f"| {_cell(notice.get('title'))} | {_cell(label)} | {_event_value(event)} | {status} | {_link(quote, event.get('source_url') or notice.get('source_url'))} |")
        internal = (profile.get("internal_deadlines") or {}).get(str(notice.get("id")))
        if internal:
            count += 1
            lines.append(f"| {_cell(notice.get('title'))} | 대학 내부마감 | {_cell(internal)} | 사용자 등록·기관 확인 | 소속기관 등록 정보 |")
    if not count:
        lines.append("| - | - | 일정 미확인 | - | - |")
    lines.extend(["", "## 이번에 할 일", "",
                  "공식 요구사항·확인할 일·준비 작업을 구분했습니다. 완료로 기록한 일은 숨겼습니다.", "",
                  "| 할 일 ID | 공고 | 할 일 | 기한 | 성격 | 이유 |", "| --- | --- | --- | --- | --- | --- |"])
    task_count = 0
    for evaluated in evaluations:
        notice = by_id.get(str(evaluated.get("notice_id")), {})
        for task in evaluated.get("tasks", []):
            if task.get("id") in completed:
                continue
            task_count += 1
            kind = {"official": "공식 절차", "preparation": "준비 작업", "verification": "확인할 일"}.get(task.get("kind"), "확인할 일")
            lines.append(f"| `{_cell(task.get('id'))}` | {_cell(notice.get('title') or task.get('notice_id'))} | {_cell(task.get('title'))} | {_due(task.get('due'))} | {kind} | {_cell(task.get('reason'))} |")
    if not task_count:
        lines.append("| - | - | 새로 남은 할 일이 없습니다 | - | - | - |")
    lines.extend(["", "## 공고별 자격 근거와 확인사항", ""])
    for notice in ordered:
        notice_id = str(notice.get("id"))
        evaluated = lookup.get(notice_id, {})
        lines.extend([f"### {_cell(notice.get('title') or notice_id)}", "",
                      f"공고 ID: `{_cell(notice_id)}` · 지원기관: {_cell(notice.get('sponsor'))} · 사업연도: {_cell(notice.get('year'))}  ",
                      f"원문: {_link('공식 공고', notice.get('source_url'))}", ""])
        for condition in evaluated.get("conditions", []):
            lines.append(f"- **{CONDITION.get(condition.get('status'), '확인 필요')} · {_cell(condition.get('label'))}**: {_cell(condition.get('reason'))}")
            if condition.get("evidence_quote"):
                lines.append(f"  - 원문 근거: {_cell(condition['evidence_quote'])}")
        for warning in evaluated.get("warnings", []):
            lines.append(f"- 확인사항: {_cell(warning)}")
        for note in notice.get("notes", []):
            lines.append(f"- 공고 메모: {_cell(note)}")
        lines.append("")
    lines.extend(["## 출처 확인 기록", "",
                  "공식 저장본과 실시간 재수집을 구분합니다. 수집 실패는 ‘공고 없음’으로 해석하지 않습니다.", "",
                  "| 출처 | 수집 상태 | 이번 조회·저장 시각 | 마지막 성공 시각 | 설명 |",
                  "| --- | --- | --- | --- | --- |"])
    for record in collection_status:
        state = record.get("status", "unknown")
        label = {"ok": "수집 성공", "error": "수집 실패", "cached": "캐시 사용", "snapshot": "공식 저장본 사용", "official_snapshot": "공식 저장본 사용", "disabled": "자동수집 미사용"}.get(state, str(state))
        name = record.get("name") or record.get("source_id") or record.get("id") or record.get("url")
        detail = record.get("error") or record.get("note") or record.get("message") or ""
        if record.get("from_cache") or record.get("cached"):
            detail = (str(detail) + " · " if detail else "") + "저장 캐시 사용"
        lines.append(f"| {_link(name, record.get('url'))} | {_cell(label)} | {_cell(record.get('fetched_at') or record.get('checked_at'))} | {_cell(record.get('last_success_at') or (record.get('fetched_at') if state == 'ok' else None))} | {_cell(detail) if detail else '-'} |")
    if not collection_status:
        lines.append("| 이번 실행의 웹 수집 기록 없음 | 실시간 조회 확인 안 됨 | - | - | 저장된 공고 데이터 기준 |")
    lines.extend(["", "보고서의 선정·미선정 및 제출 상태는 사용자가 기록한 진행상태만 표시합니다.", ""])
    return "\n".join(lines)
