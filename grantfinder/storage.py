"""Small, atomic JSON storage; no external database is required."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        temporary = stream.name
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def changes_since(previous: list, current: list) -> list:
    fields = ("title", "events", "eligibility", "required_documents", "missing_information")
    old = {notice["id"]: notice for notice in previous}
    changes = []
    for notice in current:
        before = old.get(notice["id"])
        if before is None:
            changes.append({"notice_id": notice["id"], "kind": "new", "title": notice["title"], "fields": []})
            continue
        changed = [field for field in fields if before.get(field) != notice.get(field)]
        if changed:
            changes.append({"notice_id": notice["id"], "kind": "updated", "title": notice["title"],
                            "fields": changed, "before": {f: before.get(f) for f in changed},
                            "after": {f: notice.get(f) for f in changed}})
    # Missing notices can mean a failed fetch or a run limit, rather than cancellation.
    return changes
