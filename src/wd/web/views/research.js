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
const OUTCOME = {
  pass: ["✓", "通過"], fail: ["✗", "不通過"], inconclusive: ["?", "未定"], void: ["⌀", "作廢"],
  running: ["●", "進行中"], open: ["○", "已登記"], other: ["·", "其他"],
};
const MODES = [["map", "總覽"], ["topic", "依主題"], ["time", "時間線"], ["cited", "被引用"]];
const STATE = { supported: "大致成立", refuted: "大致不成立", mixed: "有成有敗", open: "還沒有答案" };
const KIND = { route: "途徑", question: "問題", cross: "方法" };

const cache = new Map(); // `${realm}/${name}` -> {fp, data, loading, error}
const ui = {
  proj: lsGet("wd.research.proj") || "",
  mode: lsGet("wd.research.mode") || "map",
  group: lsGet("wd.research.group") || "",
  focus: "",
  q: "",
};
let rerender = () => {};

const day = (ts) => String(ts || "").slice(5, 10).replace("-", "/");
const badge = (o) => { const [i, t] = OUTCOME[o] || OUTCOME.other; return `<span class="ob o-${esc(o)}"><i aria-hidden="true">${i}</i>${t}</span>`; };

function load(p) {
  const k = `${p.realm}/${p.name}`;
  const c = cache.get(k);
  if (c && (c.fp === p.fp || c.loading)) return c;
  const next = { fp: p.fp, data: c?.data || null, loading: true, error: null };
  cache.set(k, next);
  fetch(`${p.base.replace(/\/$/, "")}/api/research?name=${encodeURIComponent(p.name)}&fp=${encodeURIComponent(p.fp)}`)
    .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then((d) => { next.data = d; })
    .catch((e) => { next.error = String(e); })
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
  const times = d.records.map((r) => Date.parse(r.ts)).filter((t) => !Number.isNaN(t));
  const span = times.length ? [dayStart(Math.min(...times)), dayStart(Math.max(...times)) + DAY] : [0, 1];
  const gmap = new Map(glist.map((g) => [g.id, g]));
  const goals = (d.goals || []).map((g) => ({ ...g, qs: (d.questions || []).filter((q) => q.parent === g.id).map((q) => gmap.get(q.id) || { id: q.id, rs: [], q, title: q.title, last: "" }) }));
  const placed = new Set(goals.flatMap((g) => g.qs.map((x) => x.id)));
  const loose = glist.filter((g) => !placed.has(g.id) && g.rs.length);
  const res = { byId, out, inn, cited, groups: glist, groupOf, byQuestion: qs.size > 0, span, goals, loose };
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

function rcard(r, D) {
  const outs = (D.out.get(r.id) || []).filter((e) => e.type !== "mentions");
  const ins = (D.inn.get(r.id) || []).filter((e) => e.type !== "mentions");
  const why = r.why_short || r.why;
  const hyp = !why && r.hypothesis && !r.hypothesis.startsWith(r.title.replace(/…$/, "")) ? r.hypothesis : "";
  const result = r.result_short || [r.verdict, r.result].filter(Boolean).join("：");
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
    <h3 class="rtitle">${esc(r.title)}</h3>
    <dl>
      ${why ? `<dt>為什麼</dt>${more(why, r.why_short ? "" : "why")}` : ""}
      ${hyp ? `<dt>假設</dt>${more(hyp, "hypothesis")}` : ""}
      ${r.lose ? `<dt>輸的條件</dt>${more(r.lose, "lose", "muted")}` : ""}
      ${result ? `<dt>結果</dt>${more(result, r.result_short ? "" : "result")}` : ""}
    </dl>
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

function topic(d, D) {
  if (!D.groups.some((g) => g.id === ui.group)) ui.group = D.groups[0]?.id ?? "";
  const g = D.groups.find((x) => x.id === ui.group) || D.groups[0];
  const item = (x) => `
    <button class="rgroup${x === g ? " on" : ""}" data-group="${esc(x.id)}">
      <span class="gname" title="${esc(x.title)}">${esc(x.title)}</span><span class="gn">${x.rs.length}</span>
      ${dots(x.rs)}<span class="glast">${esc(day(x.last))}</span>
    </button>`;
  const list = D.goals.length
    ? D.goals.map((gl) => `<p class="gsub">${esc(gl.title)}</p>${gl.qs.filter((x) => x.rs.length).map(item).join("")}`).join("") +
      (D.loose.length ? `<p class="gsub">還沒歸類</p>${D.loose.map(item).join("")}` : "")
    : D.groups.map(item).join("");
  const q = g?.q;
  return `
  <div class="rsplit">
    <nav class="rgroups" aria-label="主題">${list}</nav>
    <section class="rstory">
      <header class="rhead">
        <h2>${esc(g?.title || "")}</h2>
        ${q?.answer ? `<p class="answer"><b>目前答案</b>${esc(q.answer)}${q.state ? ` <span class="chip">${esc(q.state)}</span>` : ""}</p>` : ""}
        <p class="rcounts">${counts(g?.rs || [])}</p>
      </header>
      <div class="timeline">${(g?.rs || []).map((r) => rcard(r, D)).join("")}</div>
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
  <div class="lane">
    <button class="lhead" data-group="${esc(g.id)}">
      <span class="lt">${q.kind ? `<i class="kind k-${esc(q.kind)}">${esc(KIND[q.kind] || q.kind)}</i>` : ""}${esc(g.title)}</span>
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
  return `<div class="lane laxis"><span></span><div class="track">${ticks.join("")}</div><span></span></div>`;
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
  return `<p class="muted legend-o">${Object.entries(OUTCOME).filter(([k]) => k !== "other").map(([k, [i, t]]) => `<span class="ob o-${k}"><i>${i}</i>${t}</span>`).join(" ")}　每個點是一筆登記，橫軸是時間；點一下看那筆，點標題看整條故事線。</p>${blocks}${loose}`;
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
    paint(el, `<p class="empty">還沒有設定研究來源。在 hub 的設定加 <code>[[hub.research]]</code>（見 docs/architecture.md）。</p>`);
    return;
  }
  const keyOf = (p) => `${p.realm}/${p.name}`;
  const p = projects.find((x) => keyOf(x) === ui.proj) || projects[0];
  ui.proj = keyOf(p);
  const c = load(p);
  const d = c.data;
  const head = `
  <div class="rbar">
    ${projects.length > 1 ? `<select id="rproj">${projects.map((x) => `<option value="${esc(keyOf(x))}"${x === p ? " selected" : ""}>${esc(x.label)}</option>`).join("")}</select>` : `<h2 class="rproj">${esc(p.label)}</h2>`}
    <span class="muted">${p.count} 筆登記${d ? ` · ${counts(d.records)}` : ""}</span>
    <div class="rmodes" role="tablist">${MODES.map(([id, label]) => `<button role="tab" data-mode="${id}" aria-selected="${ui.mode === id}">${label}</button>`).join("")}</div>
  </div>
  ${sessionsFor(p, m)}
  ${d && !d.questions?.length ? `<p class="hint">目前依代號分組，關係只含記錄裡寫明的 REG 編號。等 AI 讀完內文、補上「問題」和關係之後，這裡會改成依問題分組。</p>` : ""}
  ${d?.errors?.length ? `<details class="hint"><summary>${d.errors.length} 筆讀不進來</summary>${d.errors.map((e) => `<div>${esc(e)}</div>`).join("")}</details>` : ""}`;
  if (!d) {
    paint(el, head + `<p class="empty">${c.error ? `讀取失敗：${esc(c.error)}` : "讀取中…"}</p>`);
    return;
  }
  const D = derive(d);
  const body = ui.mode === "time" ? timeline(d, D) : ui.mode === "cited" ? citedView(d, D) : ui.mode === "map" ? overview(d, D) : topic(d, D);
  if (paint(el, head + `<div class="research">${body}</div>`)) wire(el, d, D);
}

// The board re-renders every 15 s and on every hub push. Repainting identical
// markup would reset scroll positions, open <details> and the search box, so
// only touch the DOM when the markup really changed (and keep focus if so).
function paint(el, html) {
  const root = el.querySelector(":scope > [data-rv]");
  if (root && el._rhtml === html) return false;
  const a = document.activeElement, keep = a && a.id === "rq" ? [a.selectionStart, a.selectionEnd] : null;
  const listScroll = el.querySelector(".rgroups")?.scrollTop || 0;
  const opened = [...el.querySelectorAll("dd.open")].map((dd) => `${dd.closest("[data-id]")?.dataset.id}|${dd.dataset.field || dd.previousElementSibling?.textContent}`);
  el.innerHTML = `<div data-rv="1">${html}</div>`;
  el._rhtml = html;
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

function wire(el, d, D) {
  el.querySelector("#rproj")?.addEventListener("change", (ev) => { ui.proj = ev.target.value; lsSet("wd.research.proj", ui.proj); rerender(); });
  el.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => { ui.mode = b.dataset.mode; lsSet("wd.research.mode", ui.mode); rerender(); }));
  el.querySelectorAll("[data-group]").forEach((b) => b.addEventListener("click", () => {
    const fromMap = ui.mode !== "topic";
    ui.group = b.dataset.group; ui.focus = ""; ui.mode = "topic";
    lsSet("wd.research.group", ui.group); lsSet("wd.research.mode", ui.mode); rerender();
    setTimeout(() => {  // show the start of the story only if it is out of view
      const st = document.querySelector(".rstory");
      if (fromMap) window.scrollTo({ top: 0 });
      else if (st && st.getBoundingClientRect().top < 0) st.scrollIntoView({ block: "start" });
    }, 40);
  }));
  const q = el.querySelector("#rq");
  if (q) {
    let t;
    q.addEventListener("input", () => { ui.q = q.value; clearTimeout(t); t = setTimeout(rerender, 250); });
  }
  globalThis.requestAnimationFrame?.(() => markClamped(el));
  setTimeout(() => markClamped(el), 50);
  el.querySelectorAll("button.more").forEach((b) => b.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const dd = b.closest("dd"), txt = dd.querySelector(".txt");
    if (dd.classList.toggle("open")) {
      b.textContent = "收合";
      const field = dd.dataset.field, id = dd.closest("[data-id]")?.dataset.id;
      if (field && txt.textContent.endsWith("…") && id) {
        try {
          const p = currentProject();
          const r = await fetch(`${p.base.replace(/\/$/, "")}/api/research?name=${encodeURIComponent(p.name)}&id=${encodeURIComponent(id)}`);
          const full = r.ok ? (await r.json())[field] : "";
          if (full) txt.textContent = full;
        } catch { /* keep the clipped text */ }
      }
    } else b.textContent = "更多";
  }));
  // any record reference jumps to its story and highlights it
  el.querySelectorAll("[data-rid]").forEach((b) => b.addEventListener("click", (ev) => {
    if (ev.target.closest("summary")) return;
    const id = b.dataset.rid, r = D.byId.get(id);
    if (!r) return;
    ui.mode = "topic"; ui.group = D.groupOf(r); ui.focus = id;
    lsSet("wd.research.mode", ui.mode); lsSet("wd.research.group", ui.group);
    rerender();
    setTimeout(() => document.getElementById(`r-${id}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 60);
  }));
}

WD.register({ id: "research", label: "研究", render, visible: (m) => (m.research || []).length > 0 });
