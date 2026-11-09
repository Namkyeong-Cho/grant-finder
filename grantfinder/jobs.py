"""Background discovery and refresh while the local workspace server is running."""

from __future__ import annotations

import copy
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import fingerprint, read_json, write_json

ACTIVE = {"queued", "running"}


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    try:
        moment = datetime.fromisoformat(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class DiscoveryJobs:
    def __init__(self, root, model=None, workspace=None, *, workspace_factory=None, runner=None):
        if workspace_factory is None:
            from .workspace import Workspace
            workspace_factory = Workspace
        self.root = Path(root)
        self.model = model
        self.factory = workspace_factory
        self.workspace = workspace if workspace is not None else self.factory(self.root, model=model)
        self.runner = runner or self._run_workspace
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.stopping = threading.Event()
        self.threads = {}
        self.scheduler = None
        self._recover()

    def _path(self, profile_id):
        if not isinstance(profile_id, str) or not re.fullmatch(r"cv_[a-f0-9]{16}", profile_id):
            raise ValueError("CV 작업공간을 먼저 준비해 주세요.")
        return self.root / "state/jobs" / f"{profile_id}.json"

    def _recover(self):
        for path in (self.root / "state/jobs").glob("cv_*.json"):
            value = read_json(path, {})
            if value.get("status") in ACTIVE and not _alive(value.get("pid")):
                value.update(status="interrupted", stage="interrupted", finished_at=_now().isoformat(),
                             message="이전 탐색이 중단되었습니다. 자동 탐색을 다시 준비합니다.")
                write_json(path, value)

    def status(self, profile_id):
        try:
            value = read_json(self._path(profile_id), None)
        except ValueError:
            return None
        if isinstance(value, dict):
            value = dict(value)
            value.pop("pid", None)
            return value
        return None

    def view(self, state=None):
        state = copy.deepcopy(state if state is not None else self.workspace.get())
        state["job"] = self.status((state.get("profile") or {}).get("id"))
        return state

    @staticmethod
    def _run_workspace(workspace, mode, refresh, progress):
        from .opportunity_review import review_opportunities
        return workspace.search(mode, refresh=refresh, progress=progress, reviewer=review_opportunities)

    def start(self, mode="all", *, profile_id=None, refresh=True):
        if mode not in {"all", "catalog", "web"}:
            raise ValueError("탐색 방식은 all, catalog 또는 web으로 입력하세요.")
        if self.stopping.is_set():
            raise RuntimeError("탐색 서버가 종료 중입니다.")
        if not isinstance(refresh, bool):
            raise ValueError("새로 조회 여부는 참 또는 거짓으로 지정하세요.")
        target = (self.factory(self.root, model=self.model, profile_id=profile_id)
                  if profile_id else self.workspace)
        # Clearing saved data shares this workspace lock. Read the profile and
        # record the queued job together so a stale scheduler read cannot revive it.
        with self.lock, target.lock:
            state = target.get()
            identifier = (state.get("profile") or {}).get("id")
            path = self._path(identifier)
            previous = read_json(path, {})
            if previous.get("status") in ACTIVE and _alive(previous.get("pid")):
                return self.view(state)
            job = {"id": "job_" + uuid.uuid4().hex[:16], "profile_id": identifier, "pid": os.getpid(),
                   "input_fingerprint": fingerprint({"profile": state.get("profile"), "known": state.get("known", [])}),
                   "mode": mode, "status": "queued", "stage": "queued", "message": "자동 탐색을 준비하고 있습니다.",
                   "created_at": _now().isoformat(), "started_at": None, "finished_at": None, "error": None}
            write_json(path, job)
            thread = threading.Thread(target=self._work, args=(identifier, job["id"], mode, bool(refresh)), daemon=True)
            self.threads[identifier] = thread
            thread.start()
        return self.view(state)

    def _update(self, profile_id, job_id, **fields):
        with self.lock:
            path = self._path(profile_id)
            job = read_json(path, {})
            if job.get("id") == job_id:
                job.update(fields)
                write_json(path, job)

    def _work(self, profile_id, job_id, mode, refresh):
        try:
            with self.run_lock:
                if self.stopping.is_set():
                    raise InterruptedError("탐색 서버가 종료되었습니다.")
                self._update(profile_id, job_id, status="running", stage="planning",
                             started_at=_now().isoformat(), message="CV 조건으로 지원사업과 검색어를 정리하고 있습니다.")

                def progress(stage, message):
                    if self.stopping.is_set():
                        raise InterruptedError("탐색 서버가 종료되었습니다.")
                    self._update(profile_id, job_id, stage=str(stage)[:80], message=str(message)[:500])

                bound = self.factory(self.root, model=self.model, profile_id=profile_id)
                result = self.runner(bound, mode, refresh, progress)
                if self.stopping.is_set():
                    raise InterruptedError("서버 종료로 탐색이 중단되었습니다.")
                count = len(result.get("results", []))
                self._update(profile_id, job_id, status="succeeded", stage="complete", finished_at=_now().isoformat(),
                             message=f"지원 후보 {count}개 사업을 저장했습니다.", result_count=count, error=None)
        except InterruptedError as error:
            self._update(profile_id, job_id, status="interrupted", stage="interrupted",
                         finished_at=_now().isoformat(), message=str(error), error=str(error))
        except Exception as error:
            stopped = self.stopping.is_set()
            self._update(profile_id, job_id, status="interrupted" if stopped else "failed",
                         stage="interrupted" if stopped else "failed", finished_at=_now().isoformat(),
                         message=("서버 종료로 탐색이 중단되었습니다." if stopped else
                                  "탐색을 마치지 못했습니다. 저장된 결과는 유지합니다."), error=str(error)[:1500])

    def tick(self, now=None):
        """Queue a due active profile without tying execution to an open browser tab."""
        if self.stopping.is_set():
            return False
        state = self.workspace.get()
        profile_id = (state.get("profile") or {}).get("id")
        automation = state.get("automation") or {}
        if not profile_id or not automation.get("enabled"):
            return False
        moment = now or _now()
        job = self.status(profile_id) or {}
        if job.get("status") in ACTIVE:
            return False
        ended = _time(job.get("finished_at"))
        next_run = _time(automation.get("next_run_at"))
        same_inputs = job.get("input_fingerprint") == fingerprint({"profile": state.get("profile"), "known": state.get("known", [])})
        if job.get("status") == "failed" and same_inputs:
            if (ended and moment - ended < timedelta(minutes=15)) or (next_run and next_run > moment):
                return False
        due = bool(state.get("results_stale") or not state.get("searched_at") or
                   next_run is None or next_run <= moment)
        if due:
            self.start("all", profile_id=profile_id, refresh=True)
        return due

    def start_scheduler(self):
        if self.scheduler and self.scheduler.is_alive():
            return

        def loop():
            while not self.stopping.is_set():
                try:
                    self.tick()
                except (ValueError, RuntimeError, OSError):
                    # A malformed local file should not terminate the server loop.
                    pass
                self.stopping.wait(30)

        self.scheduler = threading.Thread(target=loop, daemon=True)
        self.scheduler.start()

    def close(self):
        self.stopping.set()
        for identifier, thread in list(self.threads.items()):
            if thread.is_alive():
                current = read_json(self._path(identifier), {})
                self._update(identifier, current.get("id"), status="interrupted", stage="interrupted",
                             finished_at=_now().isoformat(), message="서버 종료로 탐색이 중단되었습니다.")
