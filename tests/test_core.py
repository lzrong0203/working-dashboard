"""Core behaviour tests. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import tempfile
import time
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


if __name__ == "__main__":
    unittest.main()


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
            self.assertEqual(req("POST", "/open", None, {**h, "Content-Length": "-1"}, raw=b"x" * 10)[0], 413)
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
