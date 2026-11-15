"""Open claude.ai session links in the right browser profile, per realm.

Many people keep work and personal Claude accounts apart with separate browser
profiles. A web page cannot choose a browser profile, so the board hands the
click to this helper on the viewing machine, which runs e.g.

    open -na "Google Chrome" --args --profile-directory="Profile 2" <url>

Guard rails: binds 127.0.0.1; loopback Host/Origin only; only POST with a
custom header (so a foreign page can't trigger it without a CORS preflight we
refuse); only https://claude.ai/ URLs; only realms that are configured. Runs as
a thread inside ``wd agent`` (or standalone via ``wd opener``).
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


def make_handler(cfg: cfgmod.Config):
    view = cfg.view

    class Handler(BaseHTTPRequestHandler):
        server_version = "wd-opener"
        timeout = 10

        def log_message(self, fmt, *args):
            pass

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return _host(self.headers.get("Host", "")) in LOOPBACK and (not origin or _host(origin) in LOOPBACK)

        def _reply(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin and _host(origin) in LOOPBACK:
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
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                req = json.loads(self.rfile.read(n) or b"{}")
                realm, url = str(req["realm"]), str(req["url"])
            except (ValueError, KeyError, json.JSONDecodeError):
                return self._reply(400, {"ok": False, "error": "bad request"})
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
