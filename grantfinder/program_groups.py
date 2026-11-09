"""Program families with separate eligibility, deadlines and application IDs per track."""

import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data/program_groups.json"


def load_program_groups(path=None):
    path = Path(path) if path is not None else DEFAULT_PATH
    if not path.exists():
        return {"groups": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ValueError("program_groups.json의 groups는 목록이어야 합니다.")
    group_ids, memberships = set(), {"candidate_ids": set(), "notice_ids": set()}
    for group in data["groups"]:
        if not group.get("id") or group["id"] in group_ids or not group.get("title"):
            raise ValueError("지원사업 그룹 ID가 없거나 중복됩니다.")
        group_ids.add(group["id"])
        for kind, seen in memberships.items():
            for identifier in group.get(kind, []):
                if identifier in seen:
                    raise ValueError(f"지원 전형이 여러 사업에 중복 배정됐습니다: {identifier}")
                seen.add(identifier)
    return data


def group_records(records, kind="candidate", definitions=None):
    definitions = load_program_groups() if definitions is None else definitions
    member_key = {"candidate": "candidate_ids", "notice": "notice_ids"}.get(kind)
    if not member_key:
        raise ValueError(f"알 수 없는 지원자료 종류: {kind}")
    membership = {identifier: group for group in definitions.get("groups", []) for identifier in group.get(member_key, [])}
    result, by_key = [], {}
    for item in records:
        definition = membership.get(item["id"])
        key = ("group", definition["id"]) if definition else ("single", item["id"])
        if key not in by_key:
            group = {"id": definition["id"] if definition else item["id"],
                     "title": definition["title"] if definition else item.get("title", item["id"]),
                     "items": [], "track_labels": dict(definition.get("track_labels", {})) if definition else {}}
            by_key[key] = group
            result.append(group)
        by_key[key]["items"].append(item)
    return result
