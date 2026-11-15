"""Deliver requests to a realm's hub: in-process if local, else over ssh.

Snapshots are full state, so a failed ingest is simply dropped (the next one
supersedes it). Item operations are not: if the hub is unreachable they are
spooled under ``$WD_HOME/spool/<realm>/`` and replayed by the agent loop.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
import uuid
from contextlib import contextmanager

from wd import config as cfgmod

_BASE = [
    "BatchMode=yes",
    "ConnectTimeout=8",
    # our own multiplexed connection: cheap repeated pushes, and it never
    # interferes with (or inherits port forwards from) the user's own masters
    "ControlMaster=auto",
    "ControlPath=~/.ssh/wd-%C",
    "ControlPersist=10m",
]
SSH_OPTS = [x for o in [*_BASE, "ClearAllForwardings=yes"] for x in ("-o", o)]
SSH_MUX_OPTS = [x for o in _BASE for x in ("-o", o)]  # for `-O forward -L ...`


class HubUnreachable(RuntimeError):
    pass


# realm -> target that answered last; tried first next time (a roaming laptop
# shouldn't wait out a timeout on the office address every push)
_last_good: dict[str, str] = {}


def send(cfg: cfgmod.Config, realm: str, req: dict, timeout: float = 30) -> dict:
    r = cfg.realm(realm)
    req = {**req, "realm": realm}
    errors = []
    targets = sorted(r.hub, key=lambda t: t != _last_good.get(realm))
    for target in targets:
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
                res = json.loads(out[-1])
                _last_good[realm] = target
                return res
            except json.JSONDecodeError:
                pass
        errors.append(f"{target}: exit {p.returncode} {(p.stderr or '').strip()[:200]}")
    raise HubUnreachable("; ".join(errors) or "no hub targets configured")


def _http_ok(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def open_view(cfg: cfgmod.Config) -> list[tuple[str, int, bool]]:
    """Forward each realm's hub to its local view_port over wd's own ssh master.

    The master is the same connection the agent pushes through (no forwards of
    its own); forwards are added to it with ``ssh -O forward``. Errors about
    forwards listed in the user's ssh config are expected and ignored -- we
    only trust an actual HTTP check of the port.
    """
    out = []
    for name, r in cfg.realms.items():
        port = r.view_port or (cfg.hub.port if cfg.hub.realm == name else 0)
        if not port:
            continue
        if _http_ok(port):
            out.append((name, port, True))
            continue
        ok = False
        for target in sorted(r.hub, key=lambda t: t != _last_good.get(name)):
            if target == "local":
                break
            if subprocess.run(["ssh", *SSH_OPTS, "-O", "check", target], capture_output=True).returncode != 0:
                # DEVNULL, not pipes: `ssh -f` keeps inherited pipes open forever
                subprocess.run(["ssh", *SSH_OPTS, "-fN", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            subprocess.run(["ssh", *SSH_MUX_OPTS, "-O", "forward", "-L", f"{port}:127.0.0.1:{port}", target], capture_output=True, timeout=30)
            if _http_ok(port):
                ok = True
                _last_good[name] = target
                break
        out.append((name, port, ok))
    return out


def spool_dir(cfg: cfgmod.Config, realm: str):
    d = cfg.home / "spool" / realm
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _spool_lock(cfg: cfgmod.Config, realm: str):
    """One process at a time may enqueue, drain or send item requests for a realm
    (the agent and any CLI call share the spool)."""
    with open(spool_dir(cfg, realm) / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _order(p) -> int:
    """Submission time in ns from the file name (also accepts old second-resolution names)."""
    head = p.name.split("-", 1)[0]
    try:
        n = int(head)
    except ValueError:
        return 0
    return n if len(head) >= 19 else n * 1_000_000_000


def _queued(cfg: cfgmod.Config, realm: str) -> list:
    return sorted(spool_dir(cfg, realm).glob("*.json"), key=lambda p: (_order(p), p.name))


def _spool(cfg: cfgmod.Config, realm: str, req: dict) -> None:
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:6]}.json"
    d = spool_dir(cfg, realm)
    tmp = d / f".{name}.tmp"
    tmp.write_text(json.dumps(req, ensure_ascii=False))
    tmp.rename(d / name)


def send_or_spool(cfg: cfgmod.Config, realm: str, req: dict) -> dict:
    """Item operations. Each carries a request id (``rid``) so the hub can drop
    retries it already applied; order is preserved by draining the spool first,
    all under the realm's spool lock."""
    req = {**req, "rid": req.get("rid") or uuid.uuid4().hex}
    queued = {"ok": True, "queued": True, "id": req.get("args", {}).get("id")}
    with _spool_lock(cfg, realm):
        try:
            _drain(cfg, realm)
            return send(cfg, realm, req)
        except HubUnreachable as e:
            _spool(cfg, realm, req)
            return {**queued, "error": str(e)}


def _drain(cfg: cfgmod.Config, realm: str) -> int:
    sent = 0
    for p in _queued(cfg, realm):
        try:
            req = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        res = send(cfg, realm, req)  # HubUnreachable propagates; order is kept
        # delivered (even if the hub rejected it, retrying won't help)
        if not res.get("ok"):
            p.rename(p.with_suffix(".rejected"))
        else:
            p.unlink(missing_ok=True)
            sent += 1
    return sent


def replay_spool(cfg: cfgmod.Config, realm: str) -> int:
    """Send queued requests in order; raises HubUnreachable if the hub is down."""
    if not any(spool_dir(cfg, realm).glob("*.json")):
        return 0
    with _spool_lock(cfg, realm):
        return _drain(cfg, realm)
