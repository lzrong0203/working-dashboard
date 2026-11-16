// 研究 view: a project's own pre-registered experiments, read in place by the
// hub (wd/research.py), grouped into story lines. Four modes:
//   總覽    goals -> routes/questions as swimlanes over time (needs an overlay)
//   依主題  groups (questions from the overlay, else family codes) -> story
//   時間線  every record by date, filterable
//   被引用  what many records lean on (constants, rules): the blast radius
// Edge words are written from the newer record's side ("承接 X") and the older
// one's ("後續 Y"); links an assistant proposed are dashed and carry a quote.
import { WD, esc, lsGet, lsSet } from "../core.js";

const OUT = { follows: "承接", because: "因為", readjudicates: "重新裁定", supersedes: "取代", corrects: "更正", uses: "用到" };
const IN = { follows: "後續", because: "引出", readjudicates: "被重新裁定", supersedes: "被取代", corrects: "被更正", uses: "被用到" };
const OUTCOME_REG = {
  pass: ["✓", "通過"], fail: ["✗", "不通過"], inconclusive: ["?", "未定"], void: ["⌀", "作廢"],
  running: ["●", "進行中"], open: ["○", "已登記"], other: ["·", "其他"],
};
// records rebuilt afterwards from write-ups: no threshold was set in advance,
// so the verdict only says whether the change beat what it was compared with
const OUTCOME_REC = {
  pass: ["✓", "有改善"], fail: ["✗", "沒改善"], inconclusive: ["?", "看不出來"], void: ["⌀", "作廢"],
  running: ["●", "進行中"], open: ["○", "已記下"], other: ["·", "其他"],
};
const MODES = [["now", "現況"], ["topic", "途徑"], ["map", "歷程"], ["time", "時間線"], ["cited", "被引用"], ["report", "報告"]];
const STUCK = { method: "方法還不可信", measure: "在等量測", external: "等外部決定或資料", not_started: "還沒開始", closed: "已結案", none: "" };
const DEFAULT_LADDER = ["只有想法", "模擬對模擬", "對公開實測", "對自有或供應方實測", "目標條件下實測確認"];
// per-project vocabulary, set from the data before each render (vocab)
let OUTCOME = OUTCOME_REG, LEVEL = ["看不出來", ...DEFAULT_LADDER], W = "登記", UNIT = "個材料";
function vocab(d) {
  const lad = Array.isArray(d?.ladder) && d.ladder.length === 5 ? d.ladder.map(String) : DEFAULT_LADDER;
  LEVEL = ["看不出來", ...lad];
  OUTCOME = d?.reconstructed ? OUTCOME_REC : OUTCOME_REG;
  W = d?.reconstructed ? "紀錄" : "登記";
  UNIT = String(d?.unit || "個材料");
}
const ladderText = () => LEVEL.slice(1).map((x, i) => `${"①②③④⑤"[i]}${x}`).join(" ");
const passWords = () => (W === "紀錄" ? ["有改善的", "試過、沒改善"] : ["有通過的", "試過、沒通過"]);
const STATE = { supported: "大致成立", refuted: "大致不成立", mixed: "有成有敗", open: "還沒有答案" };
const KIND = { route: "途徑", question: "問題", cross: "方法" };
const bare = (title, kind) => (kind === "route" ? String(title).replace(/^(候選)?途徑[:：]\s*/, "") : title);

const cache = new Map(); // `${realm}/${name}` -> {fp, data, loading, error}
const ui = {
  proj: lsGet("wd.research.proj") || "",
  mode: lsGet("wd.research.mode") || "now",
  group: lsGet("wd.research.group") || "",
  focus: "",
  q: "",
  reportList: false,
};
let rerender = () => {};

const day = (ts) => String(ts || "").slice(5, 10).replace("-", "/");
const badge = (o) => { const [i, t] = OUTCOME[o] || OUTCOME.other; return `<span class="ob o-${esc(o)}"><i aria-hidden="true">${i}</i>${t}</span>`; };

function load(p) {
  const k = `${p.realm}/${p.name}`;
  const c = cache.get(k);
  if (c && c.loading) return c;
  if (c && c.fp === p.fp && !c.error) return c;
  if (c && c.error && Date.now() < c.retryAt) return c;
  const next = { fp: p.fp, data: c?.data || null, loading: true, error: null, retryAt: 0, tries: (c?.error ? c.tries : 0) + 1 };
  cache.set(k, next);
  fetch(`${p.base.replace(/\/$/, "")}/api/research?name=${encodeURIComponent(p.name)}&fp=${encodeURIComponent(p.fp)}`)
    .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then((d) => { next.data = d; next.tries = 0; })
    .catch((e) => {  // retry later (5 s, 10 s, … up to a minute), showing the last good data meanwhile
      next.error = String(e);
      const wait = Math.min(60000, 5000 * 2 ** Math.min(4, next.tries - 1));
      next.retryAt = Date.now() + wait;
      setTimeout(() => rerender(), wait + 50);
    })
    .finally(() => { next.loading = false; rerender(); });
  return next;
}

// Everything derived from one project's data, computed once per fingerprint.
const derivedCache = new WeakMap();
function derive(d) {
  if (derivedCache.has(d)) return derivedCache.get(d);
  const byId = new Map(d.records.map((r) => [r.id, r]));
  const edges = [...d.records.flatMap((r) => r.edges || []), ...(d.edges || [])];
  const out = new Map(), inn = new Map(), cited = new Map();
  for (const e of edges) {
    if (!out.has(e.from)) out.set(e.from, []);
    if (!inn.has(e.to)) inn.set(e.to, []);
    out.get(e.from).push(e); inn.get(e.to).push(e);
  }
  for (const r of d.records) for (const m of r.mentions || []) { if (!cited.has(m)) cited.set(m, []); cited.get(m).push(r.id); }
  for (const e of edges) { if (!cited.has(e.to)) cited.set(e.to, []); if (!cited.get(e.to).includes(e.from)) cited.get(e.to).push(e.from); }
  const qs = new Map((d.questions || []).map((q) => [q.id, q]));
  const groupOf = (r) => (qs.size ? (qs.has(r.question) ? r.question : "") : r.family || "");
  const groups = new Map();
  for (const r of d.records) {
    const g = groupOf(r);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(r);
  }
  const glist = [...groups.entries()].map(([id, rs]) => ({
    id, rs, last: rs[rs.length - 1]?.ts || "",
    title: id ? (qs.get(id)?.title || id) : (qs.size ? "還沒歸到問題" : "沒有代號"),
    q: qs.get(id) || null,
  })).sort((a, b) => (a.id === "") - (b.id === "") || b.last.localeCompare(a.last));
  for (const q of d.questions || []) if (!groups.has(q.id)) glist.push({ id: q.id, rs: [], last: "", title: q.title || q.id, q });
  const times = d.records.map((r) => Date.parse(r.ts)).filter((t) => !Number.isNaN(t));
  const span = times.length ? [dayStart(Math.min(...times)), dayStart(Math.max(...times)) + DAY] : [0, 1];
  const gmap = new Map(glist.map((g) => [g.id, g]));
  const goals = (d.goals || []).map((g) => ({ ...g, qs: (d.questions || []).filter((q) => q.parent === g.id).map((q) => gmap.get(q.id) || { id: q.id, rs: [], q, title: q.title, last: "" }) }));
  const placed = new Set(goals.flatMap((g) => g.qs.map((x) => x.id)));
  const loose = glist.filter((g) => !placed.has(g.id) && g.rs.length);
  const res = { byId, out, inn, cited, groups: glist, groupOf, byQuestion: qs.size > 0, span, goals, loose, repo: d.github?.repo || "" };
  derivedCache.set(d, res);
  return res;
}

function link(id, D, e, dir) {
  const r = D.byId.get(id);
  const word = (dir === "out" ? OUT : IN)[e.type] || e.type;
  const ai = e.by === "ai";
  return `<button class="rlink${ai ? " ai" : ""}" data-rid="${esc(id)}" title="${esc(ai ? `AI 推定：「${e.quote || ""}」` : "記錄裡寫明的關係")}">${esc(word)} <b>${esc(r?.code || id.slice(-4))}</b> ${badge(r?.outcome || "other")}</button>`;
}

const fmt = (x) => { const a = Math.abs(x); return a >= 100 ? x.toFixed(0) : a >= 1 ? String(+x.toPrecision(3)) : String(+x.toPrecision(2)); };

// Where the measured value landed against the line it had to cross: the pass
// side is tinted, the noise floor is a grey band around the threshold.
function gaugeSvg(g, outcome) {
  if (!g) return "";
  if (g.multi) return `<p class="gauge multi" title="${esc(g.metric)}">多條件判準：主判準的落點和最終判定不一致（要 ${g.dir === "up" ? "≥" : "≤"} ${esc(fmt(g.th))}，實測 ${esc(fmt(g.val))}），以登記原文的判定為準。</p>`;
  const n = g.noise || 0;
  let lo = Math.min(g.th - n, g.val), hi = Math.max(g.th + n, g.val);
  const pad = (hi - lo || Math.abs(g.th) * 0.2 || 1) * 0.15;
  lo -= pad; hi += pad;
  const W = 220, x = (v) => ((v - lo) / (hi - lo)) * W;
  const passX = g.dir === "up" ? [x(g.th), W] : [0, x(g.th)];
  const cmp = g.dir === "up" ? "≥" : "≤";
  return `<div class="gauge" title="${esc(g.metric)}">
    <svg viewBox="0 0 ${W} 16" preserveAspectRatio="none" aria-hidden="true">
      <rect class="pass" x="${passX[0].toFixed(1)}" y="3" width="${Math.max(0, passX[1] - passX[0]).toFixed(1)}" height="10" rx="2"/>
      ${n ? `<rect class="noise" x="${x(g.th - n).toFixed(1)}" y="3" width="${(x(g.th + n) - x(g.th - n)).toFixed(1)}" height="10"/>` : ""}
      <line class="axis" x1="0" y1="8" x2="${W}" y2="8"/>
      <line class="th" x1="${x(g.th).toFixed(1)}" y1="1" x2="${x(g.th).toFixed(1)}" y2="15"/>
      <rect class="val o-${esc(outcome)}" x="${(x(g.val) - 4).toFixed(1)}" y="4" width="8" height="8" transform="rotate(45 ${x(g.val).toFixed(1)} 8)"/>
    </svg>
    <span>主判準要 ${cmp} ${esc(fmt(g.th))}${n ? `（雜訊 ±${esc(fmt(n))}）` : ""} · 實測 <b>${esc(fmt(g.val))}</b></span>
  </div>`;
}

// Long text is clamped to three lines with a 更多 toggle. If the hub already
// clipped it (ends in …), 更多 also fetches the full field for that record.
function more(text, field, cls = "") {
  return `<dd class="clamp ${cls}"${field ? ` data-field="${esc(field)}"` : ""}><span class="txt">${esc(text)}</span><button class="more" type="button" hidden>更多</button></dd>`;
}

// Pull requests linked by the files they changed (see research_github.py): the
// record's own file, a PR the record names, or (reconstructed) a weak name+date match.
const PR_ROLE = { registered: "登記", updated: "補記", open: "待合併", cited: "內文寫到", mentions: "可能相關" };
const prUrl = (repo, n) => `https://github.com/${repo.split("/").map(encodeURIComponent).join("/")}/pull/${encodeURIComponent(n)}`;
function prChip(x, D) {
  const label = `${PR_ROLE[x.role] || x.role} #${x.n}${x.bulk ? "（整批）" : ""}`;
  const tip = [x.title, x.merged ? `合併 ${x.merged}` : x.state === "OPEN" ? "還沒合併" : ""].filter(Boolean).join("・");
  return `<a class="prc pr-${esc(x.role)}${x.bulk ? " bulk" : ""}" href="${esc(prUrl(D.repo, x.n))}" target="_blank" rel="noopener" title="${esc(tip)}">${esc(label)}</a>`;
}
function prRow(r, D) {
  const xs = r.prs || [];
  if (!xs.length || !D.repo) return "";
  const head = xs.slice(0, 3), rest = xs.slice(3);
  return `<p class="rprs"><span class="muted">GitHub</span>${head.map((x) => prChip(x, D)).join("")}${rest.length ? `<details class="prmore"><summary>另 ${rest.length} 個</summary>${rest.map((x) => prChip(x, D)).join("")}</details>` : ""}</p>`;
}
const strongPrs = (r) => (r.prs || []).filter((x) => x.role !== "mentions").slice(0, 3);
const fmtTs = (ts) => (ts ? `${String(ts).slice(5, 10).replace("-", "/")} ${String(ts).slice(11, 16)}` : "");

function rcard(r, D) {
  const outs = (D.out.get(r.id) || []).filter((e) => e.type !== "mentions");
  const ins = (D.inn.get(r.id) || []).filter((e) => e.type !== "mentions");
  const why = r.why_short || r.why;
  const hyp = !why && r.hypothesis && !r.hypothesis.startsWith(r.title.replace(/…$/, "")) ? r.hypothesis : "";
  const result = r.result_short || [r.verdict, r.result].filter(Boolean).join("：") || (r.latest_note ? `（後續補記）${r.latest_note}` : "");
  const nCited = (D.cited.get(r.id) || []).length;
  return `
  <article class="rcard o-${esc(r.outcome)}${ui.focus === r.id ? " focus" : ""}" id="r-${esc(r.id)}" data-id="${esc(r.id)}">
    <header>
      <time>${esc(day(r.ts))}</time>
      ${r.code ? `<span class="code">${esc(r.code)}</span>` : ""}
      ${badge(r.outcome)}
      ${r.kind ? `<span class="chip">${esc(r.kind)}</span>` : ""}
      <span class="rid" title="${esc(r.status)}">${esc(r.id)}</span>
    </header>
    <h3 class="rtitle">${esc(r.name || r.title)}</h3>${r.name ? `<p class="rorig">${esc(r.title)}</p>` : ""}
    <dl>
      ${why ? `<dt>為什麼</dt>${more(why, r.why_short ? "" : "why")}` : ""}
      ${r.source && r.hypothesis ? `<dt>改了什麼</dt>${more(r.hypothesis, "hypothesis")}` : hyp ? `<dt>假設</dt>${more(hyp, "hypothesis")}` : ""}
      ${r.source && r.criterion ? `<dt>怎麼比</dt><dd>${esc(r.criterion)}</dd>` : ""}
      ${r.lose ? `<dt>輸的條件</dt>${more(r.lose, "lose", "muted")}` : ""}
      ${result ? `<dt>結果</dt>${more(result, r.result_short ? "" : "result")}` : ""}
    </dl>
    ${r.source ? `<p class="rsrc">出處：${esc(r.source)}${(r.checks || []).length ? `<span class="fwarn">整理時：${r.checks.map(esc).join("；")}</span>` : ""}</p>` : ""}
    ${prRow(r, D)}
    ${gaugeSvg(r.gauge, r.outcome)}
    ${outs.length || ins.length ? `<div class="rlinks">${outs.map((e) => link(e.to, D, e, "out")).join("")}${ins.map((e) => link(e.from, D, e, "in")).join("")}</div>` : ""}
    ${nCited ? `<p class="cited">被 ${nCited} 筆提到</p>` : ""}
  </article>`;
}

function dots(rs) {
  return `<span class="odots" aria-hidden="true">${rs.map((r) => `<i class="o-${esc(r.outcome)}"></i>`).join("")}</span>`;
}

function counts(rs) {
  const c = {};
  for (const r of rs) c[r.outcome] = (c[r.outcome] || 0) + 1;
  return ["pass", "fail", "inconclusive", "void", "running", "open"].filter((o) => c[o]).map((o) => `<span class="cnt o-${o}">${OUTCOME[o][1]} <b>${c[o]}</b></span>`).join("");
}

// ---- one direction, all its evidence together ---------------------------------
// The records use different metrics, so nothing is averaged. Three views that
// synthesis-without-meta-analysis uses instead: a harvest plot (direction and
// strength of every record, numeric or not), a forest of margins (how far each
// result landed from its own threshold, in % of that threshold, with its own
// noise band), and the lineage inside the direction.
const STRONG_TYPES = new Set(["follows", "because", "readjudicates", "supersedes", "corrects"]);
function lineage(rs, D) {
  const ids = new Set(rs.map((r) => r.id));
  const edges = [];
  for (const r of rs) for (const e of D.out.get(r.id) || []) if (STRONG_TYPES.has(e.type) && ids.has(e.to)) edges.push(e);
  if (!edges.length) return "";
  const inGraph = new Set(edges.flatMap((e) => [e.from, e.to]));
  const nodes = rs.filter((r) => inGraph.has(r.id)); // already in time order
  const parents = new Map();
  for (const e of edges) { if (!parents.has(e.from)) parents.set(e.from, []); parents.get(e.from).push(e.to); }
  const lane = new Map(), laneEnd = [];
  nodes.forEach((n, i) => {
    const p = (parents.get(n.id) || []).find((pid) => lane.has(pid) && laneEnd[lane.get(pid)] === pid);
    let l = p != null ? lane.get(p) : laneEnd.findIndex((end) => end == null);
    if (l < 0) l = laneEnd.length;
    lane.set(n.id, l); laneEnd[l] = n.id;
    n._i = i;
  });
  const DX = 66, DY = 34, PAD = 18;
  const pos = (id) => { const n = nodes.find((x) => x.id === id); return [PAD + n._i * DX, PAD + lane.get(id) * DY]; };
  const W = PAD * 2 + (nodes.length - 1) * DX, H = PAD * 2 + Math.max(0, ...lane.values()) * DY + 14;
  const paths = edges.map((e) => {
    const [x1, y1] = pos(e.to), [x2, y2] = pos(e.from), mx = (x1 + x2) / 2;
    return `<path class="le t-${esc(e.type)}${e.by === "ai" ? " ai" : ""}" d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}"><title>${esc(`${OUT[e.type] || e.type}${e.quote ? `：「${e.quote}」` : ""}`)}</title></path>`;
  }).join("");
  const dotsSvg = nodes.map((n) => {
    const [cx, cy] = pos(n.id);
    return `<g class="ln-node" data-rid="${esc(n.id)}"><circle class="o-${esc(n.outcome)}" cx="${cx}" cy="${cy}" r="6"><title>${esc(`${day(n.ts)} ${n.code || ""}：${n.title}`)}</title></circle><text x="${cx}" y="${cy + 17}" text-anchor="middle">${esc((n.code || n.id.slice(-4)).slice(0, 10))}</text></g>`;
  }).join("");
  return `
  <figure class="syn lineage">
    <figcaption><b>這個方向怎麼走過來</b><span>左舊右新；線是「承接、因為、重新裁定、取代、更正」，虛線是 AI 從內文找到、附原文的關係（滑過去看引句）。只畫有關係的 ${nodes.length} 筆。</span></figcaption>
    <div class="lscroll"><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${paths}${dotsSvg}</svg></div>
  </figure>`;
}
// Harvest plot and %-of-threshold forest were removed after review (Codex and
// a second model): PASS/FAIL columns read as vote counts (a PASS can mean
// "this method is unreliable"), and margins of different metrics are not
// comparable even when divided by their thresholds. Kept: the lineage.
function synthesis(rs, D) {
  if (rs.length < 2) return "";
  const lin = lineage(rs, D);
  return lin ? `<div class="synthesis one">${lin}</div>` : "";
}

// ---- claims: what holds / fails / is unknown, and findings with their sources ----
const nameOf = (r) => r?.name || r?.title || r?.id || "";
function citeChips(ids, D) {
  return (ids || []).filter((i) => D.byId.has(i)).map((i) => {
    const r = D.byId.get(i);
    return `<button class="cite" data-rid="${esc(i)}" title="${esc(`${day(r.ts)} ${nameOf(r)}｜${(OUTCOME[r.outcome] || OUTCOME.other)[1]}`)}">${esc(day(r.ts))}</button>`;
  }).join("");
}
function threeLines(q, D) {
  const row = (label, cls, xs) => xs?.length ? `<div class="tl ${cls}"><b>${label}</b><ul>${xs.map((x) => `<li>${esc(x.text)} ${citeChips(x.cites, D)}</li>`).join("")}</ul></div>` : "";
  const body = row("成立的", "t-hold", q.holds) + row("不成立的", "t-fail", q.fails) + row("還不知道的", "t-unk", q.unknown);
  if (!body) return "";
  return `<div class="three">${body}
    <p class="tnext">${q.stuck && STUCK[q.stuck] ? `<span class="stuck">卡在：${esc(STUCK[q.stuck])}</span>` : ""}${q.next ? `<span><b>下一步</b>${esc(q.next)}</span>` : ""}${q.decision ? `<span class="dec"><b>待決定</b>${esc(q.decision)}</span>` : ""}</p>
    <p class="aidraft">AI 整理的草稿：每句都連到${W}，請點開核對後再對外使用。</p></div>`;
}
const ROLE = { supports: "支持", against: "反對", limits: "限制" };
function findingCard(f, D) {
  return `<article class="finding">
    <h4>${esc(f.statement)}</h4>
    <p class="fconf"><span class="conf">${esc(f.confidence || "")}</span>${f.basis ? `<span class="fbasis">憑什麼：${esc(f.basis)}</span>` : ""}</p>
    ${f.so_what ? `<p><b>所以</b>${esc(f.so_what)}</p>` : ""}
    ${f.change_if ? `<p class="muted"><b>什麼會推翻它</b>${esc(f.change_if)}</p>` : ""}
    <div class="fcites">${(f.cites || []).map((c) => { const r = D.byId.get(c.id); return r ? `<button class="fcite r-${esc(c.role)}" data-rid="${esc(c.id)}" title="${esc(`「${c.quote}」`)}"><i>${esc(ROLE[c.role] || c.role)}</i>${esc(day(r.ts))} ${esc(nameOf(r))}</button>` : ""; }).join("")}</div>
    ${(f.warnings || []).length ? `<p class="fwarn">⚠ ${f.warnings.map(esc).join("；")}</p>` : ""}
  </article>`;
}
// Evidence ladder: has this route reached each level, and did it pass there?
function ladder(rs) {
  const cells = [1, 2, 3, 4, 5].map((lv) => {
    const at = rs.filter((r) => (r.level || 0) === lv);
    const st = at.some((r) => r.outcome === "pass") ? "pass" : at.length ? "tried" : "none";
    return `<i class="lad l-${st}" title="${esc(`${LEVEL[lv]}：${st === "pass" ? passWords()[0] : st === "tried" ? passWords()[1] : "沒試過"}（${at.length} 筆）`)}"></i>`;
  }).join("");
  return `<span class="ladder" aria-label="證據階梯">${cells}</span>`;
}
function evidenceTable(rs, D) {
  if (!rs.length) return "";
  return `<table class="etable"><thead><tr><th>日期</th><th>在問什麼</th><th>跟什麼比</th><th>n</th><th>${W === "紀錄" ? "判定" : "登記判定"}</th></tr></thead><tbody>${rs.slice().reverse().map((r) => `
    <tr data-rid="${esc(r.id)}" tabindex="0"><td class="d">${esc(day(r.ts))}</td><td>${esc(nameOf(r))}${r.result_short ? `<div class="v">${esc(r.result_short)}</div>` : ""}</td>
    <td class="lv">${esc(LEVEL[r.level || 0])}</td><td class="n">${r.n || "—"}</td><td>${badge(r.outcome)}</td></tr>`).join("")}</tbody></table>`;
}
function lastResult(rs) {
  const done = rs.filter((r) => ["pass", "fail", "inconclusive"].includes(r.outcome));
  return done.length ? done[done.length - 1].ts : "";
}
function statusBoard(d, D) {
  if (!D.goals.length) return `<p class="hint">還沒有 AI 整理的目標與途徑。</p>` + topic(d, D);
  const decisions = [];
  const blocks = D.goals.map((g) => {
    const rows = g.qs.map((x) => {
      const q = x.q || {};
      if (q.decision) decisions.push({ q: x.title, text: q.decision });
      const lr = lastResult(x.rs), stale = lr && Date.now() - Date.parse(lr) > 14 * 864e5;
      const ans = q.template ? q.unknown?.[0]?.text : (q.holds?.[0]?.text || q.fails?.[0]?.text || q.unknown?.[0]?.text || q.answer || "");
      return `<tr data-group="${esc(x.id)}" tabindex="0">
        <td class="rt">${q.kind ? `<i class="kind k-${esc(q.kind)}">${esc(KIND[q.kind] || q.kind)}</i>` : ""}${esc(bare(x.title, q.kind))}</td>
        <td class="ra">${esc(ans)}</td><td>${ladder(x.rs)}</td>
        <td class="rs">${esc(STUCK[q.stuck] || "")}</td>
        <td class="rd${stale ? " stale" : ""}">${esc(day(lr) || "—")}</td>
        <td class="rn">${esc(q.next || "")}</td></tr>`;
    }).join("");
    return `<section class="goal${g.id === "g-cross" ? " cross" : ""}"><header><h2>${esc(g.title)}</h2>
      ${g.bar ? `<p class="bar"><b>怎樣算成功</b>${esc(g.bar)}</p>` : ""}${g.answer ? `<p class="bar"><b>現在的答案</b>${esc(g.answer)} ${citeChips(g.answer_cites, D)}</p>` : ""}</header>
      <table class="sboard"><thead><tr><th>途徑／問題</th><th>現在的答案</th><th title="${esc(ladderText())}">證據階梯</th><th>卡在哪</th><th>最近結果</th><th>下一步</th></tr></thead><tbody>${rows}</tbody></table></section>`;
  }).join("");
  const cutoff = d.records.length ? d.records[d.records.length - 1].ts.slice(0, 10) : "";
  return `<div class="nowgrid"><div>${blocks}</div><aside class="side">
    <section><h3>待決定</h3>${decisions.length ? `<ul>${decisions.map((x) => `<li><b>${esc(bare(x.q, "route"))}</b>${esc(x.text)}</li>`).join("")}</ul>` : `<p class="muted">目前沒有列出待決事項。</p>`}</section>
    <section><h3>怎麼讀</h3><p class="muted">證據階梯五格：${esc(ladderText())}。實心＝${passWords()[0]}，空心框＝${passWords()[1]}，淡＝沒試。「最近結果」超過 14 天變灰。資料到 ${esc(cutoff)}。</p></section>
  </aside></div>`;
}

function topic(d, D) {
  if (!D.groups.some((g) => g.id === ui.group)) ui.group = D.groups[0]?.id ?? "";
  const g = D.groups.find((x) => x.id === ui.group) || D.groups[0];
  const item = (x) => `
    <button class="rgroup${x === g ? " on" : ""}" data-group="${esc(x.id)}">
      <span class="gname" title="${esc(x.title)}">${esc(x.title)}</span><span class="gn">${x.rs.length}</span>
      ${dots(x.rs)}<span class="glast">${esc(day(x.last))}</span>
    </button>`;
  const list = D.goals.length
    ? D.goals.map((gl) => `<p class="gsub">${esc(gl.title)}</p>${gl.qs.map(item).join("")}`).join("") +
      (D.loose.length ? `<p class="gsub">還沒歸類</p>${D.loose.map(item).join("")}` : "")
    : D.groups.map(item).join("");
  const q = g?.q;
  return `
  <div class="rsplit">
    <nav class="rgroups" aria-label="主題">${list}</nav>
    <section class="rstory">
      <header class="rhead">
        <h2>${esc(g?.title || "")}</h2>
        ${q?.answer && !(q.holds?.length || q.fails?.length || q.unknown?.length) ? `<p class="answer"><b>目前答案（AI 整理，待確認）</b>${esc(q.answer)}${q.state ? ` <span class="st s-${esc(q.state)}">${esc(STATE[q.state] || q.state)}</span>` : ""}</p>` : ""}
      </header>
      ${q ? threeLines(q, D) : ""}
      ${(q?.findings || []).length ? `<h3 class="sub">學到的事</h3><div class="findings">${q.findings.map((f) => findingCard(f, D)).join("")}</div>` : ""}
      <h3 class="sub">證據（${(g?.rs || []).length} 筆）<span class="rcounts">${counts(g?.rs || [])}</span></h3>
      ${evidenceTable(g?.rs || [], D)}
      ${synthesis(g?.rs || [], D)}
      <details class="cards"${ui.focus ? " open" : ""}><summary>逐筆詳細（${W === "紀錄" ? "改了什麼、怎麼比、出處" : "假設、輸的條件、結果原文"}）</summary><div class="timeline">${(g?.rs || []).map((r) => rcard(r, D)).join("")}</div></details>
    </section>
  </div>`;
}

// Records of one day stack upwards, so a burst of ten runs reads as ten.
const DAY = 86400e3;
const dayStart = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
function lane(g, D) {
  const [t0, t1] = D.span, w = t1 - t0 || 1;
  const q = g.q || {};
  const perDay = new Map();
  const dotsHtml = g.rs.map((r) => {
    const t = Date.parse(r.ts);
    if (Number.isNaN(t)) return "";
    const k = dayStart(t), n = perDay.get(k) || 0;
    perDay.set(k, n + 1);
    const left = (((k + DAY / 2 - t0) / w) * 100).toFixed(2);
    return `<button class="ld o-${esc(r.outcome)}" style="left:${left}%;bottom:calc(3px + ${n} * var(--step))" data-rid="${esc(r.id)}" title="${esc(`${day(r.ts)} ${r.code || ""} ${(OUTCOME[r.outcome] || OUTCOME.other)[1]}：${r.title}`)}"></button>`;
  }).join("");
  // a very busy day compresses its stack instead of making the lane huge
  const most = Math.max(1, ...perDay.values()), rows = Math.min(most, 8), step = most > 8 ? (8 * 9) / most : 9;
  return `
  <div class="rlane">
    <button class="lhead" data-group="${esc(g.id)}">
      <span class="lt">${q.kind ? `<i class="kind k-${esc(q.kind)}">${esc(KIND[q.kind] || q.kind)}</i>` : ""}${esc(bare(g.title, q.kind))}</span>
      ${q.state ? `<span class="st s-${esc(q.state)}">${esc(STATE[q.state] || q.state)}</span>` : ""}
      ${q.answer ? `<span class="la">${esc(q.answer)}</span>` : ""}
    </button>
    <div class="track" style="height:${8 + rows * 9}px;--step:${step.toFixed(2)}px">${dotsHtml}</div>
    <span class="ln">${g.rs.length}</span>
  </div>`;
}

function axis(D) {
  const [t0, t1] = D.span, w = t1 - t0 || 1;
  const weekly = t1 - t0 < 120 * DAY;
  const ticks = [];
  const d = new Date(dayStart(t0));
  if (weekly) d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); else d.setDate(1);
  for (let i = 0; i < 60 && d.getTime() <= t1; i++) {
    if (d.getTime() >= t0 - DAY) ticks.push(`<span style="left:${Math.max(0, ((d.getTime() - t0) / w) * 100).toFixed(2)}%">${weekly ? `${d.getMonth() + 1}/${d.getDate()}` : `${d.getMonth() + 1} 月`}</span>`);
    if (weekly) d.setDate(d.getDate() + 7); else d.setMonth(d.getMonth() + 1);
  }
  return `<div class="rlane laxis"><span></span><div class="track">${ticks.join("")}</div><span></span></div>`;
}

function overview(d, D) {
  if (!D.goals.length) return `<p class="hint">還沒有 AI 整理的目標與途徑（overlay）。先看「依主題」。</p>` + topic(d, D);
  const blocks = D.goals.map((g) => `
    <section class="goal${g.id === "g-cross" ? " cross" : ""}">
      <header>
        <h2>${esc(g.title)}</h2>
        ${g.state ? `<span class="st s-${esc(g.state)}">${esc(STATE[g.state] || g.state)}</span>` : ""}
        ${g.bar ? `<p class="bar"><b>怎樣算成功</b>${esc(g.bar)}</p>` : ""}
        ${g.answer ? `<p class="bar"><b>目前</b>${esc(g.answer)}</p>` : ""}
      </header>
      <div class="lanes">${axis(D)}${g.qs.map((x) => lane(x, D)).join("")}</div>
    </section>`).join("");
  const loose = D.loose.length ? `<section class="goal"><header><h2>還沒歸類</h2></header><div class="lanes">${axis(D)}${D.loose.map((x) => lane(x, D)).join("")}</div></section>` : "";
  return `<p class="muted legend-o">${Object.entries(OUTCOME).filter(([k]) => k !== "other").map(([k, [i, t]]) => `<span class="ob o-${k}"><i>${i}</i>${t}</span>`).join(" ")}　每個點是一筆${W}，橫軸是時間；點一下看那筆，點標題看整條故事線。</p>${blocks}${loose}`;
}

// A Markdown report built from the same data: goal -> direction -> answer and
// the evidence behind it, every line traceable to a registration id.
function reportMd(d, D, label) {
  const today = new Date().toISOString().slice(0, 10);
  const cnt = (rs) => ["pass", "fail", "inconclusive", "running", "open", "void"].map((o) => [o, rs.filter((r) => r.outcome === o).length]).filter(([, n]) => n).map(([o, n]) => `${OUTCOME[o][1]} ${n}`).join("、");
  const line = (r) => `  - ${r.ts.slice(0, 10)} ${r.code ? `**${r.code}** ` : ""}${r.title}（${OUTCOME[r.outcome]?.[1] || r.outcome}，${r.id}）\n    - 為什麼：${r.why_short || r.why || r.hypothesis}\n    - 結果：${r.result_short || [r.verdict, r.result].filter(Boolean).join("：") || "—"}`;
  const out = [`# ${label}：研究進度`, "", `${today} 產生。共 ${d.records.length} 筆${W}（${cnt(d.records)}）。每一句都附${W}編號，可回查原始記錄。`, ""];
  const block = (title, rs, q) => {
    out.push(`### ${title}`, "");
    if (q?.answer) out.push(`- **目前答案**（${STATE[q.state] || q.state || "—"}）：${q.answer}`);
    out.push(`- ${W}：${rs.length} 筆（${cnt(rs) || "—"}）`);
    const recent = rs.slice(-5);
    if (recent.length) { out.push(`- 最近 ${recent.length} 筆：`); recent.forEach((r) => out.push(line(r))); }
    out.push("");
  };
  if (D.goals.length) {
    for (const g of D.goals) {
      out.push(`## ${g.title}`, "");
      if (g.bar) out.push(`**怎樣算成功**：${g.bar}`, "");
      if (g.answer) out.push(`**目前**（${STATE[g.state] || g.state || "—"}）：${g.answer}`, "");
      for (const x of g.qs) block(`${x.q?.kind ? `${KIND[x.q.kind] || x.q.kind}｜` : ""}${bare(x.title, x.q?.kind)}`, x.rs, x.q);
    }
    if (D.loose.length) { out.push("## 還沒歸類", ""); for (const x of D.loose) block(x.title, x.rs, x.q); }
  } else for (const x of D.groups) block(x.title, x.rs, x.q);
  return out.join("\n");
}

// The meeting report: written by `wd research propose` from verified findings
// only; confidence labels are computed, never chosen by the model. Footnotes
// [n] map to the appendix (date, plain name, verdict, evidence level, id).
function meeting(rep, d, D) {
  const notes = [], num = new Map();
  const fn = (ids) => (ids || []).filter((i) => D.byId.has(i)).map((i) => { if (!num.has(i)) { num.set(i, num.size + 1); notes.push(i); } return num.get(i); });
  const sup = (ids) => { const ns = fn(ids); return ns.length ? `<sup>${ns.map((n) => `<button class="fn" data-rid="${esc(notes[n - 1])}">[${n}]</button>`).join("")}</sup>` : ""; };
  const supMd = (ids) => { const ns = fn(ids); return ns.length ? ns.map((n) => `[${n}]`).join("") : ""; };
  const op = rep.one_page || {};
  const q = (id) => (d.questions || []).find((x) => x.id === id);
  // the record's own words behind a decision / settled item (verified by the generator)
  const evid = (x) => (x.evidence ? `<br><span class="muted">${W}原文：「${esc(x.evidence.quote)}」</span>` : "");
  const evidMd = (x) => (x.evidence ? `（${W}原文：「${x.evidence.quote}」）` : "");
  const warnMd = (x) => ((x.warnings || []).length ? `（⚠ ${x.warnings.join("；")}）` : "");
  const html = [], md = [];
  const stale = rep.fingerprint && d.records_fp && rep.fingerprint !== d.records_fp;
  html.push(`<header class="mhead"><h2>${esc(rep.title || "研究進度")}</h2><p class="muted">資料截止 ${esc(rep.cutoff || "")}・${esc(rep.status === "draft" ? "草稿，研究者確認前不要對外" : rep.status || "")}${stale ? "・之後有新的登記，這份還沒更新" : ""}</p></header>`);
  md.push(`# ${rep.title || "研究進度"}`, "", `資料截止 ${rep.cutoff || ""}。${rep.status === "draft" ? "草稿，研究者確認前不要對外。" : ""}`, "");
  if ((rep.warnings || []).length) html.push(`<p class="fwarn">⚠ 整份檢查：${rep.warnings.map(esc).join("；")}</p>`);
  // hand corrections made after generation (who checked it, what changed); a rerun keeps a reviewed report
  for (const r of Array.isArray(rep.reviews) ? rep.reviews : []) {
    html.push(`<p class="mreview">核對紀錄 ${esc(r.at || "")}・${esc(r.by || "")}：${esc(r.note || "")}</p>`);
    md.push(`> 核對紀錄 ${r.at || ""}・${r.by || ""}：${r.note || ""}`, "");
  }
  html.push(`<section><h3>1　一頁結論</h3><dl class="mdl"><dt>目標</dt><dd>${esc(op.goal)}</dd><dt>成功標準</dt><dd>${esc(op.success)}</dd><dt>現在的答案</dt><dd><b>${esc(op.answer)}</b></dd><dt>難在哪</dt><dd>${esc(op.why_hard)}</dd></dl>
    ${(op.numbers || []).length ? `<ul class="nums">${op.numbers.map((x) => `<li>${esc(x.text)}${sup(x.cites)}${(x.warnings || []).length ? ` <span class="fwarn">⚠ 數字 ${x.warnings.map(esc).join("、")} 在出處找不到</span>` : ""}</li>`).join("")}</ul>` : ""}</section>`);
  md.push("## 1　一頁結論", "", `- **目標**：${op.goal || ""}`, `- **成功標準**：${op.success || ""}`, `- **現在的答案**：${op.answer || ""}`, `- **難在哪**：${op.why_hard || ""}`, ...(op.numbers || []).map((x) => `- ${x.text}${supMd(x.cites)}`), "");
  html.push(`<section><h3>2　我們學到的事</h3>${(rep.learned || []).map((x, k) => `<article class="mlearn"><h4>${k + 1}．${esc(x.headline)}${sup(x.cites)}</h4>
    <p><b>憑什麼</b>${esc(x.basis)}</p><p><b>把握</b><span class="conf">${esc(x.confidence)}</span></p><p><b>所以</b>${esc(x.so_what)}</p>
    ${(x.warnings || []).length ? `<p class="fwarn">⚠ 檢查：${x.warnings.map(esc).join("；")}</p>` : ""}</article>`).join("")}</section>`);
  md.push("## 2　我們學到的事", "");
  (rep.learned || []).forEach((x, k) => md.push(`### ${k + 1}．${x.headline}${supMd(x.cites)}`, "", `- 憑什麼：${x.basis}`, `- 把握：${x.confidence}`, `- 所以：${x.so_what}`, ""));
  html.push(`<section><h3>3　路線現況</h3><table class="mtable"><thead><tr><th>路線</th><th>白話</th><th>現況</th><th>卡在哪</th><th>證據階梯</th></tr></thead><tbody>${(rep.routes || []).map((x) => {
    const qq = q(x.question), g = D.groups.find((y) => y.id === x.question);
    return `<tr data-group="${esc(x.question)}"><td>${esc(bare(qq?.title || x.label || x.question, qq?.kind))}</td><td>${esc(x.plain)}</td><td>${esc(x.status)}</td><td>${esc(x.stuck)}</td><td>${g ? ladder(g.rs) : ""}</td></tr>`;
  }).join("")}</tbody></table></section>`);
  md.push("## 3　路線現況", "", "| 路線 | 白話 | 現況 | 卡在哪 |", "|---|---|---|---|", ...(rep.routes || []).map((x) => `| ${bare(q(x.question)?.title || x.label || x.question, q(x.question)?.kind)} | ${x.plain} | ${x.status} | ${x.stuck} |`), "");
  if ((rep.unknowns || []).length) {
    html.push(`<section><h3>4　還不知道、可能翻盤的</h3><ul>${rep.unknowns.map((x) => `<li><b>${esc(x.item)}</b>${sup(x.cites)}<br><span class="muted">什麼結果會改變結論：${esc(x.change_if)}</span></li>`).join("")}</ul></section>`);
    md.push("## 4　還不知道、可能翻盤的", "", ...rep.unknowns.map((x) => `- **${x.item}**${supMd(x.cites)}：什麼結果會改變結論——${x.change_if}`), "");
  }
  if ((rep.decisions || []).length) {
    html.push(`<section><h3>5　需要決定的事</h3><table class="mtable"><thead><tr><th>決定什麼</th><th>選項與代價</th><th>建議</th><th>誰</th><th>何時前</th></tr></thead><tbody>${rep.decisions.map((x) => `<tr><td>${esc(x.decision)}${sup(x.cites)}${evid(x)}${(x.warnings || []).length ? `<br><span class="fwarn">⚠ ${x.warnings.map(esc).join("；")}</span>` : ""}</td><td>${esc(x.options)}</td><td>${esc(x.recommendation)}${x.basis ? `<br><span class="muted">依據：${esc(x.basis)}</span>` : ""}</td><td>${esc(x.who)}</td><td>${esc(x.by_when)}</td></tr>`).join("")}</tbody></table></section>`);
    md.push("## 5　需要決定的事", "", "| 決定什麼 | 選項與代價 | 建議 | 誰 | 何時前 |", "|---|---|---|---|---|", ...rep.decisions.map((x) => `| ${x.decision}${evidMd(x)}${warnMd(x)} | ${x.options} | ${x.recommendation}${x.basis ? `（依據：${x.basis}）` : ""} | ${x.who} | ${x.by_when} |`), "");
  }
  // questions that look open but were already settled (pre-registered rules, earlier re-adjudications)
  if ((rep.settled || []).length) {
    html.push(`<section><h3>已經定了、不必上會的</h3><ul>${rep.settled.map((x) => `<li><b>${esc(x.item)}</b>：${esc(x.decided)}${sup(x.cites)}${evid(x)}${x.basis ? `<br><span class="muted">依據：${esc(x.basis)}</span>` : ""}${(x.warnings || []).length ? `<br><span class="fwarn">⚠ ${x.warnings.map(esc).join("；")}</span>` : ""}</li>`).join("")}</ul></section>`);
    md.push("## 已經定了、不必上會的", "", ...rep.settled.map((x) => `- **${x.item}**：${x.decided}${evidMd(x)}${x.basis ? `（依據：${x.basis}）` : ""}${warnMd(x)}`), "");
  }
  if ((rep.next_weeks || []).length) {
    html.push(`<section><h3>6　接下來四週</h3><ul>${rep.next_weeks.map((x) => `<li><b>${esc(x.task)}</b>：做完會知道 ${esc(x.learn)}</li>`).join("")}</ul></section>`);
    md.push("## 6　接下來四週", "", ...rep.next_weeks.map((x) => `- **${x.task}**：做完會知道 ${x.learn}`), "");
  }
  const appendix = notes.map((id, k) => { const r = D.byId.get(id); return { n: k + 1, r }; });
  html.push(`<section class="appendix"><h3>附錄 A　出處</h3><table class="mtable"><thead><tr><th>註</th><th>日期</th><th>在問什麼</th><th>${W === "紀錄" ? "判定" : "登記判定"}</th><th>證據層級</th><th>${W}編號</th>${D.repo ? "<th>GitHub</th>" : ""}</tr></thead><tbody>${appendix.map(({ n, r }) => `<tr data-rid="${esc(r.id)}"><td>[${n}]</td><td>${esc(r.ts.slice(0, 10))}</td><td>${esc(nameOf(r))}</td><td>${badge(r.outcome)}</td><td>${esc(LEVEL[r.level || 0])}${r.n ? `，${r.n} ${UNIT}` : ""}</td><td class="rid">${esc(r.id)}</td>${D.repo ? `<td>${strongPrs(r).map((x) => `<a class="prc pr-${esc(x.role)}" href="${esc(prUrl(D.repo, x.n))}" target="_blank" rel="noopener" title="${esc(PR_ROLE[x.role] || "")}">#${esc(x.n)}</a>`).join(" ")}</td>` : ""}</tr>`).join("")}</tbody></table>
    ${(rep.glossary || []).length ? `<h3>附錄 B　名詞</h3><dl class="mdl">${rep.glossary.map((x) => `<dt>${esc(x.term)}</dt><dd>${esc(x.plain)}</dd>`).join("")}</dl>` : ""}</section>`);
  md.push("## 附錄 A　出處", "", `| 註 | 日期 | 在問什麼 | ${W === "紀錄" ? "判定" : "登記判定"} | 證據層級 | ${W}編號 |${D.repo ? " PR |" : ""}`, `|---|---|---|---|---|---|${D.repo ? "---|" : ""}`,
    ...appendix.map(({ n, r }) => `| [${n}] | ${r.ts.slice(0, 10)} | ${nameOf(r)} | ${(OUTCOME[r.outcome] || OUTCOME.other)[1]} | ${LEVEL[r.level || 0]}${r.n ? `，${r.n} ${UNIT}` : ""} | ${r.id} |${D.repo ? ` ${strongPrs(r).map((x) => `#${x.n}`).join(" ")} |` : ""}`), "");
  if ((rep.glossary || []).length) md.push("## 附錄 B　名詞", "", ...rep.glossary.map((x) => `- **${x.term}**：${x.plain}`), "");
  return { html: html.join(""), md: noUrls(md.join("\n")) };
}

// Exports leave the page: PR links become bare numbers and other URLs are dropped,
// so a copied report never carries private repository addresses.
function noUrls(text) {
  return text.replace(/https?:\/\/github\.com\/[^\s/]+\/[^\s/]+\/pull\/(\d+)[^\s)）」|]*/g, "PR #$1")
    .replace(/https?:\/\/[^\s)）」|]+/g, "（網址已移除）");
}

function reportView(d, D, label) {
  const rep = d.report;
  const list = reportMd(d, D, label);
  if (!rep || ui.reportList) {
    return `<div class="rreport"><p class="muted">${rep ? `證據清單：依目標與方向列出每筆${W}，給研究者核對用。` : "還沒有會議版報告。在 hub 上執行 <code>wd research propose &lt;name&gt; --stages claims,report</code> 產生。下面是證據清單。"}</p>
      <div class="rrbar">${rep ? `<button type="button" data-rlist="0">回會議版</button>` : ""}<button type="button" id="rrcopy">複製 Markdown</button><button type="button" id="rrsave">下載 .md</button></div>
      <pre id="rrmd">${esc(noUrls(list))}</pre></div>`;
  }
  const m = meeting(rep, d, D);
  return `<div class="rreport meeting"><div class="rrbar"><button type="button" id="rrcopy">複製 Markdown</button><button type="button" id="rrsave">下載 .md</button><button type="button" data-rlist="1">證據清單</button></div>
    <div class="mdoc">${m.html}</div><pre id="rrmd" hidden>${esc(m.md)}</pre></div>`;
}

function timeline(d, D) {
  const q = ui.q.trim().toLowerCase();
  const rows = d.records.filter((r) => !q || [r.id, r.code, r.title, r.why, r.hypothesis, r.verdict].join(" ").toLowerCase().includes(q));
  return `
  <div class="rfilter"><input type="search" id="rq" placeholder="搜尋代號、標題、為什麼…" value="${esc(ui.q)}"><span class="muted">${rows.length} / ${d.records.length}</span></div>
  <table class="rtable"><tbody>${rows.slice().reverse().map((r) => `
    <tr data-rid="${esc(r.id)}" tabindex="0"><td class="d">${esc(day(r.ts))}</td><td class="c">${esc(r.code)}</td><td>${badge(r.outcome)}</td>
    <td class="t">${esc(r.title)}<div class="v">${esc(r.verdict || r.result)}</div></td></tr>`).join("")}</tbody></table>`;
}

function citedView(d, D) {
  const top = [...D.cited.entries()].filter(([id]) => D.byId.has(id)).sort((a, b) => b[1].length - a[1].length).slice(0, 30);
  const max = top[0]?.[1].length || 1;
  return `
  <p class="muted">被很多筆提到的，通常是大家共用的常數、規則或方法。它改了，這些結論都要重看。</p>
  <div class="rcited">${top.map(([id, by]) => {
    const r = D.byId.get(id);
    return `<details><summary><span class="bar" style="--w:${(by.length / max * 100).toFixed(1)}%"></span><b class="n">${by.length}</b>
      ${badge(r.outcome)} <span class="code">${esc(r.code)}</span> ${esc(r.title)}</summary>
      <div class="rlinks">${by.map((x) => { const o = D.byId.get(x); return `<button class="rlink" data-rid="${esc(x)}">${esc(day(o?.ts))} <b>${esc(o?.code || x.slice(-4))}</b> ${badge(o?.outcome || "other")} ${esc((o?.title || "").slice(0, 40))}</button>`; }).join("")}</div>
    </details>`;
  }).join("")}</div>`;
}

function sessionsFor(p, ctx) {
  const ss = ctx.sessions.filter((s) => s.status !== "gone" && p.project_dir && (s.cwd || "").split("/").includes(p.project_dir));
  return ss.length ? `<p class="rsessions">相關 session：${ss.map((s) => `<span class="chip st-${esc(s.status)}">${esc(s.name || s.session_id.slice(0, 8))} · ${esc(s.status)}</span>`).join(" ")}</p>` : "";
}

function render(el, m) {
  rerender = () => WD.rerender?.();
  const projects = m.research || [];
  lastProjects = projects;
  if (!projects.length) {
  paint(el, `<p class="empty">還沒有設定研究來源。在 hub 的設定加 <code>[[hub.research]]</code>（見 docs/architecture.md）。</p>`, "none");
    return;
  }
  const keyOf = (p) => `${p.realm}/${p.name}`;
  const p = projects.find((x) => keyOf(x) === ui.proj) || projects[0];
  ui.proj = keyOf(p);
  const c = load(p);
  const d = c.data;
  vocab(d);
  const head = `
  <div class="rbar">
    ${projects.length > 1 ? `<select id="rproj">${projects.map((x) => `<option value="${esc(keyOf(x))}"${x === p ? " selected" : ""}>${esc(x.label)}</option>`).join("")}</select>` : `<h2 class="rproj">${esc(p.label)}</h2>`}
    <span class="muted">${p.count} 筆${W}${d ? ` · ${counts(d.records)}` : ""}</span>
    <div class="rmodes" role="tablist">${MODES.map(([id, label]) => `<button role="tab" data-mode="${id}" aria-selected="${ui.mode === id}">${label}</button>`).join("")}</div>
  </div>
  ${sessionsFor(p, m)}
  ${d?.reconstructed ? `<p class="hint">這些紀錄是事後從專案文件整理的，不是事先登記：沒有事先定的門檻，判定是整理時讀出的「比對照好、沒改善或看不出來」。每筆都附出處，引句對不上的欄位已被程式拿掉。</p>` : ""}
  ${d?.github ? `<p class="hint ghfresh">${d.github.error ? `<span class="fwarn">GitHub 更新失敗${d.github.error_at ? `（${esc(fmtTs(d.github.error_at))}）` : ""}：${esc(d.github.error)}</span> ` : ""}GitHub：${d.github.fetched_at ? `${esc(fmtTs(d.github.fetched_at))} 更新，${d.github.prs} 個 PR` : "還沒抓過（每小時自動更新）"}。${d.reconstructed ? "紀錄不在 repo 裡，只能用名稱和日期找「可能相關」的 PR。" : `依 PR 改到哪份${W}檔來連；內文只是提到編號的不算關係。`}</p>` : ""}
  ${d && !d.questions?.length ? `<p class="hint">目前依代號分組，關係只含記錄裡寫明的 REG 編號。等 AI 讀完內文、補上「問題」和關係之後，這裡會改成依問題分組。</p>` : ""}
  ${d?.errors?.length ? `<details class="hint"><summary>${d.errors.length} 筆讀不進來</summary>${d.errors.map((e) => `<div>${esc(e)}</div>`).join("")}</details>` : ""}`;
  if (!d) {
    paint(el, head + `<p class="empty">${c.error ? `讀取失敗：${esc(c.error)}（稍後自動重試）` : "讀取中…"}</p>`, "");
    wire(el);
    return;
  }
  const D = derive(d);
  cur = { d, D };
  const body = ui.mode === "time" ? timeline(d, D) : ui.mode === "cited" ? citedView(d, D) : ui.mode === "map" ? overview(d, D) : ui.mode === "report" ? reportView(d, D, p.label) : ui.mode === "now" ? statusBoard(d, D) : topic(d, D);
  if (paint(el, head + `<div class="research">${body}</div>`, d.fp)) {
    globalThis.requestAnimationFrame?.(() => markClamped(el));
    setTimeout(() => markClamped(el), 50);
  }
  wire(el);
}

// The board re-renders every 15 s and on every hub push. Repainting identical
// markup would reset scroll positions, open <details> and the search box, so
// only touch the DOM when the markup really changed (and keep focus if so).
function paint(el, html, key = "") {
  const root = el.querySelector(":scope > [data-rv]");
  if (root && el._rhtml === html && el._rkey === key) return false;
  const a = document.activeElement, keep = a && a.id === "rq" ? [a.selectionStart, a.selectionEnd] : null;
  const listScroll = el.querySelector(".rgroups")?.scrollTop || 0;
  const opened = [...el.querySelectorAll("dd.open")].map((dd) => `${dd.closest("[data-id]")?.dataset.id}|${dd.dataset.field || dd.previousElementSibling?.textContent}`);
  el.innerHTML = `<div data-rv="1">${html}</div>`;
  el._rhtml = html; el._rkey = key;
  if (keep) { const n = document.getElementById("rq"); if (n) { n.focus(); n.setSelectionRange(...keep); } }
  const list = el.querySelector(".rgroups");
  if (list) list.scrollTop = listScroll;
  for (const dd of el.querySelectorAll("dd.clamp")) {
    if (opened.includes(`${dd.closest("[data-id]")?.dataset.id}|${dd.dataset.field || dd.previousElementSibling?.textContent}`)) dd.classList.add("open");
  }
  return true;
}

function markClamped(el) {
  for (const dd of el.querySelectorAll("dd.clamp")) {
    const b = dd.querySelector("button.more");
    if (!b) continue;
    const t = dd.querySelector(".txt");
    const clipped = dd.classList.contains("open") || t.scrollHeight > t.clientHeight + 2 || t.textContent.endsWith("…");
    b.hidden = !clipped;
    b.textContent = dd.classList.contains("open") ? "收合" : "更多";
  }
}

let lastProjects = [];
const currentProject = () => lastProjects.find((x) => `${x.realm}/${x.name}` === ui.proj) || lastProjects[0];

// One set of delegated listeners per container, attached once. They read the
// current data (cur) at click time, so a repaint skipped because the markup
// didn't change can never leave handlers pointing at stale data.
let cur = { d: null, D: null };
function wire(el) {
  if (el._rWired || !el.addEventListener) return;
  el._rWired = true;
  const mine = (ev) => ev.target.closest?.("[data-rv]") && el.contains(ev.target);
  // text inside a closed <details> has no size yet; measure again once it opens
  el.addEventListener("toggle", (ev) => { if (ev.target.open) markClamped(ev.target); }, true);
  el.addEventListener("change", (ev) => {
    if (!mine(ev) || ev.target.id !== "rproj") return;
    ui.proj = ev.target.value; lsSet("wd.research.proj", ui.proj); rerender();
  });
  let qt;
  el.addEventListener("input", (ev) => {
    if (!mine(ev) || ev.target.id !== "rq") return;
    ui.q = ev.target.value; clearTimeout(qt); qt = setTimeout(rerender, 250);
  });
  el.addEventListener("click", async (ev) => {
    if (!mine(ev)) return;
    const t = ev.target;
    if (t.closest("a.prc")) return;  // a GitHub link opens in its own tab; don't also jump to the record
    const rl = t.closest("[data-rlist]");
    if (rl) { ui.reportList = rl.dataset.rlist === "1"; rerender(); return; }
    const mode = t.closest("[data-mode]");
    if (mode) { ui.mode = mode.dataset.mode; lsSet("wd.research.mode", ui.mode); rerender(); return; }
    if (t.closest("#rrcopy")) {
      navigator.clipboard?.writeText(el.querySelector("#rrmd").textContent).then(() => { t.textContent = "已複製"; }, () => { t.textContent = "複製失敗，請手動選取"; });
      return;
    }
    if (t.closest("#rrsave")) {
      const blob = new Blob([el.querySelector("#rrmd").textContent], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `research-${new Date().toISOString().slice(0, 10)}.md`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      return;
    }
    const more = t.closest("button.more");
    if (more) {
      ev.stopPropagation();
      const dd = more.closest("dd"), txt = dd.querySelector(".txt");
      if (dd.classList.toggle("open")) {
        more.textContent = "收合";
        const field = dd.dataset.field, id = dd.closest("[data-id]")?.dataset.id, p = currentProject();
        if (field && txt.textContent.endsWith("…") && id && p) {
          try {
            const r = await fetch(`${p.base.replace(/\/$/, "")}/api/research?name=${encodeURIComponent(p.name)}&id=${encodeURIComponent(id)}`);
            const full = r.ok ? (await r.json())[field] : "";
            if (full) txt.textContent = full;
          } catch { /* keep the clipped text */ }
        }
      } else more.textContent = "更多";
      return;
    }
    const grp = t.closest("[data-group]");
    if (grp) {
      const fromMap = ui.mode !== "topic";
      ui.group = grp.dataset.group; ui.focus = ""; ui.mode = "topic";
      lsSet("wd.research.group", ui.group); lsSet("wd.research.mode", ui.mode); rerender();
      setTimeout(() => {  // show the start of the story only if it is out of view
        const story = document.querySelector(".rstory");
        if (fromMap) window.scrollTo({ top: 0 });
        else if (story && story.getBoundingClientRect().top < 0) story.scrollIntoView({ block: "start" });
      }, 40);
      return;
    }
    const ref = t.closest("[data-rid]");
    if (ref && !t.closest("summary") && cur.D) {  // any record reference jumps to its story
      const id = ref.dataset.rid, r = cur.D.byId.get(id);
      if (!r) return;
      ui.mode = "topic"; ui.group = cur.D.groupOf(r); ui.focus = id;
      lsSet("wd.research.mode", ui.mode); lsSet("wd.research.group", ui.group);
      rerender();
      setTimeout(() => document.getElementById(`r-${id}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 60);
    }
  });
}

WD.register({ id: "research", label: "研究", render, visible: (m) => (m.research || []).length > 0 });
