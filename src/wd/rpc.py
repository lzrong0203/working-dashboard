"""Hub-side request handler. Reached via ``ssh <hub> wd rpc`` (one JSON request
on stdin, one JSON response on stdout) or in-process when the hub is local.

SSH is the only write path into a hub: there is no HTTP write endpoint, so
only someone holding your SSH key can change what the board shows.

Each ssh call is a fresh process outside any service manager, so it lowers its
own priority first (SCHED_IDLE + nice 19 + idle I/O on Linux) -- the hub may
share a machine with latency-sensitive services.
"""

from __future__ import annotations

import json
import os
import sys

from wd import config as cfgmod
from wd.items import OPS, ItemError, ItemStore


def db_path(cfg: cfgmod.Config, realm: str):
    return cfg.home / f"{realm}.db"


def lower_priority() -> None:
    try:
        os.nice(19)
    except OSError:
        pass
    if hasattr(os, "sched_setscheduler") and hasattr(os, "SCHED_IDLE"):
        try:
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:  # ioprio_set(IOPRIO_WHO_PROCESS, self, IOPRIO_CLASS_IDLE)
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall(251 if os.uname().machine == "x86_64" else 30, 1, 0, 3 << 13)
        except Exception:
            pass


def _check_envelope(env: dict, realm: str) -> str | None:
    """A snapshot must be internally consistent and belong to this realm."""
    if not isinstance(env, dict):
        return "envelope must be an object"
    if env.get("realm") != realm:
        return f"envelope realm {env.get('realm')!r} != hub realm {realm!r}"
    for k in ("host", "profile"):
        if not isinstance(env.get(k), str) or not env[k]:
            return f"envelope needs {k}"
    for s in env.get("sessions") or []:
        if not isinstance(s, dict):
            return "sessions must be objects"
        if (s.get("realm"), s.get("host"), s.get("profile")) != (realm, env["host"], env["profile"]):
            return "session realm/host/profile does not match its envelope"
    return None


def handle(cfg: cfgmod.Config, req: dict) -> dict:
    from wd.hub.store import Store  # keep agent-only code paths light

    op = req.get("op")
    realm = req.get("realm") or cfg.hub.realm
    if not cfg.hub.realm:
        return {"ok": False, "error": "this machine is not configured as a hub ([hub] realm = ...)"}
    if realm != cfg.hub.realm:
        # refuse instead of silently mixing realms
        return {"ok": False, "error": f"this hub serves realm {cfg.hub.realm!r}, not {realm!r}"}
    store = Store(db_path(cfg, realm))
    items = ItemStore(cfg.hub.data_dir, cfg.hub.git_autocommit)
    try:
        if op == "ingest":
            err = _check_envelope(req.get("envelope"), realm)
            return {"ok": False, "error": err} if err else store.ingest(req["envelope"])
        if op == "state":
            return {"ok": True, **store.state(realm)}
        if op in OPS:
            rid = req.get("rid")
            with items.locked():
                if rid and (prev := store.seen_request(rid)) is not None:
                    return prev  # a retry of something already applied
                try:
                    res = {"ok": True, **OPS[op](items, req.get("args", {}))}
                except (ItemError, KeyError, TypeError) as e:
                    res = {"ok": False, "error": str(e)}
                if rid:
                    store.remember_request(rid, res)
                if res["ok"]:
                    store.reindex_items(items)
            return res
        if op == "item.get":
            it = items.resolve(req["args"]["ref"])
            return {"ok": True, "item": it.meta, "body": it.body}
        if op == "item.list":
            a = req.get("args", {})
            out = [it.meta for it in items.all() if (not a.get("type") or it.meta.get("type") == a["type"]) and (not a.get("status") or it.meta.get("status") == a["status"])]
            return {"ok": True, "items": out}
        return {"ok": False, "error": f"unknown op {op!r}"}
    except (ItemError, KeyError, TypeError) as e:
        return {"ok": False, "error": str(e)}


def main(cfg: cfgmod.Config) -> int:
    lower_priority()
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad json: {e}"}))
        return 1
    res = handle(cfg, req)
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 0 if res.get("ok") else 1
