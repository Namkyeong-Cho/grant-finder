"""Classify saved deadlines without inventing dates or changing saved tracks."""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SEOUL = ZoneInfo("Asia/Seoul")
DEADLINE_ALIASES = ("deadline", "researcher_deadline", "application_deadline")
LABELS = {"open": "마감 전", "closed": "마감됨", "unconfirmed": "마감 미확인"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,6})?)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):?[0-5]\d)")
_NAIVE_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,6})?)?")
_TIMING_DATE = re.compile(
    r"(?<!\d)(?:"
    r"(?P<iso_year>\d{4})[-/](?P<iso_month>\d{1,2})(?:[-/](?P<iso_day>\d{1,2}))?|"
    r"(?:(?P<year>\d{4})\s*년?\s*)?(?P<month>\d{1,2})\s*"
    r"(?:[~～–-]\s*(?P<last_month>\d{1,2})\s*)?월(?:\s*(?P<day>\d{1,2})\s*일)?)(?!\d)")
_SENTENCE = re.compile(r"[。;\n]|(?<!\d)\.(?!\d)")
_CLAUSE = re.compile(r"[,，→;]")
_UNRELATED_TIMING = re.compile(r"결과|선정|결정|통보|면접|연구\s*(?:시작|개시|기간)|수행|협약|과거|이력")
_UNKNOWN_TIMING = re.compile(r"미정|미확정|미확인|미발표|미공개|미공고|확인\s*필요|여부")
_ROLLING = re.compile(
    r"(?:상시|수시|연중)\s*(?:접수|모집|신청)|(?:접수|모집|신청)\s*(?:상시|수시|연중)|"
    r"\b(?:rolling\s+(?:applications?|admissions?|submissions?)|"
    r"(?:applications?|proposals?)\s+(?:are\s+)?accepted\s+(?:on\s+a\s+)?rolling\s+basis|"
    r"year[- ]round\s+(?:applications?|submissions?))\b", re.I)
_ROLLING_UNCERTAIN = re.compile(
    r"아님|아닙|아니|불가|중단|종료|미확인|여부|예정|확인\s*필요|하지\s*않|"
    r"\b(?:not|no\s+longer|closed|suspended|unconfirmed|tbd)\b", re.I)


def _now():
    return datetime.now(SEOUL)


def _moment(now):
    moment = _now() if now is None else now
    if not isinstance(moment, datetime):
        raise TypeError("now는 datetime이어야 합니다.")
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=SEOUL)


def _deadline_record(track):
    for alias in DEADLINE_ALIASES:
        if track.get(alias):
            return track[alias], alias
    events = track.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and (event.get("kind") or event.get("type")) in DEADLINE_ALIASES:
                return event, event.get("kind") or event.get("type")
    elif isinstance(events, dict):
        for alias in DEADLINE_ALIASES:
            if events.get(alias):
                return events[alias], alias
    return None, "deadline"


def _point(record):
    if isinstance(record, list):
        for item in record:
            value, metadata = _point(item)
            if value:
                return value, metadata
        return None, {}
    if isinstance(record, dict):
        value = record.get("value") or record.get("date")
        return (value if isinstance(value, str) else None), record
    return (record if isinstance(record, str) else None), {}


def _rolling(track):
    # These are stored timing descriptions; an unsubstantiated `continuous` flag
    # or a generic `open` stage alone is not enough to create a deadline status.
    text = track.get("application_timing")
    return bool(isinstance(text, str) and _ROLLING.search(text) and not _ROLLING_UNCERTAIN.search(text))


def _forecast(result, moment, year, month, period, kind, source):
    """Use calendar-month distance for planning, retaining the original deadline."""
    current = moment.astimezone(SEOUL)
    if (year - current.year) * 12 + month - current.month < 2:
        return result
    return dict(result, status="open", label=LABELS["open"], estimated=True,
                expected_period=period, expected_kind=kind, expected_source=source)


def _coarse_period(value, precision):
    if re.fullmatch(r"\d{4}-\d{2}", value) and precision in (None, "", "month"):
        year, month = map(int, value.split("-"))
        date(year, month, 1)
        return year, month, value
    if re.fullmatch(r"\d{4}", value) and precision in (None, "", "year"):
        year = int(value)
        date(year, 1, 1)
        return year, 1, value
    quarter = re.fullmatch(r"(\d{4})-Q([1-4])", value, re.I)
    if quarter and precision in (None, "", "quarter"):
        year, number = map(int, quarter.groups())
        date(year, 1, 1)
        return year, (number - 1) * 3 + 1, value
    return None


def _timing_kind(context, known_month=False):
    if _UNRELATED_TIMING.search(context):
        return None
    # Missing exact days may coexist with a published application month.
    if known_month:
        context = re.sub(r"(?:정확한\s*|확정\s*)?(?:일자|시각|날짜|마감일|접수일(?!정))"
                         r"\s*(?:은|는|이|가)?\s*(?:아직\s*)?(?:미정|미확정|미확인|미공개|미공고)", "", context)
    if _UNKNOWN_TIMING.search(context):
        return None
    if re.search(r"마감|종료|기한|까지\s*(?:접수|신청)", context):
        return "deadline"
    if re.search(r"접수|신청", context):
        return "application"
    if re.search(r"공고|공모", context):
        return "announcement"
    if "모집" in context:
        return "application"
    return None


def _timing_forecast(track, moment, result):
    text = track.get("application_timing")
    if not isinstance(text, str):
        return result
    current = moment.astimezone(SEOUL)
    candidates = []
    for sentence in _SENTENCE.split(text):
        matches = list(_TIMING_DATE.finditer(sentence))
        inherited_year = None
        for index, match in enumerate(matches):
            range_end = index
            while (range_end + 1 < len(matches) and
                   re.fullmatch(r"\s*[~～–-]\s*", sentence[matches[range_end].end():matches[range_end + 1].start()])):
                range_end += 1
            tail = matches[range_end]
            start = max([0] + [part.end() for part in _CLAUSE.finditer(sentence[:match.start()])])
            following = _CLAUSE.search(sentence, tail.end())
            end = following.start() if following else len(sentence)
            if range_end + 1 < len(matches):
                end = min(end, matches[range_end + 1].start())
            context = sentence[start:match.start()] + " " + sentence[tail.end():end]
            year_text = match.group("iso_year") or match.group("year")
            month = int(match.group("iso_month") or match.group("month"))
            last_month = int(match.group("last_month") or month)
            year = int(year_text) if year_text else inherited_year
            if year is None and re.search(r"매년|해마다", context):
                if last_month >= month:
                    year = current.year + (current.month > last_month)
                else:
                    year = current.year - (current.month <= last_month)
            if year is None:
                continue
            try:
                first_day = date(year, month, int(match.group("iso_day") or match.group("day") or 1))
                date(year, last_month, 1)
                for related in matches[index + 1:range_end + 1]:
                    related_month = int(related.group("iso_month") or related.group("month"))
                    related_year_text = related.group("iso_year") or related.group("year")
                    related_year = int(related_year_text) if related_year_text else year + (related_month < month)
                    related_day = date(related_year, related_month,
                                       int(related.group("iso_day") or related.group("day") or 1))
                    if related_day < first_day:
                        raise ValueError("Invalid forecast period")
            except ValueError:
                continue
            # Only a calendar date establishes a year for following month clauses.
            # A programme label such as '2027년 1차' cannot establish that year.
            if year_text:
                inherited_year = year
            if index and re.fullmatch(r"\s*[~～–-]\s*", sentence[matches[index - 1].end():match.start()]):
                continue
            kind = _timing_kind(context, known_month=True)
            if kind and (year, month) >= (current.year, current.month):
                candidates.append((year, month, f"{year:04d}-{month:02d}", kind))
        if not matches:
            for match in re.finditer(r"(?<!\d)(\d{4})\s*년?([^\d,;。]{0,18}(?:공고|공모|접수|신청|모집)[^\d,;。]*)", sentence):
                year = int(match.group(1))
                kind = _timing_kind(match.group(2))
                if 1 <= year <= 9999 and kind and year > current.year:
                    candidates.append((year, 1, str(year), kind))
    if not candidates:
        return result
    priority = {"deadline": 0, "application": 1, "announcement": 2}
    year, month, period, kind = min(candidates, key=lambda item: (priority[item[3]], item[0], item[1]))
    return _forecast(result, moment, year, month, period, kind, "application_timing")


def classify_track_deadline(track, now=None):
    """Return open/closed/unconfirmed; `open` means the deadline has not passed.

    Exact dates use their source timezone; exact instants require a UTC offset.
    Approximate application periods at least two calendar months away are included
    for planning. The original deadline and saved tracks remain unchanged.
    """
    moment = _moment(now)
    track = track if isinstance(track, dict) else {}
    record, alias = _deadline_record(track)
    original, metadata = _point(record)
    value = original.strip() if original else ""
    result = {"status": "unconfirmed", "label": LABELS["unconfirmed"], "deadline": original}
    if not value:
        if _rolling(track):
            result.update(status="open", label="상시 모집")
            return result
        return _timing_forecast(track, moment, result)
    precision = metadata.get("precision")
    if metadata.get("status") == "unknown" or metadata.get("validation_error"):
        return result
    try:
        if _DATE.fullmatch(value) and precision in (None, "", "date"):
            day = date.fromisoformat(value)
            zone = metadata.get("timezone") or track.get(alias + "_timezone") or track.get("deadline_timezone") or track.get("timezone")
            if not isinstance(zone, str) or not zone:
                return _forecast(result, moment, day.year, day.month, value[:7], "deadline", "deadline")
            expired = moment.astimezone(ZoneInfo(zone)).date() > day
        elif _DATETIME.fullmatch(value) and precision in (None, "", "datetime"):
            deadline = datetime.fromisoformat(value.replace("t", "T").replace("z", "+00:00").replace("Z", "+00:00"))
            expired = moment > deadline
        elif _NAIVE_DATETIME.fullmatch(value) and precision in (None, "", "datetime"):
            deadline = datetime.fromisoformat(value)
            return _forecast(result, moment, deadline.year, deadline.month, value[:7], "deadline", "deadline")
        else:
            period = _coarse_period(value, precision)
            if period:
                return _forecast(result, moment, *period, "deadline", "deadline")
            return result
    except (ValueError, ZoneInfoNotFoundError):
        return result
    status = "closed" if expired else "open"
    return {"status": status, "label": LABELS[status], "deadline": original}


def application_deadline_statuses(applications, now=None):
    """Build runtime-only classifications for the selected tracks in each plan."""
    moment = _moment(now)
    output = {}
    for application in applications:
        selected = application.get("selected_track_ids") or []
        tracks = {track["id"]: classify_track_deadline(track, moment)
                  for track in application.get("tracks", [])
                  if isinstance(track, dict) and isinstance(track.get("id"), str)
                  and (not selected or track["id"] in selected)}
        values = [item["status"] for item in tracks.values()]
        status = "open" if "open" in values else "closed" if values and all(value == "closed" for value in values) else "unconfirmed"
        output[application["id"]] = {"status": status, "label": LABELS[status], "tracks": tracks}
    return output
