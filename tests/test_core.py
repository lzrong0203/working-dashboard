"""Core behaviour tests. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import tempfile
import sys
import time
import unittest
from datetime import datetime, timezone
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

    def test_replaced_transcript_resets_state(self):
        tail = TranscriptTail(self.tpath)
        self.assertEqual(tail.update().ai_title, "Demo title")
        size = self.tpath.stat().st_size
        repl = self.tpath.with_suffix(".new")
        line = json.dumps({"type": "last-prompt", "lastPrompt": "x"}) + "\n"
        repl.write_text((line * (size // len(line) + 1))[:size - 1] + "\n")  # same size, new inode
        os.replace(repl, self.tpath)
        st = tail.update()
        self.assertEqual(st.ai_title, "")  # stale title dropped


class SnapshotTest(unittest.TestCase):
    def test_minimal_drops_content_and_clips(self):
        s = SessionSnap(realm="work", host="h", profile="w", pid=1, session_id="x", cwd="/secret/path",
                        ai_title="t" * 500, away_summary="s", last_prompt="p", status="weird")
        full = redact(SessionSnap(**{**s.__dict__}), "full")
        self.assertLessEqual(len(full.ai_title), 120)
        self.assertEqual(full.status, "unknown")
        s.bridge_session_id = "session_x"
        m = redact(s, "minimal")
        self.assertEqual((m.cwd, m.ai_title, m.away_summary, m.last_prompt, m.bridge_session_id), ("", "", "", "", ""))


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

    def test_supersedes_at_creation_marks_old(self):
        op_new(self.store, {"title": "v2", "from": "base", "rel": "supersedes", "hypothesis": "h"})
        self.assertEqual(self.store.resolve("base").meta["status"], "superseded")

    def test_rejected_update_changes_nothing(self):
        new = op_new(self.store, {"title": "v2", "hypothesis": "h"})["id"]
        with self.assertRaises(ItemError):
            op_set(self.store, {"ref": new, "fields": {"status": "bogus"}, "add_edges": [{"type": "supersedes", "to": "base"}]})
        self.assertEqual(self.store.resolve("base").meta["status"], "running")

    def test_ids_cannot_escape_and_never_overwrite(self):
        for bad in ("../escaped", "a/b", "..", ".hidden", "x" * 200):
            with self.assertRaises(ItemError):
                op_new(self.store, {"title": "x", "id": bad, "hypothesis": "h"})
        self.assertFalse((self.store.root / "escaped.md").exists())
        with self.assertRaises(ItemError):
            op_new(self.store, {"title": "again", "id": self.base, "hypothesis": "h"})

    def test_fingerprint_sees_deletions(self):
        b = op_new(self.store, {"title": "b", "hypothesis": "h"})["id"]
        fp = self.store.fingerprint()
        (self.store.dir / f"{self.base}.md").unlink()  # not the newest card
        self.assertNotEqual(fp, self.store.fingerprint())
        self.assertTrue(b)


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

    def test_notify_once_per_episode_and_only_after_delivery(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        hot = {"key": "k", "session_id": "s1", "status": "waiting", "status_since": 100, "attention": {"level": 3}}
        cold = {**hot, "status": "busy", "status_since": 200, "attention": {"level": 0}}
        self.assertEqual(len(st.notify_candidates([hot], 3)), 1)
        self.assertEqual(len(st.notify_candidates([hot], 3)), 1)  # not delivered yet -> still pending
        st.mark_notified("k", "s1", 100, 3)
        self.assertEqual(st.notify_candidates([hot], 3), [])  # delivered
        st.notify_candidates([cold], 3)  # episode ends
        again = {**hot, "status_since": 300}
        self.assertEqual(len(st.notify_candidates([again], 3)), 1)
        self.assertEqual(len(st.notify_candidates([{**again, "session_id": "s2"}], 3)), 1)  # pid reused

    def test_stale_delivery_does_not_suppress_next_episode(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        first = {"key": "k", "session_id": "s1", "status": "waiting", "status_since": 100, "attention": {"level": 3}}
        st.notify_candidates([{**first, "status": "busy", "attention": {"level": 0}}], 3)  # went busy...
        st.mark_notified("k", "s1", 100, 3)  # ...then the old job finished
        second = {**first, "status_since": 500}
        self.assertEqual(len(st.notify_candidates([second], 3)), 1)

    def test_notifier_never_requeues_a_delivered_episode(self):
        from wd.config import Notify
        from wd.hub import notify

        sent, marks = [], []
        orig = notify.post
        notify.post = lambda n, title, msg: sent.append(msg) or True
        try:
            nt = notify.Notifier(Notify(ntfy_topic="t"), lambda *a: marks.append(a))
            job = lambda: notify.Job([("k", "s1", 100, 3)], "t", "m")
            nt.submit([job()])
            for _ in range(100):
                if marks:
                    break
                time.sleep(0.01)
            nt.submit([job()])  # stale candidate list from before the marker
            time.sleep(0.05)
        finally:
            notify.post = orig
        self.assertEqual(len(sent), 1)

    def test_session_id_change_on_same_pid_is_a_new_session(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        a = self.snap(1, "waiting")
        st.ingest(self.env(a))
        b = self.snap(1, "waiting")
        b.session_id = "other"
        st.ingest(self.env(b))
        ev = [(e["session_id"], e["from_status"], e["to_status"]) for e in st.state("personal")["events"]]
        self.assertIn(("s1", "waiting", "gone"), ev)
        self.assertIn(("other", None, "waiting"), ev)

    def test_malformed_field_types_are_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        items = ItemStore(tmp, autocommit=False)
        (tmp / "items").mkdir(parents=True)
        (tmp / "items" / "bad.md").write_text("---\nid: bad-1\ntype: experiment\ntitle: t\nstatus: running\nsessions: 123\n---\n")
        st = Store(tmp / "t.db")
        self.assertEqual(st.reindex_items(items), 0)
        self.assertIn("sessions", st.state("personal")["item_errors"][0])

    def test_reindex_survives_bad_and_duplicate_cards(self):
        tmp = Path(tempfile.mkdtemp())
        items = ItemStore(tmp, autocommit=False)
        op_new(items, {"title": "good", "id": "good-1", "hypothesis": "h"})
        (tmp / "items" / "scalar.md").write_text("---\n123\n---\n")
        (tmp / "items" / "copy.md").write_text((tmp / "items" / "good-1.md").read_text())
        st = Store(tmp / "t.db")
        self.assertEqual(st.reindex_items(items), 1)
        self.assertEqual(len(st.state("personal")["item_errors"]), 2)
        st.ingest(self.env(self.snap(1, "busy")))  # writer lock was released


class RpcTest(unittest.TestCase):
    def test_refuses_other_realm(self):
        tmp = Path(tempfile.mkdtemp())
        cfgfile = tmp / "c.toml"
        cfgfile.write_text(f'host = "hub"\nhome = "{tmp}"\n[hub]\nrealm = "personal"\ndata_dir = "{tmp}/data"\n')
        cfg = cfgmod.load(cfgfile)
        res = rpc.handle(cfg, {"op": "ingest", "realm": "work", "envelope": {}})
        self.assertFalse(res["ok"])
        self.assertIn("realm", res["error"])

    def _cfg(self):
        tmp = Path(tempfile.mkdtemp())
        cfgfile = tmp / "c.toml"
        cfgfile.write_text(f'host = "hub"\nhome = "{tmp}"\n[hub]\nrealm = "personal"\ndata_dir = "{tmp}/data"\ngit_autocommit = false\n')
        return cfgmod.load(cfgfile)

    def test_envelope_must_match_realm_and_source(self):
        cfg = self._cfg()
        s = SessionSnap(realm="work", host="h", profile="p", pid=1, session_id="x").to_dict()
        res = rpc.handle(cfg, {"op": "ingest", "realm": "personal", "envelope": {"realm": "personal", "host": "h", "profile": "p", "sessions": [s]}})
        self.assertFalse(res["ok"])
        res = rpc.handle(cfg, {"op": "ingest", "realm": "personal", "envelope": {"realm": "work", "host": "h", "profile": "p", "sessions": []}})
        self.assertFalse(res["ok"])

    def test_retried_request_is_applied_once(self):
        cfg = self._cfg()
        new = {"op": "item.new", "rid": "r1", "args": {"id": "e-1", "title": "t", "hypothesis": "h"}}
        self.assertTrue(rpc.handle(cfg, new)["ok"])
        self.assertTrue(rpc.handle(cfg, new)["ok"])  # replay: same answer, no overwrite error
        note = {"op": "item.note", "rid": "r2", "args": {"ref": "e-1", "text": "once"}}
        rpc.handle(cfg, note)
        rpc.handle(cfg, note)
        body = rpc.handle(cfg, {"op": "item.get", "args": {"ref": "e-1"}})["body"]
        self.assertEqual(body.count("once"), 1)


class ConfigTest(unittest.TestCase):
    def test_duplicate_labels_rejected(self):
        f = Path(tempfile.mkdtemp()) / "c.toml"
        f.write_text('[[profiles]]\nlabel="a"\nconfig_dir="/x"\nrealm="work"\n[[profiles]]\nlabel="a"\nconfig_dir="/y"\nrealm="personal"\n')
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load(f)

    def test_unmapped_config_dir_never_guesses_a_realm(self):
        f = Path(tempfile.mkdtemp()) / "c.toml"
        f.write_text('[[profiles]]\nlabel="p"\nconfig_dir="/x/.claude"\nrealm="personal"\n[hub]\nrealm="personal"\n')
        cfg = cfgmod.load(f)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/.claude-new"
        try:
            with self.assertRaises(cfgmod.ConfigError):
                cfg.current_realm()
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR")
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old


class SpoolTest(unittest.TestCase):
    def test_legacy_names_sort_by_time(self):
        from wd import transport

        tmp = Path(tempfile.mkdtemp())
        f = tmp / "c.toml"
        f.write_text(f'home = "{tmp}"\n')
        cfg = cfgmod.load(f)
        d = transport.spool_dir(cfg, "personal")
        (d / f"{int(time.time()) - 5}-aaaaaa.json").write_text('{"n": 0}')  # old second-resolution name
        transport._spool(cfg, "personal", {"n": 1})
        got = [json.loads(p.read_text())["n"] for p in transport._queued(cfg, "personal")]
        self.assertEqual(got, [0, 1])

    def test_fifo_names(self):
        from wd import transport

        tmp = Path(tempfile.mkdtemp())
        f = tmp / "c.toml"
        f.write_text(f'home = "{tmp}"\n')
        cfg = cfgmod.load(f)
        for i in range(30):
            transport._spool(cfg, "personal", {"n": i})
        got = [json.loads(p.read_text())["n"] for p in sorted(transport.spool_dir(cfg, "personal").glob("*.json"))]
        self.assertEqual(got, list(range(30)))


class GuardTest(unittest.TestCase):
    def test_hostname_parsing(self):
        for v in ("localhost:8740", "127.0.0.1:8741", "[::1]:8740", "http://localhost:8740", "localhost"):
            self.assertIn(_hostname(v), LOOPBACK, v)
        for v in ("evil.example:8740", "http://192.168.1.5:8740", ""):
            self.assertNotIn(_hostname(v), LOOPBACK, v)



class OpenerTest(unittest.TestCase):
    def test_guards(self):
        import http.client
        import socket as so

        from wd import opener

        with so.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "c.toml"
        f.write_text(f'home = "{tmp}"\n[realms.work]\nhub = ["x"]\nview_port = 8741\n'
                     f'[view]\nopener_port = {port}\n[view.browser_profiles]\nwork = "Profile 2"\n')
        cfg = cfgmod.load(f)
        launched = []
        orig = opener.subprocess.Popen
        opener.subprocess.Popen = lambda cmd, **kw: launched.append(cmd)
        httpd = opener.start(cfg)
        token = (tmp / "opener.token").read_text().strip()
        self.assertEqual(oct((tmp / "opener.token").stat().st_mode & 0o777), "0o600")
        board = {"Origin": "http://127.0.0.1:8741"}
        try:
            def req(method, path, body=None, headers=None, raw=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                data = raw if raw is not None else (json.dumps(body).encode() if body else None)
                c.request(method, path, body=data, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
                r = c.getresponse()
                return r.status, json.loads(r.read() or b"{}")

            self.assertEqual(req("GET", "/config", headers=board)[1]["realms"]["work"]["profile"], "Profile 2")
            self.assertEqual(req("GET", "/config")[0], 403)  # no Origin, no token
            self.assertEqual(req("GET", "/config", headers={"X-WD-Token": token})[0], 200)
            self.assertEqual(req("GET", "/config", headers={"Origin": "http://127.0.0.1:5555"})[0], 403)  # other local page
            self.assertEqual(req("GET", "/config", headers={**board, "Host": "evil.example"})[0], 403)
            ok_body = {"realm": "work", "url": "https://claude.ai/code/session_x"}
            h = {**board, "X-WD-Open": "1", "Content-Type": "application/json"}
            self.assertEqual(req("POST", "/open", ok_body, board)[0], 403)  # missing X-WD-Open
            self.assertEqual(req("POST", "/open", ok_body, {**h, "Origin": "https://evil.example"})[0], 403)
            self.assertEqual(req("POST", "/open", {"realm": "work", "url": "https://evil.example/"}, h)[0], 400)
            self.assertEqual(req("POST", "/open", {"realm": "personal", "url": ok_body["url"]}, h)[0], 404)
            self.assertEqual(req("POST", "/open", {"realm": "work", "url": "https://claude.ai/" + "a" * 2000}, h)[0], 400)
            try:  # a bogus length is never read, so the reply may race a TCP reset
                self.assertEqual(req("POST", "/open", None, {**h, "Content-Length": "-1"}, raw=b"x" * 10)[0], 413)
            except ConnectionResetError:
                pass
            self.assertEqual(req("POST", "/open", None, h, raw=b"x" * 5000)[0], 413)
            self.assertEqual(req("POST", "/open", ok_body, h)[0], 200)
            self.assertEqual(len(launched), 1)
            self.assertIn("--profile-directory=Profile 2", launched[0])
            self.assertEqual(launched[0][-1], ok_body["url"])
        finally:
            httpd.shutdown()
            opener.subprocess.Popen = orig


class AccountTest(unittest.TestCase):
    def test_half_written_file_keeps_last_good_account(self):
        from wd.collect.claude import ClaudeCollector, read_account

        d = Path(tempfile.mkdtemp()) / "cfg"
        d.mkdir()
        f = d / ".claude.json"
        f.write_text(json.dumps({"oauthAccount": {"emailAddress": "a@example.com", "organizationName": "Org"}}))
        col = ClaudeCollector(d, "h", "p", "personal")
        self.assertEqual(col.account()["email"], "a@example.com")
        f.write_text('{"oauthAccount": {"emailAdd')  # caught mid-write
        self.assertIsNone(read_account(d))
        col._account_at = 0  # force a re-read
        self.assertEqual(col.account()["email"], "a@example.com")
        f.write_text(json.dumps({"numStartups": 3}))  # really logged out
        col._account_at = 0
        self.assertEqual(col.account(), {})


class HistoryTest2(unittest.TestCase):
    def _env(self, status, sid="s"):
        return {"host": "h", "profile": "p", "realm": "personal", "sessions": [
            SessionSnap(realm="personal", host="h", profile="p", pid=1, session_id=sid, status=status, status_since=1).to_dict()]}

    def test_new_session_on_same_pid_starts_fresh(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        st.ingest(self._env("busy", "old"))
        st.ingest(self._env("waiting", "old"))
        st.ingest(self._env("idle", "new"))
        h = st.state("personal")["sessions"][0]["history"]
        self.assertEqual((h["start"], h["changes"]), ("idle", []))

    def test_truncation_moves_since(self):
        from wd.hub import store as storemod

        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        st.ingest(self._env("busy"))
        now = storemod.now_ms()
        with st.db:
            for i in range(storemod.HISTORY_MAX_CHANGES + 50):
                st.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (now - 3600_000 + i * 1000, "h/p/1", "s", "", None, "idle" if i % 2 else "waiting"))
        h = st.state("personal")["sessions"][0]["history"]
        self.assertEqual(len(h["changes"]), storemod.HISTORY_MAX_CHANGES)
        self.assertGreater(h["since"], now - 3600_000)  # knowledge starts at the cut, not the window

    def test_retention_keeps_a_baseline(self):
        from wd.hub import store as storemod

        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        st.ingest(self._env("idle"))
        with st.db:
            st.db.execute("UPDATE events SET ts = ts - ?", (20 * 86400_000,))
        st.ingest(self._env("idle"))  # pruning runs here
        self.assertEqual(st.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        h = st.state("personal")["sessions"][0]["history"]
        self.assertEqual(h["start"], "idle")
        self.assertLess(h["since"], storemod.now_ms() - 11 * 3600_000)


class HistoryTest(unittest.TestCase):
    def test_history_window(self):
        st = Store(Path(tempfile.mkdtemp()) / "t.db")
        env = lambda status: {"host": "h", "profile": "p", "realm": "personal", "sessions": [
            SessionSnap(realm="personal", host="h", profile="p", pid=1, session_id="s", status=status).to_dict()]}
        st.ingest(env("busy"))
        st.ingest(env("waiting"))
        st.ingest(env("idle"))
        h = st.state("personal")["sessions"][0]["history"]
        self.assertEqual(h["start"], "busy")
        self.assertEqual([c[1] for c in h["changes"]], ["waiting", "idle"])
        # an event older than the window becomes the start status
        with st.db:
            st.db.execute("UPDATE events SET ts = ts - ?", (13 * 3600_000,))
        h = st.state("personal")["sessions"][0]["history"]
        self.assertEqual((h["start"], h["changes"]), ("idle", []))


class AttentionTest(unittest.TestCase):
    def lvl(self, status, mins, age=5.0, started_ago_min=600):
        from wd.attention import attention
        now = 10_000_000_000
        s = {"status": status, "status_since": now - mins * 60_000, "started_at": now - started_ago_min * 60_000}
        return attention(s, now, age)[0]

    def test_policy(self):
        from wd.attention import FYI, OK, SOON, URGENT
        self.assertEqual(self.lvl("waiting", 0), SOON)  # grace: top of board, no push
        self.assertEqual(self.lvl("waiting", 5), URGENT)
        self.assertEqual(self.lvl("waiting", 30, age=600), SOON)  # silent machine never pushes
        self.assertEqual(self.lvl("idle", 3), SOON)  # fresh result
        self.assertEqual(self.lvl("idle", 3, started_ago_min=3), OK)  # just opened, nothing ran
        self.assertEqual(self.lvl("idle", 120), FYI)
        self.assertEqual(self.lvl("idle", 60 * 24), OK)  # parked
        self.assertEqual(self.lvl("busy", 30), OK)
        self.assertEqual(self.lvl("busy", 60 * 4), FYI)
        self.assertEqual(self.lvl("gone", 1), OK)
        from wd.attention import attention
        now = 10_000_000_000  # waiting since 10 h, but Claude replied 5 min ago (Remote Control)
        moved_on = {"status": "waiting", "status_since": now - 600 * 60_000, "last_reply_at": now - 5 * 60_000, "status_detail": "permission prompt"}
        self.assertEqual(attention(moved_on, now, 5)[0], FYI)


class RecapTest(unittest.TestCase):
    def test_last_reply_and_alias(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            rows = [
                {"type": "assistant", "timestamp": "2026-11-15T10:00:00Z", "message": {"content": [{"type": "text", "text": "第一段"}]}},
                {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
                {"type": "assistant", "timestamp": "2026-11-15T10:01:00Z", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
                {"type": "assistant", "timestamp": "2026-11-15T10:05:00Z", "message": {"content": [{"type": "text", "text": "做完了：三件事"}]}},
            ]
            p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            st = TranscriptTail(p).update()
            self.assertEqual(st.last_reply, "做完了：三件事")
            self.assertEqual(st.activity, "run")  # the Bash call; the later text doesn't reset it
            self.assertGreater(st.last_reply_at, 0)
        snap = redact(SessionSnap("r", "h", "p", 1, SID, status="shell", last_reply="x" * 3000))
        self.assertEqual((snap.status, snap.status_detail), ("busy", "shell"))
        self.assertLessEqual(len(snap.last_reply), 1200)
        self.assertEqual(redact(SessionSnap("r", "h", "p", 1, SID, last_reply="secret"), "minimal").last_reply, "")

    def test_title_age(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            rows = [{"type": "user", "timestamp": "2026-09-23T08:00:00Z", "gitBranch": "main"},
                    {"type": "ai-title", "aiTitle": "first topic"},
                    {"type": "user", "timestamp": "2026-11-15T10:00:00Z", "gitBranch": "main"},
                    {"type": "ai-title", "aiTitle": "first topic"}]
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            tail = TranscriptTail(p)
            st = tail.update()
            t0 = int(datetime(2026, 9, 23, 8, tzinfo=timezone.utc).timestamp() * 1000)
            self.assertEqual(st.ai_title_at, t0)  # re-appended later, still dated from the start
            with p.open("a") as f:
                f.write(json.dumps({"type": "user", "timestamp": "2026-11-16T01:00:00Z", "gitBranch": "x"}) + "\n")
                f.write(json.dumps({"type": "ai-title", "aiTitle": "renamed"}) + "\n")
            st = tail.update()
            self.assertEqual(st.ai_title, "renamed")
            self.assertEqual(st.ai_title_at, int(datetime(2026, 11, 16, 1, tzinfo=timezone.utc).timestamp() * 1000))


class ResearchTest(unittest.TestCase):
    def test_records_edges_overlay(self):
        from wd.research import ResearchProject, ResearchSource
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "registry"
            reg.mkdir()
            recs = [
                {"id": "REG-20260101-aaaa", "ts": "2026-01-01T10:00", "hypothesis": "【FOO-1：baseline】x", "status": "fail",
                 "result": {"verdict": "FAIL", "cv": "too noisy"}, "lose_condition": "sd > 1"},
                {"id": "REG-20260102-bbbb", "ts": "2026-01-02T10:00", "title": "FOO-1b：fix sampling", "status": "pass",
                 "parent": "REG-20260101-aaaa（FOO-1）", "why": "REG-20260101-aaaa failed", "result": {"verdict": "PASS"}},
                {"id": "REG-20260103-cccc", "ts": "2026-01-03T10:00", "hypothesis": "uses REG-20260102-bbbb constant", "status": "running（8/8）",
                 "result": {"verdict": "NO-VERDICT（below noise）"}},
            ]
            for r in recs:
                (reg / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False))
            (reg / "broken.json").write_text("{nope")
            proj = ResearchProject(ResearchSource("demo", reg), Path(d) / "data")
            data = proj.data()
            by = {r["id"]: r for r in data["records"]}
            self.assertEqual(len(by), 3)
            self.assertEqual(len(data["errors"]), 1)
            a, b, c = by["REG-20260101-aaaa"], by["REG-20260102-bbbb"], by["REG-20260103-cccc"]
            self.assertEqual((a["code"], a["family"], a["outcome"], a["title"]), ("FOO-1", "FOO", "fail", "FOO-1：baseline"))
            self.assertEqual(b["family"], "FOO")
            self.assertEqual({(e["type"], e["to"]) for e in b["edges"]}, {("follows", a["id"])})  # parent wins over why
            self.assertEqual(c["outcome"], "inconclusive")
            self.assertEqual(c["mentions"], [b["id"]])
            fp = data["fp"]
            ov = Path(d) / "data" / "research"
            ov.mkdir(parents=True)
            (ov / "demo.json").write_text(json.dumps({
                "questions": [{"id": "q1", "title": "Does sampling matter?"}],
                "records": {a["id"]: {"question": "q1", "why": "first try"}},
                "edges": [{"from": c["id"], "to": b["id"], "type": "uses", "quote": "uses ... constant"},
                          {"from": c["id"], "to": "REG-20990101-zzzz", "type": "because"}]}))
            data = proj.data()
            self.assertNotEqual(data["fp"], fp)  # overlay change is picked up
            self.assertEqual(data["questions"][0]["id"], "q1")
            self.assertEqual([e["type"] for e in data["edges"]], ["uses"])  # unknown target dropped
            self.assertEqual({r["id"]: r.get("why_short") for r in data["records"]}[a["id"]], "first try")

    def test_propose_pipeline(self):
        from wd.research import ResearchSource
        from wd.research_propose import propose
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "registry"
            reg.mkdir()
            a = {"id": "REG-20260101-aaaa", "ts": "2026-01-01", "title": "FOO-1：baseline", "status": "fail",
                 "result": {"verdict": "FAIL", "cv": "對 12 個公開實測材料，誤差 3 倍"}}
            b = {"id": "REG-20260102-bbbb", "ts": "2026-01-02", "title": "FOO-1b：fix", "status": "pass",
                 "why": "FOO-1 的取樣問題要修掉", "result": {"verdict": "PASS", "cv": "12 個公開實測材料上，修正後誤差降到 1.5 倍"}}
            for r in (a, b):
                (reg / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False))
            answers = {
                "請逐筆整理": {"records": [
                    {"id": b["id"], "name": "修正取樣 FOO-1b", "why": "修 FOO-1", "result": "修正後誤差降到 1.5 倍", "level": 3,
                     "level_quote": "修正後誤差降到 1.5 倍", "n": 12,
                     "edges": [{"to": "FOO-1", "type": "because", "quote": "FOO-1 的取樣問題要修掉"},
                               {"to": a["id"], "type": "uses", "quote": "made-up sentence not in record"}]},
                    {"id": a["id"], "name": "起點", "why": "起點", "result": "誤差 3 倍", "level": 3,
                     "level_quote": "對 12 個公開實測材料", "n": 12, "edges": [{"to": b["id"], "type": "follows", "quote": "FOO-1b：fix"}]}]},
                "請整理成兩層": {"goals": [{"id": "g-method", "title": "方法可信", "bar": "誤差 2 倍內"}],
                                "questions": [{"id": "q-sampling", "parent": "g-method", "kind": "question", "title": "取樣夠嗎？",
                                               "members": [a["id"], b["id"]]}]},
                "請對每條途徑": {"goal_answer": {"text": "修正後接近標準", "cites": [b["id"]]}, "questions": [{
                    "id": "q-sampling", "holds": [{"text": "修正取樣有效", "cites": [b["id"]]}], "fails": [], "unknown": [],
                    "stuck": "none", "next": "擴大材料數", "decision": "",
                    "findings": [{"statement": "修正取樣後誤差降到 1.5 倍", "basis": "對 12 個公開實測材料", "so_what": "可以用",
                                  "change_if": "新材料誤差變大", "cites": [{"id": b["id"], "role": "supports", "quote": "修正後誤差降到 1.5 倍"}]},
                                 {"statement": "誤差是 7 倍", "basis": "沒根據", "so_what": "", "change_if": "",
                                  "cites": [{"id": a["id"], "role": "supports", "quote": "完全沒有這句"}]}]}]},
                "會議報告": {"title": "進度", "one_page": {"goal": "g", "success": "s", "answer": "a", "why_hard": "w",
                                                       "numbers": [{"text": "誤差降到 1.5 倍", "findings": ["f-sampling-1"]}]},
                           "learned": [{"headline": "修正取樣有效", "basis": "公開實測", "so_what": "可用", "findings": ["f-sampling-1"]},
                                       {"headline": "沒出處的話", "basis": "", "so_what": "", "findings": []}],
                           "routes": [{"question": "q-sampling", "plain": "取樣", "status": "可用", "stuck": "無"}],
                           "unknowns": [], "decisions": [], "next_weeks": [], "glossary": []},
            }
            fake = Path(d) / "fake-claude"
            fake.write_text("#!" + sys.executable + "\nimport sys, json\nprompt = sys.stdin.read()\nA = " + repr(answers) +
                            "\nfor k, v in A.items():\n    if k in prompt:\n        print(json.dumps({'structured_output': v}, ensure_ascii=False)); break\n")
            fake.chmod(0o755)
            old = os.environ.get("WD_CLAUDE")
            os.environ["WD_CLAUDE"] = str(fake)
            try:
                path = propose(ResearchSource("demo", reg), Path(d) / "data", Path(d), log=lambda *a: None)
            finally:
                os.environ.pop("WD_CLAUDE") if old is None else os.environ.__setitem__("WD_CLAUDE", old)
            ov = json.loads(path.read_text())
            self.assertEqual(ov["records"][b["id"]]["name"], "修正取樣")  # internal code stripped
            self.assertEqual([(e["from"], e["to"], e["type"]) for e in ov["edges"]], [(b["id"], a["id"], "because")])
            q = ov["questions"][0]
            f1, f2 = q["findings"]
            self.assertEqual(f1["confidence"], "對公開實測成立")  # computed: level 3, n 12, passed
            self.assertEqual(f1["warnings"], [])
            self.assertEqual(f2["cites"], [])  # unsupported quote dropped
            self.assertIn("沒有可驗證的出處", f2["warnings"])
            self.assertTrue(any("7" in w for w in f2["warnings"]))  # number not in sources
            rep = json.loads(path.with_name(path.stem + ".report.json").read_text())
            self.assertEqual(rep["learned"][0]["confidence"], "對公開實測成立")
            self.assertEqual(rep["learned"][0]["cites"], [b["id"]])
            self.assertIn("沒有對應的結論，不應放進報告", rep["learned"][1]["warnings"])
            self.assertEqual(rep["status"], "draft")
            self.assertEqual(rep["one_page"]["numbers"][0]["cites"], [b["id"]])  # sources come from the finding
            self.assertEqual(rep["one_page"]["numbers"][0]["warnings"], [])

class ReviewFixesTest(unittest.TestCase):
    def test_research_guards(self):
        from wd.research import ResearchProject, ResearchSource, gauge
        with tempfile.TemporaryDirectory() as d:
            reg, outside = Path(d) / "registry", Path(d) / "outside.json"
            reg.mkdir()
            (reg / "REG-20260101-aaaa.json").write_text(json.dumps({"id": "REG-20260101-aaaa", "ts": "2026-01-01"}))
            outside.write_text(json.dumps({"id": "REG-20990101-evil", "ts": "2099-01-01"}))
            (reg / "REG-20990101-evil.json").symlink_to(outside)
            proj = ResearchProject(ResearchSource("demo", reg), Path(d) / "data")
            self.assertEqual([r["id"] for r in proj.data()["records"]], ["REG-20260101-aaaa"])  # symlink skipped
            self.assertIsNone(proj.record("REG-20990101-evil"))
            self.assertIsNone(proj.record("."))  # no substring scan
            self.assertEqual(proj.record("REG-20260101-aaaa")["id"], "REG-20260101-aaaa")
        self.assertIsNone(gauge({"threshold": float("inf")}, {"val": 1.0}, None))
        self.assertIsNone(gauge({"threshold": 1.0}, {"val": 2.0}, "1e999 huge")["noise"])

    def test_quotes_and_time_order(self):
        from wd.research_propose import _norm, older, quote_supported
        text = _norm("FOO-1 的取樣問題要修掉，所以重做")
        self.assertTrue(quote_supported("FOO-1 的取樣問題要修掉", text))
        self.assertTrue(quote_supported("FOO-1 的取樣…所以重做", text))
        self.assertFalse(quote_supported("…………", text))
        self.assertFalse(quote_supported("，。", text))
        self.assertFalse(quote_supported("FOO-1 的取樣…完全沒寫的話", text))
        self.assertTrue(older("2026-01-01T17:00:00Z", "2026-01-02T02:30:00+08:00"))
        # +08:00 00:30 is 16:30Z: a record at 17:00Z is newer, so it can't be the older end
        self.assertFalse(older("2026-01-01T17:00:00Z", "2026-01-02T00:30:00+08:00"))

    def test_malformed_transcript_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            bad = ['{"type":"assistant","message":"text"}', '{"type":"assistant","message":{"content":[{"type":"text","text":null}]}}',
                   "[" * 5000 + "]" * 5000, '"just a string"', '{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}']
            p.write_text("\n".join(bad) + "\n")
            st = TranscriptTail(p).update()
            self.assertEqual(st.last_reply, "ok")

    def test_fast_first_turn_counts(self):
        from wd.attention import SOON, attention
        now = 10_000_000_000
        s = {"status": "idle", "started_at": now - 120_000, "status_since": now - 90_000, "last_reply_at": now - 90_000}
        self.assertEqual(attention(s, now, 5)[0], SOON)


def _fake_claude(d: str, answers: dict) -> str:
    fake = Path(d) / "fake-claude"
    fake.write_text("#!" + sys.executable + "\nimport sys, json\nprompt = sys.stdin.read()\nA = " + repr(answers) +
                    "\nfor k, v in A.items():\n    if k in prompt:\n        print(json.dumps({'structured_output': v}, ensure_ascii=False)); break\n"
                    "else:\n    print(json.dumps({'structured_output': {'records': []}}))\n")
    fake.chmod(0o755)
    return str(fake)


class ReconstructTest(unittest.TestCase):
    DOC = """# 測試版實驗紀錄

## 現況總表
| 代號 | 狀態 |
| T1 | 已結束 |

## T1 記憶筆記
起因：使用者常問重複的事，想讓 agent 自己記下做法。
做法：開啟自動筆記，門檻 10 次。
2026-10-02 起每晚自動比較 30 題，深入題兩版無差別。
10/10 決定結束這個實驗。

## T2 找文件工具
第一輪：加上找文件工具，跟正式版比 28 題，列舉題答對多 6 題。
"""

    def run_reconstruct(self, d, answers):
        from wd.research import ResearchSource
        from wd.research_reconstruct import reconstruct
        doc = Path(d) / "log.md"
        doc.write_text(self.DOC, encoding="utf-8")
        out = Path(d) / "recs"
        src = ResearchSource("iw", out, format="reconstructed", unit="題", min_n=20,
                             ladder=["只有想法", "離線題庫", "測試版每晚比較", "使用者比較", "正式版確認"])
        old = os.environ.get("WD_CLAUDE")
        os.environ["WD_CLAUDE"] = _fake_claude(d, answers)
        try:
            paths = reconstruct(src, [doc], Path(d), repo=None, workers=1, log=lambda *a: None)
        finally:
            os.environ.pop("WD_CLAUDE") if old is None else os.environ.__setitem__("WD_CLAUDE", old)
        return src, out, paths

    def test_quotes_dates_parents_and_outcomes(self):
        t1 = {"key": "T1-r1", "line": "T1", "title": "記憶筆記", "date": "2026-10-02", "date_quote": "2026-10-02 起每晚自動比較",
              "why": "重複的問題", "why_quote": "使用者常問重複的事", "change": "自動筆記", "change_quote": "開啟自動筆記，門檻 10 次",
              "measure": "每晚比較", "measure_quote": "每晚自動比較 30 題", "n": 30, "result": "無差別", "result_quote": "深入題兩版無差別",
              "effect": "no_difference", "decision": "ended", "decision_quote": "10/10 決定結束這個實驗", "parent_key": ""}
        t2 = {"key": "T2-r1", "line": "T2", "title": "找文件", "date": "2026-11-01", "date_quote": "made up",
              "why": "", "why_quote": "", "change": "找文件工具", "change_quote": "加上找文件工具",
              "measure": "比 28 題", "measure_quote": "跟正式版比 28 題", "n": 99, "result": "多對 6 題", "result_quote": "列舉題答對多 6 題",
              "effect": "better", "decision": "adopted", "decision_quote": "已經移進正式版（原文沒有這句）", "parent_key": "T1-r1"}
        bogus = {**t2, "key": "X-1", "line": "", "change_quote": "nowhere", "result_quote": "nowhere either"}
        with tempfile.TemporaryDirectory() as d:
            src, out, paths = self.run_reconstruct(d, {"## T1 記憶筆記": {"records": [t1]}, "## T2 找文件工具": {"records": [t2, bogus]}})
            recs = {json.loads(p.read_text())["source"]["key"]: json.loads(p.read_text()) for p in paths}
            self.assertEqual(set(recs), {"T1-r1", "T2-r1"})  # nothing verifiable about the bogus round
            a, b = recs["T1-r1"], recs["T2-r1"]
            self.assertTrue(a["id"].startswith("REC-20261002-"))
            self.assertEqual((a["kind"], a["n"], a["result"]["verdict"][:4]), ("reconstructed", 30, "FAIL"))  # no difference = didn't help
            self.assertTrue(a["status"].startswith("ended"))
            self.assertEqual(b["ts"], "")  # date not in the section and no commit backs it
            self.assertEqual(b["n"], 0)  # 99 isn't in the text
            self.assertEqual(b["decision"], "none")  # decision quote not in the text
            self.assertEqual(b["parent"], a["id"])
            self.assertTrue(b["result"]["verdict"].startswith("PASS"))
            # the loader reads them, links follow the parent, the page knows they were reconstructed
            from wd.research import ResearchProject
            data = ResearchProject(src, Path(d) / "data").data()
            self.assertTrue(data["reconstructed"])
            self.assertEqual(data["ladder"][2], "測試版每晚比較")
            r_b = next(r for r in data["records"] if r["id"] == b["id"])
            self.assertEqual(r_b["outcome"], "pass")
            self.assertIn(("follows", a["id"]), [(e["type"], e["to"]) for e in r_b["edges"]])
            self.assertIn("log.md §T2 找文件工具", r_b["source"])
            # a rerun that no longer finds T2 removes its file, and leaves unrelated files alone
            (out / "notes.json").write_text("{}")
            self.run_reconstruct(d, {"## T1 記憶筆記": {"records": [t1]}})
            self.assertEqual(sorted(p.name for p in out.glob("*.json")), sorted([f"{a['id']}.json", "notes.json"]))

    def test_refuses_prereg_sources(self):
        from wd import research_reconstruct
        from wd.research import ResearchSource

        class Cfg:
            class hub:
                research = [ResearchSource("mof", Path("/nonexistent"))]
                realm = "work"
            profiles = []
        self.assertEqual(research_reconstruct.main(Cfg, "mof", ["x.md"], None, "sonnet", None), 2)


class CodexRound2Test(unittest.TestCase):
    def test_confidence_ignores_open_and_void_and_follows_ladder(self):
        from wd.research import ResearchSource
        from wd.research_propose import confidence
        recs = {"a": {"outcome": "pass", "level": 2, "n": 0}, "b": {"outcome": "running", "level": 5, "n": 50},
                "c": {"outcome": "void", "level": 5, "n": 50}}
        self.assertEqual(confidence([{"id": "a", "role": "supports"}, {"id": "b", "role": "supports"}], recs), "只在模擬內成立")
        self.assertEqual(confidence([{"id": "c", "role": "supports"}], recs), "分不出來或還在跑")
        src = ResearchSource("iw", Path("."), format="reconstructed", unit="題", min_n=20,
                             ladder=["只有想法", "離線題庫", "測試版每晚比較", "使用者比較", "正式版確認"])
        recs2 = {"x": {"outcome": "pass", "level": 3, "n": 28}, "y": {"outcome": "fail", "level": 3, "n": 5}}
        self.assertEqual(confidence([{"id": "x", "role": "supports"}], recs2, src), "在「測試版每晚比較」成立")
        self.assertEqual(confidence([{"id": "y", "role": "supports"}], recs2, src), "初步、樣本小（少於 20 題不裁定）")
        self.assertEqual(confidence([], recs2, src), "沒有直接支持的紀錄")

    def test_claim_words_respect_negation_and_dates_are_not_figures(self):
        from wd.research_propose import checks, numbers_ok
        recs = {"a": {"outcome": "fail"}}
        cites = [{"id": "a", "role": "supports"}]
        self.assertEqual(checks("測試版整體沒有比較好", cites, recs, {"a": ""}, [], "紀錄"), [])
        self.assertEqual(checks("測試版比較好", cites, recs, {"a": ""}, [], "紀錄"), ["支持的紀錄沒有一筆通過，卻用了「較好」"])
        self.assertEqual(numbers_ok("10/06 起 4 天沒跑", ["2026-10-06 起，4 天都沒有跑"]), [])

    def test_numbers_are_whole_tokens(self):
        from wd.research_propose import numbers_ok
        self.assertEqual(numbers_ok("誤差 12 倍、1.5 倍、3 倍", ["共 312 個，11.50 分，REG-20260103-abcd"]), ["12", "1.5", "3"])
        self.assertEqual(numbers_ok("誤差 12 倍", ["誤差 12 倍"]), [])

    def test_report_is_cleaned_and_nan_rejected(self):
        from wd.research import ResearchProject, ResearchSource, clean_report
        rep = clean_report({"learned": {}, "reviews": [None, {"at": "11/16", "by": "x", "note": "y"}], "decisions": [{"decision": "d", "cites": ["a", 3]}]})
        self.assertEqual(rep["learned"], [])
        self.assertEqual([r["note"] for r in rep["reviews"]], ["y"])
        self.assertEqual(rep["decisions"][0]["cites"], ["a"])
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "reg"
            reg.mkdir()
            (reg / "REG-20260101-aaaa.json").write_text(json.dumps({"id": "REG-20260101-aaaa", "ts": "2026-01-01"}))
            ov = Path(d) / "data" / "research"
            ov.mkdir(parents=True)
            (ov / "demo.report.json").write_text('{"title": "t", "x": NaN}')
            data = ResearchProject(ResearchSource("demo", reg), Path(d) / "data").data()
            self.assertIsNone(data["report"])
            json.dumps(data, allow_nan=False)  # the API can always serialise it

    def test_pixel_keeps_others_mappings_and_unreadable_config(self):
        from wd import pixel
        with tempfile.TemporaryDirectory() as d:
            old = pixel.HOME
            pixel.HOME = Path(d)
            try:
                (Path(d) / "config.json").write_text(json.dumps({"standalone": {"areaMappings": {"mine-by-hand": ["laptop"]}}}))
                pixel.update_settings(area_mappings={"a": ["h1"]})
                pixel.update_settings(area_mappings={"b": ["h2"]})
                m = json.loads((Path(d) / "config.json").read_text())["standalone"]["areaMappings"]
                self.assertEqual(m, {"mine-by-hand": ["laptop"], "b": ["h2"]})  # a (ours, gone) removed; the hand-made one kept
                (Path(d) / "config.json").write_text("{broken")
                with self.assertRaises(pixel.UnreadableConfig):
                    pixel.update_settings(area_mappings={"c": ["h3"]})
                self.assertEqual((Path(d) / "config.json").read_text(), "{broken")
                import plistlib
                self.assertEqual(plistlib.loads(pixel.plist_for("/n", "/Users/A&B/cli.js", 3100, Path("/w")))["ProgramArguments"][1], "/Users/A&B/cli.js")
            finally:
                pixel.HOME = old


class GithubAndDecisionsTest(unittest.TestCase):
    def _registry(self, d, n=7):
        reg = Path(d) / "registry"
        reg.mkdir()
        ids = [f"REG-2026010{k + 1}-{k:04x}" for k in range(n)]
        for k, rid in enumerate(ids):
            body = {"id": rid, "ts": f"2026-01-0{k + 1}", "title": f"try {k}", "status": "pass"}
            if k == 2:
                body["why"] = "見 PR #5 的討論；PR #6 沒合併"
            (reg / f"{rid}.json").write_text(json.dumps(body, ensure_ascii=False))
        return reg, ids

    def test_links_follow_changed_files_not_prose(self):
        from wd.research import ResearchProject, ResearchSource
        with tempfile.TemporaryDirectory() as d:
            reg, ids = self._registry(d)
            a, b, c = ids[:3]
            pr = lambda n, files, state="MERGED", merged="2026-01-10T00:00:00Z", title="t": {  # noqa: E731
                "n": n, "title": title, "state": state, "merged": merged if state == "MERGED" else "", "branch": "x", "files": files}
            cache = {"repo": "o/r", "fetched_at": "2026-01-20T10:00:00+0800", "error": "", "prs": [
                pr(1, [f"registry/{a}.json"], merged="2026-01-01T00:00:00Z"),
                pr(2, [f"registry/{a}.json", f"notes/rounds/{b}.json"], merged="2026-01-05T00:00:00Z"),  # b's note is not b's record
                pr(3, [f"registry/{i}.json" for i in ids[1:]], merged="2026-01-07T00:00:00Z"),  # 6 record files: bulk
                pr(4, [f"registry/{b}.json"], state="OPEN"),
                pr(5, ["src/x.py"], title=f"mentions {a} in prose"),  # text mention: no link
                pr(6, ["src/y.py"], state="CLOSED"),
            ]}
            data_dir = Path(d) / "data"
            (data_dir / "research").mkdir(parents=True)
            (data_dir / "research" / "demo.github.json").write_text(json.dumps(cache))
            data = ResearchProject(ResearchSource("demo", reg, repo="o/r"), data_dir).data()
            by = {r["id"]: r for r in data["records"]}
            self.assertEqual([(x["n"], x["role"]) for x in by[a]["prs"]], [(1, "registered"), (2, "updated")])
            self.assertEqual([(x["n"], x["role"], x["bulk"]) for x in by[b]["prs"]], [(3, "registered", True), (4, "open", False)])
            self.assertEqual([(x["n"], x["role"]) for x in by[c]["prs"]], [(3, "registered"), (5, "cited")])  # closed #6 not linked
            self.assertEqual(data["github"]["prs"], 6)
            self.assertNotIn("prs", ResearchProject(ResearchSource("demo2", reg), data_dir).data()["records"][0])  # no repo: no links

    def test_weak_mentions_only_for_reconstructed_and_near_in_time(self):
        from wd.research_github import links
        recs = [{"id": "REC-1", "line": "T9", "ts": "2026-11-04"}, {"id": "REC-2", "line": "", "ts": "2026-11-04"}]
        prs = [{"n": n, "title": t, "state": "MERGED", "merged": m, "branch": b, "files": []} for n, t, m, b in [
            (10, "Round 3 log", "2026-11-05T00:00:00Z", "t9-third-try"), (11, "T90 cleanup", "2026-11-05T00:00:00Z", "x"),
            (12, "T9 old", "2026-10-01T00:00:00Z", "x"), (13, "T9 next", "2026-11-12T00:00:00Z", "x")]]
        out = links(recs, {}, {"prs": prs}, weak=True)
        self.assertEqual([(x["n"], x["role"]) for x in out["REC-1"]], [(10, "mentions"), (13, "mentions")])
        self.assertNotIn("REC-2", out)
        self.assertEqual(links(recs, {}, {"prs": prs}, weak=False), {})

    def test_fetch_failure_keeps_last_good_list(self):
        from wd import research_github as gh
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.github.json"
            path.write_text(json.dumps({"repo": "o/r", "fetched_at": "t0", "prs": [{"n": 1}]}))
            old = gh.gh_exe
            gh.gh_exe = lambda: None
            try:
                out = gh.fetch("o/r", path)
            finally:
                gh.gh_exe = old
            self.assertIn("gh not found", out["error"])
            self.assertEqual((out["fetched_at"], out["prs"]), ("t0", [{"n": 1}]))
            self.assertEqual(json.loads(path.read_text())["prs"], [{"n": 1}])
        self.assertEqual(gh.pr_refs("見PR #42、PR12 與 https://github.com/o/r/pull/7；#9 不算；PR-AUC 不算"), [7, 12, 42])

    def test_decision_text_is_the_records_own_words(self):
        from wd.research import ResearchSource, decision_text
        src = ResearchSource("m", Path("."))
        rec = {"status": "registered；凍結（使用者 1/09）", "result": {"verdict": "FAIL", "adopt": "NOT-ADOPTED（待使用者決定）",
                                                                 "next_step_proposal": "先補資料"}}
        t = decision_text(rec, src)
        self.assertIn("狀態：registered；凍結（使用者 1/09）", t)
        self.assertIn("決定：NOT-ADOPTED（待使用者決定）", t)
        self.assertIn("下一步：先補資料", t)
        self.assertEqual(decision_text({"status": "fail", "result": {"verdict": "FAIL"}}, src), "")  # a bare verdict isn't a decision
        rec_x = {"status": "ended", "decision": "none", "quotes": {"decision": "10/10 決定結束這個實驗"}}
        self.assertEqual(decision_text(rec_x, src), "原文：10/10 決定結束這個實驗")

    def test_report_decisions_must_quote_a_record(self):
        from wd.research import ResearchSource
        from wd.research_propose import propose
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "registry"
            reg.mkdir()
            a = {"id": "REG-20260101-aaaa", "ts": "2026-01-01", "title": "路線 A", "status": "registered；凍結（使用者 1/09）",
                 "result": {"verdict": "FAIL", "cv": "對 12 個公開實測材料，誤差 3 倍", "adopt": "NOT-ADOPTED：等補資料再談"}}
            b = {"id": "REG-20260102-bbbb", "ts": "2026-01-02", "title": "門檻", "status": "pass",
                 "result": {"verdict": "PASS", "cv": "12 個公開實測材料上誤差 1.5 倍", "adopt": "達成門檻要多少尚未定義，待主管決定"}}
            for r in (a, b):
                (reg / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False))
            f = {"statement": "誤差降到 1.5 倍", "basis": "12 個公開實測材料", "so_what": "可用", "change_if": "新材料變差",
                 "cites": [{"id": b["id"], "role": "supports", "quote": "12 個公開實測材料上誤差 1.5 倍"}]}
            answers = {
                "請逐筆整理": {"records": [{"id": i, "name": "n", "why": "w", "result": "r", "level": 3, "level_quote": "公開實測材料",
                                       "n": 12, "edges": []} for i in (a["id"], b["id"])]},
                "請整理成兩層": {"goals": [{"id": "g-main", "title": "主目標", "bar": "誤差 2 倍內"}],
                           "questions": [{"id": "q-route", "parent": "g-main", "kind": "route", "title": "路線",
                                          "members": [a["id"], b["id"]]}]},
                "請對每條途徑": {"goal_answer": {"text": "接近", "cites": [b["id"]]}, "questions": [{
                    "id": "q-route", "holds": [], "fails": [], "unknown": [], "stuck": "none", "next": "", "decision": "",
                    "findings": [f]}]},
                # answered only when the report prompt carries the records' own decision text
                "依途徑分組，新的在前）：\n[q-route] 路線\n  REG-20260102-bbbb": {
                    "title": "進度", "one_page": {"goal": "g", "success": "s", "answer": "a", "why_hard": "w", "numbers": []},
                    "learned": [{"headline": "誤差降到 1.5 倍", "basis": "公開實測", "so_what": "可用", "findings": ["f-route-1"]}],
                    "routes": [], "unknowns": [], "next_weeks": [], "glossary": [],
                    "decisions": [{"decision": "門檻定多少", "options": "", "recommendation": "", "who": "主管", "by_when": "",
                                   "findings": ["f-route-1"], "evidence": {"id": b["id"], "quote": "尚未定義，待主管決定"}},
                                  {"decision": "路線 A 要不要繼續", "options": "", "recommendation": "", "who": "主管", "by_when": "",
                                   "findings": [], "evidence": {"id": "", "quote": ""}}],
                    "settled": [{"item": "路線 A", "decided": "等補資料", "findings": [],
                                 "evidence": {"id": a["id"], "quote": "等補資料再談"}},
                                {"item": "編的", "decided": "x", "findings": [], "evidence": {"id": a["id"], "quote": "記錄裡沒有這句話"}}]},
            }
            old = os.environ.get("WD_CLAUDE")
            os.environ["WD_CLAUDE"] = _fake_claude(d, answers)
            try:
                path = propose(ResearchSource("demo", reg), Path(d) / "data", Path(d), log=lambda *a: None)
            finally:
                os.environ.pop("WD_CLAUDE") if old is None else os.environ.__setitem__("WD_CLAUDE", old)
            rep = json.loads(path.with_name(path.stem + ".report.json").read_text())
            d1, d2 = rep["decisions"]
            self.assertEqual(d1["evidence"]["id"], b["id"])
            self.assertEqual(d1["warnings"], [])
            self.assertNotIn("evidence", d2)
            self.assertTrue(any("還沒定" in w for w in d2["warnings"]))
            s1, s2 = rep["settled"]
            self.assertEqual((s1["evidence"]["id"], s1["warnings"]), (a["id"], []))
            self.assertIn(a["id"], s1["cites"])
            self.assertNotIn("evidence", s2)
            self.assertTrue(any("已經定了" in w for w in s2["warnings"]))
            from wd.research import clean_report
            self.assertEqual(clean_report(rep)["settled"][0]["evidence"]["id"], a["id"])  # survives the page's cleaning

    def test_repo_must_look_like_owner_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(f'[hub]\nrealm = "work"\n[[hub.research]]\nname = "x"\npath = "{d}"\nrepo = "https://github.com/o/r"\n')
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(p)
            p.write_text(f'[hub]\nrealm = "work"\n[[hub.research]]\nname = "x"\npath = "{d}"\nrepo = "o/r.x"\n')
            self.assertEqual(cfgmod.load(p).hub.research[0].repo, "o/r.x")


if __name__ == "__main__":
    unittest.main()
