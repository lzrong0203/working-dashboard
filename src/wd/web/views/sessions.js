// Sessions view: what needs you first, then every machine's sessions.
import { WD, esc, ago } from "../app.js";

const STATUS_LABEL = { waiting: "等你", busy: "執行中", idle: "閒置", gone: "已結束", unknown: "?" };
const STALE_S = 180; // a source silent this long is probably offline

function base(p) { return (p || "").split("/").filter(Boolean).pop() || p || ""; }

function card(s, now, itemsById) {
  const stale = s.source_age_s > STALE_S && s.status !== "gone";
  const att = s.attention || { level: 0, reason: "" };
  const link = s.bridge_session_id ? `https://claude.ai/code/${encodeURIComponent(s.bridge_session_id)}` : "";
  const items = (s.linked_items || []).map((id) => {
    const it = itemsById.get(id);
    return `<span class="chip item" title="${esc(it?.hypothesis || "")}">${esc(it?.title || id)}</span>`;
  }).join("");
  const meta = [base(s.cwd), s.git_branch && `⎇ ${s.git_branch}`, s.tmux && `tmux ${s.tmux}`].filter(Boolean).map(esc).join(" · ");
  return `
  <article class="card st-${esc(s.status)} lvl-${att.level} ${stale ? "stale" : ""}" data-key="${esc(s.key)}">
    <header>
      <span class="dot" aria-hidden="true"></span>
      <span class="name">${esc(s.name || s.session_id.slice(0, 8))}</span>
      <span class="chip realm realm-${esc(s.realm)}">${esc(s.profile)}</span>
      <span class="when">${esc(STATUS_LABEL[s.status] || s.status)} ${esc(ago(s.status_since, now))}</span>
    </header>
    ${att.reason ? `<p class="reason">${esc(att.reason)}</p>` : ""}
    ${stale ? `<p class="reason">這台機器 ${esc(ago(now - s.source_age_s * 1000, now))}沒回報，狀態可能過時</p>` : ""}
    ${s.ai_title ? `<h3 class="title">${esc(s.ai_title)}</h3>` : ""}
    ${s.away_summary ? `<p class="summary">${esc(s.away_summary)} <span class="age">· ${esc(ago(s.away_summary_at, now))}前</span></p>` : ""}
    ${s.last_prompt ? `<p class="prompt" title="最後一句 prompt">› ${esc(s.last_prompt)}</p>` : ""}
    ${items ? `<div class="items">${items}</div>` : ""}
    <footer>
      <span class="meta">${meta}</span>
      ${link ? `<a class="open" href="${link}" target="_blank" rel="noopener">開啟 ↗</a>` : ""}
    </footer>
  </article>`;
}

WD.register({
  id: "sessions",
  label: "Sessions",
  render(el, m) {
    const now = Date.now();
    const itemsById = new Map(m.items.map((it) => [it.id, it]));
    const live = m.sessions.filter((s) => s.status !== "gone");
    const needs = live.filter((s) => s.attention.level >= 2);
    const hosts = [...new Set([...m.sources.map((s) => s.host), ...live.map((s) => s.host)])].sort();
    const gone = m.sessions.filter((s) => s.status === "gone");

    const hostBlock = (h) => {
      const ss = live.filter((s) => s.host === h && s.attention.level < 2);
      const srcs = m.sources.filter((s) => s.host === h);
      const silent = srcs.length && srcs.every((s) => s.age_s > STALE_S);
      return `
      <section class="host ${silent ? "silent" : ""}">
        <h2>${esc(h)} <small>${srcs.map((s) => esc(s.profile)).join(" · ")}${silent ? " · 失聯" : ""}</small></h2>
        <div class="grid">${ss.map((s) => card(s, now, itemsById)).join("") || `<p class="empty">沒有其他 session</p>`}</div>
      </section>`;
    };

    el.innerHTML = `
      ${m.offline.length ? `<p class="banner">連不上：${m.offline.map((s) => esc(s.url)).join("、")}（ssh -L 通道開了嗎？）</p>` : ""}
      ${needs.length ? `<section class="needs"><h2>需要你</h2><div class="grid">${needs.map((s) => card(s, now, itemsById)).join("")}</div></section>` : ""}
      ${hosts.map(hostBlock).join("")}
      ${gone.length ? `<details class="gone"><summary>最近結束的 ${gone.length} 個</summary><div class="grid">${gone.map((s) => card(s, now, itemsById)).join("")}</div></details>` : ""}
    `;
  },
});
