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
import plistlib
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


class UnreadableConfig(Exception):
    pass


def _read_config() -> dict:
    """pixel-agents' config; {} when it doesn't exist yet. A file that exists
    but can't be parsed raises: overwriting it would throw away its settings."""
    try:
        d = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise UnreadableConfig(f"{HOME / 'config.json'}: {e}") from e
    if not isinstance(d, dict):
        raise UnreadableConfig(f"{HOME / 'config.json'}: not a JSON object")
    return d


OWNED = "wd-owned.json"  # which areaMappings keys the bridge wrote, so others' mappings survive


def _owned() -> set[str]:
    try:
        return {x for x in json.loads((HOME / OWNED).read_text(encoding="utf-8")) if isinstance(x, str)}
    except (OSError, ValueError, TypeError):
        return set()


def update_settings(area_mappings: dict | None = None, **kw) -> None:
    d = _read_config()
    st = d.setdefault("standalone", {})
    if not isinstance(st, dict):
        st = d["standalone"] = {}
    changed = False
    for k, v in kw.items():
        if st.get(k) != v:
            st[k], changed = v, True
    if area_mappings is not None:
        old = st.get("areaMappings") if isinstance(st.get("areaMappings"), dict) else {}
        mine = _owned()
        merged = {k: v for k, v in old.items() if k not in mine and k not in area_mappings} | area_mappings
        if merged != old:
            st["areaMappings"], changed = merged, True
        if set(area_mappings) != mine:
            HOME.mkdir(parents=True, exist_ok=True)
            _write_json(HOME / OWNED, sorted(area_mappings))
    if changed:
        HOME.mkdir(parents=True, exist_ok=True)
        _write_json(HOME / "config.json", d)


# ---- talking to pixel-agents ---------------------------------------------------------
def server() -> tuple[str, str, int] | None:
    """(base url, token, pid) of a live local pixel-agents server, from its registry."""
    best = None
    for p in sorted((HOME / "servers").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            os.kill(int(d["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if d.get("standalone") and d.get("token") and d.get("port"):
            best = (f"http://127.0.0.1:{int(d['port'])}", str(d["token"]), int(d["pid"]))
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
        self.server_id: tuple | None = None
        self.cfg_err = ""

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
        base, token, _pid = srv
        if srv != self.server_id:  # a restarted server knows none of our sessions: replay them all
            self.known.clear()
            self.server_id = srv
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
            try:
                update_settings(area_mappings=mappings, watchAllSessions=True, alwaysShowLabels=True)
                self.mappings = mappings
            except UnreadableConfig as e:  # leave the file alone; characters still move, seats may be unassigned
                if str(e) != self.cfg_err:
                    print(f"[wd agent] pixel bridge: not touching settings: {e}", file=sys.stderr)
                    self.cfg_err = str(e)
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
                if not (ev("SessionStart", source="startup", transcript_path="") and ev("Stop")):
                    continue
                k = self.known[key] = {"sid": sid, "status": "idle", "activity": "", "tool": ""}
            status, act = s.get("status"), s.get("activity") or ""
            if status == k["status"] and (status != "busy" or act == k["activity"]):
                continue
            # the cached state only moves once the server took every event; otherwise retry next step
            if k["tool"]:
                if not ev("PostToolUse", tool_name=TOOL_FOR.get(k["activity"], "Tool"), tool_use_id=k["tool"], tool_input={}):
                    continue
                k["tool"] = ""
            if status in ("busy", "waiting"):  # pixel-agents flags permission on the running tool, so start one first
                tool = f"wd-{uuid.uuid4().hex[:10]}"
                if not ev("PreToolUse", tool_name=TOOL_FOR.get(act, "Tool"), tool_use_id=tool, tool_input={}):
                    continue
                k["tool"] = tool
                if status == "waiting" and not ev("Notification", notification_type="permission_prompt", message="waiting for you"):
                    continue
            elif not ev("Stop"):
                continue
            k["status"], k["activity"] = status, act
        for key in [k for k in self.known if k not in seen]:
            if post_hook(base, token, {"session_id": self.known[key]["sid"], "hook_event_name": "SessionEnd", "reason": "other",
                                       "cwd": "/wd"}):
                del self.known[key]  # kept on failure, so the end is sent again


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
def plist_for(node: str, cli: str, port: int, workdir: Path) -> bytes:
    return plistlib.dumps({
        "Label": "io.github.workdash.pixel",
        "ProgramArguments": [node, cli, "--host", "127.0.0.1", "--port", str(port), "--no-terminal"],
        "WorkingDirectory": str(workdir), "RunAtLoad": True, "KeepAlive": True, "Nice": 10,
        "StandardOutPath": str(workdir / "pixel.log"), "StandardErrorPath": str(workdir / "pixel.log"),
    })


def setup(cfg: cfgmod.Config, hosts: list[str], cli: str, node: str, port: int = 3100, replace_layout: bool = False) -> list[str]:
    done = []
    HOME.mkdir(parents=True, exist_ok=True)
    lp = HOME / "layout.json"
    if lp.exists() and not replace_layout:
        done.append(f"layout: kept your existing {lp} (pass --replace-layout to rebuild it; the old one is backed up)")
    else:
        if lp.exists():
            backup = lp.with_name(f"layout.{time.strftime('%Y%m%d-%H%M%S')}.json")
            os.replace(lp, backup)
            done.append(f"layout: previous one saved as {backup.name}")
        _write_json(lp, make_layout(hosts))
        done.append(f"layout: one room per machine ({', '.join(hosts)}) -> {lp}")
    update_settings(watchAllSessions=True, alwaysShowLabels=True, showAreas=True, soundEnabled=False, hooksInfoShown=True)
    done.append("settings: Watch All Sessions on, labels always shown, sound off")
    if sys.platform == "darwin":
        workdir = cfg.home / "pixel"
        workdir.mkdir(parents=True, exist_ok=True)
        plist = Path.home() / "Library/LaunchAgents/io.github.workdash.pixel.plist"
        plist.write_bytes(plist_for(node, cli, port, workdir))
        done.append(f"launchd job: {plist} (load with: launchctl bootstrap gui/$(id -u) {plist})")
    return done
