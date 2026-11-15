"""Read-only web server for one realm's board. stdlib only.

* binds 127.0.0.1 -- reach it through ``ssh -L``; nothing is exposed publicly
* rejects requests whose Host/Origin isn't loopback (blocks DNS rebinding)
* allows CORS from loopback origins so one browser tab can merge several
  realms' hubs (each forwarded to a different local port)
* pushes state over Server-Sent Events when the DB or the item cards change
* bounded: socket timeouts, a cap on concurrent SSE streams, notifications on
  their own thread
"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from wd import config as cfgmod
from wd.attention import URGENT
from wd.hub import notify
from wd.hub.store import Store
from wd.items import ItemStore
from wd.rpc import db_path, lower_priority

WEB = Path(__file__).resolve().parent.parent / "web"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
TICK_S = 1.0
RECOMPUTE_S = 15.0
MAX_SSE = 16
MAX_CONNECTIONS = 64
SOCKET_TIMEOUT_S = 30


class BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on concurrent connections/threads."""

    daemon_threads = True

    def __init__(self, *a, max_connections: int = MAX_CONNECTIONS, **kw):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # over capacity: drop, don't queue
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _hostname(value: str) -> str:
    """'localhost:8740' / '[::1]:8740' / 'http://127.0.0.1:8741' -> bare hostname."""
    if not value:
        return ""
    if "://" not in value:
        value = "//" + value
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


class Board:
    """Owns the DB connection; recomputes state and wakes SSE clients."""

    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.realm = cfg.hub.realm
        path = db_path(cfg, self.realm)
        self.store = Store(path)
        self.items = ItemStore(cfg.hub.data_dir, cfg.hub.git_autocommit)
        from wd.research import ResearchProject

        self.research = {r.name: ResearchProject(r, cfg.hub.data_dir) for r in cfg.hub.research}
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.version = 0
        self.payload = b"{}"
        self._last_body = None
        self.sse_slots = threading.BoundedSemaphore(MAX_SSE)
        self.notifier = None
        if cfg.hub.notify.ntfy_topic:
            mark_store = Store(path)  # its own connection, used only by the notifier thread
            self.notifier = notify.Notifier(cfg.hub.notify, mark_store.mark_notified)
        self._recompute(force=True)

    def _recompute(self, force: bool = False) -> None:
        with self.lock:
            if self.items.fingerprint() != self.store.items_fingerprint():
                with self.items.locked():
                    self.store.reindex_items(self.items)
            state = self.store.state(self.realm)
            state["research"] = []
            for proj in self.research.values():
                try:
                    state["research"].append(proj.summary())
                except Exception as e:  # a broken source must not take the board down
                    print(f"[wd serve] research {proj.src.name}: {e!r}", file=sys.stderr)
            jobs = []
            if self.notifier:
                cands = self.store.notify_candidates(state["sessions"], URGENT)
                jobs = notify.build_jobs(self.realm, cands, state["sessions"], self.cfg.hub.notify, URGENT)
        if jobs:
            self.notifier.submit(jobs)
        body = json.dumps({k: v for k, v in state.items() if k != "now"}, ensure_ascii=False, sort_keys=True, default=str)
        if force or body != self._last_body:
            self._last_body = body
            state["hub"] = {"realm": self.realm, "host": self.cfg.host, "peers": self.cfg.hub.peers}
            with self.cond:
                self.payload = json.dumps(state, ensure_ascii=False, default=str).encode()
                self.version += 1
                self.cond.notify_all()

    def watch(self) -> None:
        last_v, last_full = None, time.time()
        while True:
            time.sleep(TICK_S)
            try:
                with self.lock:
                    v = self.store.data_version()
                    items_changed = self.items.fingerprint() != self.store.items_fingerprint()
                if v != last_v or items_changed or time.time() - last_full > RECOMPUTE_S:
                    last_v, last_full = v, time.time()
                    self._recompute()
            except Exception as e:  # keep serving even if one recompute fails
                print(f"[wd serve] recompute: {e!r}", file=sys.stderr)


def make_handler(board: Board):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wd"
        timeout = SOCKET_TIMEOUT_S  # applies to reads and writes on the socket

        def log_message(self, fmt, *args):  # quiet
            pass

        def _guard(self) -> bool:
            host = _hostname(self.headers.get("Host", ""))
            origin = self.headers.get("Origin")
            if host not in LOOPBACK or (origin and _hostname(origin) not in LOOPBACK):
                self.send_error(403, "loopback only")
                return False
            return True

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and _hostname(origin) in LOOPBACK:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def do_GET(self):
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                with board.cond:
                    body = board.payload
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/research":
                name = (parse_qs(urlsplit(self.path).query).get("name") or [""])[0]
                proj = board.research.get(name)
                if not proj:
                    self.send_error(404, "no such research source")
                    return
                d = proj.data()
                etag = f'"{d["fp"]}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self._cors()
                    self.end_headers()
                    return
                body = json.dumps(d, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/events":
                if not board.sse_slots.acquire(blocking=False):
                    self.send_error(503, "too many event streams")
                    return
                try:
                    return self._sse()
                finally:
                    board.sse_slots.release()
            self._static(path)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            seen = -1
            try:
                while True:
                    with board.cond:
                        if board.version == seen:
                            board.cond.wait(timeout=15)
                        version, body = board.version, board.payload
                    if version != seen:
                        seen = version
                        self.wfile.write(b"event: state\ndata: " + body + b"\n\n")
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB / rel).resolve()
            if WEB not in target.parents or not target.is_file():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text") else ctype)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(cfg: cfgmod.Config, port: int | None = None) -> int:
    if not cfg.hub.realm:
        print("this machine is not a hub: set [hub] realm in your config", file=sys.stderr)
        return 2
    lower_priority()
    board = Board(cfg)
    threading.Thread(target=board.watch, daemon=True).start()
    port = port or cfg.hub.port
    httpd = BoundedHTTPServer(("127.0.0.1", port), make_handler(board))
    print(f"wd hub ({board.realm}) on http://127.0.0.1:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
