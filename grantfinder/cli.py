"""CLI orchestration for repeatable classroom and personal use."""

import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .llm import OllamaClient
from .schema import PROMPT_VERSION, crosscheck_reviewed, unpack_response, validate_extraction
from .storage import changes_since, fingerprint, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")


def clock(value=None):
    if not value:
        return datetime.now(KST)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)


def resolve(path):
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def records(path, field):
    value = read_json(path, {})
    value = registry_links(value)
    return value if isinstance(value, list) else value.get(field, [])


def registry_links(value):
    from .source_registry import load_registry, resolve_links
    path = ROOT / "data/sources.json"
    registry = load_registry(path) if path.exists() else {"pages": []}
    return resolve_links(value, registry)


def sources_command(args):
    from .source_registry import load_registry
    registry = load_registry(ROOT / "data/sources.json")
    query = args.query.casefold()
    sources = [source for source in registry["sources"] if not query or query in
               json.dumps(source, ensure_ascii=False).casefold()]
    ids = {source["id"] for source in sources}
    pages = [page for page in registry["pages"] if page["source_id"] in ids]
    if args.json:
        print(json.dumps({"sources": sources, "pages": pages}, ensure_ascii=False, indent=2))
    else:
        print(f"사이트 {len(sources)}개 · 공식 페이지 {len(pages)}개 · 자동수집 {sum(bool(s.get('enabled')) for s in sources)}곳")
        for source in sources:
            state = "자동수집" if source.get("enabled") else "수동 확인"
            print(f"{source['id']} | {source['name']} | {state}\n  {source.get('url') or 'URL 미입력'}")
    return 0


def load_progress():
    return read_json(ROOT / "data/progress.json", {"completed_tasks": [], "applications": {}})


def application_records(profile):
    from .application_history import load_history
    return load_history(ROOT / "data/application_history.json", profile.get("id"))


def application_history_appendix(profile):
    from .application_history import history_markdown
    return history_markdown(application_records(profile))


def active_profile_path():
    return read_json(ROOT / "data/settings.json", {}).get("active_profile", "data/profile.example.json")


def catalog_appendix():
    seasons = registry_links(read_json(ROOT / "data/seasons.json", {}))
    lines = ["", "## 공고 예정·통상 시기 참고", "", seasons.get("basis", ""), "",
             "| 사업 | 공고·접수 시기 | 근거 성격 | 확인할 일 |", "| --- | --- | --- | --- |"]
    for item in seasons.get("items", []):
        lines.append(f"| [{item['program']}]({item['source_url']}) | {item['timing']} | {item['evidence_type']} | {item['action']} |")
    discovery = read_json(ROOT / "outputs/discovered.json", {})
    if discovery:
        lines.extend(["", "## 목록에서 발견한 공고 링크", "",
                      f"목록 확인 실행: {discovery.get('checked_at', '미확인')}. 아래는 탐색 후보입니다. 지원자격·최종 마감 분석을 마친 목록이 아닙니다.", "",
                      "| 제목 | 제목·URL로 본 유형 |", "| --- | --- |"])
        for item in discovery.get("candidates", []):
            title = item["title"].replace("|", "\\|")
            if "선정결과" in title:
                kind = "결과 공지"
            elif "장학" in title:
                kind = "장학·교수 연구비 대상 여부 확인"
            elif any(word in title for word in ("공모전", "수상", "지정계획")):
                kind = "시상·지정 등, 연구비 공모 여부 확인"
            elif any(word in title for word in ("수정", "정정", "연장")):
                kind = "수정·정정 후보"
            else:
                kind = "연구지원 후보·본문 확인 필요"
            lines.append(f"| [{title}]({item['url']}) | {kind} |")
        lines.append("")
    return "\n".join(lines)


def doctor(args):
    client = OllamaClient(args.model)
    version = client.request("version")
    models = client.request("tags").get("models", [])
    client.ensure_local_model()
    print(json.dumps({"grant_finder": __version__, "python": sys.version.split()[0], "host": client.host,
                      "ollama": version.get("version"), "selected_model": client.model,
                      "installed_models": [m["name"] for m in models], "capabilities": client.capabilities,
                      "local_only": True}, ensure_ascii=False, indent=2))
    return 0


def serve_command(args):
    from .server import serve
    return serve(ROOT, port=args.port, model=args.model, open_browser=args.open)


def discover_command(args):
    from .collectors import discover, load_sources
    candidates, statuses = [], []
    for source in load_sources(ROOT / "data/sources.json"):
        if not source.get("enabled"):
            continue
        if not source.get("discovery_url") and source.get("kind") != "listing":
            statuses.append({"source_id": source["id"], "status": "not_configured", "found": None,
                             "note": "공고 본문만 수집하며 신규 목록 탐색은 설정되지 않았습니다."})
            continue
        try:
            links = discover(source, ROOT / "cache/collections", refresh=args.refresh)
            candidates.extend(dict(item, source_id=source["id"]) for item in links)
            statuses.append({"source_id": source["id"], "status": "ok", "found": len(links)})
        except Exception as error:
            statuses.append({"source_id": source["id"], "status": "error", "error": str(error)})
    output = {"checked_at": clock().isoformat(), "candidates": candidates, "sources": statuses}
    write_json(ROOT / "outputs/discovered.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if any(s["status"] == "ok" for s in statuses) else 1


def write_html_report(notices, evaluations, profile, as_of, changes, statuses, progress, summary):
    from .html_report import render_html_report
    from .program_groups import load_program_groups
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    document = render_html_report(
        notices, evaluations, profile, as_of, changes, statuses, progress,
        summary=summary, seasons=registry_links(read_json(ROOT / "data/seasons.json", {})),
        discovery=read_json(output / "discovered.json", {}),
        catalog=registry_links(read_json(output / "funding_catalog.json", {})),
        application_history=application_records(profile),
        program_groups=load_program_groups(ROOT / "data/program_groups.json"),
    )
    path = output / "latest_report.html"
    path.write_text(document, encoding="utf-8")
    return path


def report_command(args):
    """Reformat saved results without changing their provenance, history, or counters."""
    from .report import render_report
    output = ROOT / "outputs"
    notices = read_json(output / "notices.json")
    evaluations = read_json(output / "evaluations.json")
    profile = read_json(output / "profile_used.json")
    summary = read_json(output / "run_summary.json")
    if not all(isinstance(item, expected) for item, expected in
               ((notices, list), (evaluations, list), (profile, dict), (summary, dict))):
        raise ValueError("저장된 분석 결과가 없습니다. 먼저 demo 또는 run을 실행하세요.")
    if not summary.get("as_of"):
        raise ValueError("저장된 결과에 판단 기준시각이 없습니다. demo 또는 run을 다시 실행하세요.")
    as_of = clock(summary["as_of"])
    progress = load_progress()
    changes, statuses = summary.get("changes", []), summary.get("collection_status", [])
    view_summary = dict(summary, report_only=True)
    path = write_html_report(notices, evaluations, profile, as_of, changes, statuses, progress, view_summary)
    mode = {"demo":"준비된 검토자료 데모", "cv_review":"CV·공식 지원기관 자료 기반 맞춤 검토"}.get(summary.get("mode"), "Ollama 로컬 모델 분석 결과")
    prefix = (f"자료 모드: **{mode}**\n\n저장된 결과의 보고서 형식만 재생성했습니다. 웹 조회·모델 추론을 실행하지 않았습니다.\n\n")
    markdown = render_report(notices, evaluations, profile, as_of, changes, statuses, progress)
    (output / "latest_report.md").write_text(prefix + markdown + application_history_appendix(profile) + catalog_appendix(), encoding="utf-8")
    print(f"HTML 보고서: {path}")
    if args.open:
        import webbrowser
        if not webbrowser.open(path.resolve().as_uri()):
            print("브라우저를 자동으로 열지 못했습니다. HTML 파일을 직접 열어 주세요.")
    return 0


def make_report(notices, profile, as_of, statuses, mode, errors=None, progress_only=False, review_only=False):
    from .evaluate import evaluate_notice
    from .report import render_report
    progress = load_progress()
    for notice in notices:
        notice["application_status"] = progress.get("applications", {}).get(notice["id"])
    evaluations = [evaluate_notice(notice, profile, as_of) for notice in notices]
    history_path = ROOT / f"state/history/{mode}.json"
    previous = read_json(history_path, [])
    changes = changes_since(previous, notices)
    report = render_report(notices, evaluations, profile, as_of, changes, statuses, progress)
    prefix = "자료 모드: **" + {"demo":"준비된 검토자료 데모 — LLM 실행 없음", "cv_review":"CV·공식 지원기관 자료 기반 맞춤 검토"}.get(mode, "Ollama 로컬 모델 분석 결과") + "**\n\n"
    if errors:
        prefix += "분석 실패: " + "; ".join(f"{item['notice_id']}: {item['error']}" for item in errors) + "\n\n"
    model_calls = sum(n.get("extraction_metadata", {}).get("method") == "ollama_local"
                      and not n.get("extraction_metadata", {}).get("cache_hit") for n in notices)
    cache_hits = sum(bool(n.get("extraction_metadata", {}).get("cache_hit")) for n in notices)
    if progress_only:
        model_calls, cache_hits = 0, 0
        prefix += "진행 상태만 갱신했습니다. 웹 조회·모델 추론은 실행하지 않았습니다.\n\n"
    if review_only:
        model_calls, cache_hits = 0, 0
        prefix += "CV·사용자 확인과 공식자료 조사 결과를 반영했습니다. 이 명령은 추가 모델 추론을 실행하지 않습니다.\n\n"
    if mode != "demo":
        prefix += f"이번 실행의 신규 추론 {model_calls}건 · 이전 추론 캐시 재사용 {cache_hits}건\n\n"
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    (output / "latest_report.md").write_text(prefix + report + application_history_appendix(profile) + catalog_appendix(), encoding="utf-8")
    write_json(output / "notices.json", notices)
    write_json(output / "evaluations.json", evaluations)
    write_json(output / "profile_used.json", profile)
    summary = {"mode": mode, "as_of": as_of.isoformat(), "created_at": clock().isoformat(),
               "notices": len(notices), "errors": errors or [], "collection_status": statuses,
               "model_calls": model_calls, "cache_hits": cache_hits, "progress_only": progress_only, "review_only": review_only,
               "models": sorted({n.get("extraction_metadata", {}).get("model") for n in notices
                                 if n.get("extraction_metadata", {}).get("model")}),
               "profile_is_demo": profile.get("is_demo", False), "changes": changes}
    write_html_report(notices, evaluations, profile, as_of, changes, statuses, progress, summary)
    write_json(output / "run_summary.json", summary)
    # Merge partial runs rather than treating omitted records as removed notices.
    merged = {item["id"]: item for item in previous}
    merged.update({item["id"]: item for item in notices})
    write_json(history_path, list(merged.values()))
    print(f"HTML 보고서: {output / 'latest_report.html'}")
    print(f"Markdown 보고서: {output / 'latest_report.md'}")
    print(f"공고 {len(notices)}건, 변경 {len(changes)}건, 분석 실패 {len(errors or [])}건")
    return summary


def personalize(args):
    from .funding_catalog import load_catalog, catalog_markdown
    profile = read_json(resolve(args.profile))
    if not isinstance(profile, dict) or profile.get("is_demo"):
        raise ValueError("분석할 연구자 프로필을 --profile로 지정하세요. 수업용 가상 예제는 demo 명령으로 실행합니다.")
    catalog = load_catalog(ROOT / "data/research", profile)
    if not catalog["candidates"]:
        raise ValueError("data/research에 공식자료 조사 후보가 없습니다.")
    output = ROOT / "outputs"
    write_json(output / "funding_catalog.json", catalog)
    (output / "funding_catalog.md").write_text(catalog_markdown(catalog, profile), encoding="utf-8")
    write_json(ROOT / "data/settings.json", {"active_profile": args.profile})
    notices = read_json(output / "notices.json", [])
    previous_summary = read_json(output / "run_summary.json", {})
    make_report(notices, profile, clock(args.as_of or catalog["checked_on"]),
                previous_summary.get("collection_status", []), "cv_review", review_only=True)
    print(f"지원사업 {catalog['program_count']}개 · 세부 전형 {len(catalog['candidates'])}개를 반영했습니다.")
    if args.open:
        import webbrowser
        webbrowser.open((output / "latest_report.html").resolve().as_uri())
    return 0


def demo(args):
    notices = copy.deepcopy(records(ROOT / "data/reviewed_notices.json", "notices"))
    if not notices:
        raise ValueError("준비된 검토자료가 없습니다.")
    for notice in notices:
        notice["evidence_status"] = "official_snapshot"
        notice["extraction_metadata"] = {"method": "human_reviewed_demo", "model": None}
    profile = read_json(resolve(args.profile))
    statuses = [{"source_id": "demo", "status": "snapshot", "fetched_at": "2026-11-06",
                 "note": "저장된 공식 원문을 사람이 검토한 실습자료. 현재 웹 재조회·LLM 추론 없음."}]
    make_report(notices, profile, clock(args.as_of), statuses, "demo")
    return 0


def run(args):
    from .collectors import collect_source, load_sources
    client = OllamaClient(args.model)
    client.ensure_local_model()
    seeds = records(ROOT / "data/seeds.json", "notices")
    if args.notice:
        seeds = [seed for seed in seeds if seed["id"] in args.notice]
    if args.limit:
        seeds = seeds[:args.limit]
    if not seeds:
        raise ValueError("분석할 공고가 없습니다. data/seeds.json의 id를 확인하세요.")
    sources = {source["id"]: source for source in load_sources(ROOT / "data/sources.json")}
    reviewed = {notice["id"]: notice for notice in records(ROOT / "data/reviewed_notices.json", "notices")}
    fetched, statuses, notices, errors = {}, [], [], []
    for i, seed in enumerate(seeds, 1):
        try:
            if args.live:
                if seed["source_id"] not in fetched:
                    fetched[seed["source_id"]] = collect_source(sources[seed["source_id"]],
                                                              ROOT / "cache/collections", refresh=True)
                    statuses.append(fetched[seed["source_id"]])
                record = fetched[seed["source_id"]]
                if record.get("status") != "ok":
                    raise RuntimeError(record.get("error", "공식 출처 수집 실패"))
                text, evidence_status = record["text"], "live"
            else:
                snapshot = ROOT / "data" / seed["snapshot_file"]
                text, evidence_status = snapshot.read_text(encoding="utf-8"), "official_snapshot"
                statuses.append({"source_id": seed["source_id"], "status": "snapshot",
                                 "fetched_at": "2026-11-06", "url": seed["source_url"],
                                 "note": "저장된 공식 원문 사용. 실시간 웹 조회 아님."})
            # Reference IDs locate the URL; changing storage layout needs no new inference.
            cache_seed = {key: value for key, value in seed.items() if key not in {"source_ref", "additional_source_refs"}}
            cache_key = fingerprint({"text": text, "seed": cache_seed, "model": client.model, "prompt": PROMPT_VERSION})
            cache_path = ROOT / f"cache/extractions/{cache_key}.json"
            cached = None if args.reanalyze else read_json(cache_path)
            if cached:
                metadata = cached.get("extraction_metadata", {})
                raw = metadata.get("raw_output")
                # Reapply current evidence validation without another inference call.
                notice = validate_extraction(unpack_response(raw) if raw else cached, text, seed)
                notice["extraction_metadata"] = metadata
                notice["extraction_metadata"]["validation_version"] = "2026-11-06-v4"
                if seed["id"] in reviewed:
                    crosscheck_reviewed(notice, reviewed[seed["id"]])
                write_json(cache_path, notice)
                print(f"[{i}/{len(seeds)}] 분석 캐시: {seed['title']}", flush=True)
            else:
                print(f"[{i}/{len(seeds)}] 로컬 모델 분석: {seed['title']}", flush=True)
                notice = client.extract(text, seed)
                if seed["id"] in reviewed:
                    crosscheck_reviewed(notice, reviewed[seed["id"]])
                notice["extraction_metadata"].update(source_hash=fingerprint(text), analyzed_at=clock().isoformat())
                write_json(cache_path, notice)
            notice["evidence_status"] = evidence_status
            if seed.get("content_warning"):
                notice["missing_information"].append(seed["content_warning"])
            if args.live:
                if record.get("warning"):
                    notice["missing_information"].append(record["warning"])
                for supplement in record.get("supporting_results") or []:
                    if supplement.get("status") != "ok":
                        notice["missing_information"].append(f"추가 공식 원문 수집 실패: {supplement.get('url')}")
            notice["missing_information"] = list(dict.fromkeys(notice["missing_information"]))
            notice["extraction_metadata"]["cache_hit"] = bool(cached)
            notices.append(notice)
        except Exception as error:
            errors.append({"notice_id": seed["id"], "error": str(error)})
            print(f"분석 실패 {seed['id']}: {error}", file=sys.stderr, flush=True)
    profile = read_json(resolve(args.profile))
    make_report(notices, profile, clock(args.as_of), statuses, "local_model", errors)
    return 0 if notices else 1


def ask(args):
    from .agent import answer_question
    notices = read_json(ROOT / "outputs/notices.json", [])
    if not notices:
        raise ValueError("먼저 demo 또는 run을 실행해 공고 자료를 준비하세요.")
    summary = read_json(ROOT / "outputs/run_summary.json", {})
    result = answer_question(OllamaClient(args.model), args.question, notices,
                             read_json(ROOT / "outputs/evaluations.json", []),
                             read_json(ROOT / "outputs/profile_used.json", {}), load_progress(),
                             clock(summary.get("as_of")), catalog=registry_links(read_json(ROOT / "outputs/funding_catalog.json", {})))
    write_json(ROOT / "outputs/agent_trace.json", result)
    (ROOT / "outputs/last_answer.md").write_text(result["answer"] + "\n", encoding="utf-8")
    print(result["answer"])
    print(f"\n도구 호출 {len(result['tool_calls'])}회. 기록: outputs/agent_trace.json")
    return 0


def update_progress(args):
    progress = load_progress()
    if args.command == "task-done":
        evaluations = read_json(ROOT / "outputs/evaluations.json", [])
        valid = {task["id"] for item in evaluations for task in item.get("tasks", [])}
        if args.task_id not in valid:
            raise ValueError("현재 보고서에 없는 할 일 ID입니다.")
        progress["completed_tasks"] = sorted(set(progress.get("completed_tasks", []) + [args.task_id]))
    else:
        valid = {seed["id"] for seed in records(ROOT / "data/seeds.json", "notices")}
        if args.notice_id not in valid:
            raise ValueError("등록되지 않은 공고 ID입니다.")
        progress.setdefault("applications", {})[args.notice_id] = args.status
    write_json(ROOT / "data/progress.json", progress)
    notices = read_json(ROOT / "outputs/notices.json", [])
    summary = read_json(ROOT / "outputs/run_summary.json", {})
    if notices:
        make_report(notices, read_json(ROOT / "outputs/profile_used.json", {}), clock(summary.get("as_of")),
                    summary.get("collection_status", []), summary.get("mode", "demo"), summary.get("errors"), progress_only=True)
    print("로컬 진행 상태를 저장했습니다.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="교수·연구자 연구비 보조 에이전트 (Ollama 로컬 추론)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("doctor", doctor), ("demo", demo), ("run", run), ("discover", discover_command),
                          ("ask", ask), ("report", report_command), ("personalize", personalize), ("sources", sources_command),
                          ("task-done", update_progress), ("application", update_progress), ("serve", serve_command)):
        sub = commands.add_parser(name)
        sub.set_defaults(handler=handler)
        if name in ("doctor", "run", "ask", "serve"):
            sub.add_argument("--model", help="설치된 로컬 Ollama 모델명. 환경변수 OLLAMA_MODEL도 지원")
        if name in ("demo", "run"):
            sub.add_argument("--profile", default="data/profile.example.json" if name == "demo" else active_profile_path())
            sub.add_argument("--as-of", help="재현용 판단 기준일, 예: 2026-11-06. 기본은 현재 한국시간")
        if name == "run":
            sub.add_argument("--live", action="store_true", help="공식 사이트를 다시 수집. 실패 시 스냅샷으로 대체하지 않음")
            sub.add_argument("--limit", type=int)
            sub.add_argument("--notice", action="append", help="등록된 공고 id만 분석. 여러 번 지정 가능")
            sub.add_argument("--reanalyze", action="store_true", help="기존 모델 분석 캐시 대신 다시 추론")
        if name == "discover":
            sub.add_argument("--refresh", action="store_true")
        if name == "serve":
            sub.add_argument("--port", type=int, default=8765, help="로컬 화면 포트. 기본 8765")
            sub.add_argument("--open", action="store_true", help="탐색 화면을 브라우저에서 열기")
        if name == "ask":
            sub.add_argument("question")
        if name == "sources":
            sub.add_argument("--query", default="", help="사이트 이름·도메인·ID 검색")
            sub.add_argument("--json", action="store_true", help="참조를 해소한 사이트·페이지를 JSON으로 출력")
        if name == "report":
            sub.add_argument("--open", action="store_true", help="저장한 HTML 보고서를 기본 브라우저에서 열기")
        if name == "personalize":
            sub.add_argument("--profile", default="data/profile.example.json")
            sub.add_argument("--as-of", help="조사 기준일. 기본은 후보 조사 데이터의 확인일")
            sub.add_argument("--open", action="store_true")
        if name == "task-done":
            sub.add_argument("task_id")
        if name == "application":
            sub.add_argument("notice_id")
            sub.add_argument("status", choices=["interested", "preparing", "submitted", "institution_approved",
                                               "awaiting_result", "shortlisted", "selected", "rejected", "on_hold"])
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1
