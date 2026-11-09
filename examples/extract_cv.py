#!/usr/bin/env python3
"""Extract academic profile fields with the same local engine as CV upload."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grantfinder.cv_profile import parse_cv_profile
from grantfinder.llm import OllamaClient
from grantfinder.storage import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="CV → 연구지원 검색 조건 JSON (로컬 Ollama)")
    parser.add_argument("cv_file", type=Path, help="PDF·DOCX·TXT·MD·TEX·JSON CV 파일")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/cv_extracted.json")
    parser.add_argument("--model", default="qwen3.8:27b", help="ollama list에 설치된 로컬 모델")
    args = parser.parse_args(argv)
    source, output = args.cv_file.expanduser().resolve(), args.output.expanduser().resolve()
    try:
        if not source.is_file():
            raise ValueError(f"CV 파일을 찾지 못했습니다: {source}")
        if output.suffix.lower() != ".json":
            raise ValueError("결과 파일은 .json 확장자로 지정하세요.")
        protected = {"notices.json", "evaluations.json", "profile_used.json", "run_summary.json", "funding_catalog.json", "discovered.json"}
        if (output == source or output.is_relative_to(ROOT / "state") or output.is_relative_to(ROOT / "data")
                or (output.parent == ROOT / "outputs" and output.name in protected)):
            raise ValueError("CV 원본·기존 작업공간과 별도의 결과 JSON 경로를 지정하세요.")
        client = OllamaClient(args.model, timeout=300)
        started = time.monotonic()
        profile = parse_cv_profile(source.name, source.read_bytes(), client=client)
        elapsed = time.monotonic() - started
        write_json(output, profile)
        print(f"이름: {profile.get('name') or '미기재'}")
        print(f"박사학위: {profile.get('phd_date') or '미기재'}")
        print(f"최초 전임 임용: {profile.get('first_faculty_date') or '미기재'}")
        print("연구 키워드: " + ", ".join(profile.get("keywords", [])))
        print(f"수행 과제: {len(profile.get('active_projects', []))}개")
        for project in profile.get("active_projects", []):
            print(f"  {project.get('program') or project.get('title')} · {project.get('start_date') or '시작 미기재'} ~ {project.get('end_date') or '종료 미기재'}")
        print(f"분석: {client.model} · {elapsed:.2f}초")
        print(f"결과: {output}")
    except (ValueError, RuntimeError, OSError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
