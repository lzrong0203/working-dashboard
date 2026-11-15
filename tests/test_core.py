"""Core behaviour tests. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from wd import config as cfgmod
from wd import rpc
from wd.collect.claude import ClaudeCollector, TranscriptTail
from wd.hub.server import LOOPBACK, _hostname
from wd.hub.store import Store
from wd.items import Item, ItemError, ItemStore, op_close, op_new, op_set
from wd.snapshot import SessionSnap, redact

SID = "11111111-2222-3333-4444-555555555555"


def write_registry(config_dir: Path, pid: int, **extra) -> None:
    (config_dir / "sessions").mkdir(parents=True, exist_ok=True)
    d = {"pid": pid, "sessionId": SID, "cwd": "/work/demo-project", "status": "busy", "name": "demo",
         "kind": "interactive", "updatedAt": 1_000, "statusUpdatedAt": 900, "startedAt": 100,
         "version": "2.1.0", "tmux": "main:@1.%1", "bridgeSessionId": "session_abc", **extra}
    (config_dir / "sessions" / f"{pid}.json").write_text(json.dumps(d))


def transcript_lines(*records: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cdir = self.tmp / "claude"
        write_registry(self.cdir, os.getpid())
        self.tpath = self.cdir / "projects" / "-work-demo-project" / f"{SID}.jsonl"
        self.tpath.parent.mkdir(parents=True)
        self.tpath.write_text(transcript_lines(
            {"type": "user", "message": {"content": "SECRET BODY must never be read"}, "gitBranch": "main"},
            {"type": "ai-title", "aiTitle": "Demo title"},
            {"type": "system", "subtype": "away_summary", "content": "I am doing X; next Y.", "timestamp": "2026-11-15T01:02:03.000Z"},
            {"type": "last-prompt", "lastPrompt": "please do X"},
        ))

    def test_collect_registry_and_tail(self):
        snaps = ClaudeCollector(self.cdir, "laptop", "personal", "personal").collect()
        self.assertEqual(len(snaps), 1)
        s = snaps[0]
        self.assertEqual((s.status, s.name, s.tmux, s.bridge_session_id), ("busy", "demo", "main:@1.%1", "session_abc"))
        self.assertEqual((s.ai_title, s.last_prompt, s.git_branch), ("Demo title", "please do X", "main"))
        self.assertEqual(s.away_summary, "I am doing X; next Y.")
        self.assertGreater(s.away_summary_at, 0)
        self.assertNotIn("SECRET", json.dumps(s.to_dict()))

    def test_dead_pid_ignored(self):
        write_registry(self.cdir, 999_999_9)
        snaps = ClaudeCollector(self.cdir, "laptop", "personal", "personal").collect()
        self.assertEqual([s.pid for s in snaps], [os.getpid()])

    def test_incremental_tail_and_partial_line(self):
        tail = TranscriptTail(self.tpath)
        tail.update()
        with open(self.tpath, "a") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Newer"}) + "\n")
            f.write('{"type": "last-prompt", "lastPr')  # half-written line
        st = tail.update()
        self.assertEqual(st.ai_title, "Newer")
        self.assertEqual(st.last_prompt, "please do X")  # partial line not consumed yet
        with open(self.tpath, "a") as f:
            f.write('ompt": "finished line"}\n')
        self.assertEqual(tail.update().last_prompt, "finished line")


class SnapshotTest(unittest.TestCase):
    def test_minimal_drops_content_and_clips(self):
        s = SessionSnap(realm="work", host="h", profile="w", pid=1, session_id="x", cwd="/secret/path",
                        ai_title="t" * 500, away_summary="s", last_prompt="p", status="weird")
        full = redact(SessionSnap(**{**s.__dict__}), "full")
        self.assertLessEqual(len(full.ai_title), 120)
        self.assertEqual(full.status, "unknown")
        m = redact(s, "minimal")
        self.assertEqual((m.cwd, m.ai_title, m.away_summary, m.last_prompt), ("", "", "", ""))


class ItemsTest(unittest.TestCase):
    def setUp(self):
        self.store = ItemStore(Path(tempfile.mkdtemp()), autocommit=False)
        self.base = op_new(self.store, {"title": "baseline", "slug": "base", "hypothesis": "h0"})["id"]

    def test_lineage_and_close(self):
        child = op_new(self.store, {"title": "no memory", "from": "base", "rel": "ablation_of", "rel_note": "memory off", "hypothesis": "h1", "session": "laptop/personal/s1"})
        it = self.store.resolve(child["id"])
        self.assertEqual(it.edges(), [{"type": "ablation_of", "to": self.base, "note": "memory off"}])
        self.assertEqual(it.meta["sessions"], ["laptop/personal/s1"])
        op_close(self.store, {"ref": child["id"], "verdict": "refuted", "conclusion": "no drop"})
        it = self.store.resolve(child["id"])
        self.assertEqual((it.meta["status"], it.meta["verdict"]), ("done", "refuted"))
        # round-trips through markdown
        self.assertEqual(Item.from_markdown(it.to_markdown()).meta["verdict"], "refuted")

    def test_one_lineage_edge_only(self):
        other = op_new(self.store, {"title": "other", "hypothesis": "h"})["id"]
        with self.assertRaises(ItemError):
            op_new(self.store, {"title": "x", "from": "base", "edges": [{"type": "variant_of", "to": other}], "hypothesis": "h"})

    def test_missing_target_and_cycle(self):
        with self.assertRaises(ItemError):
            op_new(self.store, {"title": "x", "from": "nope", "hypothesis": "h"})
        b = op_new(self.store, {"title": "b", "from": "base", "hypothesis": "h"})["id"]
        it = self.store.resolve("base")
        it.meta["edges"] = [{"type": "derived_from", "to": b}]
        with self.assertRaises(ItemError):
            self.store.validate(it)

    def test_supersedes_marks_old(self):
        new = op_new(self.store, {"title": "v2", "hypothesis": "h"})["id"]
        op_set(self.store, {"ref": new, "add_edges": [{"type": "supersedes", "to": "base"}]})
        self.assertEqual(self.store.resolve("base").meta["status"], "superseded")


class StoreTest(unittest.TestCase):
    def env(self, *sessions):
        return {"host": "laptop", "profile": "personal", "realm": "personal", "sessions": [s.to_dict() for s in sessions]}

    def snap(self, pid, status):
        return SessionSnap(realm="personal", host="laptop", profile="personal", pid=pid, session_id=f"s{pid}", status=status, status_since=1)

    def test_ingest_transitions_and_gone(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        st.ingest(self.env(self.snap(1, "busy"), self.snap(2, "idle")))
        st.ingest(self.env(self.snap(1, "waiting")))
        state = st.state("personal")
        by = {s["pid"]: s for s in state["sessions"]}
        self.assertEqual((by[1]["status"], by[2]["status"]), ("waiting", "gone"))
        self.assertEqual(state["sessions"][0]["pid"], 1)  # waiting sorts first
        kinds = [(e["from_status"], e["to_status"]) for e in state["events"]]
        self.assertIn(("busy", "waiting"), kinds)
        self.assertIn(("idle", "gone"), kinds)

    def test_notify_once_per_episode(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        self.assertTrue(st.should_notify("k", 3, 3))
        self.assertFalse(st.should_notify("k", 3, 3))
        self.assertFalse(st.should_notify("k", 0, 3))  # episode ends
        self.assertTrue(st.should_notify("k", 3, 3))


class RpcTest(unittest.TestCase):
    def test_refuses_other_realm(self):
        tmp = Path(tempfile.mkdtemp())
        cfgfile = tmp / "c.toml"
        cfgfile.write_text(f'host = "hub"\nhome = "{tmp}"\n[hub]\nrealm = "personal"\ndata_dir = "{tmp}/data"\n')
        cfg = cfgmod.load(cfgfile)
        res = rpc.handle(cfg, {"op": "ingest", "realm": "work", "envelope": {}})
        self.assertFalse(res["ok"])
        self.assertIn("realm", res["error"])


class GuardTest(unittest.TestCase):
    def test_hostname_parsing(self):
        for v in ("localhost:8740", "127.0.0.1:8741", "[::1]:8740", "http://localhost:8740", "localhost"):
            self.assertIn(_hostname(v), LOOPBACK, v)
        for v in ("evil.example:8740", "http://192.168.1.5:8740", ""):
            self.assertNotIn(_hostname(v), LOOPBACK, v)


if __name__ == "__main__":
    unittest.main()
