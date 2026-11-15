"""The hub's SQLite store. One file per realm, WAL mode.

Writers (``wd rpc`` processes spawned by ssh) and the reader (``wd serve``) are
separate processes; the server notices commits through ``PRAGMA data_version``
and pushes fresh state to browsers over SSE. No IPC needed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from wd.attention import LEVEL_NAMES, attention
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
  key TEXT PRIMARY KEY, level INTEGER, at INTEGER
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

EVENT_RETENTION_DAYS = 14
GONE_KEEP_S = 6 * 3600  # ended sessions stay visible (greyed) for a while


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def data_version(self) -> int:
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    # -- ingest --------------------------------------------------------------
    def ingest(self, env: dict) -> dict:
        """Apply one full snapshot from (host, profile). Idempotent."""
        host, profile, realm = env["host"], env["profile"], env["realm"]
        ts = now_ms()
        incoming = {s.key: s for s in (SessionSnap.from_dict(d) for d in env.get("sessions", []))}
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(host,profile) DO UPDATE SET realm=excluded.realm, last_push=excluded.last_push, agent_version=excluded.agent_version",
            (host, profile, realm, ts, env.get("agent_version", "")),
        )
        existing = {
            r["key"]: r
            for r in cur.execute("SELECT key, status, session_id, data FROM sessions WHERE host=? AND profile=?", (host, profile))
        }
        for key, s in incoming.items():
            old = existing.get(key)
            if old is None or old["status"] != s.status:
                cur.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?)",
                    (ts, key, s.session_id, s.name, old["status"] if old else None, s.status),
                )
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
                cur.execute(
                    "UPDATE sessions SET status='gone', status_since=?, data=? WHERE key=?",
                    (ts, json.dumps(d, ensure_ascii=False), key),
                )
                cur.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (ts, key, old["session_id"], d.get("name", ""), old["status"], "gone"))
        cur.execute("DELETE FROM sessions WHERE status='gone' AND status_since < ?", (ts - GONE_KEEP_S * 1000,))
        cur.execute("DELETE FROM events WHERE ts < ?", (ts - EVENT_RETENTION_DAYS * 86400_000,))
        self.db.commit()
        return {"ok": True, "sessions": len(incoming)}

    # -- items index ---------------------------------------------------------
    def reindex_items(self, items: ItemStore) -> int:
        rows = [(it.id, it.meta.get("type"), it.meta.get("status"), json.dumps({**it.meta, "body": it.body}, ensure_ascii=False, default=str), str(it.meta.get("updated", ""))) for it in items.all()]
        cur = self.db.cursor()
        cur.execute("DELETE FROM items")
        cur.executemany("INSERT INTO items VALUES (?,?,?,?,?)", rows)
        cur.execute("INSERT INTO meta VALUES ('items_mtime', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(items.mtime()),))
        self.db.commit()
        return len(rows)

    def items_mtime(self) -> float:
        r = self.db.execute("SELECT v FROM meta WHERE k='items_mtime'").fetchone()
        return float(r[0]) if r else -1.0

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
            for ref in it.get("sessions") or []:
                by_session.setdefault(str(ref).rsplit("/", 1)[-1], []).append(it["id"])
        sessions = []
        for r in self.db.execute("SELECT data FROM sessions"):
            s = json.loads(r["data"])
            src = sources.get((s["host"], s["profile"]))
            age = src["age_s"] if src else 1e9
            level, reason = attention(s, ts, age)
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
            "events": recent,
        }

    # -- notification bookkeeping -------------------------------------------
    def should_notify(self, key: str, level: int, threshold: int) -> bool:
        """True exactly once per episode of level >= threshold."""
        row = self.db.execute("SELECT level FROM notified WHERE key=?", (key,)).fetchone()
        if level < threshold:
            if row:
                self.db.execute("DELETE FROM notified WHERE key=?", (key,))
                self.db.commit()
            return False
        if row:
            return False
        self.db.execute("INSERT INTO notified VALUES (?,?,?)", (key, level, now_ms()))
        self.db.commit()
        return True
