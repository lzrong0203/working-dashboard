"""Research records: a project's own pre-registrations, read in place.

Some projects already keep one file per pre-registered experiment (hypothesis,
pass/lose criteria, who decides, verdict). wd doesn't copy them: the hub reads
the directory, normalises a few fields, and serves them to the 研究 view.

What the records themselves rarely say is kept in an *overlay* file next to the
hub's data: which question each record serves, one-line why/result, and typed
links found in the prose. An assistant proposes the overlay; links it proposed
are marked ``by: "ai"`` and carry the quote they rest on, so a human can check
them instead of trusting them.

Overlay (JSON)::

    {"questions": [{"id": "q-scaling", "title": "...", "answer": "...",
                    "state": "supported|refuted|open|mixed", "note": "..."}],
     "records":   {"REG-...": {"question": "q-scaling", "why": "...", "result": "..."}},
     "edges":     [{"from": "REG-b", "to": "REG-a", "type": "because",
                    "quote": "...", "by": "ai"}]}
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from wd.research_github import cache_path as github_cache_path
from wd.research_github import links as github_links
from wd.research_github import pr_refs
from wd.research_github import read_cache as read_github_cache

MAX_FILE = 4 * 1024 * 1024
MAX_FILES = 5000
MAX_TOTAL = 256 * 1024 * 1024
# REG- = written before the run (pre-registration); REC- = reconstructed afterwards
# from a project's own write-ups by `wd research reconstruct`.
RID = re.compile(r"RE[GC]-\d{8}-[0-9a-f]{4}")
# family codes such as ABL-2c, EVAL-GATE-1, E3, S2 (but not REG-/REC- ids)
CODE = re.compile(r"(?<![A-Za-z0-9])((?!RE[GC]-)[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)+[a-z]?|E\d+[a-z]?|S\d)(?![A-Za-z0-9])")

# Edge types, newest record -> the older record it points at.
STRONG = ("follows", "because", "readjudicates", "supersedes", "corrects")
EDGE_TYPES = (*STRONG, "uses", "mentions")

DEFAULT_FIELDS: dict[str, list[str]] = {
    "id": ["id"],
    "ts": ["ts", "timestamp", "date", "created"],
    "title": ["title", "short_name", "name"],
    "line": ["line", "family"],
    "kind": ["kind"],
    "why": ["why", "motivation", "rationale"],
    "hypothesis": ["hypothesis"],
    "criterion": ["criterion"],
    "lose": ["lose_condition", "lose"],
    "expected": ["prior_expectation", "expected"],
    "status": ["status"],
    "result": ["result", "outcome"],
    "parent": ["parent", "parents"],
    "superseded_by": ["superseded_by"],
    "original": ["original"],
    "noise": ["noise_floor", "noise"],
    # what the record itself says was decided, and what comes next (read at top level and inside result)
    "decision": ["adopt", "adopted", "decision", "decided"],
    "next": ["next_step", "next_step_proposal", "next"],
}


# Evidence ladder, weakest to strongest (levels 1-5). The default fits lab
# science; a project with a different path to "proven" sets its own
# (`ladder = [...]` in its [[hub.research]] entry).
DEFAULT_LADDER = ["只有想法", "模擬對模擬", "對公開實測", "對自有或供應方實測", "目標條件下實測確認"]


@dataclass
class ResearchSource:
    name: str
    path: Path
    label: str = ""
    format: str = "prereg-json"
    overlay: Path | None = None
    fields: dict[str, list[str]] = field(default_factory=dict)
    forbidden: list[str] = field(default_factory=list)  # words reports must not use (the project's own non-claims)
    ladder: list[str] = field(default_factory=lambda: list(DEFAULT_LADDER))
    unit: str = "個材料"  # what a record's n counts
    min_n: int = 8  # below this n a result is "preliminary"
    repo: str = ""  # "owner/name" on GitHub: its pull requests are linked to the records (read-only)

    @property
    def reconstructed(self) -> bool:
        """Records written after the fact from the project's write-ups, not before the run."""
        return self.format == "reconstructed"

    def field_names(self, key: str) -> list[str]:
        return self.fields.get(key) or DEFAULT_FIELDS[key]


def _flat(v, n: int) -> str:
    """Any JSON value as one clipped line of text."""
    if v is None:
        return ""
    if isinstance(v, dict):
        v = "；".join(f"{k}: {_flat(x, n)}" for k, x in v.items() if x not in (None, "", [], {}))
    elif isinstance(v, list):
        v = "；".join(_flat(x, n) for x in v)
    s = " ".join(str(v).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _get(d: dict, names: list[str]):
    for k in names:
        if d.get(k) not in (None, "", [], {}):
            return d[k]
    return None


def outcome(status: str, verdict: str) -> str:
    """pass | fail | inconclusive | void | running | open | other.
    The verdict wins over the status field: records keep `status: pass` after a
    later FAIL verdict, and the verdict is what the decision rules produced."""
    s, v = status.lower(), verdict.lower()
    if s.startswith("void"):
        return "void"
    if "no-verdict" in v or "未定" in v or "inconclusive" in v:
        return "inconclusive"
    if v.startswith("pass"):
        return "pass"
    if v.startswith("fail"):
        return "fail"
    for prefix, out in (("running", "running"), ("registered", "open"), ("open", "open"),
                        ("pass", "pass"), ("adopted", "pass"), ("fail", "fail"), ("adjudicated_fail", "fail"),
                        ("gate_failed", "fail")):
        if s.startswith(prefix):
            return out
    return "other"


def addenda(d: dict) -> list[tuple[str, str]]:
    """Later notes appended to a record (addendum_*, second/third adjudication,
    corrections), in file order. Results written after the run often live here."""
    out = []
    for k, v in d.items():
        kl = k.lower()
        if kl.startswith(("addendum", "second_adjudication", "third_adjudication", "correction", "amendment", "caveat")) and v not in (None, "", [], {}):
            out.append((k, _flat(v, 4000)))
    return out


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", str(v or ""))
    x = float(m.group(1)) if m else None
    return x if x is None or math.isfinite(x) else None


def gauge(criterion, result, noise) -> dict | None:
    """Threshold vs measured value, when both are plain numbers: enough to draw
    where the result landed relative to the line it had to cross."""
    if not isinstance(criterion, dict) or not isinstance(result, dict):
        return None
    th, val = criterion.get("threshold"), result.get("val")
    if isinstance(th, bool) or isinstance(val, bool) or not isinstance(th, (int, float)) or not isinstance(val, (int, float)):
        return None
    if not (math.isfinite(th) and math.isfinite(val)):
        return None
    d = str(criterion.get("direction") or "").lower()
    nf = _num(noise)
    if nf is not None and not math.isfinite(nf):
        nf = None
    return {"metric": _flat(criterion.get("metric"), 120), "dir": "down" if d.startswith("down") or d == "lower" else "up",
            "th": float(th), "val": float(val), "noise": abs(nf) if nf is not None else None}


def _checked_gauge(gz: dict | None, out: str) -> dict | None:
    """Flag a gauge whose position contradicts the verdict: the rule had more
    than one condition (or `val` means something else), so the bar would lie."""
    if not gz:
        return gz
    crossed = gz["val"] >= gz["th"] if gz["dir"] == "up" else gz["val"] <= gz["th"]
    if (out == "pass" and not crossed) or (out == "fail" and crossed):
        gz["multi"] = True
    return gz


def decision_text(d: dict, src: ResearchSource, n: int = 400) -> str:
    """What the record itself says was decided or comes next, verbatim: a
    status with words in it (not a bare pass/fail), adoption, decision, next
    step. Empty when the record says none of these."""
    res = _get(d, src.field_names("result"))
    res = res if isinstance(res, dict) else {}
    parts: list[str] = []
    st = _flat(_get(d, src.field_names("status")), 160)
    if st and not re.fullmatch(r"[a-z_ -]+", st.lower()):
        parts.append(f"狀態：{st}")
    for key, label in (("decision", "決定"), ("next", "下一步")):
        for name in src.field_names(key):
            for where in (d, res):
                v = where.get(name)
                if v not in (None, "", [], {}) and str(v).lower() != "none":
                    parts.append(f"{label}：{_flat(v, 300)}")
    q = d.get("quotes")
    if isinstance(q, dict) and isinstance(q.get("decision"), str) and q["decision"].strip():
        parts.append(f"原文：{_flat(q['decision'], 300)}")
    seen: set[str] = set()
    parts = [x for x in parts if not (x in seen or seen.add(x))]
    return _flat(" ／ ".join(parts), n) if parts else ""


def family_of(code: str) -> str:
    return re.sub(r"(-?\d+[a-z]?)+$", "", code) or code


def normalise(d: dict, src: ResearchSource) -> dict:
    g = lambda k: _get(d, src.field_names(k))  # noqa: E731
    rid = str(g("id") or "")
    hyp = _flat(g("hypothesis"), 4000)
    title = _flat(g("title"), 160)
    if not title:
        m = re.match(r"【([^】]{1,160})", hyp)
        title = m.group(1) if m else _flat(hyp, 90)
    line = _flat(g("line"), 80)
    m = CODE.search(line) or CODE.search(title) or CODE.search(hyp[:200])
    code = m.group(1) if m else ""
    res = g("result")
    verdict = _flat(res.get("verdict"), 160) if isinstance(res, dict) else ""
    if isinstance(res, dict):
        detail = res.get("cv") if isinstance(res.get("cv"), str) else {k: res[k] for k in ("val", "test") if k in res}
        result = _flat(detail, 600)
    else:
        result = _flat(res, 600)
    status = _flat(g("status"), 120)
    kind = _flat(g("kind"), 60)

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(to: str, typ: str, frm: str = rid) -> None:
        if to and frm and to != frm and (frm, to) not in seen:
            seen.add((frm, to))
            edges.append({"from": frm, "to": to, "type": typ, "by": "field"})

    for to in RID.findall(_flat(g("parent"), 2000)):
        add(to, "follows")
    for to in RID.findall(_flat(g("why"), 4000)):
        add(to, "because")
    if "adjudication" in kind:
        for to in RID.findall(_flat(g("original"), 2000) + " " + hyp[:400]):
            add(to, "readjudicates")
    for verb, to in re.findall(r"(取代|更正|supersedes|corrects)[^。.]{0,30}?(REG-\d{8}-[0-9a-f]{4})", title + " " + hyp):
        add(to, "corrects" if verb in ("更正", "corrects") else "supersedes")
    for newer in RID.findall(_flat(g("superseded_by"), 400)):
        if (newer, rid) not in seen:
            seen.add((newer, rid))
            edges.append({"from": newer, "to": rid, "type": "supersedes", "by": "field"})
    notes = addenda(d)
    whole = json.dumps(d, ensure_ascii=False)
    mentions = sorted({x for x in RID.findall(whole) if x != rid and (rid, x) not in seen})

    return {
        "id": rid,
        "ts": _flat(g("ts"), 40),
        "title": title,
        "code": code,
        "family": family_of(code) if code else "",
        "line": line,
        "kind": kind,
        "status": status,
        "verdict": verdict,
        "outcome": outcome(status, verdict),
        "why": _flat(g("why"), 700),
        "hypothesis": _flat(hyp, 700),
        "criterion": _flat(g("criterion"), 300),
        "lose": _flat(g("lose"), 400),
        "expected": _flat(g("expected"), 400),
        "result": result,
        "latest_note": (_flat(f"{notes[-1][0]}：{notes[-1][1]}", 700) if notes else ""),
        "n_notes": len(notes),
        "gauge": _checked_gauge(gauge(g("criterion"), res, g("noise")), outcome(status, verdict)),
        "edges": edges,
        "mentions": mentions,
        "pr_refs": pr_refs(whole),
        # reconstructed records: where in the write-up it came from, and what the checks removed
        "source": (f"{_flat(d['source'].get('doc'), 120)} §{_flat(d['source'].get('section'), 120)}"
                   if isinstance(d.get("source"), dict) else ""),
        "checks": [_flat(w, 120) for w in d.get("warnings", []) if isinstance(w, str)][:6] if isinstance(d.get("warnings"), list) else [],
    }


def _cited(xs) -> list[dict]:
    return [{"text": _flat(x.get("text"), 200), "cites": [c for c in x.get("cites", []) if isinstance(c, str)][:8]}
            for x in xs or [] if isinstance(x, dict) and x.get("text")][:4]


def _question(q: dict) -> dict:
    """An overlay question, keeping the structured claim fields."""
    out = {k: _flat(q.get(k), 500) for k in ("id", "title", "answer", "state", "parent", "kind", "note", "by", "stuck", "next", "decision")}
    out["template"] = bool(q.get("template"))
    for k in ("holds", "fails", "unknown"):
        out[k] = _cited(q.get(k))
    out["findings"] = [{
        "id": _flat(f.get("id"), 60), "statement": _flat(f.get("statement"), 240), "basis": _flat(f.get("basis"), 300),
        "so_what": _flat(f.get("so_what"), 240), "change_if": _flat(f.get("change_if"), 240),
        "confidence": _flat(f.get("confidence"), 60), "warnings": [_flat(w, 120) for w in f.get("warnings", []) if isinstance(w, str)][:6],
        "by": _flat(f.get("by"), 10),
        "cites": [{"id": _flat(c.get("id"), 40), "role": _flat(c.get("role"), 10), "quote": _flat(c.get("quote"), 240)}
                  for c in f.get("cites", []) if isinstance(c, dict)][:8],
    } for f in q.get("findings", []) if isinstance(f, dict)][:4]
    return out


def _no_nan(word: str):
    raise ValueError(f"non-finite number {word} in JSON")


def _ids(xs) -> list[str]:
    return [x for x in xs if isinstance(x, str)][:12] if isinstance(xs, list) else []


def _items(xs, keys: tuple[str, ...], n: int = 12, clip: int = 400) -> list[dict]:
    """A report section as a list of flat dicts with known text keys (+ cites/findings/warnings)."""
    out = []
    for x in xs if isinstance(xs, list) else []:
        if not isinstance(x, dict):
            continue
        d = {k: _flat(x.get(k), clip) for k in keys}
        for k in ("cites", "findings"):
            d[k] = _ids(x.get(k))
        d["warnings"] = [_flat(w, 200) for w in x.get("warnings", []) if isinstance(w, str)][:6] if isinstance(x.get("warnings"), list) else []
        ev = x.get("evidence")
        if isinstance(ev, dict) and ev.get("id") and ev.get("quote"):  # the record's own words behind a decision
            d["evidence"] = {"id": _flat(ev["id"], 40), "quote": _flat(ev["quote"], 300)}
        out.append(d)
    return out[:n]


def clean_report(rep) -> dict | None:
    """A meeting report reduced to the shape the page renders, so a hand edit
    gone wrong costs a section, not the page."""
    if not isinstance(rep, dict):
        return None
    op = rep.get("one_page") if isinstance(rep.get("one_page"), dict) else {}
    out = {k: _flat(rep.get(k), 300) for k in ("title", "cutoff", "fingerprint", "generated", "model", "status")}
    out["one_page"] = {**{k: _flat(op.get(k), 400) for k in ("goal", "success", "answer", "why_hard")},
                       "numbers": _items(op.get("numbers"), ("text", "confidence"), 5)}
    out["learned"] = _items(rep.get("learned"), ("headline", "basis", "so_what", "confidence"), 8)
    out["routes"] = _items(rep.get("routes"), ("question", "label", "plain", "status", "stuck"), 12)
    out["unknowns"] = _items(rep.get("unknowns"), ("item", "change_if"), 6)
    out["decisions"] = _items(rep.get("decisions"), ("decision", "options", "recommendation", "basis", "who", "by_when"), 8)
    out["settled"] = _items(rep.get("settled"), ("item", "decided", "basis"), 8)
    out["next_weeks"] = _items(rep.get("next_weeks"), ("task", "learn"), 8)
    out["glossary"] = _items(rep.get("glossary"), ("term", "plain"), 16)
    out["reviews"] = _items(rep.get("reviews"), ("at", "by", "note"), 10, 1000)
    out["warnings"] = [_flat(w, 200) for w in rep.get("warnings", []) if isinstance(w, str)][:6] if isinstance(rep.get("warnings"), list) else []
    return out


class ResearchProject:
    """One configured source; re-reads only when its files change."""

    def __init__(self, src: ResearchSource, data_dir: Path):
        self.src = src
        self.overlay_path = src.overlay or (data_dir / "research" / f"{src.name}.json")
        self._fp = ""
        self._data: dict = {}
        self._index: dict[str, Path] = {}
        self._lock = threading.Lock()

    def _files(self) -> list[Path]:
        """Regular .json files directly inside the registry dir; symlinks are
        skipped so a link can't pull in files from elsewhere."""
        try:
            root = self.src.path.resolve()
            out = [p for p in self.src.path.iterdir()
                   if p.suffix == ".json" and not p.is_symlink() and p.is_file() and p.resolve().parent == root]
        except OSError:
            return []
        return sorted(out)[:MAX_FILES]

    def records_fingerprint(self) -> str:
        """The registry files only (not the overlay or report): what a report snapshot was made from."""
        h = hashlib.sha1()
        for p in self._files():
            try:
                st = p.stat()
                h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
            except OSError:
                pass
        return h.hexdigest()[:16]

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        for p in [*self._files(), self.overlay_path, self.overlay_path.with_name(self.overlay_path.stem + ".report.json"),
                  github_cache_path(self.overlay_path)]:
            try:
                st = p.stat()
                h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_ctime_ns}:{st.st_ino}:{st.st_size};".encode())
            except OSError:
                h.update(f"{p.name}:-;".encode())
        return h.hexdigest()[:16]

    def data(self) -> dict:
        with self._lock:
            fp = self.fingerprint()
            if fp != self._fp:
                self._data, self._fp = self._load(), fp
                self._data["fp"] = fp
                self._data["records_fp"] = self.records_fingerprint()
            return self._data

    def _load(self) -> dict:
        records, errors, index, total = [], [], {}, 0
        files = self._files()
        if len(files) >= MAX_FILES:
            errors.append(f"only the first {MAX_FILES} files are read")
        for p in files:
            try:
                size = p.stat().st_size
                if size > MAX_FILE:
                    raise ValueError("file too large")
                total += size
                if total > MAX_TOTAL:
                    errors.append("registry larger than the read budget; the rest is skipped")
                    break
                d = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    raise ValueError("not a JSON object")
                r = normalise(d, self.src)
                if not r["id"]:
                    raise ValueError("no id")
                records.append(r)
                index[r["id"]] = p
            except Exception as e:  # one bad record must not hide the rest
                errors.append(f"{p.name}: {e}")
        records.sort(key=lambda r: (r["ts"], r["id"]))
        self._index = index
        ids = {r["id"] for r in records}
        questions, edges, goals = [], [], []
        try:
            if self.overlay_path.stat().st_size > MAX_FILE * 4:
                raise ValueError("overlay too large")
            ov = json.loads(self.overlay_path.read_text(encoding="utf-8"), parse_constant=_no_nan)
            goals = [{**{k: _flat(g.get(k), 400) for k in ("id", "title", "bar", "answer", "state")},
                      "answer_cites": [c for c in g.get("answer_cites", []) if isinstance(c, str)][:8]}
                     for g in ov.get("goals", []) if isinstance(g, dict) and g.get("id")]
            questions = [_question(q) for q in ov.get("questions", []) if isinstance(q, dict) and q.get("id")]
            per = ov.get("records", {}) or {}
            for r in records:
                o = per.get(r["id"])
                if isinstance(o, dict):
                    r["question"] = str(o.get("question") or "")
                    r["why_short"] = _flat(o.get("why"), 200)
                    r["result_short"] = _flat(o.get("result"), 240)
                    r["name"] = _flat(o.get("name"), 40)
                    r["level"] = int(o.get("level") or 0) if str(o.get("level") or "0").isdigit() else 0
                    r["n"] = int(o.get("n") or 0) if str(o.get("n") or "0").isdigit() else 0
            for e in ov.get("edges", []):
                if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids and e.get("type") in EDGE_TYPES:
                    edges.append({"from": e["from"], "to": e["to"], "type": e["type"],
                                  "quote": _flat(e.get("quote"), 300), "by": str(e.get("by") or "ai")})
        except FileNotFoundError:
            pass
        except Exception as e:
            errors.append(f"overlay: {e}")
        report = None
        rp = self.overlay_path.with_name(self.overlay_path.stem + ".report.json")
        try:
            if rp.stat().st_size <= MAX_FILE:
                report = clean_report(json.loads(rp.read_text(encoding="utf-8"), parse_constant=_no_nan))
        except FileNotFoundError:
            pass
        except Exception as e:
            errors.append(f"report: {e}")
        # field edges pointing at unknown ids are kept out of the graph
        for r in records:
            r["edges"] = [e for e in r["edges"] if e["from"] in ids and e["to"] in ids]
            r["mentions"] = [m for m in r["mentions"] if m in ids]
        github = None
        if self.src.repo:
            cache = read_github_cache(github_cache_path(self.overlay_path))
            try:
                by_id = {r["id"]: r for r in records}
                for rid, prs in github_links(records, index, cache, weak=self.src.reconstructed).items():
                    by_id[rid]["prs"] = prs[:12]
            except Exception as e:  # a bad cache costs the links, not the page
                errors.append(f"github: {e}")
            github = {"repo": self.src.repo, "fetched_at": _flat(cache.get("fetched_at"), 40),
                      "error": _flat(cache.get("error"), 300), "error_at": _flat(cache.get("error_at"), 40),
                      "prs": len(cache.get("prs") or [])}
        return {
            "name": self.src.name,
            "label": self.src.label or self.src.name,
            "project_dir": self.src.path.parent.name,
            "reconstructed": self.src.reconstructed,
            "ladder": list(self.src.ladder),
            "unit": self.src.unit,
            "min_n": self.src.min_n,
            "records": records,
            "goals": goals,
            "questions": questions,
            "report": report,
            "edges": edges,
            "github": github,
            "errors": errors[:50],
        }

    def record(self, rid: str) -> dict | None:
        """One record with long text fields unclipped (for the 更多 button).
        Only ids the loader already indexed are served: one exact file read."""
        self.data()
        p = self._index.get(rid or "")
        if not p:
            return None
        try:
            if p.is_symlink() or p.stat().st_size > MAX_FILE:
                return None
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(d, dict) or str(_get(d, self.src.field_names("id")) or "") != rid:
            return None
        g = lambda k: _get(d, self.src.field_names(k))  # noqa: E731
        res = g("result")
        if isinstance(res, dict):
            detail = res.get("cv") if isinstance(res.get("cv"), str) else {k: res[k] for k in ("val", "test") if k in res}
            result = _flat(detail, 20000)
        else:
            result = _flat(res, 20000)
        return {"id": rid, "why": _flat(g("why"), 20000), "hypothesis": _flat(g("hypothesis"), 20000),
                "lose": _flat(g("lose"), 20000), "expected": _flat(g("expected"), 20000), "result": result}

    def summary(self) -> dict:
        d = self.data()
        return {"name": d["name"], "label": d["label"], "fp": d["fp"], "count": len(d["records"]),
                "project_dir": d["project_dir"], "errors": len(d["errors"])}
