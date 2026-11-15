"""Open claude.ai session links in the right browser profile, per realm.

Many people keep work and personal Claude accounts apart with separate browser
profiles. A web page cannot choose a browser profile, so the board hands the
click to this helper on the viewing machine, which runs e.g.

    open -na "Google Chrome" --args --profile-directory="Profile 2" <url>

Guard rails: binds 127.0.0.1; browser calls only from the board's own origins
(its view ports), anything without an Origin needs the per-user token in
``$WD_HOME/opener.token`` (0600); POST with a custom header (so even a trusted
origin needs a CORS preflight); bounded body; only https://claude.ai/ URLs;
only configured realms. Another OS account on the same machine can forge an
Origin header, so this does not defend against other local users -- at worst
they could open a claude.ai URL in your browser profile. Runs as a thread inside
``wd agent`` (or standalone via ``wd opener``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from wd import config as cfgmod

ALLOWED_PREFIX = "https://claude.ai/"
MAX_BODY = 4096
MAX_URL = 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _host(value: str) -> str:
    if not value:
        return ""
    try:
        return (urlsplit(value if "://" in value else "//" + value).hostname or "").lower()
    except ValueError:
        return ""


def chrome_profile_names() -> dict[str, str]:
    """Profile directory -> display name, from Chrome's Local State (macOS)."""
    p = Path.home() / "Library/Application Support/Google/Chrome/Local State"
    try:
        cache = json.loads(p.read_text()).get("profile", {}).get("info_cache", {})
        return {k: str(v.get("name") or k) for k, v in cache.items()}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def open_command(view: cfgmod.View, profile_dir: str, url: str) -> list[str]:
    if sys.platform == "darwin":
        return ["open", "-na", view.browser, "--args", f"--profile-directory={profile_dir}", url]
    return ["google-chrome", f"--profile-directory={profile_dir}", url]


def trusted_origins(cfg: cfgmod.Config) -> set[str]:
    """Only the board itself may use the opener: the local ports its hubs are
    viewed on (each realm's view_port, plus this machine's own hub port)."""
    ports = {r.view_port for r in cfg.realms.values() if r.view_port}
    if cfg.hub.realm:
        ports.add(cfg.hub.port)
    return {f"http://{h}:{p}" for p in ports for h in ("127.0.0.1", "localhost")}


def token_path(cfg: cfgmod.Config) -> Path:
    return cfg.home / "opener.token"


def load_token(cfg: cfgmod.Config) -> str:
    """Per-user secret for non-browser clients (mode 0600, created on first use)."""
    import os
    import secrets

    p = token_path(cfg)
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        p.parent.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(24)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok


def make_handler(cfg: cfgmod.Config):
    view = cfg.view
    origins = trusted_origins(cfg)
    token = load_token(cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = "wd-opener"
        timeout = 10

        def log_message(self, fmt, *args):
            pass

        def _origin_ok(self) -> bool:
            """Browser calls must come from the board's own origin; calls without
            an Origin (curl, scripts) must present the per-user token instead."""
            if _host(self.headers.get("Host", "")) not in LOOPBACK:
                return False
            origin = self.headers.get("Origin")
            if origin:
                return origin in origins
            return self.headers.get("X-WD-Token") == token

        def _reply(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            if self.command == "POST" and not getattr(self, "_body_read", False):
                # rejected before reading: drain a modest body so the client sees
                # this reply rather than a TCP reset, then drop the connection
                self.close_connection = True
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if 0 < n <= 16 * MAX_BODY and not self.headers.get("Transfer-Encoding") and not self.headers.get("Expect"):
                    try:  # best effort, never wait long for a body we are refusing
                        self.connection.settimeout(1.0)
                        self.rfile.read(n)
                    except OSError:
                        pass
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin and origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):  # CORS preflight, loopback origins only
            if not self._origin_ok():
                return self._reply(403, {"ok": False})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Access-Control-Allow-Methods", "POST, GET")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-WD-Open")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):
            if not self._origin_ok():
                return self._reply(403, {"ok": False, "error": "loopback only"})
            if urlsplit(self.path).path != "/config":
                return self._reply(404, {"ok": False})
            names = chrome_profile_names()
            realms = {r: {"profile": d, "label": view.browser_labels.get(r) or names.get(d, d)} for r, d in view.browser_profiles.items()}
            return self._reply(200, {"ok": True, "realms": realms})

        def do_POST(self):
            if not self._origin_ok() or self.headers.get("X-WD-Open") != "1":
                return self._reply(403, {"ok": False, "error": "forbidden"})
            if urlsplit(self.path).path != "/open":
                return self._reply(404, {"ok": False})
            if self.headers.get("Transfer-Encoding"):
                return self._reply(400, {"ok": False, "error": "chunked bodies not accepted"})
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._reply(411, {"ok": False, "error": "Content-Length required"})
            if not 0 < n <= MAX_BODY:
                return self._reply(413, {"ok": False, "error": "body too large"})
            try:
                self._body_read = True
                req = json.loads(self.rfile.read(n))
                realm, url = str(req["realm"]), str(req["url"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                return self._reply(400, {"ok": False, "error": "bad request"})
            if len(url) > MAX_URL:
                return self._reply(400, {"ok": False, "error": "url too long"})
            profile = view.browser_profiles.get(realm)
            if not profile:
                return self._reply(404, {"ok": False, "error": f"no browser profile for realm {realm!r}"})
            if not url.startswith(ALLOWED_PREFIX) or any(c in url for c in "\r\n\x00 "):
                return self._reply(400, {"ok": False, "error": "only https://claude.ai/ links"})
            try:
                subprocess.Popen(open_command(view, profile, url), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                return self._reply(500, {"ok": False, "error": str(e)})
            return self._reply(200, {"ok": True})

    return Handler


def start(cfg: cfgmod.Config) -> ThreadingHTTPServer | None:
    """Start in a background thread; returns None if not configured or port busy."""
    if not cfg.view.browser_profiles:
        return None
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", cfg.view.opener_port), make_handler(cfg))
    except OSError as e:
        print(f"[wd opener] not started: {e}", file=sys.stderr)
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def serve(cfg: cfgmod.Config) -> int:
    if not cfg.view.browser_profiles:
        print("no [view.browser_profiles] configured", file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg.view.opener_port), make_handler(cfg))
    print(f"wd opener on http://127.0.0.1:{cfg.view.opener_port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
