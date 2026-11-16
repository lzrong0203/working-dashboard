"""`wd research reconstruct`: records rebuilt from a project's own write-ups.

Some projects never pre-registered anything: what they tried, how they compared
it and what came of it lives in Markdown logs and commit messages. This stage
asks Claude to read those documents one section at a time and write one record
per experiment round, in the shape `wd research propose` already reads.

The model only proposes. Programs decide what survives:

- every text field needs a verbatim quote from its own section; a field whose
  quote isn't there is dropped (and the record says so);
- the effect (better / no difference / worse ...) needs a verified result quote,
  the decision a verified decision quote, the sample size a number that appears
  in the section;
- a date must appear in the section, or in a commit subject the model quoted;
  otherwise the first commit that names the record's line is used;
- a parent link must point at an earlier round.

Records get REC- ids (stable across reruns: document + line + key) and
``kind: reconstructed``, so the board says they were written afterwards, not
registered before the run. Files this command wrote earlier and no longer
produces are removed; any other file in the directory is left alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wd.research import ResearchSource, _flat
from wd.research_propose import _norm, call_claude, quote_supported

SECTION_MAX = 14000
EXCERPT_MAX = 6000
EFFECTS = ("better", "no_difference", "worse", "mixed", "unclear", "running")
DECISIONS = ("adopted", "ended", "continue", "pending", "none")
DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")

X_SCHEMA = {"type": "object", "properties": {"records": {"type": "array", "items": {"type": "object", "properties": {
    "key": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$"},
    "line": {"type": "string"}, "title": {"type": "string"},
    "date": {"type": "string"}, "date_quote": {"type": "string"},
    "why": {"type": "string"}, "why_quote": {"type": "string"},
    "change": {"type": "string"}, "change_quote": {"type": "string"},
    "measure": {"type": "string"}, "measure_quote": {"type": "string"},
    "n": {"type": "integer", "minimum": 0},
    "result": {"type": "string"}, "result_quote": {"type": "string"},
    "effect": {"type": "string", "enum": list(EFFECTS)},
    "decision": {"type": "string", "enum": list(DECISIONS)}, "decision_quote": {"type": "string"},
    "parent_key": {"type": "string"},
}, "required": ["key", "line", "title", "date", "date_quote", "why", "why_quote", "change", "change_quote", "measure",
                "measure_quote", "n", "result", "result_quote", "effect", "decision", "decision_quote", "parent_key"]}}},
    "required": ["records"]}

X_PROMPT = """這是專案文件「{doc}」的其中一節。文件開頭（總覽，供對照）：
{overview}

這個 repo 的 commit 紀錄（日期｜說明），用來判斷日期：
{commits}

請把「這一節」裡每一輪實驗（試了一個改法、跟什麼比、結果如何）整理成一筆紀錄。同一個改法做了好幾輪，每輪一筆。
這一節如果只是說明或維護規則、沒有實驗，就回傳空的 records。
- key：英文短代號，在整份文件裡唯一，例如 T9-r2；line：文件裡用的代號（例如 T9），沒有就給空字串。
- title：20 字內白話短名，不要用代號。
- date：這一輪的日期 YYYY-MM-DD；date_quote 從這一節或 commit 紀錄逐字複製能看出日期的一小段。看不出來就兩個都給空字串。
- why（為什麼做）、change（改了什麼）、measure（怎麼比、跟什麼比）、result（結果與數字）：各 80 字內白話。
  每一項都要附 *_quote：從這一節逐字複製的一小段（80 字內）。文字裡沒寫就給空字串，不要猜。
- n：比較用了幾{unit}，文字裡沒寫就給 0。
- effect：這輪的改法跟對照比，better（較好）、no_difference（看不出差別）、worse（較差）、mixed（有好有壞）、unclear（沒有可判斷的結果）、running（還在跑）。
- decision：這輪之後的處置，adopted（採用、移進正式版）、ended（結束、不採用）、continue（繼續下一輪）、pending（還沒決定）、none（沒寫）；decision_quote 逐字複製根據。
- parent_key：它承接的前一輪的 key（同一節或其他節），沒有就給空字串。

這一節：
## {heading}
{body}
"""


# How a reconstructed round reads on the board. There was no pre-registered
# threshold, so "pass" here only means "the change did better than what it was
# compared with". A change that made no difference counts as not helping: in
# these logs the question is always "is it worth adopting?".
def reconstructed_outcome(effect: str, decision: str) -> tuple[str, str]:
    """(status, verdict) in the vocabulary research.outcome() understands."""
    if effect == "running":
        return "running", ""
    status = {"adopted": "adopted（已採用）", "ended": "ended（已結束）", "continue": "continue（繼續）",
              "pending": "pending（待決）"}.get(decision, "")
    verdict = {"better": "PASS（事後判讀：比對照好）", "worse": "FAIL（事後判讀：比對照差）",
               "no_difference": "FAIL（事後判讀：看不出差別）", "mixed": "NO-VERDICT（事後判讀：有好有壞）",
               "unclear": "NO-VERDICT（沒有可判斷的結果）"}.get(effect, "NO-VERDICT（沒有可判斷的結果）")
    return status, verdict


def sections(text: str) -> list[tuple[str, str]]:
    """Level-2 sections; whatever comes before the first one is 總覽."""
    out, head, buf = [], "總覽", []
    for line in text.splitlines():
        if line.startswith("## "):
            if "".join(buf).strip():
                out.append((head, "\n".join(buf).strip()))
            head, buf = line[3:].strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((head, "\n".join(buf).strip()))
    return out


def git_log(repo: Path | None, limit: int = 400) -> list[tuple[str, str]]:
    if not repo:
        return []
    try:
        p = subprocess.run(["git", "-C", str(repo), "log", f"-{limit}", "--format=%ad|%s", "--date=short"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [tuple(x.split("|", 1)) for x in p.stdout.splitlines() if "|" in x]  # type: ignore[misc]


def _verified_date(r: dict, text: str, commits: list[tuple[str, str]]) -> str:
    date = str(r.get("date") or "")
    m = DATE.fullmatch(date)
    if not m:
        return ""
    forms = {date, f"{int(m.group(2))}/{int(m.group(3))}", f"{m.group(2)}/{m.group(3)}"}
    quote = _norm(_flat(r.get("date_quote"), 200))
    # the quoted passage itself must carry the date: a date merely somewhere in
    # the section may belong to another round
    if quote and quote in _norm(text) and any(_norm(f) in quote for f in forms):
        return date
    for d, subj in commits:  # a commit of that day whose subject the model quoted
        if d == date and quote and len(quote) >= 6 and quote in _norm(subj):
            return date
    return ""


def _first_commit(line: str, commits: list[tuple[str, str]]) -> str:
    if not line:
        return ""
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(line)}(?![0-9])")
    days = [d for d, subj in commits if pat.search(subj)]
    return min(days) if days else ""


def rec_id(doc: str, line: str, key: str, date: str, taken: set[str]) -> str:
    h = hashlib.sha1(f"{doc}\0{line}\0{key}".encode()).hexdigest()
    day = (date or "0000-00-00").replace("-", "")
    for k in range(0, len(h) - 4):
        rid = f"REC-{day}-{h[k:k + 4]}"
        if rid not in taken:
            return rid
    raise RuntimeError("id space exhausted")


def check(r: dict, heading: str, body: str, commits: list[tuple[str, str]]) -> tuple[dict, list[str]]:
    """Keep only what the section supports; return the cleaned fields and warnings."""
    text = f"{heading}\n{body}"
    tn = _norm(text)
    out, warn = {}, []
    for f in ("why", "change", "measure", "result"):
        q = _flat(r.get(f"{f}_quote"), 300)
        if _flat(r.get(f), 400) and quote_supported(q, tn):
            out[f], out[f"{f}_quote"] = _flat(r.get(f), 240), q
        else:
            out[f], out[f"{f}_quote"] = "", ""
            if _flat(r.get(f), 400):
                warn.append(f"{f} 的引句在原文找不到，已拿掉")
    eff = r.get("effect") if r.get("effect") in EFFECTS else "unclear"
    if eff not in ("running", "unclear") and not out["result"]:
        eff = "unclear"
        warn.append("沒有可核對的結果，判定改成看不出來")
    dec = r.get("decision") if r.get("decision") in DECISIONS else "none"
    dq = _flat(r.get("decision_quote"), 300)
    if dec != "none" and not quote_supported(dq, tn):
        dec, dq = "none", ""
        warn.append("處置的引句在原文找不到，已拿掉")
    n = int(r.get("n") or 0) if isinstance(r.get("n"), int) else 0
    if n and not re.search(rf"(?<!\d){n}(?!\d)", text):
        n = 0
    date = _verified_date(r, text, commits)
    line = _flat(r.get("line"), 40)
    if not date:
        date = _first_commit(line, commits)
        if date:
            warn.append("日期取自第一個提到它的 commit")
    out.update({"effect": eff, "decision": dec, "decision_quote": dq if dec != "none" else "", "n": n, "date": date,
                "line": line, "title": _flat(r.get("title"), 60), "key": str(r.get("key") or ""),
                "parent_key": str(r.get("parent_key") or "")})
    return out, warn


def _write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rec-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def reconstruct(src: ResearchSource, docs: list[Path], config_dir: Path, repo: Path | None = None, model: str = "sonnet",
                workers: int = 4, log=print) -> list[Path]:
    out_dir = src.path
    out_dir.mkdir(parents=True, exist_ok=True)
    commits = git_log(repo)
    commit_txt = "\n".join(f"{d}｜{s}" for d, s in commits) or "（沒有）"
    jobs = []
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace")
        try:
            rel = str(doc.resolve().relative_to(repo.resolve())) if repo else doc.name
        except ValueError:
            rel = doc.name
        secs = sections(text)
        overview = secs[0][1][:3000] if secs else ""
        for heading, body in secs:
            if len(body) > SECTION_MAX:
                log(f"[reconstruct] {rel} §{heading}: {len(body)} chars, reading the first {SECTION_MAX}")
            jobs.append((rel, heading, body[:SECTION_MAX], overview))

    def run(job):
        rel, heading, body, overview = job
        prompt = X_PROMPT.format(doc=rel, overview=overview, commits=commit_txt, heading=heading, body=body, unit=src.unit)
        try:
            data = call_claude(prompt, X_SCHEMA, config_dir, model)
        except Exception as e:
            log(f"[reconstruct] {rel} §{heading}: {e}")
            return job, None
        return job, data

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(run, jobs))
    if all(data is None for _, data in results):
        raise RuntimeError("every section failed; records left as they were")

    rows, taken = [], set()
    for (rel, heading, body, _), data in results:
        for r in (data or {}).get("records", []) if isinstance(data, dict) else []:
            if not isinstance(r, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}", str(r.get("key") or "")):
                continue
            clean, warn = check(r, heading, body, commits)
            if not (clean["change"] or clean["result"]):
                continue  # nothing verifiable about this round
            rid = rec_id(rel, clean["line"], clean["key"], clean["date"], taken)
            taken.add(rid)
            rows.append((rid, rel, heading, body, clean, warn))

    by_key = {(c["key"]): rid for rid, _, _, _, c, _ in rows}
    when = {rid: c["date"] for rid, _, _, _, c, _ in rows}
    written, now = [], time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for rid, rel, heading, body, c, warn in rows:
        parent = by_key.get(c["parent_key"], "")
        if parent and (parent == rid or (when[parent] and c["date"] and when[parent] > c["date"])):
            parent = ""
        status, verdict = reconstructed_outcome(c["effect"], c["decision"])
        rec = {
            "id": rid, "ts": c["date"], "title": c["title"], "line": c["line"], "kind": "reconstructed",
            "why": c["why"], "hypothesis": c["change"], "criterion": c["measure"], "status": status,
            "result": {"verdict": verdict, "cv": c["result"]}, "parent": parent, "n": c["n"],
            "effect": c["effect"], "decision": c["decision"],
            "source": {"doc": rel, "section": heading, "key": c["key"]},
            "quotes": {k: c[f"{k}_quote"] for k in ("why", "change", "measure", "result", "decision") if c.get(f"{k}_quote")},
            "source_excerpt": body[:EXCERPT_MAX], "warnings": warn,
            "reconstructed": {"by": model, "at": now},
        }
        path = out_dir / f"{rid}.json"
        _write_json(path, rec)
        written.append(path)
    keep = {p.name for p in written}
    for p in out_dir.glob("REC-*.json"):  # earlier output of this command that this run no longer produces
        try:
            if p.name not in keep and not p.is_symlink() and json.loads(p.read_text(encoding="utf-8")).get("kind") == "reconstructed":
                p.unlink()
        except (OSError, ValueError):
            pass
    log(f"[reconstruct] {len(written)} records from {len(jobs)} sections in {len(docs)} documents")
    return written


def main(cfg, name: str, docs: list[str], repo: str | None, model: str, profile: str | None, workers: int = 4) -> int:
    src = next((r for r in cfg.hub.research if r.name == name), None)
    if not src:
        print(f"no [[hub.research]] named {name!r} on this hub", file=sys.stderr)
        return 2
    if not src.reconstructed:
        print(f"[[hub.research]] {name} is not format = \"reconstructed\"; refusing to write into its directory", file=sys.stderr)
        return 2
    prof = next((p for p in cfg.profiles if (p.label == profile if profile else p.realm == cfg.hub.realm)), None)
    if not prof:
        print("no profile for this hub's realm; pass --profile", file=sys.stderr)
        return 2
    paths = [Path(d).expanduser() for d in docs]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    reconstruct(src, paths, prof.config_dir, Path(repo).expanduser() if repo else None, model=model, workers=workers,
                log=lambda m: print(m, flush=True))
    return 0
