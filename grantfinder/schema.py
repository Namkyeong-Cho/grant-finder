"""Structured extraction and evidence checks independent of the model."""

import copy
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

PROMPT_VERSION = "2026-11-06-v3"
EVENT_KINDS = ["application_start", "researcher_deadline", "institution_deadline", "result_expected",
               "guidelines_release", "followup_deadline"]


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string", "maxLength": 180}
STRINGS = {"type": "array", "items": STRING, "maxItems": 4}
DATE_ITEM = obj({
    "label": STRING, "value": {"type": ["string", "null"]},
    "precision": {"type": "string", "enum": ["datetime", "date", "month", "week", "relative", "unknown"]},
    "timezone": STRING, "evidence_quote": STRING,
})
EXTRACTION_SCHEMA = obj({
    "application_mode": {"type": "string", "enum": ["continuous", "window", "unknown"]},
    "application_start": DATE_ITEM, "researcher_deadline": DATE_ITEM,
    "institution_deadline": DATE_ITEM, "guidelines_release": DATE_ITEM,
    "result_expected": {"type": "array", "items": DATE_ITEM, "maxItems": 2},
    "followup_deadlines": {"type": "array", "items": DATE_ITEM, "maxItems": 2},
    "eligibility": {"type": "array", "maxItems": 4, "items": obj({
        "field": STRING, "operator": {"type": "string", "enum": ["eq", "in", "lte", "gte", "exists", "manual"]},
        "value": {"type": ["string", "number", "boolean", "array", "null"], "items": STRING},
        "label": STRING, "evidence_quote": STRING,
    })},
    "required_documents": STRINGS, "keywords": STRINGS, "notes": STRINGS, "missing_information": STRINGS,
})


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def unpack_response(raw: dict) -> dict:
    """Fixed field names avoid confusing start/deadline enum labels in small models."""
    output = copy.deepcopy(raw)
    events, seen = [], set()
    labels = {"application_start": "접수 시작", "researcher_deadline": "연구자 제출 마감",
              "institution_deadline": "기관승인 마감", "guidelines_release": "세부요강 공개 예정",
              "result_expected": "심사결과 발표 예정", "followup_deadline": "후속 서류 마감"}
    for field in ("application_start", "researcher_deadline", "institution_deadline", "guidelines_release",
                  "result_expected", "followup_deadlines"):
        items = raw.get(field, [])
        items = items if isinstance(items, list) else [items]
        for item in items:
            if not isinstance(item, dict) or not item.get("value"):
                continue
            event = copy.deepcopy(item)
            value = str(event["value"]).strip()
            match = re.fullmatch(r"(20\d{2})[./\-]\s*(\d{1,2})[./\-]\s*(\d{1,2})\.?", value)
            if match and event.get("precision") in ("date", "datetime"):
                # A date with no source time must remain a date, even if the model chose datetime.
                event["value"] = "-".join([match[1], match[2].zfill(2), match[3].zfill(2)])
                event["precision"] = "date"
            event["kind"] = "followup_deadline" if field == "followup_deadlines" else field
            quote = event.get("evidence_quote", "")
            if event.get("precision") in ("date", "datetime") and not re.search(r"\d{1,2}:\d{2}", quote):
                try:
                    point = datetime.fromisoformat(str(event["value"]).replace("Z", "+00:00"))
                    if (point.hour, point.minute, point.second) == (0, 0, 0):
                        event.update(value=point.date().isoformat(), precision="date")
                except ValueError:
                    pass
            lower_quote = quote.casefold()
            # Explicit source wording takes precedence over a misassigned model field.
            if "deadline for submissions" in lower_quote and "after" not in lower_quote:
                event["kind"] = "researcher_deadline"
            elif "we will open" in lower_quote or "submissions open" in lower_quote:
                event["kind"] = "application_start"
            if event.get("precision") in ("month", "week", "relative"):
                # Never display an invented day inferred from an approximate interval.
                event["value"] = quote
                if "months after" in lower_quote or re.search(r"개월\s*후", quote):
                    event["precision"] = "relative"
            event["status"] = "planned" if event["kind"] in ("result_expected", "guidelines_release") else "announced"
            if event.get("label") in EVENT_KINDS:
                event["label"] = labels[event["kind"]]
            identity = (event["kind"], event.get("value"))
            if identity in seen:
                continue
            seen.add(identity)
            events.append(event)
    output["events"] = events
    return output


def _date_has_evidence(value: str, precision: str, quote: str, source: str, timezone="Asia/Seoul") -> bool:
    """Check that the ISO date/time actually occurs in its quoted source context."""
    target = datetime.fromisoformat(value.replace("Z", "+00:00")) if precision == "datetime" else date.fromisoformat(value)
    if precision == "datetime" and target.tzinfo:
        target = target.astimezone(ZoneInfo(timezone))
    pairs = [(int(m), int(d)) for m, d in re.findall(r"(?<!\d)(?=(\d{1,2})\s*[.월/\-]\s*(\d{1,2})(?!\d))", quote)]
    months = "january february march april may june july august september october november december".split()
    for index, month in enumerate(months, 1):
        for day in re.findall(r"\b" + month + r"\s+(\d{1,2})\b", quote, flags=re.I):
            pairs.append((index, int(day)))
    if (target.month, target.day) not in pairs:
        return False
    years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", quote)
    years += ["20" + year for year in re.findall(r"[‘’'](\d{2})\s*[.년/\-]", quote)]
    if years and str(target.year) not in years:
        return False
    if not years and str(target.year) not in source and not re.search(r"[‘’']" + str(target.year)[2:] + r"\s*[.년/\-]", source):
        return False
    if precision == "datetime":
        times = []
        for hour, minute, meridian in re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?", quote, re.I):
            hour = int(hour)
            if meridian:
                hour = hour % 12 + (12 if meridian.lower() == "pm" else 0)
            times.append((hour, int(minute)))
        if (target.hour, target.minute) not in times:
            return False
    return True


def validate_extraction(raw: dict, text: str, seed: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("모델 출력이 JSON 객체가 아닙니다.")
    notice = {key: copy.deepcopy(seed.get(key)) for key in
              ("id", "source_id", "title", "sponsor", "year", "track", "source_url")}
    notice.update({"application_mode": raw.get("application_mode", "unknown"), "events": [], "eligibility": []})
    for field in ("required_documents", "keywords", "notes", "missing_information"):
        value = raw.get(field, [])
        notice[field] = [str(item) for item in value] if isinstance(value, list) else []
    source = normalize(text)
    for event in raw.get("events", []):
        if not isinstance(event, dict) or event.get("kind") not in EVENT_KINDS:
            continue
        event = copy.deepcopy(event)
        event["source_url"] = seed["source_url"]
        quote = normalize(event.get("evidence_quote", ""))
        problem = None
        if not quote or quote not in source:
            problem = "원문에서 인용 근거를 찾지 못함"
        elif event["kind"] in ("application_start", "guidelines_release") and (
                re.search(r"(?:작성일|등록일|게시일)", event.get("evidence_quote", ""))
                or re.search(r"(?:작성일|등록일|게시일)\s*" + re.escape(event.get("evidence_quote", "").strip()), text)):
            problem = "게시일을 접수 시작·요강 공개일로 대체하지 않음"
        elif event.get("precision") in ("date", "datetime") and event.get("value"):
            try:
                value = str(event["value"])
                if event["precision"] == "date":
                    date.fromisoformat(value)
                else:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    ZoneInfo(event.get("timezone", "Asia/Seoul"))
                if not _date_has_evidence(value, event["precision"], event.get("evidence_quote", ""), text,
                                          event.get("timezone", "Asia/Seoul")):
                    problem = "날짜·시각 값이 원문 인용과 일치하지 않음"
            except (ValueError, KeyError):
                problem = "날짜 형식 또는 시간대 검증 실패"
        if problem:
            event["raw_value"] = event.get("value")
            notice["missing_information"].append(f"{event.get('label', event['kind'])}: {problem}")
            event.update(value=None, precision="unknown", status="unknown", validation_error=problem)
        notice["events"].append(event)
    for condition in raw.get("eligibility", []):
        if not isinstance(condition, dict):
            continue
        condition = copy.deepcopy(condition)
        quote = normalize(condition.get("evidence_quote", ""))
        if not quote or quote not in source:
            condition["operator"] = "manual"
            condition["validation_error"] = "원문에서 자격 조건의 인용 근거를 찾지 못함"
            notice["missing_information"].append(condition["validation_error"])
        notice["eligibility"].append(condition)
    if not notice["events"]:
        notice["missing_information"].append("일정 추출 결과 없음: 원문·첨부 확인 필요")
    if not notice["eligibility"]:
        notice["missing_information"].append("지원자격 추출 결과 없음: 원문·첨부 확인 필요")
    notice["missing_information"] = list(dict.fromkeys(notice["missing_information"]))
    return notice


def crosscheck_reviewed(notice: dict, reference: dict) -> dict:
    """Flag starter-data disagreements; never replace a model result with an answer key."""
    expected = {(e["kind"], e.get("label", "")): e for e in reference.get("events", [])}
    # Event labels may differ, so compare unique high-priority kinds only.
    for kind in ("application_start", "researcher_deadline", "institution_deadline", "guidelines_release"):
        refs = [e for e in expected.values() if e["kind"] == kind and e.get("value")]
        actual = [e for e in notice.get("events", []) if e["kind"] == kind and e.get("value")]
        if len(refs) == 1 and not actual:
            notice["missing_information"].append(f"검토 정답표의 {kind} 일정이 추출되지 않음")
        elif len(refs) == 1 and len(actual) == 1:
            left, right = str(refs[0]["value"]), str(actual[0]["value"])
            try:
                a = datetime.fromisoformat(left)
                b = datetime.fromisoformat(right)
                different = a != b if a.tzinfo and b.tzinfo else a.isoformat() != b.isoformat()
            except ValueError:
                different = left != right
            if different:
                actual[0]["source_conflict"] = True
                notice["missing_information"].append(
                    f"출처 충돌/검토 필요: {kind} 모델={right}, 이전 검토자료={left}. 최신 원문 확인")
    return notice
