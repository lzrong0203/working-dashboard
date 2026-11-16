"""Web server for one realm's board. stdlib only.

* binds 127.0.0.1 -- reach it through ``ssh -L``; nothing is exposed publicly
* rejects requests whose Host/Origin isn't loopback (blocks DNS rebinding)
* allows CORS from loopback origins so one browser tab can merge several
  realms' hubs (each forwarded to a different local port)
* one write: a person confirming or dismissing report items
  (``POST /api/research/decisions``), accepted only from the board's own pages
  (this hub's port, its peers, the realms' view ports) with a custom header
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
MAX_WRITE = 32 * 1024


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


def _http_port(origin: str) -> int | None:
    """The port of an http:// origin (80 when omitted); None for anything else."""
    try:
        u = urlsplit(origin)
        if u.scheme != "http" or not u.hostname:
            return None
        return u.port or 80
    except ValueError:
        return None


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
            # publish under the same lock: a write request and the watch loop both
            # recompute, and an older state must never be published after a newer one
            body = json.dumps({k: v for k, v in state.items() if k != "now"}, ensure_ascii=False, sort_keys=True, default=str)
            if force or body != self._last_body:
                self._last_body = body
                state["hub"] = {"realm": self.realm, "host": self.cfg.host, "peers": self.cfg.hub.peers}
                with self.cond:
                    self.payload = json.dumps(state, ensure_ascii=False, default=str).encode()
                    self.version += 1
                    self.cond.notify_all()
        if jobs:
            self.notifier.submit(jobs)

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

        def _write_origin(self) -> str:
            """The Origin a write may come from: a board page, i.e. a loopback
            origin on this hub's port, a peer hub's port or a realm's view port.
            Other local pages (dev servers, the pixel office) may read, not write."""
            origin = self.headers.get("Origin") or ""
            port = _http_port(origin)
            if not port or _hostname(origin) not in LOOPBACK:
                return ""
            cfg = board.cfg
            ports = {self.server.server_address[1], cfg.hub.port}
            ports |= {_http_port(x) for x in cfg.hub.peers if isinstance(x, str)}
            ports |= {r.view_port for r in cfg.realms.values() if r.view_port}
            ports.discard(None)
            return origin if port in ports else ""

        def _json(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            if self.command == "POST" and not getattr(self, "_body_read", False):
                # refused before reading: drain a modest body so the client gets this
                # reply instead of a TCP reset (the connection is closed afterwards)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if 0 < n <= 4 * MAX_WRITE and not self.headers.get("Transfer-Encoding") and not self.headers.get("Expect"):
                    try:
                        self.connection.settimeout(1.0)
                        self.rfile.read(n)
                    except OSError:
                        pass
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            origin = self._write_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):  # CORS preflight for the one write endpoint
            if not self._guard():
                return
            origin = self._write_origin()
            if urlsplit(self.path).path != "/api/research/decisions" or not origin:
                self.send_error(403, "not allowed")
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-WD-Write")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_POST(self):
            if not self._guard():
                return
            self.close_connection = True
            url = urlsplit(self.path)
            if url.path != "/api/research/decisions":
                return self._json(404, {"ok": False})
            if not self._write_origin() or self.headers.get("X-WD-Write") != "1" \
                    or not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(403, {"ok": False, "error": "forbidden"})
            if self.headers.get("Transfer-Encoding"):
                return self._json(400, {"ok": False, "error": "chunked bodies not accepted"})
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._json(411, {"ok": False, "error": "Content-Length required"})
            if not 0 < n <= MAX_WRITE:
                return self._json(413, {"ok": False, "error": "body too large"})
            proj = board.research.get((parse_qs(url.query).get("name") or [""])[0])
            if not proj:
                return self._json(404, {"ok": False, "error": "no such research source"})
            from wd.research import RegisterConflict

            try:
                self._body_read = True
                raw = self.rfile.read(n)
                if len(raw) != n:
                    return self._json(400, {"ok": False, "error": "incomplete body"})
                items = proj.update_register(json.loads(raw))
            except RegisterConflict as e:
                return self._json(409, {"ok": False, "error": str(e)[:200]})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._json(400, {"ok": False, "error": str(e)[:200]})
            board._recompute()  # the summary's fingerprint changes; other open pages re-read
            return self._json(200, {"ok": True, "items": items})

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
                rid = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
                if rid:
                    rec = proj.record(rid)
                    if not rec:
                        self.send_error(404, "no such record")
                        return
                    body = json.dumps(rec, ensure_ascii=False, allow_nan=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self._cors()
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if proj.src.repo:  # hourly, in the background; the page shows the cache it has
                    from wd.research_github import cache_path, refresh_async

                    refresh_async(proj.src.repo, cache_path(proj.overlay_path))
                d = proj.data()
                etag = f'"{d["fp"]}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self._cors()
                    self.end_headers()
                    return
                body = json.dumps(d, ensure_ascii=False, allow_nan=False).encode()
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
