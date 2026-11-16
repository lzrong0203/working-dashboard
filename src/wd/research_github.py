"""Pull requests next to research records, read-only.

The hub machine's own ``gh`` login lists a project's pull requests together with
the files each one changed. Links come from those file lists, not from prose:

- ``registered`` / ``updated``: the PR changed the record's own file. The first
  merged one added it, later ones appended results or re-adjudications.
- ``open``: an unmerged PR that changes the record's file.
- ``cited``: the record itself names the PR ("PR #12", ".../pull/12").
- ``mentions`` (reconstructed records only, weak): the record's line name
  appears in the PR's title or branch, merged within a few days of the record.

A PR that changes many record files at once is flagged ``bulk``. A PR whose text
merely mentions a record id is *not* linked: mentioning is not a relation.

The cache sits beside the overlay (``<name>.github.json``). Only ``wd research
github`` and the hub's hourly refresh write it; nothing is ever written to
GitHub.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

REFRESH = 3600  # seconds between automatic refreshes
BULK = 5  # a PR changing more record files than this is a bulk change, not about one record
WINDOW = (3, 14)  # weak matches: merged from N days before to M days after the record
MAX_MENTIONS = 5
MAX_CACHE = 16 * 1024 * 1024
MAX_FILES_PR = 3000  # GitHub's own cap on a PR's file listing
ROLE_ORDER = ("registered", "updated", "open", "cited", "mentions")
# "PR #12", "PR12" (this repo), "github.com/owner/name/pull/12" (that repo); a bare "#12" is too often something else
PR_REF = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d{1,6})(?!\d)|(?<![A-Za-z/])PR\s*#?\s*(\d{1,6})(?!\d)")


def cache_path(overlay_path: Path) -> Path:
    return overlay_path.with_name(overlay_path.stem + ".github.json")


def pr_refs(text: str) -> list[list]:
    """PR numbers a record names, as [repo, n]; repo is "" for a bare "PR #n"
    (meaning the project's own repo), so a URL into another repo can't match."""
    out = {(repo.lower(), int(n or bare)) for repo, n, bare in PR_REF.findall(text or "")}
    return [list(x) for x in sorted(out)][:20]


def gh_exe() -> str | None:
    # launchd and systemd start the hub with a bare PATH
    extra = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", str(Path.home() / ".local/bin")])
    return shutil.which("gh", path=os.environ.get("PATH", "") + os.pathsep + extra)


def read_cache(path: Path, repo: str | None = None) -> dict:
    """The cache, with ``prs`` always a list. With ``repo``, a cache written for
    another repository counts as empty (its PR numbers mean other PRs)."""
    try:
        if path.stat().st_size > MAX_CACHE:
            return {"error": "cache too large", "prs": []}
        d = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {"prs": []}
    if not isinstance(d, dict):
        return {"prs": []}
    if repo is not None and str(d.get("repo") or "").lower() != repo.lower():
        return {"prs": []}
    if not isinstance(d.get("prs"), list):
        d["prs"] = []
    return d


@contextmanager
def _locked(path: Path):
    """One writer at a time per cache, across processes (the hub and the CLI)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _clean_pr(p: dict) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("number"), int):
        return None
    files = [f.get("path") for f in p.get("files") or [] if isinstance(f, dict) and isinstance(f.get("path"), str)]
    return {"n": p["number"], "title": str(p.get("title") or "")[:200], "state": str(p.get("state") or ""),
            "merged": str(p.get("mergedAt") or ""), "branch": str(p.get("headRefName") or "")[:120],
            "files": files[:100], "files_cut": len(files) >= 100}  # the PR list shows at most 100 files per PR


def _all_files(exe: str, repo: str, n: int, env: dict, timeout: int) -> list[str] | None:
    """Every file a PR changed (the REST listing pages to GitHub's 3000-file cap)."""
    try:
        p = subprocess.run([exe, "api", "--paginate", f"repos/{repo}/pulls/{n}/files?per_page=100", "--jq", ".[].filename"],
                           capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [x for x in p.stdout.splitlines() if x] if p.returncode == 0 else None


def fetch(repo: str, path: Path, timeout: int = 300) -> dict:
    """List every PR of ``repo`` with its changed files and write the cache.
    A failure keeps the last good list (of the same repo) and records the error
    beside it, merged into whatever the cache holds by then."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    exe = gh_exe()
    try:
        if not exe:
            raise RuntimeError("gh not found on this machine")
        cmd = [exe, "pr", "list", "-R", repo, "--state", "all", "-L", "5000",
               "--json", "number,title,state,mergedAt,headRefName,files"]
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip()[-300:] or f"gh exit {p.returncode}")
        raw = json.loads(p.stdout)
        if not isinstance(raw, list):
            raise RuntimeError("unexpected gh output")
        prs = [x for x in map(_clean_pr, raw) if x]
        for x in prs:  # the list stops at 100 files per PR; big PRs get their full list separately
            if x["files_cut"]:
                full = _all_files(exe, repo, x["n"], env, timeout)
                if full is not None:
                    x["files"], x["files_cut"] = full[:MAX_FILES_PR], len(full) > MAX_FILES_PR
        out = {"repo": repo, "fetched_at": now, "error": "", "prs": prs}
        with _locked(path):
            _write(path, out)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        with _locked(path):  # re-read under the lock: a newer success must not be rolled back
            out = {**read_cache(path, repo), "repo": repo, "error": err, "error_at": now}
            _write(path, out)
    return out


_running: set[str] = set()
_tried: dict[str, float] = {}  # last start per cache, so a failing write can't cause a fetch per request
_seen: dict[str, tuple[float, str]] = {}  # cache mtime -> the repo it was written for
_guard = threading.Lock()


def _cache_repo(path: Path, mtime: float) -> str:
    key = str(path)
    if _seen.get(key, (None, ""))[0] != mtime:
        _seen[key] = (mtime, str(read_cache(path).get("repo") or "").lower())
    return _seen[key][1]


def refresh_async(repo: str, path: Path, every: int = REFRESH) -> bool:
    """Start a background refresh when the cache is missing, older than
    ``every`` seconds, or written for another repo. Attempts are spaced
    ``every`` seconds apart whatever their outcome. Returns True when started."""
    key = str(path)
    with _guard:
        if key in _running or time.time() - _tried.get(key, 0) < every:
            return False
        try:
            mtime = path.stat().st_mtime
            fresh = time.time() - mtime < every and _cache_repo(path, mtime) == repo.lower()
        except OSError:
            fresh = False
        if fresh:
            return False
        _running.add(key)
        _tried[key] = time.time()

    def run():
        try:
            fetch(repo, path)
        except Exception:
            pass
        finally:
            with _guard:
                _running.discard(key)

    threading.Thread(target=run, name=f"wd-github-{path.stem}", daemon=True).start()
    return True


def _day(ts: str) -> date | None:
    try:
        return datetime.fromisoformat(str(ts)[:10]).date()
    except ValueError:
        return None


def links(records: list[dict], files: dict[str, Path], cache: dict, weak: bool = False) -> dict[str, list[dict]]:
    """Record id -> the PRs that changed it, cite it, or (weak) may belong to it."""
    prs = [p for p in cache.get("prs") or [] if isinstance(p, dict) and isinstance(p.get("n"), int)]
    by_n = {p["n"]: p for p in prs}
    # match on "<dir>/<file>" so a same-named note elsewhere in the repo doesn't count
    rid_of = {f"{p.parent.name}/{p.name}": rid for rid, p in files.items()}
    touched: dict[int, list[str]] = {}
    for p in prs:
        hit = sorted({rid_of[k] for k in ("/".join(f.split("/")[-2:]) for f in p.get("files") or [] if isinstance(f, str)) if k in rid_of})
        if hit:
            touched[p["n"]] = hit
    out: dict[str, list[dict]] = {}

    def add(rid: str, p: dict, role: str) -> None:
        lst = out.setdefault(rid, [])
        if any(x["n"] == p["n"] for x in lst):
            return
        lst.append({"n": p["n"], "role": role, "state": p.get("state", ""), "merged": str(p.get("merged") or "")[:10],
                    "title": str(p.get("title") or "")[:120], "bulk": len(touched.get(p["n"], [])) > BULK})

    merged = sorted((p for p in prs if p.get("merged")), key=lambda p: p["merged"])
    seen: set[str] = set()
    for p in merged:
        for rid in touched.get(p["n"], []):
            add(rid, p, "updated" if rid in seen else "registered")
            seen.add(rid)
    for p in prs:
        if p.get("state") == "OPEN":
            for rid in touched.get(p["n"], []):
                add(rid, p, "open")
    own = str(cache.get("repo") or "").lower()
    for r in records:
        for ref in r.get("pr_refs") or []:
            if not (isinstance(ref, list) and len(ref) == 2 and ref[0] in ("", own)):
                continue
            p = by_n.get(ref[1])
            if p and (p.get("merged") or p.get("state") == "OPEN"):
                add(r["id"], p, "cited")
    if weak:
        for r in records:
            tok, t0 = str(r.get("line") or "").strip(), _day(r.get("ts", ""))
            if not (2 <= len(tok) <= 24) or not t0:
                continue
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", re.I)
            near = []
            for p in merged:
                d = _day(p["merged"])
                if d and -WINDOW[0] <= (d - t0).days <= WINDOW[1] and pat.search(f"{p.get('title', '')} {p.get('branch', '')}"):
                    near.append((abs((d - t0).days), p))
            for _, p in sorted(near, key=lambda x: (x[0], x[1]["n"]))[:MAX_MENTIONS]:
                add(r["id"], p, "mentions")
    for lst in out.values():  # registered first, then the newest specific updates; bulk changes last
        lst.sort(key=lambda x: (ROLE_ORDER.index(x["role"]), x["bulk"],
                                -int((x["merged"] or "0").replace("-", "")) if x["role"] == "updated" else int((x["merged"] or "9999").replace("-", "")),
                                x["n"]))
    return out


def main(cfg, name: str) -> int:
    import sys

    from wd.research import ResearchProject

    src = next((r for r in cfg.hub.research if r.name == name), None)
    if not src:
        print(f"no [[hub.research]] named {name!r} on this hub", file=sys.stderr)
        return 2
    if not src.repo:
        print(f"[[hub.research]] {name} has no repo = \"owner/name\"", file=sys.stderr)
        return 2
    proj = ResearchProject(src, cfg.hub.data_dir)
    out = fetch(src.repo, cache_path(proj.overlay_path))
    if out.get("error"):
        print(f"fetch failed: {out['error']}", file=sys.stderr)
    data = proj.data()
    recs = data["records"]
    linked = [r for r in recs if r.get("prs")]
    roles: dict[str, int] = {}
    for r in linked:
        for x in r["prs"]:
            roles[x["role"]] = roles.get(x["role"], 0) + 1
    print(f"{len(out.get('prs', []))} PRs; {len(linked)}/{len(recs)} records linked; " +
          ", ".join(f"{k} {roles[k]}" for k in ROLE_ORDER if k in roles))
    return 1 if out.get("error") else 0
