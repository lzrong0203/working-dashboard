"""Hub-side request handler. Reached via ``ssh <hub> wd rpc`` (one JSON request
on stdin, one JSON response on stdout) or in-process when the hub is local.

SSH is the only write path into a hub: there is no HTTP write endpoint, so
only someone holding your SSH key can change what the board shows.
"""

from __future__ import annotations

import json
import sys

from wd import config as cfgmod
from wd.hub.store import Store
from wd.items import OPS, ItemError, ItemStore


def db_path(cfg: cfgmod.Config, realm: str):
    return cfg.home / f"{realm}.db"


def handle(cfg: cfgmod.Config, req: dict) -> dict:
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
            return store.ingest(req["envelope"])
        if op == "state":
            return {"ok": True, **store.state(realm)}
        if op in OPS:
            res = OPS[op](items, req.get("args", {}))
            store.reindex_items(items)
            return {"ok": True, **res}
        if op == "item.get":
            it = items.resolve(req["args"]["ref"])
            return {"ok": True, "item": it.meta, "body": it.body}
        if op == "item.list":
            a = req.get("args", {})
            out = [it.meta for it in items.all() if (not a.get("type") or it.meta.get("type") == a["type"]) and (not a.get("status") or it.meta.get("status") == a["status"])]
            return {"ok": True, "items": out}
        return {"ok": False, "error": f"unknown op {op!r}"}
    except (ItemError, KeyError) as e:
        return {"ok": False, "error": str(e)}


def main(cfg: cfgmod.Config) -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad json: {e}"}))
        return 1
    res = handle(cfg, req)
    print(json.dumps(res, ensure_ascii=False, default=str))
    return 0 if res.get("ok") else 1
