"""CV-based, evidence-linked opportunity catalog. Research data is not a live crawl."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

CATEGORIES = {"government": "국내 정부·공공", "corporate": "기업 연구지원", "foundation": "민간재단",
              "international": "국제협력·펠로십", "visiting_math": "수학 방문연구", "institutional": "소속기관·지역협력"}
AVAILABILITY = {"open": "현재 모집 확인", "announced": "공식 일정 발표", "rolling": "수시 신청",
                "future_watch": "향후 모집 관찰", "closed": "최근 회차 마감", "unknown": "모집 여부 확인", "excluded": "현재 제외"}
ROUTES = {"direct": "개인·연구책임자 신청", "collaboration": "공동연구·컨소시엄", "institution": "소속기관 신청·추천", "host_required": "해외 호스트·체류 필요"}
SUPPORT = {"research_grant": "연구비", "equipment": "장비·인프라", "compute": "GPU·계산자원", "fellowship": "펠로십",
           "visit": "방문연구", "workshop": "공동연구 모임", "collaborative": "공동연구비", "award": "시상", "cloud": "클라우드 크레딧"}
SUPPORT.update({"exchange":"연구자·학생 교류", "contract":"위탁·용역 연구"})


def safe_url(value):
    try:
        parsed = urlsplit(str(value or ""))
        return parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def priority_key(item):
    availability = {"open": 0, "rolling": 1, "announced": 2, "future_watch": 3, "unknown": 4, "closed": 5, "excluded": 6}
    fit = {"high": 0, "medium": 1, "conditional": 2, "excluded": 3}
    return (item.get("fit") == "excluded", fit.get(item.get("fit"), 2),
            availability.get(item.get("availability"), 4), item.get("route") != "direct",
            str(item.get("deadline") or "9999"), item.get("title", ""))


def load_catalog(directory: Path, profile: dict, registry_path=None):
    from .source_registry import load_registry, resolve_links
    registry_path = Path(registry_path) if registry_path else directory.parent / "sources.json"
    registry = load_registry(registry_path) if registry_path.exists() else {"pages": []}
    candidates, seen, sources = [], set(), []
    required = ("id", "title", "sponsor", "category", "support_type", "route", "availability", "fit",
                "fit_reason", "eligibility_summary", "application_timing", "next_action", "source_url", "checked_on")
    for path in sorted(directory.glob("*_candidates.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        sources.append(path.name)
        for original in data.get("candidates", []):
            item = resolve_links(original, registry)
            missing = [key for key in required if not item.get(key)]
            if missing:
                raise ValueError(f"{path.name}: 후보 필수 항목 누락 {item.get('id')}: {', '.join(missing)}")
            if item["id"] in seen:
                raise ValueError(f"중복 후보 ID: {item['id']}")
            if not safe_url(item["source_url"]):
                raise ValueError(f"후보 원문 URL 확인 필요: {item['id']}")
            if item["availability"] not in AVAILABILITY or item["route"] not in ROUTES:
                raise ValueError(f"후보 모집상태·신청경로 확인 필요: {item['id']}")
            date.fromisoformat(item["checked_on"])
            item.setdefault("personal_constraints", [])
            item.setdefault("additional_sources", [])
            item.setdefault("deadline", None)
            seen.add(item["id"])
            candidates.append(item)
    candidates.sort(key=priority_key)
    from .program_groups import load_program_groups, group_records
    definitions = load_program_groups(directory.parent / "program_groups.json")
    programs = group_records(candidates, "candidate", definitions)
    return {"profile_id": profile.get("id"), "profile_name": profile.get("name"),
            "checked_from": min((x["checked_on"] for x in candidates), default="2026-11-06"),
            "checked_on": max((x["checked_on"] for x in candidates), default="2026-11-06"),
            "method": "공식 지원기관 자료를 정리한 공통 후보 목록. 개인별 적합성·신청자격은 별도 확인", "source_files": sources,
            "candidates": candidates, "program_groups": definitions, "program_count": len(programs),
            "counts": {"total": len(candidates), "current": sum(x["availability"] in {"open", "rolling", "announced"} and x["fit"] != "excluded" for x in candidates),
                       "future": sum(x["availability"] in {"future_watch", "closed"} and x["fit"] != "excluded" for x in candidates),
                       "excluded": sum(x["fit"] == "excluded" or x["availability"] == "excluded" for x in candidates)}}


def catalog_markdown(catalog, profile):
    lines = [f"# {profile.get('name','연구자')}: CV 기반 연구지원 후보 조사", "",
             f"자료 확인: {catalog.get('checked_from', catalog['checked_on'])} ~ {catalog['checked_on']} · {catalog['method']}", "",
             f"소속: {profile.get('affiliation')} · 박사 {profile.get('phd_date')} · 최초 조교수 임용 {profile.get('first_faculty_date')}",
             "수행 과제: " + "; ".join(f"{p.get('program', p.get('title','과제'))} ({p.get('start_date','시작 미확인')}~{p.get('end_date','종료 미확인')})" for p in profile.get('active_projects') or []),
             "사용자 확인: " + "; ".join(x.get('statement','') for x in profile.get('user_confirmations',[])), "",
             "모집 중이라는 사실과 개인별 최종 신청자격은 구분합니다. 연구비·방문연구·계산자원도 별도로 표시합니다.", ""]
    from .program_groups import group_records
    programs = group_records(catalog['candidates'], 'candidate', catalog.get('program_groups'))
    lines.extend([f"지원사업 {len(programs)}개 · 세부 전형 {len(catalog['candidates'])}개", ""])
    for group in programs:
        lines.extend([f"## {group['title']} · {len(group['items'])}개 전형", ""])
        for item in group['items']:
            lines.extend([f"### {group['track_labels'].get(item['id'],item['title'])}", "",
                          f"- 기관: {item['sponsor']} · 지원: {SUPPORT.get(item['support_type'],item['support_type'])} · 경로: {ROUTES[item['route']]}",
                          f"- 모집: {AVAILABILITY[item['availability']]} · 일정: {item['application_timing']}",
                          f"- 연구 연결: {item['fit_reason']}", f"- 자격: {item['eligibility_summary']}"])
            lines.extend(f"- 개인별 제약: {constraint}" for constraint in item['personal_constraints'])
            lines.extend([f"- 할 일: {item['next_action']}", f"- 근거: [{item['sponsor']}]({item['source_url']})", ""])
    return "\n".join(lines)
