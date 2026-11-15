"""wd -- command line for agents and humans.

  wd agent [--once] [--print]     collect sessions on this machine and push them
  wd serve [--port N]             run this machine's hub web server (127.0.0.1)
  wd rpc                          hub side of the ssh transport (stdin -> stdout)
  wd status                       sessions as seen by the hub, in the terminal
  wd exp new|close ...            record an experiment's intent and outcome
  wd item new|note|set|link|show|list ...
  wd config                       print the effective configuration

Inside a Claude Code session, item commands automatically attach the calling
session (CLAUDE_CODE_SESSION_ID) and pick the realm from CLAUDE_CONFIG_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from wd import __version__
from wd import config as cfgmod


def _session_ref(cfg: cfgmod.Config) -> str | None:
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    prof = cfg.profile_for_config_dir(os.environ.get("CLAUDE_CONFIG_DIR"))
    return f"{cfg.host}/{prof.label if prof else 'default'}/{sid}"


def _call(cfg, args, op: str, payload: dict, spool: bool = True) -> int:
    from wd.transport import HubUnreachable, send, send_or_spool

    realm = args.realm or cfg.current_realm()
    req = {"op": op, "args": payload}
    try:
        res = send_or_spool(cfg, realm, req) if spool else send(cfg, realm, req)
    except HubUnreachable as e:
        print(f"hub unreachable: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif not res.get("ok"):
        print(f"error: {res.get('error')}", file=sys.stderr)
    elif res.get("queued"):
        print(f"queued (hub unreachable, will retry): {res.get('id')}")
    else:
        _pretty(op, res)
    return 0 if res.get("ok") else 1


def _pretty(op: str, res: dict) -> None:
    if op == "item.list":
        for it in res["items"]:
            v = f" [{it['verdict']}]" if it.get("verdict") else ""
            print(f"{it['id']:22} {it.get('type',''):10} {it.get('status',''):10}{v} {it.get('title','')}")
    elif op == "item.get":
        print(json.dumps(res["item"], ensure_ascii=False, indent=2, default=str))
        if res.get("body", "").strip():
            print(res["body"].rstrip())
    else:
        print(res.get("id", "ok"))


def _ago(ms: int, now: int) -> str:
    s = max(0, (now - ms) // 1000)
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h"


def cmd_status(cfg, args) -> int:
    from wd.transport import HubUnreachable, send

    realms = [args.realm] if args.realm else sorted({p.realm for p in cfg.profiles} | ({cfg.hub.realm} if cfg.hub.realm else set()))
    for realm in realms:
        try:
            st = send(cfg, realm, {"op": "state"})
        except HubUnreachable as e:
            print(f"[{realm}] hub unreachable: {e}")
            continue
        if not st.get("ok"):
            print(f"[{realm}] {st.get('error')}")
            continue
        now = st["now"]
        print(f"[{realm}] {len(st['sessions'])} sessions")
        for s in st["sessions"]:
            if s["status"] == "gone" and not args.all:
                continue
            what = s.get("ai_title") or s.get("last_prompt") or ""
            mark = "!" if s["attention"]["level"] >= 3 else " "
            print(f" {mark} {s['status']:8} {_ago(s.get('status_since') or now, now):>4}  {s['host']}/{s['profile']:9} {s.get('name','')[:24]:24} {what[:60]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wd", description="working-dashboard")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", help="config file (default ~/.config/wd/config.toml)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--realm", help="override realm (default: from CLAUDE_CONFIG_DIR)")
    common.add_argument("--json", action="store_true", help="raw JSON output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("agent", help="collect and push sessions")
    a.add_argument("--once", action="store_true")
    a.add_argument("--print", action="store_true", help="print what would be sent, send nothing")

    s = sub.add_parser("serve", help="run the hub web server")
    s.add_argument("--port", type=int)

    sub.add_parser("rpc", help="hub side of the ssh transport")
    sub.add_parser("opener", help="run the browser-profile link opener standalone")
    v = sub.add_parser("view", help="forward every realm's hub to localhost and open the board")
    v.add_argument("--no-open", action="store_true")
    sub.add_parser("config", help="print effective config")
    st = sub.add_parser("status", parents=[common], help="sessions in the terminal")
    st.add_argument("--all", action="store_true", help="include ended sessions")

    # experiments ------------------------------------------------------------
    e = sub.add_parser("exp", help="experiments").add_subparsers(dest="sub", required=True)
    en = e.add_parser("new", parents=[common], help="record a new experiment (before launching it)")
    en.add_argument("title")
    en.add_argument("--from", dest="from_", help="parent item (id, slug or id prefix)")
    en.add_argument("--rel", default="derived_from", help="ablation_of | variant_of | scaled_from | rerun_of | derived_from")
    en.add_argument("--rel-note", help="what differs from the parent, e.g. 'no long-term memory'")
    en.add_argument("--hypothesis", required=True, help="what you expect to learn / believe will happen")
    en.add_argument("--expected", help="expected result, ideally measurable")
    en.add_argument("--change", help="what was changed vs the parent")
    en.add_argument("--launcher", help="script/command that runs it")
    en.add_argument("--metric", help="primary metric name")
    en.add_argument("--project")
    en.add_argument("--slug")
    en.add_argument("--status", default="running")
    ec = e.add_parser("close", parents=[common], help="record the outcome")
    ec.add_argument("ref")
    ec.add_argument("--verdict", required=True, choices=["supported", "refuted", "inconclusive", "n/a"])
    ec.add_argument("--conclusion", required=True)
    ec.add_argument("--results", help="path or short metric summary")
    ec.add_argument("--status", default="done", choices=["done", "failed", "abandoned"])

    # generic items ----------------------------------------------------------
    i = sub.add_parser("item", help="work items").add_subparsers(dest="sub", required=True)
    inew = i.add_parser("new", parents=[common])
    inew.add_argument("type", choices=["experiment", "eval", "code", "ops", "project"])
    inew.add_argument("title")
    inew.add_argument("--from", dest="from_")
    inew.add_argument("--rel", default="derived_from")
    inew.add_argument("--project")
    inew.add_argument("--slug")
    inote = i.add_parser("note", parents=[common])
    inote.add_argument("ref")
    inote.add_argument("text")
    inote.add_argument("--kind", default="observation", choices=["hypothesis", "observation", "decision", "todo"])
    iset = i.add_parser("set", parents=[common], help="set fields: key=value ...; edges via --edge type:target")
    iset.add_argument("ref")
    iset.add_argument("pairs", nargs="*")
    iset.add_argument("--edge", action="append", default=[], help="e.g. supersedes:exp-2026..., informed_by:<ref>")
    ilink = i.add_parser("link", parents=[common], help="attach the current session to an item")
    ilink.add_argument("ref")
    ilink.add_argument("--session")
    ishow = i.add_parser("show", parents=[common])
    ishow.add_argument("ref")
    ilist = i.add_parser("list", parents=[common])
    ilist.add_argument("--type")
    ilist.add_argument("--status")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = cfgmod.load(args.config)
        return dispatch(cfg, args)
    except cfgmod.ConfigError as e:
        print(f"wd: {e}", file=sys.stderr)
        return 2


def _current_realm_or_error(cfg) -> str:
    try:
        return cfg.current_realm()
    except cfgmod.ConfigError as e:
        return f"error: {e}"


def dispatch(cfg: cfgmod.Config, args) -> int:
    if args.cmd == "agent":
        from wd.agent import run

        return run(cfg, once=args.once, print_only=args.print)
    if args.cmd == "serve":
        from wd.hub.server import serve

        return serve(cfg, args.port)
    if args.cmd == "rpc":
        from wd import rpc

        return rpc.main(cfg)
    if args.cmd == "config":
        print(json.dumps({
            "config_file": str(cfg.path) if cfg.path else None,
            "host": cfg.host,
            "home": str(cfg.home),
            "profiles": [{"label": p.label, "config_dir": str(p.config_dir), "realm": p.realm} for p in cfg.profiles],
            "realms": {k: {"hub": r.hub, "send": r.send} for k, r in cfg.realms.items()},
            "hub": {"realm": cfg.hub.realm, "port": cfg.hub.port, "data_dir": str(cfg.hub.data_dir), "peers": cfg.hub.peers, "notify": bool(cfg.hub.notify.ntfy_topic)},
            "current_realm": _current_realm_or_error(cfg),
        }, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "opener":
        from wd import opener

        return opener.serve(cfg)
    if args.cmd == "view":
        import webbrowser

        from wd.transport import open_view

        results = open_view(cfg)
        for name, port, ok in results:
            print(f"{'✓' if ok else '✗'} {name:10} http://127.0.0.1:{port}")
        first = next((p for _, p, ok in results if ok), None)
        if first and not args.no_open:
            webbrowser.open(f"http://127.0.0.1:{first}")
        return 0 if first else 1

    session = _session_ref(cfg)
    if args.cmd == "exp" and args.sub == "new":
        from wd.items import new_id

        payload = {
            "id": new_id("experiment"),  # client-side id so a spooled request keeps it
            "type": "experiment", "title": args.title, "from": args.from_, "rel": args.rel,
            "rel_note": args.rel_note, "hypothesis": args.hypothesis, "expected": args.expected,
            "change": args.change, "launcher": args.launcher, "metric": args.metric,
            "project": args.project, "slug": args.slug, "status": args.status, "session": session,
        }
        return _call(cfg, args, "item.new", payload)
    if args.cmd == "exp" and args.sub == "close":
        return _call(cfg, args, "item.close", {"ref": args.ref, "verdict": args.verdict, "conclusion": args.conclusion, "results": args.results, "status": args.status, "session": session})
    if args.cmd == "item":
        if args.sub == "new":
            from wd.items import new_id

            return _call(cfg, args, "item.new", {"id": new_id(args.type), "type": args.type, "title": args.title, "from": args.from_, "rel": args.rel, "project": args.project, "slug": args.slug, "session": session})
        if args.sub == "note":
            return _call(cfg, args, "item.note", {"ref": args.ref, "text": args.text, "kind": args.kind, "session": session})
        if args.sub == "set":
            fields = dict(p.split("=", 1) for p in args.pairs)
            edges = [{"type": e.split(":", 1)[0], "to": e.split(":", 1)[1]} for e in args.edge]
            return _call(cfg, args, "item.set", {"ref": args.ref, "fields": fields, "add_edges": edges, "session": session})
        if args.sub == "link":
            return _call(cfg, args, "item.link", {"ref": args.ref, "session": args.session or session})
        if args.sub == "show":
            return _call(cfg, args, "item.get", {"ref": args.ref}, spool=False)
        if args.sub == "list":
            return _call(cfg, args, "item.list", {"type": args.type, "status": args.status}, spool=False)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
