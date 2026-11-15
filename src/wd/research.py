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

    {"questions": [{"id": "q-humidity", "title": "...", "answer": "...",
                    "state": "supported|refuted|open|mixed", "note": "..."}],
     "records":   {"REG-...": {"question": "q-humidity", "why": "...", "result": "..."}},
     "edges":     [{"from": "REG-b", "to": "REG-a", "type": "because",
                    "quote": "...", "by": "ai"}]}
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE = 4 * 1024 * 1024
RID = re.compile(r"REG-\d{8}-[0-9a-f]{4}")
# family codes such as HUMID-1c, DFT-GATE-1, E8b, S2 (but not REG-... ids)
CODE = re.compile(r"(?<![A-Za-z0-9])((?!REG-)[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)+[a-z]?|E\d+[a-z]?|S\d)(?![A-Za-z0-9])")

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
}


@dataclass
class ResearchSource:
    name: str
    path: Path
    label: str = ""
    format: str = "prereg-json"
    overlay: Path | None = None
    fields: dict[str, list[str]] = field(default_factory=dict)

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
    """pass | fail | inconclusive | void | running | open | other."""
    s, v = status.lower(), verdict.lower()
    if "no-verdict" in v or "未定" in v or "inconclusive" in v:
        return "inconclusive"
    for prefix, out in (("void", "void"), ("running", "running"), ("registered", "open"), ("open", "open"),
                        ("pass", "pass"), ("adopted", "pass"), ("fail", "fail"), ("adjudicated_fail", "fail"),
                        ("gate_failed", "fail")):
        if s.startswith(prefix):
            return out
    if v.startswith("pass"):
        return "pass"
    if v.startswith("fail"):
        return "fail"
    return "other"


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", str(v or ""))
    return float(m.group(1)) if m else None


def gauge(criterion, result, noise) -> dict | None:
    """Threshold vs measured value, when both are plain numbers: enough to draw
    where the result landed relative to the line it had to cross."""
    if not isinstance(criterion, dict) or not isinstance(result, dict):
        return None
    th, val = criterion.get("threshold"), result.get("val")
    if isinstance(th, bool) or isinstance(val, bool) or not isinstance(th, (int, float)) or not isinstance(val, (int, float)):
        return None
    d = str(criterion.get("direction") or "").lower()
    nf = _num(noise)
    return {"metric": _flat(criterion.get("metric"), 120), "dir": "down" if d.startswith("down") or d == "lower" else "up",
            "th": float(th), "val": float(val), "noise": abs(nf) if nf is not None else None}


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
    whole = json.dumps(d, ensure_ascii=False)
    mentions = sorted({x for x in RID.findall(whole) if x != rid and (rid, x) not in seen})

    return {
        "id": rid,
        "ts": _flat(g("ts"), 40),
        "title": title,
        "code": code,
        "family": family_of(code) if code else "",
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
        "gauge": gauge(g("criterion"), res, g("noise")),
        "edges": edges,
        "mentions": mentions,
    }


class ResearchProject:
    """One configured source; re-reads only when its files change."""

    def __init__(self, src: ResearchSource, data_dir: Path):
        self.src = src
        self.overlay_path = src.overlay or (data_dir / "research" / f"{src.name}.json")
        self._fp = ""
        self._data: dict = {}
        self._lock = threading.Lock()

    def _files(self) -> list[Path]:
        try:
            return sorted(p for p in self.src.path.iterdir() if p.suffix == ".json" and p.is_file())
        except OSError:
            return []

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        for p in [*self._files(), self.overlay_path]:
            try:
                st = p.stat()
                h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
            except OSError:
                h.update(f"{p.name}:-;".encode())
        return h.hexdigest()[:16]

    def data(self) -> dict:
        with self._lock:
            fp = self.fingerprint()
            if fp != self._fp:
                self._data, self._fp = self._load(), fp
                self._data["fp"] = fp
            return self._data

    def _load(self) -> dict:
        records, errors = [], []
        for p in self._files():
            try:
                if p.stat().st_size > MAX_FILE:
                    raise ValueError("file too large")
                d = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    raise ValueError("not a JSON object")
                r = normalise(d, self.src)
                if not r["id"]:
                    raise ValueError("no id")
                records.append(r)
            except Exception as e:  # one bad record must not hide the rest
                errors.append(f"{p.name}: {e}")
        records.sort(key=lambda r: (r["ts"], r["id"]))
        ids = {r["id"] for r in records}
        questions, edges, goals = [], [], []
        try:
            ov = json.loads(self.overlay_path.read_text(encoding="utf-8"))
            goals = [{k: _flat(g.get(k), 400) for k in ("id", "title", "bar", "answer", "state")}
                     for g in ov.get("goals", []) if isinstance(g, dict) and g.get("id")]
            questions = [{k: _flat(q.get(k), 500) for k in ("id", "title", "answer", "state", "parent", "kind", "note", "by")}
                         for q in ov.get("questions", []) if isinstance(q, dict) and q.get("id")]
            per = ov.get("records", {}) or {}
            for r in records:
                o = per.get(r["id"])
                if isinstance(o, dict):
                    r["question"] = str(o.get("question") or "")
                    r["why_short"] = _flat(o.get("why"), 200)
                    r["result_short"] = _flat(o.get("result"), 200)
            for e in ov.get("edges", []):
                if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids and e.get("type") in EDGE_TYPES:
                    edges.append({"from": e["from"], "to": e["to"], "type": e["type"],
                                  "quote": _flat(e.get("quote"), 300), "by": str(e.get("by") or "ai")})
        except FileNotFoundError:
            pass
        except Exception as e:
            errors.append(f"overlay: {e}")
        # field edges pointing at unknown ids are kept out of the graph
        for r in records:
            r["edges"] = [e for e in r["edges"] if e["from"] in ids and e["to"] in ids]
            r["mentions"] = [m for m in r["mentions"] if m in ids]
        return {
            "name": self.src.name,
            "label": self.src.label or self.src.name,
            "project_dir": self.src.path.parent.name,
            "records": records,
            "goals": goals,
            "questions": questions,
            "edges": edges,
            "errors": errors[:50],
        }

    def record(self, rid: str) -> dict | None:
        """One record with long text fields unclipped (for the 更多 button)."""
        if not RID.fullmatch(rid or "") and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", rid or ""):
            return None
        for p in self._files():
            if p.stem != rid and rid not in p.name:
                continue
            try:
                if p.stat().st_size > MAX_FILE:
                    return None
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
            if not isinstance(d, dict) or str(_get(d, self.src.field_names("id")) or "") != rid:
                continue
            g = lambda k: _get(d, self.src.field_names(k))  # noqa: E731
            res = g("result")
            if isinstance(res, dict):
                detail = res.get("cv") if isinstance(res.get("cv"), str) else {k: res[k] for k in ("val", "test") if k in res}
                result = _flat(detail, 20000)
            else:
                result = _flat(res, 20000)
            return {"id": rid, "why": _flat(g("why"), 20000), "hypothesis": _flat(g("hypothesis"), 20000),
                    "lose": _flat(g("lose"), 20000), "expected": _flat(g("expected"), 20000), "result": result}
        return None

    def summary(self) -> dict:
        d = self.data()
        return {"name": d["name"], "label": d["label"], "fp": d["fp"], "count": len(d["records"]),
                "project_dir": d["project_dir"], "errors": len(d["errors"])}
