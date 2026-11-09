"""Serve the CV workspace on the local computer."""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .workspace import WorkspaceBusyError

MAX_REQUEST_BYTES = 15 * 1024 * 1024


def create_server(root: Path, port=8765, model=None, workspace=None, jobs=None):
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("포트는 0~65535 사이의 정수로 지정하세요.")
    if workspace is None:
        from .workspace import Workspace
        workspace = Workspace(root, model=model)
    if jobs is None:
        from .jobs import DiscoveryJobs
        jobs = DiscoveryJobs(root, model=model, workspace=workspace)
    operations = threading.Lock()
    dashboard = Path(__file__).parent / "web/dashboard.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "GrantFinder"

        def log_message(self, format, *args):
            # Never log request bodies, filenames, or CV text.
            pass

        def _reply(self, status, payload, mime="application/json; charset=utf-8"):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _local_request(self):
            expected_port = self.server.server_port
            host = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{expected_port}", f"localhost:{expected_port}"}
            if host not in allowed:
                raise ValueError("로컬 주소에서 화면을 열어 주세요.")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{item}" for item in allowed}:
                raise ValueError("이 화면에서 실행한 요청만 처리합니다.")

        def do_GET(self):
            try:
                self._local_request()
                address = urlsplit(self.path)
                path = address.path
                if path == "/api/workspace":
                    self._reply(200, jobs.view())
                elif path == "/api/workspace/file":
                    with workspace.lock:
                        identifier = (workspace.get().get("profile") or {}).get("id")
                        if not identifier:
                            self._reply(404, {"error": "저장된 연구자 정보 파일이 없습니다."})
                            return
                        requested = parse_qs(address.query, keep_blank_values=True).get("profile_id", [])
                        if requested != [identifier]:
                            self._reply(409, {"error": "현재 연구자가 바뀌었습니다. 화면에서 파일 열기를 다시 눌러 주세요."})
                            return
                        if not isinstance(identifier, str) or not re.fullmatch(r"cv_[a-f0-9]{16}", identifier):
                            raise ValueError("연구자 정보 파일의 ID를 확인하세요.")
                        saved_file = Path(root).resolve() / "state/workspaces" / f"{identifier}.json"
                        if saved_file.resolve() != saved_file:
                            raise ValueError("등록된 작업공간의 원본 파일만 열 수 있습니다.")
                        try:
                            payload = saved_file.read_bytes()
                        except FileNotFoundError:
                            self._reply(404, {"error": "저장된 연구자 정보 파일이 없습니다."})
                            return
                        self._reply(200, payload, "application/json; charset=utf-8")
                elif path == "/api/memory":
                    state = workspace.get()
                    self._reply(200, {key: state.get(key) for key in
                                      ("profile", "known", "memory", "applications", "automation")})
                elif path in {"/", "/index.html"}:
                    self._reply(200, dashboard.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._reply(404, {"error": "화면 주소를 확인하세요."})
            except (ValueError, OSError) as error:
                self._reply(400, {"error": str(error)})

        def _body(self):
            self._local_request()
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("JSON 요청이 필요합니다.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("요청 크기를 확인하세요.") from error
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("파일을 포함한 요청은 15MB 이하로 보내 주세요.")
            try:
                value = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError("요청 내용을 읽지 못했습니다.") from error
            if not isinstance(value, dict):
                raise ValueError("요청 내용을 확인하세요.")
            return value

        def _mutate(self):
            locked = False
            try:
                if self.command == "DELETE":
                    self._local_request()
                    body = {}
                else:
                    body = self._body()
                locked = operations.acquire(blocking=False)
                if not locked:
                    self._reply(409, {"error": "진행 중인 작업이 끝난 뒤 다시 실행해 주세요."})
                    return
                route = (self.command, urlsplit(self.path).path)
                status = 200
                if route == ("POST", "/api/cv"):
                    encoded = body.get("content_base64")
                    if not isinstance(encoded, str):
                        raise ValueError("CV 파일을 선택해 주세요.")
                    try:
                        payload = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as error:
                        raise ValueError("CV 업로드 내용을 읽지 못했습니다.") from error
                    result = workspace.upload_cv(body.get("filename"), payload)
                elif route == ("POST", "/api/homepage"):
                    result = workspace.import_homepage(body.get("url"))
                elif route == ("DELETE", "/api/workspace"):
                    with jobs.lock, workspace.lock:
                        current = workspace.get()
                        identifier = (current.get("profile") or {}).get("id")
                        worker = jobs.threads.get(identifier)
                        if worker is not None and worker.is_alive():
                            raise WorkspaceBusyError("탐색 작업이 진행 중입니다. 작업이 끝난 뒤 저장된 정보를 비워 주세요.")
                        result = workspace.clear_saved()
                elif route == ("PUT", "/api/profile"):
                    result = (workspace.save_conditions(body["text"]) if "text" in body
                              else workspace.save_profile(body.get("profile")))
                elif route == ("PUT", "/api/known"):
                    result = workspace.save_known(body.get("items"))
                elif route == ("POST", "/api/sources"):
                    result = workspace.add_source(body)
                elif route == ("POST", "/api/selection"):
                    result = workspace.prepare_selection()
                elif route == ("POST", "/api/target-search"):
                    self._reply(200, workspace.search_targets(body))
                    return
                elif route == ("POST", "/api/search"):
                    result = workspace.search(body.get("mode", "all"))
                elif route == ("PUT", "/api/automation"):
                    result = workspace.save_automation(body)
                elif route == ("POST", "/api/discovery"):
                    result = jobs.start(body.get("mode", "all"), refresh=body.get("refresh", True))
                    status = 202
                elif route == ("POST", "/api/applications"):
                    result = workspace.add_application(body)
                elif route[0] in {"PATCH", "DELETE"} and route[1].startswith("/api/applications/"):
                    identifier = route[1].removeprefix("/api/applications/")
                    if not identifier or "/" in identifier:
                        raise ValueError("지원 계획을 확인하세요.")
                    result = (workspace.update_application(identifier, body) if route[0] == "PATCH"
                              else workspace.delete_application(identifier))
                else:
                    self._reply(404, {"error": "요청 주소를 확인하세요."})
                    return
                self._reply(status, jobs.view(result))
            except WorkspaceBusyError as error:
                self._reply(409, {"error": str(error)})
            except (ValueError, TypeError, KeyError) as error:
                self._reply(400, {"error": str(error)})
            except (OSError, RuntimeError) as error:
                self._reply(503, {"error": str(error)})
            finally:
                if locked:
                    operations.release()

        do_POST = _mutate
        do_PUT = _mutate
        do_PATCH = _mutate
        do_DELETE = _mutate

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.discovery_jobs = jobs
    return server


def serve(root: Path, port=8765, model=None, open_browser=False):
    server = create_server(root, port, model)
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"연구지원 탐색 화면: {address}", flush=True)
    print("종료: Ctrl+C", flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(address,)).start()
    server.discovery_jobs.start_scheduler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.discovery_jobs.close()
        server.server_close()
    return 0
