"""Deliver requests to a realm's hub: in-process if local, else over ssh.

Snapshots are full state, so a failed ingest is simply dropped (the next one
supersedes it). Item operations are not: if the hub is unreachable they are
spooled under ``$WD_HOME/spool/<realm>/`` and replayed by the agent loop.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

from wd import config as cfgmod

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


class HubUnreachable(RuntimeError):
    pass


def send(cfg: cfgmod.Config, realm: str, req: dict, timeout: float = 30) -> dict:
    r = cfg.realm(realm)
    req = {**req, "realm": realm}
    errors = []
    for target in r.hub:
        if target == "local":
            from wd import rpc  # local import keeps agent-only installs light

            return rpc.handle(cfg, req)
        try:
            p = subprocess.run(
                ["ssh", *SSH_OPTS, target, f"{r.hub_cmd} rpc"],
                input=json.dumps(req, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{target}: timeout")
            continue
        out = (p.stdout or "").strip().splitlines()
        if out:
            try:
                return json.loads(out[-1])
            except json.JSONDecodeError:
                pass
        errors.append(f"{target}: exit {p.returncode} {(p.stderr or '').strip()[:200]}")
    raise HubUnreachable("; ".join(errors) or "no hub targets configured")


def spool_dir(cfg: cfgmod.Config, realm: str):
    d = cfg.home / "spool" / realm
    d.mkdir(parents=True, exist_ok=True)
    return d


def send_or_spool(cfg: cfgmod.Config, realm: str, req: dict) -> dict:
    try:
        return send(cfg, realm, req)
    except HubUnreachable as e:
        name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.json"
        (spool_dir(cfg, realm) / name).write_text(json.dumps(req, ensure_ascii=False))
        return {"ok": True, "queued": True, "error": str(e), "id": req.get("args", {}).get("id")}


def replay_spool(cfg: cfgmod.Config, realm: str) -> int:
    sent = 0
    for p in sorted(spool_dir(cfg, realm).glob("*.json")):
        try:
            res = send(cfg, realm, json.loads(p.read_text()))
        except HubUnreachable:
            break
        # delivered (even if the hub rejected it, retrying won't help)
        if not res.get("ok"):
            p.rename(p.with_suffix(".rejected"))
        else:
            p.unlink()
            sent += 1
    return sent
