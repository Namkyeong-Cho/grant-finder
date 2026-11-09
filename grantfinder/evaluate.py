"""Evidence-aware eligibility, calendar and preparation rules (stdlib only)."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


KST = ZoneInfo("Asia/Seoul")
FIELD_LABELS = {
    "affiliation_type": "소속기관 유형", "employment": "전임·비전임 신분",
    "country": "소속기관 국가", "nationality": "국적", "phd_date": "박사학위 취득일",
    "first_faculty_date": "최초 조교수 이상 임용일", "birth_date": "생년월일",
    "active_projects": "수행·신청 중 과제", "institution_url": "소속기관 공지 주소",
}
DOCUMENT_KEYS = {
    "cv": ("cv", "이력서"), "research_plan": ("계획서", "제안서"),
    "representative_papers": ("대표논문", "논문실적"),
    "recommendation_letters": ("추천서",),
}


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Seoul")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return KST


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=KST)


def _event_relation(event: dict, as_of: datetime) -> int | None:
    """-1 past, 0 same date/instant, 1 future; imprecise values stay unknown."""
    value = event.get("value")
    if not isinstance(value, str) or not value.strip():
        return None
    if event.get("status") == "unknown" or event.get("validation_error") or event.get("source_conflict"):
        return None
    precision = event.get("precision", "unknown")
    zone = _zone(event.get("timezone"))
    now = _aware(as_of).astimezone(zone)
    try:
        if precision == "date":
            point = date.fromisoformat(value)
            return (point > now.date()) - (point < now.date())
        if precision == "datetime":
            if not re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", value):
                return None
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not point.tzinfo:
                point = point.replace(tzinfo=zone)
            return (point > now) - (point < now)
    except (ValueError, TypeError):
        return None
    return None


def _get(profile: dict, field: str) -> Any:
    current: Any = profile
    for key in field.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _normal(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip().casefold()
    aliases = {
        "전임": "full_time", "전임교원": "full_time", "full-time": "full_time",
        "비전임": "part_time", "비전임교원": "part_time", "part-time": "part_time",
        "대학": "university", "대학교": "university", "대한민국": "kr",
        "한국": "kr", "south korea": "kr", "republic of korea": "kr",
    }
    return aliases.get(text, text)


def _comparable(actual: Any, expected: Any) -> tuple[Any, Any] | None:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return None
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual, expected
    if isinstance(actual, str) and isinstance(expected, str):
        try:
            return date.fromisoformat(actual), date.fromisoformat(expected)
        except ValueError:
            return None
    return None


def _condition(rule: dict, profile: dict) -> dict:
    field = str(rule.get("field") or "")
    label = str(rule.get("label") or FIELD_LABELS.get(field, field) or "신청 조건")
    quote = str(rule.get("evidence_quote") or "")
    result = {"label": label, "status": "needs_review", "reason": "", "evidence_quote": quote}
    if rule.get("operator") == "manual":
        result["reason"] = "원문 조건과 예외를 직접 확인해야 합니다."
        return result
    if not quote.strip():
        result["reason"] = "조건을 뒷받침하는 원문 인용이 없습니다."
        return result
    actual = _get(profile, field)
    if actual is None or actual == "":
        result["reason"] = f"프로필에 {FIELD_LABELS.get(field, label)} 정보가 없습니다."
        return result
    operator, expected = rule.get("operator"), rule.get("value")
    if expected is None and operator != "exists":
        result["reason"] = "원문 조건의 비교값이 구조화되지 않았습니다."
        return result
    matched: bool | None = None
    if operator == "eq":
        matched = _normal(actual) == _normal(expected)
    elif operator == "in" and isinstance(expected, list) and expected:
        matched = _normal(actual) in [_normal(item) for item in expected]
    elif operator == "exists":
        matched = bool(actual) if expected is not False else not bool(actual)
    elif operator in ("lte", "gte"):
        pair = _comparable(actual, expected)
        if pair is not None:
            matched = pair[0] <= pair[1] if operator == "lte" else pair[0] >= pair[1]
    if matched is None:
        result["reason"] = "이 복합 조건이나 비교 방식은 원문과 함께 확인해야 합니다."
    else:
        result["status"] = "met" if matched else "not_met"
        display = json.dumps(actual, ensure_ascii=False)
        result["reason"] = f"프로필 값 {display}: 추출된 조건 {'충족' if matched else '미충족'}."
    return result


def _has_conflict(notice: dict) -> bool:
    metadata = notice.get("extraction_metadata") or {}
    if notice.get("source_conflict") or metadata.get("source_conflict"):
        return True
    if any(event.get("source_conflict") for event in notice.get("events") or []):
        return True
    notes = (notice.get("notes") or []) + (metadata.get("warnings") or []) + (notice.get("missing_information") or [])
    return any("충돌" in str(item) or "불일치" in str(item) for item in notes)


def _task(notice: dict, code: str, title: str, due: str | None, kind: str, reason: str) -> dict:
    notice_id = str(notice.get("id", ""))
    digest = hashlib.sha256(f"{notice_id}|{code}|{due or ''}".encode()).hexdigest()[:12]
    return {
        "id": f"task_{digest}", "notice_id": notice_id, "title": title,
        "due": due, "kind": kind, "reason": reason,
        "source_url": notice.get("source_url", ""),
    }


def _application_state(notice: dict) -> str:
    state = notice.get("application_status", "")
    if isinstance(state, dict):
        state = state.get("status", "")
    return str(state)


def evaluate_notice(notice: dict, profile: dict, as_of: datetime) -> dict:
    """Compare explicit source conditions; never turn unknowns into eligibility."""
    as_of = _aware(as_of)
    events = list(notice.get("events") or [])
    warnings: list[str] = []
    tasks: list[dict] = []
    metadata = notice.get("extraction_metadata") or {}
    conditions = [_condition(rule, profile) for rule in notice.get("eligibility", [])]
    # Apply the specific verified 2027 call rule, rather than treating an active grant as unknown.
    if notice.get("id") in {"msit_2027_core_a", "msit_2027_young_a"}:
        ongoing = [project for project in profile.get("active_projects") or []
                   if project.get("category") == "nrf_individual_basic"
                   and project.get("role") == "PI"
                   and isinstance(project.get("end_date"), str)
                   and re.fullmatch(r"\d{4}-\d{2}-\d{2}", project["end_date"])
                   and project["end_date"] > "2027-12-31"
                   and str(project.get("start_date", "")) <= as_of.date().isoformat()]
        if ongoing:
            conditions.append({
                "label": "현재 개인기초연구 수행에 따른 2027 신규 신청 제한", "status": "not_met",
                "reason": f"{ongoing[0].get('program', '개인기초연구')}를 {ongoing[0]['end_date']}까지 수행 중입니다. 해당 두 유형은 2027년 내 최종종료 예외에 해당하지 않습니다.",
                "evidence_quote": "개인연구를 수행 중인 연구책임자는 개인연구 신규과제 연구책임자로 신청 불가",
            })
    if not conditions:
        conditions.append({"label": "신청자격 확인",
                           "status": "needs_review", "reason": "신청자격이 구조화되지 않았습니다.",
                           "evidence_quote": ""})
    missing = list(notice.get("missing_information") or [])
    incomplete = bool(missing or metadata.get("needs_review") or metadata.get("evidence_errors")
                      or metadata.get("warnings") or metadata.get("eligibility_complete") is False)
    if any(item["status"] == "not_met" for item in conditions):
        eligibility = "ineligible"
    elif incomplete or any(item["status"] != "met" for item in conditions):
        eligibility = "needs_review"
    else:
        eligibility = "eligible"

    conflict = _has_conflict(notice)
    deadlines = [event for event in events if event.get("kind") == "researcher_deadline"]
    if len({str(event.get("value")) for event in deadlines if event.get("value")}) > 1:
        conflict = True
    starts = [event for event in events if event.get("kind") == "application_start"]
    deadline = deadlines[0] if deadlines else None
    status = "unknown"
    if conflict:
        warnings.append("출처 사이의 날짜·조건 충돌이 있어 최종 공고 확인이 필요합니다.")
        if eligibility != "ineligible":
            eligibility = "needs_review"
    elif deadline:
        relation = _event_relation(deadline, as_of)
        if relation == -1:
            status = "closed"
        elif relation is not None:
            start_relations = [_event_relation(event, as_of) for event in starts]
            if any(item == 1 for item in start_relations):
                status = "upcoming"
            elif any(item in (-1, 0) for item in start_relations) or notice.get("application_mode") == "continuous":
                status = "open"
            else:
                warnings.append("접수 시작 여부를 확인하지 못했습니다. 미래 마감만으로 접수 중이라 표시하지 않습니다.")
        else:
            warnings.append("연구자 제출 마감이 일·시각 단위로 확인되지 않았습니다.")
    else:
        warnings.append("연구자 제출 마감이 확인되지 않았습니다.")
    if metadata.get("error"):
        warnings.append(f"추출 오류: {metadata['error']}")
    warnings.extend(str(item) for item in metadata.get("warnings") or [])
    if not deadline and any(event.get("kind") == "institution_deadline" for event in events):
        warnings.append("기관승인 기한을 연구자 제출 마감으로 대체하지 않았습니다.")

    profile_terms = [str(word).casefold() for word in profile.get("keywords", []) if str(word).strip()]
    notice_terms = " ".join(str(word) for word in notice.get("keywords", [])).casefold()
    topic_matches = [word for word in profile.get("keywords", []) if str(word).casefold() in notice_terms]
    if not profile_terms:
        warnings.append("연구 키워드가 없어 연구주제 비교를 하지 못했습니다.")

    notice_id = str(notice.get("id", ""))
    internal = (profile.get("internal_deadlines") or {}).get(notice_id)
    internal_event = {"value": internal, "precision": "datetime" if internal and "T" in str(internal) else "date", "timezone": "Asia/Seoul"}
    internal_past = bool(internal and _event_relation(internal_event, as_of) == -1)
    state = _application_state(notice)
    applied = state in {"submitted", "institution_approved", "awaiting_result", "shortlisted", "document_passed", "제출", "기관승인", "결과대기", "서면통과"}
    shortlisted = state in {"shortlisted", "document_passed", "서면통과"}
    terminal = state in {"selected", "rejected", "on_hold", "hold", "선정", "미선정", "보류"}
    active = status != "closed" and eligibility != "ineligible" and not terminal
    initial_phase = not applied and not terminal
    due = internal or (deadline.get("value") if deadline and not conflict else None)
    if internal_past and active and initial_phase:
        warnings.append("등록된 대학 내부마감이 지났습니다. 외부마감으로 내부마감을 덮어쓰지 않았습니다.")
        tasks.append(_task(notice, "internal_recovery", "대학 내부마감 경과 후 접수 가능 여부 확인", None, "verification", "산학협력단에 내부 절차와 현재 처리 가능 여부를 확인합니다."))
    if active and initial_phase:
        if not internal:
            tasks.append(_task(notice, "internal_deadline", "대학 내부 제출기한·기관 절차 확인", None, "verification", "등록된 내부마감이 없습니다. 해당 사업의 기관 제출·예산검토 절차를 확인합니다."))
        for item in conditions:
            if item["status"] == "needs_review":
                tasks.append(_task(notice, f"condition_{item['label']}", f"자격 확인: {item['label']}", None, "verification", item["reason"]))
        for item in missing:
            tasks.append(_task(notice, f"missing_{item}", f"추가 확인: {item}", None, "verification", "추출 결과에 확인이 필요한 정보로 남았습니다."))
        if conflict:
            tasks.append(_task(notice, "source_conflict", "정정공고·최종 신청요강에서 충돌 해소", None, "verification", "서로 다른 출처의 날짜·조건을 자동으로 합치지 않습니다."))
        if not internal_past:
            for document in notice.get("required_documents") or []:
                document = str(document)
                if any(term in document for term in ("1차 합격", "1차합격", "합격 후", "통과 후", "발표심사")) and not shortlisted:
                    continue
                doc_key = next((key for key, terms in DOCUMENT_KEYS.items() if any(term in document.casefold() for term in terms)), document)
                available = (profile.get("documents") or {}).get(doc_key)
                if available is not True:
                    title = f"{document} {'준비' if available is False else '보유 여부 확인·준비'}"
                    tasks.append(_task(notice, f"document_{document}", title, due, "preparation", "해당 회차 양식·제출 요건에 맞는 자료를 확인합니다."))
            if deadline and not conflict and _event_relation(deadline, as_of) in (0, 1):
                tasks.append(_task(notice, "researcher_submit", "연구자 제출 완료 확인", due, "official", "자격과 제출서류를 확인한 뒤 연구자 마감까지 제출을 완료합니다."))
    for event in events:
        kind, value = event.get("kind"), event.get("value")
        label = str(event.get("label") or kind)
        if kind == "guidelines_release" and active and initial_phase:
            tasks.append(_task(notice, f"guidelines_{label}", "세부 신청요강·서식 공개 여부 확인", value, "verification", "공고에 안내된 공개 예정 일정에 원문을 다시 확인합니다."))
        elif kind == "institution_deadline" and applied and state not in {"institution_approved", "기관승인"}:
            tasks.append(_task(notice, f"institution_{label}", "주관연구기관 승인 상태 확인", value, "official", "연구자 제출 완료와 기관승인 완료는 별도입니다."))
        elif kind == "followup_deadline" and shortlisted:
            tasks.append(_task(notice, f"followup_{label}", str(event.get("label") or "후속 심사 서류 준비·제출"), value, "official", "사용자가 확인한 서면통과 상태에 따른 후속 단계입니다."))
        elif kind == "result_expected" and applied:
            tasks.append(_task(notice, f"result_{label}", "공식 통지 경로에서 심사결과 확인", value, "verification", "발표 예정 일정이며 실제 선정 여부는 사용자 확인 후 기록합니다."))
    if applied and not any(event.get("kind") == "result_expected" for event in events):
        tasks.append(_task(notice, "result_unknown", "결과 발표 일정·통지 경로 확인", None, "verification", "현재 원문에서 결과 발표일을 확인하지 못했습니다."))
    return {
        "notice_id": notice_id, "status": status, "eligibility": eligibility,
        "conditions": conditions, "topic_matches": topic_matches, "events": events,
        "tasks": tasks, "warnings": list(dict.fromkeys(warnings)),
    }
