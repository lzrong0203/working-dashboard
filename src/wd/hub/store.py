"""The hub's SQLite store. One file per realm, WAL mode.

Writers (``wd rpc`` processes spawned by ssh) and the reader (``wd serve``) are
separate processes; the server notices commits through ``PRAGMA data_version``
and pushes fresh state to browsers over SSE. No IPC needed.

Every write runs inside ``with self.db:`` so a failure rolls back instead of
leaving a transaction (and SQLite's writer lock) open.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from wd.attention import LEVEL_NAMES, OK, attention
from wd.items import ItemStore
from wd.snapshot import SessionSnap

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  key TEXT PRIMARY KEY, realm TEXT, host TEXT, profile TEXT,
  session_id TEXT, status TEXT, status_since INTEGER, data TEXT, last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS sources (
  host TEXT, profile TEXT, realm TEXT, last_push INTEGER, agent_version TEXT,
  PRIMARY KEY (host, profile)
);
CREATE TABLE IF NOT EXISTS events (
  ts INTEGER, key TEXT, session_id TEXT, name TEXT, from_status TEXT, to_status TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, type TEXT, status TEXT, data TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS notified (
  key TEXT PRIMARY KEY, session_id TEXT, episode INTEGER, level INTEGER, at INTEGER
);
CREATE TABLE IF NOT EXISTS requests (
  rid TEXT PRIMARY KEY, ts INTEGER, response TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

EVENT_RETENTION_DAYS = 14
REQUEST_RETENTION_DAYS = 30
GONE_KEEP_S = 6 * 3600  # ended sessions stay visible (greyed) for a while


def now_ms() -> int:
    return int(time.time() * 1000)


def episode_of(s: dict) -> int:
    return int(s.get("status_since") or 0)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(notified)")}
        if "episode" not in cols:  # older schema; markers are disposable
            self.db.execute("DROP TABLE notified")
            self.db.executescript(SCHEMA)
        self.db.commit()

    def data_version(self) -> int:
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    def _meta(self, k: str, default: str = "") -> str:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    # -- ingest --------------------------------------------------------------
    def ingest(self, env: dict) -> dict:
        """Apply one full snapshot from (host, profile). Idempotent."""
        host, profile, realm = env["host"], env["profile"], env["realm"]
        ts = now_ms()
        incoming = {s.key: s for s in (SessionSnap.from_dict(d) for d in env.get("sessions", []))}
        with self.db:
            cur = self.db.cursor()
            cur.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(host,profile) DO UPDATE SET realm=excluded.realm, last_push=excluded.last_push, agent_version=excluded.agent_version",
                (host, profile, realm, ts, env.get("agent_version", "")),
            )
            existing = {
                r["key"]: r
                for r in cur.execute("SELECT key, status, session_id, data FROM sessions WHERE host=? AND profile=?", (host, profile))
            }
            event = "INSERT INTO events VALUES (?,?,?,?,?,?)"
            for key, s in incoming.items():
                old = existing.get(key)
                if old is not None and old["session_id"] != s.session_id:
                    # same pid, different session: pid reuse or /clear. Close the old
                    # episode explicitly so transitions and alerts aren't swallowed.
                    oldname = json.loads(old["data"]).get("name", "")
                    cur.execute(event, (ts, key, old["session_id"], oldname, old["status"], "gone"))
                    cur.execute("DELETE FROM notified WHERE key=?", (key,))
                    old = None
                if old is None or old["status"] != s.status:
                    cur.execute(event, (ts, key, s.session_id, s.name, old["status"] if old else None, s.status))
                cur.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "realm=excluded.realm, session_id=excluded.session_id, status=excluded.status, "
                    "status_since=excluded.status_since, data=excluded.data, last_seen=excluded.last_seen",
                    (key, realm, host, profile, s.session_id, s.status, s.status_since, json.dumps(s.to_dict(), ensure_ascii=False), ts),
                )
            for key, old in existing.items():  # sessions that ended since the last push
                if key not in incoming and old["status"] != "gone":
                    d = json.loads(old["data"])
                    d["status"], d["status_since"] = "gone", ts
                    cur.execute("UPDATE sessions SET status='gone', status_since=?, data=? WHERE key=?", (ts, json.dumps(d, ensure_ascii=False), key))
                    cur.execute(event, (ts, key, old["session_id"], d.get("name", ""), old["status"], "gone"))
            cur.execute("DELETE FROM sessions WHERE status='gone' AND status_since < ?", (ts - GONE_KEEP_S * 1000,))
            cur.execute("DELETE FROM events WHERE ts < ?", (ts - EVENT_RETENTION_DAYS * 86400_000,))
            cur.execute("DELETE FROM requests WHERE ts < ?", (ts - REQUEST_RETENTION_DAYS * 86400_000,))
        return {"ok": True, "sessions": len(incoming)}

    # -- idempotent requests --------------------------------------------------
    def seen_request(self, rid: str) -> dict | None:
        r = self.db.execute("SELECT response FROM requests WHERE rid=?", (rid,)).fetchone()
        return json.loads(r[0]) if r else None

    def remember_request(self, rid: str, response: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO requests VALUES (?,?,?)", (rid, now_ms(), json.dumps(response, ensure_ascii=False, default=str)))

    # -- items index ---------------------------------------------------------
    def reindex_items(self, items: ItemStore) -> int:
        """Rebuild the index from the card files. Retries if cards change mid-scan."""
        for _ in range(3):
            before = items.fingerprint()
            cards = items.all()
            if items.fingerprint() == before:
                break
        rows = [(it.id, str(it.meta.get("type")), str(it.meta.get("status")), json.dumps({**it.meta, "body": it.body}, ensure_ascii=False, default=str), str(it.meta.get("updated", ""))) for it in cards]
        with self.db:
            self.db.execute("DELETE FROM items")
            self.db.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)", rows)
            for k, v in (("items_fp", before), ("items_errors", json.dumps(items.errors, ensure_ascii=False))):
                self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        return len(rows)

    def items_fingerprint(self) -> str:
        return self._meta("items_fp", "-")

    # -- read model ----------------------------------------------------------
    def state(self, realm: str) -> dict:
        ts = now_ms()
        sources = {
            (r["host"], r["profile"]): dict(r, age_s=(ts - r["last_push"]) / 1000)
            for r in self.db.execute("SELECT * FROM sources")
        }
        items = [json.loads(r["data"]) for r in self.db.execute("SELECT data FROM items ORDER BY updated DESC")]
        by_session: dict[str, list[str]] = {}
        for it in items:
            refs = it.get("sessions")
            for ref in refs if isinstance(refs, list) else []:
                by_session.setdefault(str(ref).rsplit("/", 1)[-1], []).append(it["id"])
        sessions = []
        for r in self.db.execute("SELECT data FROM sessions"):
            s = json.loads(r["data"])
            src = sources.get((s["host"], s["profile"]))
            age = src["age_s"] if src else 1e9
            try:
                level, reason = attention(s, ts, age)
            except Exception as e:  # user policy code must not break the board
                print(f"[wd] attention() failed: {e!r}", file=sys.stderr)
                level, reason = OK, ""
            s["attention"] = {"level": level, "name": LEVEL_NAMES.get(level, str(level)), "reason": reason}
            s["source_age_s"] = age
            s["linked_items"] = by_session.get(s["session_id"], [])
            sessions.append(s)
        sessions.sort(key=lambda s: (-s["attention"]["level"], s["status"] == "gone", -(s.get("status_since") or 0)))
        recent = [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 200")]
        return {
            "realm": realm,
            "now": ts,
            "sessions": sessions,
            "sources": list(sources.values()),
            "items": items,
            "item_errors": json.loads(self._meta("items_errors", "[]")),
            "events": recent,
        }

    # -- notification bookkeeping -------------------------------------------
    def notify_candidates(self, sessions: list[dict], threshold: int) -> list[dict]:
        """Sessions at/above threshold not yet notified in this episode.

        Markers of sessions that dropped below the threshold are cleared, so the
        next time they cross it they notify again. Marking happens only after a
        push was actually delivered (``mark_notified``).
        """
        marked = {r["key"]: (r["session_id"], r["episode"]) for r in self.db.execute("SELECT key, session_id, episode FROM notified")}
        out, clear = [], []
        for s in sessions:
            hot = s["attention"]["level"] >= threshold and s["status"] != "gone"
            # an episode = one stretch in the same status (identified by when it began)
            if hot and marked.get(s["key"]) != (s["session_id"], episode_of(s)):
                out.append(s)
            elif not hot and s["key"] in marked:
                clear.append(s["key"])
        if clear:
            with self.db:
                self.db.executemany("DELETE FROM notified WHERE key=?", [(k,) for k in clear])
        return out

    def mark_notified(self, key: str, session_id: str, episode: int, level: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO notified VALUES (?,?,?,?,?)", (key, session_id, episode, level, now_ms()))
