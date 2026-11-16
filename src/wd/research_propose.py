"""`wd research propose`: let Claude read the records and draft the overlay
and a meeting report, bottom-up, with every claim tied to its sources.

Runs on the hub machine with the realm's own Claude profile, through
``claude -p`` (no tools, no MCP, no session saved, neutral cwd). Stages, each
reading only what the previous one verified:

1. records   -- per record: a plain name (no internal codes), why, result
                (including later addenda), evidence level 1-5 with a verbatim
                quote, number of materials, and typed links with quotes;
2. structure -- goals and the routes/questions under them, from the record
                summaries (an existing outline may seed the names);
3. claims    -- per route: what holds / fails / is unknown, where it is stuck,
                next step, decision needed, and 0-3 findings, each citing
                records with a role (supports / against / limits) and a quote;
4. report    -- a meeting report built from the findings, plus each record's
                own words on what was decided or comes next, so an item the
                records already settled isn't presented as an open decision.

Programs, not the model, decide: confidence labels (from cited records'
evidence level, sample size and verdicts), whether a number in a sentence
appears in its sources, forbidden words, links pointing forward in time, and
the "too many open records to conclude" template, and that every decision or
settled item quotes a record that shows it is still open / already decided. Everything AI-written is
``by: "ai"``; edges marked ``by: "human"`` survive reruns.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from wd.research import (CODE, DEFAULT_LADDER, MAX_FILE, ResearchProject, ResearchSource, _flat, _get, addenda, decision_text,
                         match_register, read_register, register_path)

LINK_TYPES = ("because", "follows", "readjudicates", "supersedes", "corrects", "uses")
STUCK = ("method", "measure", "external", "not_started", "closed", "none")

SYSTEM = (
    "你是研究整理助理，幫一位研究者把大量、分散的實驗登記整理成人看得懂的內容。"
    "只根據提供的文字，不要猜測沒寫的事；不確定就說不確定。用繁體中文、白話、短句，"
    "不要用內部實驗代號（像 ABL-2c、E3、REG-…）當主詞。只輸出符合 JSON schema 的結果。"
)

S_EDGE = {"type": "object", "properties": {"to": {"type": "string"}, "type": {"type": "string", "enum": list(LINK_TYPES)},
                                           "quote": {"type": "string"}}, "required": ["to", "type", "quote"]}
R_SCHEMA = {"type": "object", "properties": {"records": {"type": "array", "items": {
    "type": "object",
    "properties": {
        "id": {"type": "string"}, "name": {"type": "string"}, "why": {"type": "string"}, "result": {"type": "string"},
        "level": {"type": "integer", "minimum": 0, "maximum": 5}, "level_quote": {"type": "string"},
        "n": {"type": "integer", "minimum": 0}, "edges": {"type": "array", "items": S_EDGE},
    },
    "required": ["id", "name", "why", "result", "level", "level_quote", "n", "edges"],
}}}, "required": ["records"]}

Q_SCHEMA = {"type": "object", "properties": {
    "goals": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string", "pattern": "^g-[a-z0-9-]{2,40}$"}, "title": {"type": "string"}, "bar": {"type": "string"},
    }, "required": ["id", "title", "bar"]}},
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string", "pattern": "^q-[a-z0-9-]{2,40}$"}, "parent": {"type": "string"},
        "kind": {"type": "string", "enum": ["route", "question", "cross"]}, "title": {"type": "string"},
        "members": {"type": "array", "items": {"type": "string"}},
    }, "required": ["id", "parent", "kind", "title", "members"]}},
}, "required": ["goals", "questions"]}

S_CITED = {"type": "object", "properties": {"text": {"type": "string"}, "cites": {"type": "array", "items": {"type": "string"}}},
           "required": ["text", "cites"]}
C_SCHEMA = {"type": "object", "properties": {
    "goal_answer": S_CITED,
    "questions": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"},
        "holds": {"type": "array", "items": S_CITED}, "fails": {"type": "array", "items": S_CITED},
        "unknown": {"type": "array", "items": S_CITED},
        "stuck": {"type": "string", "enum": list(STUCK)}, "next": {"type": "string"}, "decision": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "statement": {"type": "string"}, "basis": {"type": "string"}, "so_what": {"type": "string"},
            "change_if": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "role": {"type": "string", "enum": ["supports", "against", "limits"]},
                "quote": {"type": "string"}}, "required": ["id", "role", "quote"]}},
        }, "required": ["statement", "basis", "so_what", "change_if", "cites"]}},
    }, "required": ["id", "holds", "fails", "unknown", "stuck", "next", "decision", "findings"]}},
}, "required": ["goal_answer", "questions"]}

S_EVID = {"type": "object", "properties": {"id": {"type": "string"}, "quote": {"type": "string"}}, "required": ["id", "quote"]}
M_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"},
    "one_page": {"type": "object", "properties": {
        "goal": {"type": "string"}, "success": {"type": "string"}, "answer": {"type": "string"}, "why_hard": {"type": "string"},
        "numbers": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "findings": {"type": "array", "items": {"type": "string"}}}, "required": ["text", "findings"]}}},
        "required": ["goal", "success", "answer", "why_hard", "numbers"]},
    "learned": {"type": "array", "items": {"type": "object", "properties": {
        "headline": {"type": "string"}, "basis": {"type": "string"}, "so_what": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}}}, "required": ["headline", "basis", "so_what", "findings"]}},
    "routes": {"type": "array", "items": {"type": "object", "properties": {
        "question": {"type": "string"}, "plain": {"type": "string"}, "status": {"type": "string"}, "stuck": {"type": "string"}},
        "required": ["question", "plain", "status", "stuck"]}},
    "unknowns": {"type": "array", "items": {"type": "object", "properties": {
        "item": {"type": "string"}, "change_if": {"type": "string"}, "findings": {"type": "array", "items": {"type": "string"}}},
        "required": ["item", "change_if", "findings"]}},
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "decision": {"type": "string"}, "options": {"type": "string"}, "recommendation": {"type": "string"},
        "who": {"type": "string"}, "by_when": {"type": "string"}, "findings": {"type": "array", "items": {"type": "string"}},
        "evidence": S_EVID},
        "required": ["decision", "options", "recommendation", "who", "by_when", "findings", "evidence"]}},
    "settled": {"type": "array", "items": {"type": "object", "properties": {
        "item": {"type": "string"}, "decided": {"type": "string"}, "findings": {"type": "array", "items": {"type": "string"}},
        "evidence": S_EVID},
        "required": ["item", "decided", "findings", "evidence"]}},
    "next_weeks": {"type": "array", "items": {"type": "object", "properties": {
        "task": {"type": "string"}, "learn": {"type": "string"}}, "required": ["task", "learn"]}},
    "glossary": {"type": "array", "items": {"type": "object", "properties": {
        "term": {"type": "string"}, "plain": {"type": "string"}}, "required": ["term", "plain"]}},
}, "required": ["title", "one_page", "learned", "routes", "unknowns", "decisions", "settled", "next_weeks", "glossary"]}

R_PROMPT = """全部登記的索引（找關係的對象；每行：編號｜代號｜日期｜標題）：
{index}

請逐筆整理下面這 {n} 筆登記，給一個不懂專案的同事看：
- name：16 字內的白話短名，講這筆在問什麼。不要用代號。
- why：為什麼做（60 字內，講動機或承接了哪個結果）。
- result：結果與意義（80 字內）。結果常寫在「後續補記」裡，要一起看；真的沒有任何結果才寫「還沒有結果」。
- level：證據層級，{levels}。0 表示看不出來。
- level_quote：從這筆登記文字逐字複製、能看出層級的一小段（40 字內）；level 為 0 時給空字串。
- n：這筆的樣本數，以「{unit}」計，文字裡沒寫就給 0。
- edges：它和更早登記之間有文字根據的關係；type 只能是 because（因為那筆的結果才做）、follows（同一條線的下一步或重做）、readjudicates（用新規則重判）、supersedes（取代）、corrects（更正）、uses（用到那筆定下的常數、規則或方法）。quote 必須從這筆登記逐字複製（80 字內）。to 可以是編號或索引裡的代號。

登記內容：
{records}
"""

Q_PROMPT = """下面是一個研究專案全部實驗登記的白話摘要（每行：編號｜日期｜判定｜白話名｜結果｜證據層級）。
{context}{structure}
請整理成兩層：
1. goals（2–5 個）：研究的大目標或命題。bar：怎樣才算達成，盡量具體、有數字（只能用文字裡出現過的標準，找不到就寫「尚未定義」）。
   另外固定加一個 id 為 g-cross 的目標，標題「橫向：方法與資料問題」。
2. questions（8–24 個）：每個目標底下的途徑或問題。parent 是目標 id；kind：route（一條達成目標的假設路線）、question（要回答的問題）、cross（g-cross 底下的方法類）；
   title 一句白話（途徑寫成短句，問題寫成問句，不要用代號）；members 是屬於它的登記編號，每筆最多屬於一個。id 用 q- 加英文小寫短名。
如果有「既有的路線整理」，它不一定對，但可以沿用其中資料支持的目標與途徑名稱。

登記摘要：
{index}
"""

C_PROMPT = """目標：{goal}（怎樣算成功：{bar}）

下面是這個目標底下每條途徑或問題，以及屬於它的登記摘要（編號｜日期｜登記判定｜證據層級｜樣本數（{unit}）｜白話名｜為什麼｜結果｜登記原文寫的決定或下一步）。
{verdict_note}

{blocks}

請對每條途徑或問題回答（只根據上面的摘要）：
- holds / fails / unknown：目前成立的、不成立的、還不知道的，各 0–3 句，每句 40 字內，cites 列出根據的登記編號。
- stuck：卡在哪，只能是 method（方法還不可信）、measure（在等量測）、external（在等外部決定或資料）、not_started（還沒開始）、closed（已結案）、none（沒有卡住）。
- next：下一步（一句）；decision：需要研究者或主管決定、而且登記原文還沒定的事（沒有就給空字串）。登記原文已經寫了採用、不採用、凍結、規則或下一步的，算已經定了，不要列成 decision。
- findings：0–3 條真正學到的事（會改變下一步、或打開或關掉一條路的才寫）。每條：
  statement 一句可被反駁的結論（最多一個主要數字，數字照原文寫，例如「從 47% 提到 66%」；原文本來就用倍數時才寫幾倍；不要自己換算，也不用 dex、R² 這類術語；數字後面說跟什麼比）；
  basis 憑什麼（跟什麼比、幾{unit}）；so_what 所以要怎樣；change_if 什麼結果會推翻它；
  cites 每個根據的登記：role 是 supports（支持這句結論）、against（反對）、limits（限制適用範圍），quote 從該登記摘要的「結果」或「為什麼」逐字複製。
最後 goal_answer：這個目標目前的整體答案（一句，60 字內）與根據。
"""

M_PROMPT = """請把下面的研究整理寫成一份 10–15 分鐘的會議報告，聽眾是主管和同事，不熟悉內部術語。
規則：
- 不要出現內部實驗代號；一句最多一個主要數字，照結論清單裡的寫法，不要自己換算成倍數；不用 dex、R² 等術語（真的要用就放進 glossary 解釋）；每個數字後面說跟什麼比。
- 只能使用下面「結論清單」裡的內容；learned 與 unknowns 一定要列出所根據的結論 id（findings 欄位），不得新增沒有出處的說法。
- 把握程度不要自己寫，報告產生器會依結論 id 自動帶出。
- 最後面「研究者已經核對過的事」：已確認的會由報告產生器原樣放進報告，decisions 與 settled 不要重複列、也不要改寫；判定不對的不要再列。
- learned 選 3–5 條「會改變下一步、或打開或關掉一條路線」的；routes 列主命題的各條途徑；unknowns 最多 3 條可能翻盤的；
  decisions 只列還沒定、而且研究者自己定不了的事，並列出根據的結論 id；evidence 給一筆登記的編號和能看出「這件事還沒定」的引句（從原文逐字連續複製，40 字內，不要用…省略，例如寫著待決定、尚未定義），找不到就 id 與 quote 都給空字串；
  下面「登記裡寫的決定與下一步」已經寫了採用、不採用、凍結、規則或下一步的事，放進 settled（已經定了、不必上會），evidence 給那筆的編號和逐字連續複製的引句（40 字內，不要用…省略），不要當成待決；next_weeks 最多 5 項，各寫做完會知道什麼。
- 要能在會議上講：one_page 的 goal 60 字內、success 80 字內、answer 80 字內、why_hard 80 字內；numbers 正好 3 個，每個一句、列出根據的結論 id；
  learned 的 headline 一句結論（30 字內）、basis 100 字內、so_what 80 字內；unknowns 的 item 40 字內、change_if 80 字內；decisions 每欄 60 字內；next_weeks 每項 50 字內。
{rules}
目標與途徑：
{goals}

結論清單（id｜把握｜結論｜憑什麼｜所以｜什麼會推翻它）：
{findings}

登記裡寫的決定與下一步（登記原文，依途徑分組，新的在前）：
{decided}

研究者已經核對過的事（以研究者為準）：
{confirmed}
"""


# ---- per-project vocabulary --------------------------------------------------------
VERDICT_NOTE = {
    False: "登記判定是「這筆是否符合它事先登記的規則」，不等於支持或反對這條途徑；例如一筆想證明「某方法不可靠」的登記通過，是在說那個方法不可靠。",
    True: "這些紀錄是事後從專案文件整理的，沒有事先定的門檻。判定是整理時讀出的「這次改法比對照好（pass）、沒改善或變差（fail）、看不出來（inconclusive）、還在跑（running）」，不等於支持或反對這條途徑。",
}


def levels_text(src: ResearchSource) -> str:
    return "；".join(f"{k} {name}" for k, name in enumerate(src.ladder, 1))


def adapt(prompt: str, src: ResearchSource) -> str:
    """Reconstructed records were never registered: say 紀錄, not 登記."""
    return prompt.replace("事先登記", "事先寫下").replace("登記", "紀錄") if src.reconstructed else prompt


def conf_ladder(src: ResearchSource | None) -> list[str]:
    """Confidence labels, weakest first. The default ladder keeps its
    established wording; a custom ladder gets labels built from its steps."""
    ladder = list(src.ladder) if src else list(DEFAULT_LADDER)
    unit, min_n = (src.unit, src.min_n) if src else ("個材料", 8)
    word = "紀錄" if src and src.reconstructed else "登記"
    if ladder == DEFAULT_LADDER:
        low, mid3, mid4, top = "只在模擬內成立", "對公開實測成立", "對實測成立", "目標條件下實測確認"
    else:
        low, mid3, mid4, top = f"只在「{ladder[1]}」成立", f"在「{ladder[2]}」成立", f"在「{ladder[3]}」成立", f"{ladder[4]}"
    return [f"沒有直接支持的{word}", "證據層級不明", "分不出來或還在跑", low, f"初步、樣本小（少於 {min_n} {unit}不裁定）", mid3, mid4, top]


def brief(raw: dict, src: ResearchSource) -> str:
    """The parts of one record worth reading, with generous clips."""
    g = lambda k, n: _flat(_get(raw, src.field_names(k)), n)  # noqa: E731
    res = _get(raw, src.field_names("result"))
    parts = [
        ("編號", g("id", 40)), ("時間", g("ts", 30)), ("標題", g("title", 200)), ("線", g("line", 80)),
        ("種類", g("kind", 60)), ("狀態", g("status", 160)), ("承接", g("parent", 300)), ("為什麼", g("why", 1500)),
        ("假設", g("hypothesis", 1800)), ("判準", g("criterion", 400)), ("輸的條件", g("lose", 600)),
        ("事前預期", g("expected", 500)),
        ("結果", _flat({k: res[k] for k in ("verdict", "verdict_note", "cv", "diagnosis") if k in res}, 1400)
         if isinstance(res, dict) else _flat(res, 1000)),
        ("決定與下一步", decision_text(raw, src, 800)),
    ]
    notes = addenda(raw)
    if notes:  # later results and re-adjudications live here; newest last, budgeted
        budget, kept = 3500, []
        for k, v in reversed(notes):
            piece = f"{k}：{v[:900]}"
            if budget - len(piece) < 0:
                break
            kept.append(piece)
            budget -= len(piece)
        parts.append(("後續補記（新的在後）", "\n".join(reversed(kept))))
    if isinstance(raw.get("source_excerpt"), str):  # reconstructed: the write-up it came from
        parts.append(("原文節錄", raw["source_excerpt"][:3000]))
    return "\n".join(f"{k}：{v}" for k, v in parts if v)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def quote_supported(quote: str, text_norm: str) -> bool:
    """True when the quote really appears in the record. A quote the model
    shortened with … must have every remaining piece present, and the pieces
    must carry real words (at least 6 non-punctuation characters overall)."""
    q = _norm(quote).strip("…")
    pieces = [x for x in q.split("…") if x]
    if not pieces or len(_PUNCT.sub("", "".join(pieces))) < 6:
        return False
    return all(len(_PUNCT.sub("", x)) >= 2 and x in text_norm for x in pieces)


# words that say an item is still open; a "settled" quote containing them is suspect, an "open" one needs them
OPEN_WORDS = re.compile(r"待[^，。；,;]{0,10}?(?:決定|確認|裁定|定案|拍板|討論)|待定|尚未|還沒|未定|未決|pending|tbd|to be decided|undecided|awaiting", re.I)


def evidence_ok(quote: str, text_norm: str) -> bool:
    """A decision's evidence: one contiguous verbatim piece (no … that could
    drop a 不), short is fine, compared with the record's decoded text."""
    q = _norm(quote).strip("「」『』\"'“”‘’")
    return bool(q) and "…" not in q and "..." not in q and len(_PUNCT.sub("", q)) >= 3 and q in text_norm


def _instant(ts: str):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def older(a: str, b: str) -> bool:
    """Is timestamp a strictly earlier than b? Compares instants, not strings."""
    ta, tb = _instant(a), _instant(b)
    return ta < tb if ta and tb else str(a) < str(b)


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




# ---- rules the model doesn't get to decide -----------------------------------------
def _strings(x):
    """Every string inside a JSON value."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


NUM = re.compile(r"\d+(?:\.\d+)?")


_NOT_EVIDENCE = re.compile(r"RE[GC]-\d{8}-[0-9a-f]{4}|\d{4}-\d\d-\d\d(?:T[\d:.]+(?:Z|[+-]\d\d:?\d\d)?)?")


def numbers_ok(text: str, sources: list[str]) -> list[str]:
    """Numbers in a sentence that appear, as whole numbers, in none of its
    cited records (record ids and timestamps don't count as evidence)."""
    blob = _NOT_EVIDENCE.sub(" ", " ".join(sources))
    have = set(re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", blob))
    text = re.sub(r"(?<![\d.])\d{1,2}/\d{1,2}(?![\d.])", " ", text or "")  # a date like 10/06 isn't a figure
    return [x for x in NUM.findall(text) if x not in have]


def confidence(cites: list[dict], recs: dict, src: ResearchSource | None = None) -> str:
    """A fixed vocabulary computed from the supporting records' evidence level,
    sample size and verdicts (never chosen by the model)."""
    none, unclear, running, low, small, mid3, mid4, top = conf_ladder(src)
    min_n = src.min_n if src else 8
    sup = [recs[c["id"]] for c in cites if c.get("role") == "supports" and c["id"] in recs]
    against = any(c.get("role") == "against" for c in cites)
    done = [r for r in sup if r.get("outcome") in ("pass", "fail")]  # finished and not void: only these carry weight
    if not sup:
        label = none
    elif not done:
        label = running
    else:
        best = max(done, key=lambda r: (int(r.get("level") or 0), int(r.get("n") or 0)))
        lv, n = int(best.get("level") or 0), int(best.get("n") or 0)
        if lv >= 5:
            label = top
        elif lv >= 3 and n >= min_n:
            label = mid4 if lv == 4 else mid3
        elif lv >= 3:
            label = small
        elif lv == 2:
            label = low
        else:
            label = unclear
    return label + ("；有反證" if against else "")


def checks(text: str, cites: list[dict], recs: dict, raw_text: dict, forbidden: list[str], word: str = "登記") -> list[str]:
    warn = []
    srcs = [raw_text.get(c["id"], "") for c in cites]
    miss = numbers_ok(text, srcs)
    if miss:
        warn.append(f"數字 {', '.join(miss)} 不在出處原文裡（若是推算值請核對）")
    for w in forbidden:
        if w and w in text:
            warn.append(f"用了不宣稱的詞「{w}」")
    sup = [recs[c["id"]] for c in cites if c.get("role") == "supports" and c["id"] in recs]
    # "沒有比較好" is the opposite of a claim; only flag the words when not negated just before
    claim = next((m.group(0) for m in re.finditer(r"成立|證明|較好|更好", text or "")
                  if not re.search(r"[沒不未無非]", (text or "")[max(0, m.start() - 8):m.start()])), "")
    if sup and all(r.get("outcome") != "pass" for r in sup) and claim:
        warn.append(f"支持的{word}沒有一筆通過，卻用了「{claim}」")
    if CODE.search(text or ""):
        warn.append("含內部代號")
    return warn


def strip_codes(text: str) -> str:
    return re.sub(r"\s{2,}", " ", CODE.sub("", text or "")).strip(" ：:，,")


def confirmed_block(register: list[dict]) -> str:
    """What a person already confirmed or dismissed, for the report prompt."""
    rows = {("decision", "confirmed"): "已確認的待決定", ("settled", "confirmed"): "已確認的已定事項",
            ("decision", "dismissed"): "判定不對、不要再列", ("settled", "dismissed"): "判定不對、不要再列"}
    out: dict[str, list[str]] = {}
    for r in register:
        line = f"- {r['text'] or r['from_text']}" + (f"：{r['detail']}" if r["detail"] and r["status"] == "confirmed" else "")
        out.setdefault(rows[(r["kind"], r["status"])], []).append(line[:400])
    return "\n".join(f"{k}：\n" + "\n".join(v[:30]) for k, v in out.items()) or "（還沒有）"


def decided_block(overlay: dict, recs: dict, ids: list[str], dtext: dict[str, str], per_q: int = 4, budget: int = 20000) -> str:
    """The records' own words on decisions and next steps, grouped by question,
    newest first, identical texts once: what the report stage must not
    present as still open."""
    groups: dict[str, list[str]] = {}
    for i in ids:
        if dtext.get(i):
            groups.setdefault(overlay["records"].get(i, {}).get("question", ""), []).append(i)
    titles = {q["id"]: q["title"] for q in overlay.get("questions", [])}
    out, used = [], 0
    for qid in [*titles, ""]:
        seen, rows = set(), []
        for i in sorted(groups.get(qid, []), key=lambda i: recs[i]["ts"], reverse=True):
            key = _norm(dtext[i])
            if key in seen:
                continue
            seen.add(key)
            rows.append(f"  {i}｜{recs[i]['ts'][:10]}｜{dtext[i][:220]}")
            if len(rows) >= per_q:
                break
        if rows:
            block = f"[{qid or '未分類'}] {titles.get(qid, '未分類')}\n" + "\n".join(rows)
            if used + len(block) > budget:
                break
            out.append(block)
            used += len(block)
    return "\n".join(out) or "（登記裡沒有寫決定或下一步）"


# ---- the pipeline ------------------------------------------------------------------
class Writer:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(path.with_name(f".{path.name}.lock"), "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("another `wd research propose` is already writing this overlay") from None

    def write(self, data: dict, path: Path | None = None) -> None:
        path = path or self.path
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def close(self) -> None:
        self.lock.close()


def propose(src: ResearchSource, data_dir: Path, config_dir: Path, model: str = "sonnet", batch: int = 16,
            context: Path | None = None, only_questions: bool = False, log=print, structure: Path | None = None,
            synth_model: str = "", stages: str = "all", forbidden: list[str] | None = None) -> Path:
    synth = synth_model or model
    forbidden = forbidden or []
    proj = ResearchProject(src, data_dir)
    data = proj.data()
    recs = {r["id"]: dict(r) for r in data["records"]}
    raw, raw_text = {}, {}
    for p in sorted(src.path.glob("*.json")):
        try:
            if p.is_symlink() or p.stat().st_size > MAX_FILE:
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            rid = str(_get(d, src.field_names("id")) or "")
            if rid in recs:
                raw[rid] = d
                raw_text[rid] = json.dumps(d, ensure_ascii=False)
        except Exception:
            continue
    ids = [i for i in recs if i in raw]
    dtext = {i: decision_text(raw[i], src) for i in ids}  # the record's own words; never AI-written
    plain = {i: _norm(" ".join(_strings(raw[i]))) for i in ids}  # decoded values: quotes and newlines as written
    code_to_id: dict[str, str] = {}
    for r in recs.values():
        if r["code"]:
            code_to_id[r["code"]] = "" if r["code"] in code_to_id else r["id"]
    out_path = proj.overlay_path
    w = Writer(out_path)
    try:
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            old = {}
        overlay = {"goals": old.get("goals", []), "questions": old.get("questions", []), "records": old.get("records", {}),
                   "edges": [e for e in old.get("edges", []) if isinstance(e, dict)]}
        want = set(stages.split(",")) if stages != "all" else {"records", "structure", "claims", "report"}
        if only_questions:  # #17: structure only, no record batches
            want = {"structure"}

        def save():
            w.write({**overlay, "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model, "synth_model": synth})

        # 1. records ----------------------------------------------------------------
        if "records" in want:
            short_index = "\n".join(f"{i}｜{recs[i]['code'] or '-'}｜{recs[i]['ts'][:10]}｜{recs[i]['title'][:60]}" for i in ids)
            kept = dropped = 0
            overlay["edges"] = [e for e in overlay["edges"] if e.get("by") == "human"]
            for n in range(0, len(ids), batch):
                chunk = ids[n:n + batch]
                body = "\n\n---\n".join(brief(raw[i], src) for i in chunk)
                try:
                    rd = call_claude(adapt(R_PROMPT, src).format(index=short_index, n=len(chunk), records=body, levels=levels_text(src), unit=src.unit),
                                     R_SCHEMA, config_dir, model)
                except Exception as e:
                    log(f"[propose] records batch {n // batch + 1}: {e}")
                    continue
                for r in rd.get("records", []) if isinstance(rd, dict) and isinstance(rd.get("records"), list) else []:
                    try:
                        i = r.get("id") if isinstance(r, dict) else None
                        if i not in chunk:
                            continue
                        text = _norm(raw_text[i])
                        lv = int(r.get("level") or 0)
                        if lv and not quote_supported(_flat(r.get("level_quote"), 200), text):
                            lv = 0
                        entry = overlay["records"].setdefault(i, {})
                        n_ = max(0, int(r.get("n") or 0))
                        if n_ and not re.search(rf"(?<![\d.]){n_}(?![\d.])", _NOT_EVIDENCE.sub(" ", raw_text[i])):
                            n_ = 0  # a count the record doesn't state can't move confidence
                        entry.update({"name": _flat(strip_codes(r.get("name")), 40), "why": _flat(r.get("why"), 200),
                                      "result": _flat(r.get("result"), 240), "level": lv, "n": n_, "by": "ai"})
                        for e in r.get("edges", []) if isinstance(r.get("edges"), list) else []:
                            to = str(e.get("to", "")) if isinstance(e, dict) else ""
                            to = to if to in recs else code_to_id.get(to, "")
                            quote = _flat(e.get("quote"), 300) if isinstance(e, dict) else ""
                            if not to or to == i or e.get("type") not in LINK_TYPES or not older(recs[to]["ts"], recs[i]["ts"]) \
                                    or not quote_supported(quote, text):
                                dropped += 1
                                continue
                            if not any(x["from"] == i and x["to"] == to for x in overlay["edges"]):
                                overlay["edges"].append({"from": i, "to": to, "type": e["type"], "quote": quote, "by": "ai"})
                                kept += 1
                    except Exception as ex:
                        log(f"[propose] record skipped: {ex!r}")
                save()
                log(f"[propose] records {n // batch + 1}/{(len(ids) + batch - 1) // batch}: links kept {kept}, dropped {dropped}")

        def summ(i):
            o = overlay["records"].get(i, {})
            r = recs[i]
            return o, r

        # 2. structure --------------------------------------------------------------
        if "structure" in want:
            ctx = ""
            if context and context.is_file():
                ctx = "\n專案說明（節錄）：\n" + context.read_text(encoding="utf-8", errors="replace")[:10000] + "\n"
            struct = ""
            if structure and structure.is_file():
                struct = "\n既有的路線整理（同事整理，不一定對）：\n" + structure.read_text(encoding="utf-8", errors="replace")[:8000] + "\n"
            idx = "\n".join(f"{i}｜{recs[i]['ts'][:10]}｜{recs[i]['outcome']}｜{summ(i)[0].get('name') or recs[i]['title'][:40]}｜"
                            f"{summ(i)[0].get('result', '')[:80]}｜{summ(i)[0].get('level', 0)}" for i in ids)
            qd = call_claude(adapt(Q_PROMPT, src).format(context=ctx, structure=struct, index=idx), Q_SCHEMA, config_dir, synth)
            if not isinstance(qd, dict) or not isinstance(qd.get("questions"), list) or not qd["questions"]:
                raise RuntimeError("structure stage returned no questions; overlay left as it was")
            goals = [{k: _flat(g.get(k), 300) for k in ("id", "title", "bar")} | {"by": "ai"}
                     for g in qd.get("goals", []) if isinstance(g, dict) and re.fullmatch(r"g-[a-z0-9-]{2,40}", str(g.get("id", "")))]
            gids = {g["id"] for g in goals}
            qs, member_of = [], {}
            for q in qd["questions"]:
                if not isinstance(q, dict) or not re.fullmatch(r"q-[a-z0-9-]{2,40}", str(q.get("id", ""))) or any(x["id"] == q["id"] for x in qs):
                    continue
                qs.append({"id": q["id"], "parent": q.get("parent") if q.get("parent") in gids else "",
                           "kind": q.get("kind", "question"), "title": _flat(strip_codes(q.get("title")), 120), "by": "ai"})
                for m in q.get("members", []) if isinstance(q.get("members"), list) else []:
                    if isinstance(m, str) and m in recs:
                        member_of.setdefault(m, q["id"])
            overlay["goals"], overlay["questions"] = goals, qs
            for i in ids:
                overlay["records"].setdefault(i, {})["question"] = member_of.get(i, "")
            save()
            log(f"[propose] structure: {len(goals)} goals, {len(qs)} questions")
            if only_questions:
                return out_path

        # 3. claims -------------------------------------------------------------------
        enriched = {i: {**recs[i], **overlay["records"].get(i, {})} for i in ids}
        if "claims" in want:
            for g in overlay["goals"]:
                qs = [q for q in overlay["questions"] if q.get("parent") == g["id"]]
                if not qs:
                    continue
                blocks = []
                for q in qs:
                    mem = [i for i in ids if overlay["records"].get(i, {}).get("question") == q["id"]]
                    lines = [f"  {i}｜{recs[i]['ts'][:10]}｜{recs[i]['outcome']}｜{enriched[i].get('level', 0)}｜{enriched[i].get('n', 0)}｜"
                             f"{enriched[i].get('name', '')}｜{enriched[i].get('why', '')}｜{enriched[i].get('result', '')}｜{dtext[i][:200] or '-'}"
                             for i in mem]
                    blocks.append(f"[{q['id']}] {q['title']}（{len(mem)} 筆）\n" + ("\n".join(lines) or "  （沒有登記）"))
                try:
                    cd = call_claude(adapt(C_PROMPT, src).format(goal=g["title"], bar=g.get("bar", ""), blocks="\n\n".join(blocks), unit=src.unit,
                                                                   verdict_note=VERDICT_NOTE[src.reconstructed]), C_SCHEMA, config_dir, synth,
                                    timeout=1800)
                except Exception as e:
                    log(f"[propose] claims {g['id']}: {e}")
                    continue
                ga = cd.get("goal_answer") if isinstance(cd, dict) else None
                if isinstance(ga, dict):
                    g["answer"] = _flat(ga.get("text"), 200)
                    g["answer_cites"] = [c for c in ga.get("cites", []) if c in recs][:8]
                byq = {q["id"]: q for q in qs}
                for item in cd.get("questions", []) if isinstance(cd, dict) and isinstance(cd.get("questions"), list) else []:
                    q = byq.get(item.get("id")) if isinstance(item, dict) else None
                    if not q:
                        continue
                    mem = [i for i in ids if overlay["records"].get(i, {}).get("question") == q["id"]]
                    cited = lambda xs: [{"text": _flat(x.get("text"), 160), "cites": [c for c in x.get("cites", []) if c in recs][:6]}  # noqa: E731
                                        for x in xs if isinstance(x, dict) and x.get("text")][:3]
                    q["holds"], q["fails"], q["unknown"] = cited(item.get("holds", [])), cited(item.get("fails", [])), cited(item.get("unknown", []))
                    q["stuck"] = item.get("stuck") if item.get("stuck") in STUCK else "none"
                    q["next"], q["decision"] = _flat(item.get("next"), 200), _flat(item.get("decision"), 200)
                    open_n = sum(1 for i in mem if enriched[i].get("result", "").startswith("還沒有結果"))
                    q["template"] = bool(mem) and open_n * 2 > len(mem)
                    if q["template"]:  # too little has come back to conclude anything
                        q["holds"], q["fails"] = [], []
                        q["unknown"] = [{"text": f"{len(mem)} 筆中 {open_n} 筆還沒有結果，目前不能下結論", "cites": mem[:6]}]
                    findings = []
                    for k, f in enumerate(item.get("findings", []) if isinstance(item.get("findings"), list) else []):
                        if not isinstance(f, dict) or not f.get("statement"):
                            continue
                        cites = []
                        for c in f.get("cites", []) if isinstance(f.get("cites"), list) else []:
                            if isinstance(c, dict) and c.get("id") in recs and c.get("role") in ("supports", "against", "limits"):
                                raw_c = raw_text.get(c["id"], "")
                                # AI summaries count only when every number in them is in the record itself,
                                # so an invented summary can't vouch for itself
                                summ_ok = [t for t in (enriched[c["id"]].get("result", ""), enriched[c["id"]].get("why", ""))
                                           if t and not numbers_ok(t, [raw_c])]
                                src_text = _norm(" ".join([*summ_ok, raw_c])) + plain.get(c["id"], "")
                                if quote_supported(_flat(c.get("quote"), 300), src_text):
                                    cites.append({"id": c["id"], "role": c["role"], "quote": _flat(c.get("quote"), 200)})
                        st_ = _flat(f.get("statement"), 200)
                        basis = _flat(f.get("basis"), 240)
                        findings.append({
                            "id": f"f-{q['id'][2:]}-{k + 1}", "statement": st_, "basis": basis,
                            "so_what": _flat(f.get("so_what"), 200), "change_if": _flat(f.get("change_if"), 200), "cites": cites,
                            "confidence": confidence(cites, enriched, src),
                            "warnings": checks(f"{st_} {basis}", cites, enriched, raw_text, forbidden, "紀錄" if src.reconstructed else "登記") + ([] if cites else ["沒有可驗證的出處"]),
                            "by": "ai"})
                    q["findings"] = findings[:3]
                overlay["claims_fp"] = data.get("records_fp") or proj.records_fingerprint()
                save()
                log(f"[propose] claims {g['id']}: {len(qs)} questions")

        # 4. meeting report -------------------------------------------------------------
        if "report" in want:
            fl = {f["id"]: (q, f) for q in overlay["questions"] for f in q.get("findings", [])}
            if not fl:
                raise RuntimeError("no findings yet; run the claims stage first")
            goals_txt = "\n".join(f"- {g['title']}（成功標準：{g.get('bar', '')}；目前：{g.get('answer', '')}）\n" + "\n".join(
                f"    - [{q['id']}] {q['title']}｜卡在：{q.get('stuck', '')}｜下一步：{q.get('next', '')}｜待決：{q.get('decision', '')}"
                for q in overlay["questions"] if q.get("parent") == g["id"]) for g in overlay["goals"])
            f_txt = "\n".join(f"{fid}｜{f['confidence']}｜{f['statement']}｜{f['basis']}｜{f['so_what']}｜{f['change_if']}" for fid, (q, f) in fl.items())
            rules = (f"- 不得使用這些詞：{'、'.join(forbidden)}\n" if forbidden else "")
            register = read_register(register_path(out_path))
            md = call_claude(adapt(M_PROMPT, src).format(rules=rules, goals=goals_txt, findings=f_txt, decided=decided_block(overlay, recs, ids, dtext),
                                                         confirmed=confirmed_block(register)),
                             M_SCHEMA, config_dir, synth, timeout=1500)
            if not isinstance(md, dict) or not md.get("learned"):
                raise RuntimeError("report stage returned nothing usable; previous report kept")
            weakest_order = conf_ladder(src)

            def from_findings(fids):
                fids = [x for x in fids if x in fl]
                cites = sorted({c["id"] for x in fids for c in fl[x][1]["cites"] if c["id"] in recs}, key=lambda i: recs[i]["ts"])
                labels = [fl[x][1]["confidence"] for x in fids]
                base = [lb.split("；")[0] for lb in labels]
                conf = min(base, key=lambda lb: weakest_order.index(lb) if lb in weakest_order else 0) if base else weakest_order[0]
                if any("有反證" in lb for lb in labels):
                    conf += "；有反證"
                return fids, cites, conf

            report = {"title": _flat(md.get("title"), 120), "one_page": {}, "learned": [], "routes": [], "unknowns": [],
                      "decisions": [], "next_weeks": [], "glossary": []}
            op = md.get("one_page") or {}
            report["one_page"] = {k: _flat(op.get(k), 300) for k in ("goal", "success", "answer", "why_hard")}
            nums = []
            for x in op.get("numbers", []) if isinstance(op.get("numbers"), list) else []:
                if not isinstance(x, dict):
                    continue
                fids, cites, conf = from_findings(x.get("findings", []))
                nums.append({"text": _flat(x.get("text"), 160), "cites": cites[:6], "confidence": conf,
                             "warnings": numbers_ok(x.get("text"), [raw_text.get(c, "") for c in cites]) + ([] if fids else ["沒有對應的結論"])})
            report["one_page"]["numbers"] = nums[:3]
            for x in md.get("learned", [])[:5]:
                fids, cites, conf = from_findings(x.get("findings", []))
                text = f"{x.get('headline', '')} {x.get('basis', '')}"
                report["learned"].append({"headline": _flat(x.get("headline"), 160), "basis": _flat(x.get("basis"), 300),
                                          "so_what": _flat(x.get("so_what"), 200), "findings": fids, "cites": cites, "confidence": conf,
                                          "warnings": checks(text, [{"id": c, "role": "supports"} for c in cites], enriched, raw_text, forbidden, "紀錄" if src.reconstructed else "登記")
                                          + ([] if fids else ["沒有對應的結論，不應放進報告"])})
            def qmatch(v):  # the model may answer with a title, or a paraphrase of one, instead of an id
                v = str(v or "")
                if not v:
                    return ""
                for q in overlay["questions"]:
                    if v == q["id"] or v in q["title"] or q["title"] in v:
                        return q["id"]
                chars = lambda t: set(_PUNCT.sub("", t))  # noqa: E731
                best = max(overlay["questions"], key=lambda q: len(chars(v) & chars(q["title"])) / (len(chars(v) | chars(q["title"])) or 1), default=None)
                if best and len(chars(v) & chars(best["title"])) / (len(chars(v) | chars(best["title"])) or 1) >= 0.5:
                    return best["id"]
                return ""
            report["routes"] = [{"question": qmatch(x.get("question")), "label": _flat(strip_codes(x.get("question")), 80),
                                 **{k: _flat(x.get(k), 160) for k in ("plain", "status", "stuck")}}
                                for x in md.get("routes", []) if isinstance(x, dict)][:8]
            for x in md.get("unknowns", [])[:3]:
                fids, cites, _ = from_findings(x.get("findings", []))
                report["unknowns"].append({"item": _flat(x.get("item"), 200), "change_if": _flat(x.get("change_if"), 200), "cites": cites})
            word = "紀錄" if src.reconstructed else "登記"

            def evidence(x):  # the record's own words behind an open / settled item, checked here, not trusted
                ev = x.get("evidence") if isinstance(x.get("evidence"), dict) else {}
                rid, quote = str(ev.get("id") or ""), _flat(ev.get("quote"), 300)
                rid = rid if rid in recs else code_to_id.get(rid, "")
                if rid in plain and evidence_ok(quote, plain[rid]):
                    return {"id": rid, "quote": _flat(quote, 200)}
                return None

            # a person's word wins: items already confirmed or dismissed are not generated again
            for sec, key in (("decisions", "decision"), ("settled", "item")):
                if isinstance(md.get(sec), list):
                    md[sec] = [x for x in md[sec] if isinstance(x, dict) and not match_register(str(x.get(key) or ""), register)]
            for x in [x for x in md.get("decisions", []) if isinstance(x, dict)][:4]:
                fids, cites, _ = from_findings(x.get("findings", []) if isinstance(x.get("findings"), list) else [])
                ev = evidence(x)
                warn = ([] if fids else ["沒有對應的結論"]) + ([] if ev else [f"找不到{word}原文證明這件事還沒定，上會前請先核對"]) \
                    + (["引句看不出這件事還沒定，請核對"] if ev and not OPEN_WORDS.search(ev["quote"]) else [])
                report["decisions"].append({**{k: _flat(x.get(k), 240) for k in ("decision", "options", "recommendation", "who", "by_when")},
                                            "findings": fids, "cites": cites + ([ev["id"]] if ev and ev["id"] not in cites else []),
                                            **({"evidence": ev} if ev else {}), "warnings": warn})
            report["settled"] = []
            for x in [x for x in md.get("settled", []) if isinstance(x, dict)][:6]:
                fids, cites, _ = from_findings(x.get("findings", []) if isinstance(x.get("findings"), list) else [])
                ev = evidence(x)
                report["settled"].append({"item": _flat(x.get("item"), 120), "decided": _flat(x.get("decided"), 240), "findings": fids,
                                          "cites": cites + ([ev["id"]] if ev and ev["id"] not in cites else []),
                                          **({"evidence": ev} if ev else {}),
                                          "warnings": [f"找不到{word}原文證明已經定了"] if not ev
                                          else ["引句寫的是還沒定，請核對是不是真的定了"] if OPEN_WORDS.search(ev["quote"]) else []})
            report["next_weeks"] = [{k: _flat(x.get(k), 200) for k in ("task", "learn")} for x in md.get("next_weeks", []) if isinstance(x, dict)][:5]
            report["glossary"] = [{k: _flat(x.get(k), 160) for k in ("term", "plain")} for x in md.get("glossary", []) if isinstance(x, dict)][:10]
            report["cutoff"] = max((recs[i]["ts"] for i in ids), default="")[:10]
            # the findings' snapshot, not today's: a report-only rerun over old claims stays marked stale
            report["fingerprint"] = overlay.get("claims_fp") or data.get("records_fp") or proj.records_fingerprint()
            report["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            report["model"] = synth
            report["status"] = "draft"
            hits = sorted({word for word in forbidden for t in _strings(report) if word and word in t})
            report["warnings"] = [f"用了專案不宣稱的詞：{'、'.join(hits)}"] if hits else []
            rp = out_path.with_name(out_path.stem + ".report.json")
            try:
                prev = json.loads(rp.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                prev = {}
            if isinstance(prev, dict) and (prev.get("reviews") or prev.get("status") not in (None, "", "draft")):
                # someone checked or edited the current report: keep it, park the new draft beside it
                rp = rp.with_name(out_path.stem + ".report.new.json")
                log(f"[propose] the current report has human edits; new draft written to {rp.name} instead")
            w.write(report, rp)
            log(f"[propose] report: {len(report['learned'])} findings, {len(report['decisions'])} decisions")
    finally:
        w.close()
    return out_path


def main(cfg, name: str, model: str, batch: int, context: str | None, profile: str | None, only_questions: bool,
         structure: str | None = None, synth_model: str = "", stages: str = "all") -> int:
    src = next((r for r in cfg.hub.research if r.name == name), None)
    if not src:
        print(f"no [[hub.research]] named {name!r} on this hub", file=sys.stderr)
        return 2
    prof = next((p for p in cfg.profiles if (p.label == profile if profile else p.realm == cfg.hub.realm)), None)
    if not prof:
        print("no profile for this hub's realm; pass --profile", file=sys.stderr)
        return 2
    path = propose(src, cfg.hub.data_dir, prof.config_dir, model=model, batch=batch,
                   context=Path(context).expanduser() if context else None, only_questions=only_questions,
                   log=lambda m: print(m, flush=True), structure=Path(structure).expanduser() if structure else None,
                   synth_model=synth_model, stages=stages, forbidden=list(getattr(src, "forbidden", []) or []))
    print(f"overlay written: {path}")
    return 0
