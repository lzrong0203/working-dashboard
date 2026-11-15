"""The per-machine agent: collect -> redact -> push to each realm's hub.

One process handles every profile on the machine. Each (profile) snapshot is
pushed only when it changed, plus a heartbeat so the hub can tell "no sessions"
from "agent is down".
"""

from __future__ import annotations

import hashlib
import json
import sys
import time

from wd import __version__
from wd import config as cfgmod
from wd.collect.claude import ClaudeCollector
from wd.snapshot import redact
from wd.transport import HubUnreachable, replay_spool, send

VOLATILE = ("updated_at",)


def build_envelopes(cfg: cfgmod.Config, collectors: dict[str, ClaudeCollector]) -> list[dict]:
    envs = []
    for p in cfg.profiles:
        # keyed by the full identity, so a collector can never serve another realm
        ident = f"{p.label}|{p.config_dir}|{p.realm}"
        col = collectors.get(ident)
        if col is None:
            col = collectors[ident] = ClaudeCollector(p.config_dir, cfg.host, p.label, p.realm)
        send_mode = cfg.realm(p.realm).send
        sessions = [redact(s, send_mode).to_dict() for s in col.collect()]
        acct = col.account() if send_mode != "minimal" else {}
        envs.append({
            "account": acct.get("email", ""),
            "account_org": acct.get("org", ""),
            "v": 1,
            "host": cfg.host,
            "profile": p.label,
            "realm": p.realm,
            "agent_version": __version__,
            "sent_at": int(time.time() * 1000),
            "sessions": sessions,
        })
    return envs


def digest(env: dict) -> str:
    stable = [{k: v for k, v in s.items() if k not in VOLATILE} for s in env["sessions"]]
    return hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run(cfg: cfgmod.Config, once: bool = False, print_only: bool = False) -> int:
    if not (once or print_only):
        from wd import opener

        opener.start(cfg)  # no-op unless [view.browser_profiles] is configured
        if cfg.view.keep_tunnels:
            import threading

            from wd.transport import keep_views

            threading.Thread(target=keep_views, args=(cfg,), name="wd-views", daemon=True).start()
        if cfg.view.pixel:
            import threading

            from wd.pixel import run_bridge

            threading.Thread(target=run_bridge, args=(cfg,), name="wd-pixel", daemon=True).start()
    collectors: dict[str, ClaudeCollector] = {}
    last_digest: dict[str, str] = {}
    last_sent: dict[str, float] = {}
    last_error: dict[str, str] = {}
    retry_at: dict[str, float] = {}
    while True:
        for env in build_envelopes(cfg, collectors):
            if print_only:
                print(json.dumps(env, ensure_ascii=False, indent=2))
                continue
            key = env["profile"]
            d = digest(env)
            due = time.time() - last_sent.get(key, 0) >= cfg.heartbeat_s
            if d == last_digest.get(key) and not due:
                continue
            if time.time() < retry_at.get(key, 0):  # hub was unreachable; back off
                continue
            try:
                res = send(cfg, env["realm"], {"op": "ingest", "envelope": env})
                if res.get("ok"):
                    last_digest[key], last_sent[key] = d, time.time()
                else:
                    print(f"[wd agent] {key}: hub rejected: {res.get('error')}", file=sys.stderr)
            except HubUnreachable as e:
                if last_error.get(key) != str(e):  # log changes, not every retry
                    print(f"[wd agent] {time.strftime('%H:%M')} {key}: hub unreachable: {e}", file=sys.stderr)
                    last_error[key] = str(e)
                retry_at[key] = time.time() + 60
                continue
            last_error.pop(key, None)
            retry_at.pop(key, None)
        if not print_only:
            for realm in {p.realm for p in cfg.profiles}:
                try:
                    replay_spool(cfg, realm)
                except HubUnreachable:
                    pass  # reported by the ingest path above; retried next loop
                except Exception as e:  # never let the spool kill the agent
                    print(f"[wd agent] spool {realm}: {e}", file=sys.stderr)
        if once or print_only:
            return 0
        time.sleep(cfg.interval_s)
