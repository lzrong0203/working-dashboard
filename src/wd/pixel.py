"""Optional: show every realm's sessions in pixel-agents' office.

pixel-agents (MIT, github.com/pixel-agents-hq/pixel-agents) draws Claude Code
sessions as pixel characters in an office. It watches sessions on its own
machine only. This module lets it show all of them:

* ``wd pixel setup`` writes a layout with one room per machine (named Areas),
  enables "Watch All Sessions" for the local default profile, and (on macOS)
  installs a launchd job running ``pixel-agents --host 127.0.0.1 --no-terminal``.
  It never installs pixel-agents' own Claude hooks.
* the agent's ``bridge`` thread reads every realm's board state (through the
  same view ports the browser uses) and replays it to pixel-agents as the
  Claude Code hook events it already understands (SessionStart, PreToolUse,
  PostToolUse, Notification, Stop, SessionEnd), with only a tool *name*, never
  arguments. Sessions of this machine's ``~/.claude`` are left to pixel-agents'
  own watcher, so nobody appears twice.
* each session's room comes from ``areaMappings`` (name -> machine's Area).

Everything stays on the viewing machine: pixel-agents binds 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from wd import config as cfgmod

HOME = Path(os.environ.get("PIXEL_AGENTS_HOME", Path.home() / ".pixel-agents"))
TOOL_FOR = {"edit": "Edit", "read": "Read", "run": "Bash", "delegate": "Task", "plan": "TodoWrite", "tool": "Tool"}
AREA_COLORS = ["#4c6ef5", "#12b886", "#f76707", "#ae3ec9", "#e64980", "#1098ad"]
PARK_MS = 24 * 3600e3


# ---- layout -------------------------------------------------------------------
def make_layout(hosts: list[str], per_row: int = 4, rows_ws: int = 3) -> dict:
    """One room per host, side by side, doors between them; each room gets
    per_row x rows_ws workstations (desk + PC + bench) and a few wall props."""
    floors = [7, 1, 7, 1, 7, 1]
    inner = per_row * 3 + 1
    rows = 2 + rows_ws * 4 + 2
    cols = 1 + len(hosts) * (inner + 1)
    wall, void = 0, 255
    wall_color = {"h": 214, "s": 30, "b": -100, "c": -55}
    tiles, colors, areas_at = [void] * (cols * rows), [None] * (cols * rows), [None] * (cols * rows)
    furniture = []

    def put(kind, c, r):
        furniture.append({"uid": f"wd-{len(furniture) + 1}", "type": kind, "col": c, "row": r})

    def tile(c, r, v, col=None, area=None):
        i = r * cols + c
        tiles[i], colors[i], areas_at[i] = v, col, area

    for c in range(cols):
        tile(c, 1, wall, wall_color)
    for k, host in enumerate(hosts):
        x0 = 1 + k * (inner + 1)
        floor = floors[k % len(floors)]
        for r in range(2, rows):
            tile(x0 - 1, r, wall, wall_color)
            for c in range(x0, x0 + inner):
                tile(c, r, floor, None, host)
        if k == len(hosts) - 1:
            for r in range(1, rows):
                tile(x0 + inner, r, wall, wall_color)
        if k > 0:
            for r in (rows - 4, rows - 3):
                tile(x0 - 1, r, floor, None, host)
        put("DOUBLE_BOOKSHELF", x0 + 1, 0)
        put("WHITEBOARD", x0 + 5, 0)
        put("CLOCK", x0 + 8, 0)
        put("HANGING_PLANT", x0 + inner - 2, 0)
        for row in range(rows_ws):
            r = 2 + row * 4
            for j in range(per_row):
                c = x0 + j * 3
                put("DESK_FRONT", c, r)
                put("PC_FRONT_OFF", c + 1, r)
                put("CUSHIONED_BENCH", c + 1, r + 2)
        put("PLANT", x0, rows - 2)
        put("COFFEE", x0 + inner - 1, rows - 1)
        put("BIN", x0 + inner - 2, rows - 1)
    return {
        "version": 1, "cols": cols, "rows": rows, "layoutRevision": int(time.time()),
        "tiles": tiles, "tileColors": colors, "furniture": furniture,
        "areas": [{"label": h, "color": AREA_COLORS[i % len(AREA_COLORS)]} for i, h in enumerate(hosts)],
        "areaTiles": areas_at,
    }


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_config() -> dict:
    try:
        d = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def update_settings(**kw) -> None:
    d = _read_config()
    st = d.setdefault("standalone", {})
    if not isinstance(st, dict):
        st = d["standalone"] = {}
    changed = False
    for k, v in kw.items():
        if st.get(k) != v:
            st[k], changed = v, True
    if changed:
        HOME.mkdir(parents=True, exist_ok=True)
        _write_json(HOME / "config.json", d)


# ---- talking to pixel-agents ---------------------------------------------------------
def server() -> tuple[str, str] | None:
    """(base url, token) of a live local pixel-agents server, from its registry."""
    best = None
    for p in sorted((HOME / "servers").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            os.kill(int(d["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if d.get("standalone") and d.get("token") and d.get("port"):
            best = (f"http://127.0.0.1:{int(d['port'])}", str(d["token"]))
    return best


def post_hook(base: str, token: str, event: dict) -> bool:
    req = urllib.request.Request(f"{base}/api/hooks/claude", data=json.dumps(event).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def claude_dir_name(cwd: str) -> str:
    """The label pixel-agents gives a session it watches itself: the last
    dash-separated piece of Claude Code's project-dir name for that cwd."""
    mangled = re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
    parts = [p for p in mangled.split("-") if p]
    return parts[-1] if parts else ""


class Bridge:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.known: dict[str, dict] = {}  # session key -> {sid, label, status, activity, tool}
        self.mappings: dict[str, list[str]] = {}

    def native(self, s: dict) -> bool:
        """Sessions pixel-agents watches itself: this machine's default profile."""
        if s.get("host") != self.cfg.host:
            return False
        prof = next((p for p in self.cfg.profiles if p.label == s.get("profile")), None)
        return bool(prof) and prof.config_dir.resolve() == (Path.home() / ".claude").resolve()

    def sessions(self) -> list[dict] | None:
        out, ok = [], False
        for name, r in self.cfg.realms.items():
            port = r.view_port or (self.cfg.hub.port if self.cfg.hub.realm == name else 0)
            if not port:
                continue
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=4) as resp:
                    st = json.loads(resp.read())
                ok = True
                out += [s for s in st.get("sessions", []) if s.get("status") != "gone"]
            except (OSError, ValueError):
                continue
        return out if ok else None

    def step(self) -> None:
        srv = server()
        live = self.sessions()
        if not srv or live is None:
            return
        base, token = srv
        now = time.time() * 1000
        live = [s for s in live if not (s.get("status") == "idle" and now - (s.get("status_since") or now) > PARK_MS)]
        # labels: the session's name, made unique across machines
        names: dict[str, int] = {}
        for s in live:
            n = s.get("name") or str(s.get("session_id", ""))[:8]
            names[n] = names.get(n, 0) + 1
        mappings = {}
        for s in live:
            host = s.get("host", "")
            if self.native(s):
                label = claude_dir_name(s.get("cwd", ""))
            else:
                n = s.get("name") or str(s.get("session_id", ""))[:8]
                label = n if names[n] == 1 else f"{n}@{host}"
            if label:
                mappings[label] = [host]
            s["_label"] = label
        if mappings != self.mappings:
            update_settings(areaMappings=mappings, watchAllSessions=True, alwaysShowLabels=True)
            self.mappings = mappings
        seen = set()
        for s in live:
            if self.native(s) or not s["_label"]:
                continue
            key = s["key"]
            seen.add(key)
            sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wd:{key}:{s.get('session_id')}"))
            cwd = f"/wd/{s.get('host', '')}/{s['_label']}"
            k = self.known.get(key)
            ev = lambda name, **kw: post_hook(base, token, {"session_id": sid, "hook_event_name": name, "cwd": cwd, **kw})  # noqa: E731
            if not k or k["sid"] != sid:
                if not ev("SessionStart", source="startup", transcript_path=""):
                    continue
                ev("Stop")
                k = self.known[key] = {"sid": sid, "status": "idle", "activity": "", "tool": ""}
            status, act = s.get("status"), s.get("activity") or ""
            if status == k["status"] and (status != "busy" or act == k["activity"]):
                continue
            if k["tool"]:
                ev("PostToolUse", tool_name=TOOL_FOR.get(k["activity"], "Tool"), tool_use_id=k["tool"], tool_input={})
                k["tool"] = ""
            if status == "busy":
                k["tool"] = f"wd-{uuid.uuid4().hex[:10]}"
                ev("PreToolUse", tool_name=TOOL_FOR.get(act, "Tool"), tool_use_id=k["tool"], tool_input={})
            elif status == "waiting":  # pixel-agents flags permission on the running tool, so start one first
                k["tool"] = f"wd-{uuid.uuid4().hex[:10]}"
                ev("PreToolUse", tool_name=TOOL_FOR.get(act, "Tool"), tool_use_id=k["tool"], tool_input={})
                ev("Notification", notification_type="permission_prompt", message="waiting for you")
            else:
                ev("Stop")
            k["status"], k["activity"] = status, act
        for key in [k for k in self.known if k not in seen]:
            post_hook(base, token, {"session_id": self.known[key]["sid"], "hook_event_name": "SessionEnd", "reason": "other",
                                    "cwd": "/wd"})
            del self.known[key]


def run_bridge(cfg: cfgmod.Config, every_s: float = 5.0, stop=None) -> None:
    b, last_err = Bridge(cfg), ""
    while not (stop and stop.is_set()):
        try:
            b.step()
            last_err = ""
        except Exception as e:  # the bridge must never take the agent down
            if repr(e) != last_err:
                print(f"[wd agent] pixel bridge: {e!r}", file=sys.stderr)
                last_err = repr(e)
        time.sleep(every_s)


# ---- setup ---------------------------------------------------------------------------
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.github.workdash.pixel</string>
  <key>ProgramArguments</key><array>
    <string>{node}</string><string>{cli}</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>{port}</string><string>--no-terminal</string>
  </array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def setup(cfg: cfgmod.Config, hosts: list[str], cli: str, node: str, port: int = 3100) -> list[str]:
    done = []
    HOME.mkdir(parents=True, exist_ok=True)
    _write_json(HOME / "layout.json", make_layout(hosts))
    done.append(f"layout: one room per machine ({', '.join(hosts)}) -> {HOME / 'layout.json'}")
    update_settings(watchAllSessions=True, alwaysShowLabels=True, showAreas=True, soundEnabled=False, hooksInfoShown=True)
    done.append("settings: Watch All Sessions on, labels always shown, sound off")
    if sys.platform == "darwin":
        workdir = cfg.home / "pixel"
        workdir.mkdir(parents=True, exist_ok=True)
        plist = Path.home() / "Library/LaunchAgents/io.github.workdash.pixel.plist"
        plist.write_text(PLIST.format(node=node, cli=cli, port=port, workdir=workdir, log=workdir / "pixel.log"))
        done.append(f"launchd job: {plist} (load with: launchctl bootstrap gui/$(id -u) {plist})")
    return done
