"""`wd research propose`: let Claude read the records and draft the overlay.

Runs on the hub machine, with the realm's own Claude profile, through
``claude -p`` (no tools, no MCP, no session saved). Two passes:

1. questions -- from a one-line index of every record (plus an optional
   context file such as the project README), draft 8-20 research questions,
   their current answers, and which records belong to each;
2. records   -- in batches, for each record: which question, why it was done,
   what came out, and typed links to earlier records *with a verbatim quote*.
   A link whose quote can't be found in the record's own text is dropped.

Everything written is marked ``by: "ai"``. Edges a person marked
``by: "human"`` in the overlay are kept across reruns. The overlay is written
after every batch, so the board fills in while it runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from wd.research import ResearchProject, ResearchSource, _flat, _get

LINK_TYPES = ("because", "follows", "readjudicates", "supersedes", "corrects", "uses")

SYSTEM = (
    "你是研究整理助理，幫一位研究者看懂他自己大量、分散的實驗登記。"
    "只根據提供的文字回答，不要猜測沒寫的事。用繁體中文、白話、短句。"
    "只輸出符合 JSON schema 的結果。"
)

Q_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": "^q-[a-z0-9-]{2,40}$"},
            "title": {"type": "string"},
            "answer": {"type": "string"},
            "state": {"type": "string", "enum": ["supported", "refuted", "mixed", "open"]},
            "members": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "title", "answer", "state", "members"],
    }}},
    "required": ["questions"],
}

R_SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "question": {"type": "string"},
            "why": {"type": "string"},
            "result": {"type": "string"},
            "edges": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "type": {"type": "string", "enum": list(LINK_TYPES)},
                    "quote": {"type": "string"},
                },
                "required": ["to", "type", "quote"],
            }},
        },
        "required": ["id", "question", "why", "result", "edges"],
    }}},
    "required": ["records"],
}

Q_PROMPT = """下面是一個研究專案的全部實驗登記索引（每行：編號｜日期｜代號｜結果｜標題｜判定）。
{context}
請整理出 8 到 20 個「研究問題」——這些實驗到底在回答哪些問題。要求：
- 每個問題用一句問句寫（例：「模擬結果對得上真實量測嗎？」），不要用代號當標題。
- answer：根據這些登記的結果，目前的答案（1–2 句，說清楚是成立、不成立還是還不知道）。
- state：supported（大致成立）、refuted（大致不成立）、mixed（有成立也有不成立）、open（還沒有答案）。
- members：屬於這個問題的登記編號。每筆登記最多屬於一個問題；規則修訂、工具校正這類也可以自成一個問題。
- id：q- 加英文小寫短名（例：q-sim-to-real）。

登記索引：
{index}
"""

R_PROMPT = """研究問題：
{questions}

全部登記的索引（用來找關係的對象）：
{index}

請逐筆整理下面這 {n} 筆登記：
- question：它屬於哪個研究問題的 id；真的都不屬於就填空字串。
- why：為什麼做這個實驗（60 字內；講動機，或是承接哪個結果、要解決什麼問題）。
- result：結果與意義（60 字內；還沒結果就寫「還沒有結果」）。
- edges：它和「更早」的登記之間、文字裡有根據的關係，沒有就給空陣列。type 只能是：
  because（因為那筆的結果才做這筆）、follows（同一條線的下一步／修正重做）、
  readjudicates（用新規則重新判定那筆）、supersedes（取代那筆）、corrects（更正那筆的錯）、
  uses（用到那筆定下的常數、規則或方法）。
  quote 必須是從「這筆登記的文字」裡逐字複製、能支持這個關係的一小段（80 字內）。
  to 可以是登記編號，也可以是索引裡的代號（例如 E8、HUMID-1b），我會換成編號。

登記內容：
{records}
"""


def brief(raw: dict, src: ResearchSource) -> str:
    """The parts of one record worth reading, with generous clips."""
    g = lambda k, n: _flat(_get(raw, src.field_names(k)), n)  # noqa: E731
    res = _get(raw, src.field_names("result"))
    parts = [
        ("編號", g("id", 40)), ("時間", g("ts", 30)), ("標題", g("title", 200)), ("線", g("line", 80)),
        ("種類", g("kind", 60)), ("狀態", g("status", 160)), ("承接", g("parent", 300)), ("為什麼", g("why", 1500)),
        ("假設", g("hypothesis", 1800)), ("判準", g("criterion", 400)), ("輸的條件", g("lose", 600)),
        ("事前預期", g("expected", 500)),
        ("結果", _flat({k: res[k] for k in ("verdict", "cv", "diagnosis", "next_step_proposal") if k in res}, 1400)
         if isinstance(res, dict) else _flat(res, 1000)),
    ]
    return "\n".join(f"{k}：{v}" for k, v in parts if v)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def call_claude(prompt: str, schema: dict, config_dir: Path, model: str, timeout: int = 900) -> dict:
    exe = os.environ.get("WD_CLAUDE") or str(Path.home() / ".local/bin/claude")
    cmd = [exe, "-p", "--model", model, "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--setting-sources", "user", "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
           "--system-prompt", SYSTEM]
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    with tempfile.TemporaryDirectory() as cwd:  # no project CLAUDE.md, no repo access
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    out = json.loads(p.stdout)
    data = out.get("structured_output")
    if data is None:  # older CLIs: the JSON is in the text result
        text = out.get("result", "")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    return data


def propose(src: ResearchSource, data_dir: Path, config_dir: Path, model: str = "sonnet", batch: int = 16,
            context: Path | None = None, only_questions: bool = False, log=print) -> Path:
    proj = ResearchProject(src, data_dir)
    recs = proj.data()["records"]
    raw = {}
    for p in sorted(src.path.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            raw[str(_get(d, src.field_names("id")) or "")] = d
        except Exception:
            continue
    ids = [r["id"] for r in recs if r["id"] in raw]
    by = {r["id"]: r for r in recs}
    code_to_id: dict[str, str] = {}
    for r in recs:  # a code names a record only if it is unambiguous
        if r["code"]:
            code_to_id[r["code"]] = "" if r["code"] in code_to_id else r["id"]
    index = "\n".join(f"{i}｜{by[i]['ts'][:10]}｜{by[i]['code'] or '-'}｜{by[i]['outcome']}｜{by[i]['title'][:100]}｜{by[i]['verdict'][:60]}" for i in ids)

    out_path = proj.overlay_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = json.loads(out_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        old = {}
    human_edges = [e for e in old.get("edges", []) if isinstance(e, dict) and e.get("by") == "human"]

    def write(ov: dict) -> None:
        ov = {**ov, "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model}
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, out_path)

    ctx = ""
    if context and context.is_file():
        ctx = "\n專案說明（節錄）：\n" + context.read_text(encoding="utf-8", errors="replace")[:12000] + "\n"
    log(f"[propose] {len(ids)} records; pass 1: questions")
    qd = call_claude(Q_PROMPT.format(context=ctx, index=index), Q_SCHEMA, config_dir, model)
    questions, member_of = [], {}
    for q in qd.get("questions", []):
        if not re.fullmatch(r"q-[a-z0-9-]{2,40}", str(q.get("id", ""))) or any(x["id"] == q["id"] for x in questions):
            continue
        questions.append({k: _flat(q.get(k), 400) for k in ("id", "title", "answer", "state")} | {"by": "ai"})
        for m in q.get("members", []):
            if m in by:
                member_of.setdefault(m, q["id"])
    qids = {q["id"] for q in questions}
    overlay = {"questions": questions, "records": {i: {"question": member_of.get(i, "")} for i in ids}, "edges": list(human_edges)}
    write(overlay)
    log(f"[propose] {len(questions)} questions")
    if only_questions:
        return out_path

    qtext = "\n".join(f"{q['id']}：{q['title']}" for q in questions)
    short_index = "\n".join(f"{i}｜{by[i]['code'] or '-'}｜{by[i]['ts'][:10]}｜{by[i]['title'][:60]}" for i in ids)
    kept = dropped = 0
    for n in range(0, len(ids), batch):
        chunk = ids[n:n + batch]
        body = "\n\n---\n".join(brief(raw[i], src) for i in chunk)
        try:
            rd = call_claude(R_PROMPT.format(questions=qtext, index=short_index, n=len(chunk), records=body), R_SCHEMA, config_dir, model)
        except Exception as e:  # one bad batch shouldn't lose the others
            log(f"[propose] batch {n // batch + 1}: {e}")
            continue
        for r in rd.get("records", []):
            i = r.get("id")
            if i not in chunk:
                continue
            text = _norm(json.dumps(raw[i], ensure_ascii=False))
            entry = overlay["records"].setdefault(i, {})
            q = r.get("question") or entry.get("question", "")
            entry.update({"question": q if q in qids else entry.get("question", ""),
                          "why": _flat(r.get("why"), 200), "result": _flat(r.get("result"), 200)})
            for e in r.get("edges", []):
                to = str(e.get("to", ""))
                to = to if to in by else code_to_id.get(to, "")
                quote = _flat(e.get("quote"), 300)
                if not to or to == i or e.get("type") not in LINK_TYPES or by[to]["ts"] > by[i]["ts"]:
                    dropped += 1
                    continue
                if len(_norm(quote)) < 4 or _norm(quote).replace("…", "") not in text:
                    dropped += 1  # no verbatim support in the record itself
                    continue
                if any(x["from"] == i and x["to"] == to for x in overlay["edges"]):
                    continue
                overlay["edges"].append({"from": i, "to": to, "type": e["type"], "quote": quote, "by": "ai"})
                kept += 1
        write(overlay)
        log(f"[propose] batch {n // batch + 1}/{(len(ids) + batch - 1) // batch}: links kept {kept}, dropped {dropped}")
    return out_path


def main(cfg, name: str, model: str, batch: int, context: str | None, profile: str | None, only_questions: bool) -> int:
    src = next((r for r in cfg.hub.research if r.name == name), None)
    if not src:
        print(f"no [[hub.research]] named {name!r} on this hub", file=sys.stderr)
        return 2
    prof = next((p for p in cfg.profiles if (p.label == profile if profile else p.realm == cfg.hub.realm)), None)
    if not prof:
        print("no profile for this hub's realm; pass --profile", file=sys.stderr)
        return 2
    path = propose(src, cfg.hub.data_dir, prof.config_dir, model=model, batch=batch,
                   context=Path(context).expanduser() if context else None, only_questions=only_questions)
    print(f"overlay written: {path}")
    return 0

