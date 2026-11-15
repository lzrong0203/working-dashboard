// 研究 view: a project's own pre-registered experiments, read in place by the
// hub (wd/research.py), grouped into story lines. Three modes:
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
const MODES = [["topic", "依主題"], ["time", "時間線"], ["cited", "被引用"]];

const cache = new Map(); // `${realm}/${name}` -> {fp, data, loading, error}
const ui = {
  proj: lsGet("wd.research.proj") || "",
  mode: lsGet("wd.research.mode") || "topic",
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
  const res = { byId, out, inn, cited, groups: glist, groupOf, byQuestion: qs.size > 0 };
  derivedCache.set(d, res);
  return res;
}

function link(id, D, e, dir) {
  const r = D.byId.get(id);
  const word = (dir === "out" ? OUT : IN)[e.type] || e.type;
  const ai = e.by === "ai";
  return `<button class="rlink${ai ? " ai" : ""}" data-rid="${esc(id)}" title="${esc(ai ? `AI 推定：「${e.quote || ""}」` : "記錄裡寫明的關係")}">${esc(word)} <b>${esc(r?.code || id.slice(-4))}</b> ${badge(r?.outcome || "other")}</button>`;
}

function rcard(r, D) {
  const outs = (D.out.get(r.id) || []).filter((e) => e.type !== "mentions");
  const ins = (D.inn.get(r.id) || []).filter((e) => e.type !== "mentions");
  const why = r.why_short || r.why;
  const hyp = !why && r.hypothesis && !r.hypothesis.startsWith(r.title.replace(/…$/, "")) ? r.hypothesis : "";
  const result = r.result_short || [r.verdict, r.result].filter(Boolean).join("：");
  const nCited = (D.cited.get(r.id) || []).length;
  return `
  <article class="rcard o-${esc(r.outcome)}${ui.focus === r.id ? " focus" : ""}" id="r-${esc(r.id)}">
    <header>
      <time>${esc(day(r.ts))}</time>
      ${r.code ? `<span class="code">${esc(r.code)}</span>` : ""}
      ${badge(r.outcome)}
      ${r.kind ? `<span class="chip">${esc(r.kind)}</span>` : ""}
      <span class="rid" title="${esc(r.status)}">${esc(r.id)}</span>
    </header>
    <h3 class="rtitle">${esc(r.title)}</h3>
    <dl>
      ${why ? `<dt>為什麼</dt><dd class="clamp">${esc(why)}</dd>` : ""}
      ${hyp ? `<dt>假設</dt><dd class="clamp">${esc(hyp)}</dd>` : ""}
      ${r.lose ? `<dt>輸的條件</dt><dd class="clamp muted">${esc(r.lose)}</dd>` : ""}
      ${result ? `<dt>結果</dt><dd class="clamp">${esc(result)}</dd>` : ""}
    </dl>
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
  const list = D.groups.map((x) => `
    <button class="rgroup${x === g ? " on" : ""}" data-group="${esc(x.id)}">
      <span class="gname">${esc(x.title)}</span><span class="gn">${x.rs.length}</span>
      ${dots(x.rs)}<span class="glast">${esc(day(x.last))}</span>
    </button>`).join("");
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
  const body = ui.mode === "time" ? timeline(d, D) : ui.mode === "cited" ? citedView(d, D) : topic(d, D);
  if (paint(el, head + `<div class="research">${body}</div>`)) wire(el, d, D);
}

// The board re-renders every 15 s and on every hub push. Repainting identical
// markup would reset scroll positions, open <details> and the search box, so
// only touch the DOM when the markup really changed (and keep focus if so).
function paint(el, html) {
  const root = el.querySelector(":scope > [data-rv]");
  if (root && el._rhtml === html) return false;
  const a = document.activeElement, keep = a && a.id === "rq" ? [a.selectionStart, a.selectionEnd] : null;
  el.innerHTML = `<div data-rv="1">${html}</div>`;
  el._rhtml = html;
  if (keep) { const n = document.getElementById("rq"); if (n) { n.focus(); n.setSelectionRange(...keep); } }
  return true;
}

function wire(el, d, D) {
  el.querySelector("#rproj")?.addEventListener("change", (ev) => { ui.proj = ev.target.value; lsSet("wd.research.proj", ui.proj); rerender(); });
  el.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => { ui.mode = b.dataset.mode; lsSet("wd.research.mode", ui.mode); rerender(); }));
  el.querySelectorAll("[data-group]").forEach((b) => b.addEventListener("click", () => { ui.group = b.dataset.group; ui.focus = ""; lsSet("wd.research.group", ui.group); rerender(); }));
  const q = el.querySelector("#rq");
  if (q) {
    let t;
    q.addEventListener("input", () => { ui.q = q.value; clearTimeout(t); t = setTimeout(rerender, 250); });
  }
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
