"""Collect live Claude Code sessions from one config dir.

Sources, cheapest first:

1. ``<config_dir>/sessions/<pid>.json`` -- Claude Code's own live registry
   (status busy/idle/waiting, name, cwd, tmux pane, Remote Control id).
   Reading a few small JSON files costs ~nothing, so we do it every poll.
2. The tail of each session's transcript ``projects/*/<sessionId>.jsonl`` --
   only the newest ``ai-title``, ``last-prompt``, ``away_summary`` (Claude's own
   "what I'm doing / what's next" note) and ``gitBranch``. Read incrementally:
   after the first read we only parse bytes appended since the last poll.
3. ``claude agents --json`` -- the documented interface, used only as a fallback
   when the registry dir is missing (it spawns a ~150 MB process each call).

Nothing here reads message bodies, tool calls or tool results.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wd.snapshot import SessionSnap

FIRST_READ_BYTES = 4 * 1024 * 1024
MAX_CATCHUP_BYTES = 8 * 1024 * 1024


def _iso_ms(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class TailState:
    offset: int = 0
    ai_title: str = ""
    last_prompt: str = ""
    away_summary: str = ""
    away_summary_at: int = 0
    git_branch: str = ""


class TranscriptTail:
    """Incrementally extracts summary fields from one transcript file."""

    def __init__(self, path: Path):
        self.path = path
        self.state = TailState()

    def update(self) -> TailState:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return self.state
        st = self.state
        if size < st.offset:  # truncated or replaced
            st.offset = 0
        start = st.offset
        if start == 0 or size - start > MAX_CATCHUP_BYTES:
            start = max(0, size - FIRST_READ_BYTES)
        if start >= size:
            return st
        with open(self.path, "rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
        if start > 0 and start != st.offset:
            # we jumped into the middle of a line; drop the partial first line
            nl = chunk.find(b"\n")
            chunk = chunk[nl + 1 :] if nl >= 0 else b""
        # keep an unterminated last line for the next poll
        end = chunk.rfind(b"\n")
        if end < 0:
            return st
        consumed = chunk[: end + 1]
        leftover = len(chunk) - (end + 1)  # bytes of the unterminated last line
        st.offset = size - leftover  # chunk always ends at `size`
        for line in consumed.splitlines():
            self._absorb(line)
        return st

    def _absorb(self, line: bytes) -> None:
        # cheap prefilter: only parse the few record types we care about
        if not (b'"ai-title"' in line or b'"last-prompt"' in line or b"away_summary" in line or b'"gitBranch"' in line):
            return
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        t = d.get("type")
        st = self.state
        if t == "ai-title" and d.get("aiTitle"):
            st.ai_title = str(d["aiTitle"])
        elif t == "last-prompt" and d.get("lastPrompt"):
            st.last_prompt = str(d["lastPrompt"])
        elif t == "system" and d.get("subtype") == "away_summary" and d.get("content"):
            st.away_summary = str(d["content"])
            st.away_summary_at = _iso_ms(d.get("timestamp", ""))
        if d.get("gitBranch"):
            st.git_branch = str(d["gitBranch"])


class ClaudeCollector:
    """Keeps per-session transcript tails between polls for one config dir."""

    def __init__(self, config_dir: Path, host: str, profile: str, realm: str):
        self.config_dir = config_dir
        self.host = host
        self.profile = profile
        self.realm = realm
        self._tails: dict[str, TranscriptTail] = {}

    def _registry(self) -> list[dict]:
        sdir = self.config_dir / "sessions"
        if not sdir.is_dir():
            return self._via_cli()
        out = []
        for p in sdir.glob("*.json"):
            try:
                d = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            pid = d.get("pid")
            if isinstance(pid, int) and pid_alive(pid):
                out.append(d)
        return out

    def _via_cli(self) -> list[dict]:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config_dir))
        try:
            r = subprocess.run(["claude", "agents", "--json"], env=env, capture_output=True, text=True, timeout=30)
            return [d for d in json.loads(r.stdout or "[]") if isinstance(d, dict)]
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return []

    def _tail_for(self, session_id: str) -> TailState | None:
        if not session_id:
            return None
        tail = self._tails.get(session_id)
        if tail is None:
            hits = list((self.config_dir / "projects").glob(f"*/{session_id}.jsonl"))
            if not hits:
                return None
            tail = self._tails[session_id] = TranscriptTail(hits[0])
        return tail.update()

    def collect(self) -> list[SessionSnap]:
        snaps = []
        live_ids = set()
        for d in self._registry():
            sid = str(d.get("sessionId", ""))
            live_ids.add(sid)
            t = self._tail_for(sid)
            snaps.append(
                SessionSnap(
                    realm=self.realm,
                    host=self.host,
                    profile=self.profile,
                    pid=int(d.get("pid", 0)),
                    session_id=sid,
                    name=str(d.get("name", "")),
                    kind=str(d.get("kind", "interactive")),
                    status=str(d.get("status", "unknown")),
                    status_since=int(d.get("statusUpdatedAt") or d.get("updatedAt") or 0),
                    updated_at=int(d.get("updatedAt") or 0),
                    started_at=int(d.get("startedAt") or 0),
                    version=str(d.get("version", "")),
                    cwd=str(d.get("cwd", "")),
                    tmux=str(d.get("tmux") or ""),
                    bridge_session_id=str(d.get("bridgeSessionId") or ""),
                    git_branch=t.git_branch if t else "",
                    ai_title=t.ai_title if t else "",
                    away_summary=t.away_summary if t else "",
                    away_summary_at=t.away_summary_at if t else 0,
                    last_prompt=t.last_prompt if t else "",
                )
            )
        for sid in list(self._tails):  # forget tails of ended sessions
            if sid not in live_ids:
                del self._tails[sid]
        return snaps
