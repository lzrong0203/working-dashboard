// Sessions view: who needs you, then what every machine is doing.
// One data model, four looks (WD.look): desk, studio, flap, kanban.
// Every state shows its word plus a second cue (shape, pose, lamp, column);
// colour is never the only signal.
import { WD, esc, ago, dur, ikey, opener, word, FORMAL, hash, strip } from "../core.js";
import { mountOffice } from "./office.js";

const STALE_S = 180; // a source silent this long is probably offline
const base = (p) => (p || "").split("/").filter(Boolean).pop() || p || "";

function shortAccount(email) {
  if (!email) return "";
  const [user, domain = ""] = email.split("@");
  return `${user}@${domain.split(".")[0]}`;
}

function openLink(s, cls = "open") {
  if (!s.bridge_session_id) return "";
  const link = `https://claude.ai/code/${encodeURIComponent(s.bridge_session_id)}`;
  const target = opener.realms?.[s.realm];
  const label = target ? `開啟 ↗ <small>${esc(target.label)}</small>` : "開啟 ↗";
  const title = target ? `在 Chrome「${target.label}」開啟` : (s.account ? `需要以 ${s.account} 登入的瀏覽器` : "");
  return `<a class="${cls}" href="${esc(link)}" target="_blank" rel="noopener" data-open-realm="${esc(s.realm)}" title="${esc(title)}">${label}</a>`;
}

function linkedItems(s, ctx) {
  return (s.linked_items || []).map((id) => {
    const it = ctx.itemsById.get(ikey(s.realm, id));
    return `<span class="chip item" title="${esc(it?.hypothesis || "")}">${esc(it?.title || id)}</span>`;
  }).join("");
}

const metaLine = (s) => [base(s.cwd), s.git_branch && `⎇ ${s.git_branch}`, s.tmux && `tmux ${s.tmux}`].filter(Boolean).map(esc).join(" · ");
const isStale = (s) => s.source_age_s > STALE_S && s.status !== "gone";
const nameOf = (s) => s.name || String(s.session_id || "").slice(0, 8);

function statusBadge(s, now) {
  return `<span class="badge st-${esc(s.status)}" title="${esc(FORMAL[s.status] || s.status)}"><i class="dot" aria-hidden="true"></i>${esc(word(s.status))} <span class="ago">${esc(ago(s.status_since, now))}</span></span>`;
}

// ---- pixel studio art (40x24 grid; names never reach the SVG) ---------------
const SHIRTS = ["#e64980", "#7950f2", "#1c7ed6", "#0ca678", "#f59f00", "#e8590c", "#5c940d", "#c2255c"];
const HAIRS = ["#212529", "#5c3d2e", "#8d5524", "#e0b46c", "#495057", "#a61e4d"];
const SKINS = ["#f6d5b8", "#eec39a", "#d9a066", "#b9773e"];
const px = (x, y, w, h, fill, cls) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}"${cls ? ` class="${cls}"` : ""}/>`;

function studioArt(s, i) {
  const k = hash(nameOf(s).toLowerCase());
  const shirt = SHIRTS[k % SHIRTS.length], hair = HAIRS[(k >>> 5) % HAIRS.length], skin = SKINS[(k >>> 9) % SKINS.length];
  const glasses = ((k >>> 13) & 3) === 0;
  const st = s.status;
  const p = [px(0, 0, 40, 20, "var(--wall)"), px(0, 20, 40, 4, "var(--floor)"),
    px(2, 2, 7, 6, "var(--window)"), px(5, 2, 1, 6, "var(--wall)"), px(2, 5, 7, 1, "var(--wall)"),
    px(9, 10, 1, 8, "#5f5d73"), px(9, 17, 8, 1, "#5f5d73"), px(10, 18, 1, 2, "#5f5d73"), px(15, 18, 1, 2, "#5f5d73")];
  const screen = st === "gone" ? "#2b2d42" : st === "waiting" ? "#fff0e0" : st === "idle" ? "#9aa5b1" : "#dbe4ff";
  p.push(px(21, 6, 11, 8, "#2b2d42"), px(22, 7, 9, 6, screen), px(26, 14, 2, 1, "#2b2d42"));
  if (st === "busy") p.push(px(23, 8, 5, 1, "#4263eb", "code c1"), px(23, 10, 6, 1, "#4263eb", "code c2"), px(23, 12, 4, 1, "#4263eb", "code c3"));
  if (st === "waiting") p.push(px(26, 8, 1, 3, "#e8590c"), px(26, 12, 1, 1, "#e8590c"));
  if (st === "idle") {
    p.push(px(11, 12, 6, 3, shirt), px(14, 11, 5, 3, skin), px(14, 10, 5, 2, hair));
    p.push(`<g class="zz"><text x="20" y="5" class="z">z</text><text x="23" y="2.5" class="z z2">z</text></g>`);
  } else if (st !== "gone") {
    p.push(px(11, 10, 6, 5, shirt), px(12, 4, 5, 5, skin), px(12, 3, 5, 2, hair), px(12, 4, 1, 3, hair), px(16, 6, 1, 1, "#212529"));
    if (glasses) p.push(px(15, 5, 3, 1, "#212529"));
    if (st === "busy") p.push(`<g class="arms">${px(16, 12, 5, 1, skin)}</g>`, px(19, 14, 6, 1, "#adb5bd"));
    if (st === "waiting") p.push(`<g class="hand">${px(10, 2, 1, 8, shirt)}${px(9, 0, 3, 2, skin)}</g>`, px(1, 0, 7, 6, "#ffffff"), px(4, 1, 1, 3, "#e8590c"), px(4, 5, 1, 1, "#e8590c"));
  }
  p.push(px(7, 15, 28, 2, "#a0703c"), px(8, 17, 1, 3, "#a0703c"), px(33, 17, 1, 3, "#a0703c"));
  return `<svg class="scene" style="--i:${i % 7}" viewBox="0 0 40 24" shape-rendering="crispEdges" aria-hidden="true">${p.join("")}</svg>`;
}

// What has this session been up to? The newer of Claude Code's own recap
// (away_summary, written when you come back to a session) and Claude's latest
// message to you, which usually closes a turn with its own summary.
const plain = (t) => String(t || "").replace(/\*\*|__|`/g, "").replace(/^#+\s*/gm, "");
function recap(s, now, lines = 4) {
  const away = s.away_summary ? { t: s.away_summary, at: s.away_summary_at || 0, label: "回顧" } : null;
  const reply = s.last_reply ? { t: plain(s.last_reply), at: s.last_reply_at || 0, label: s.status === "busy" ? "正在說" : "最後回覆" } : null;
  const r = away && reply ? (reply.at > away.at ? reply : away) : away || reply;
  if (!r) return s.last_prompt ? `<p class="summary">› ${esc(s.last_prompt)}</p>` : "";
  return `<p class="summary recap" style="--lines:${Number(lines) || 4}" title="${esc(r.t)}"><b class="lbl">${esc(r.label)}</b>${esc(r.t)}${r.at ? ` <span class="age">· ${esc(ago(r.at, now))}前</span>` : ""}</p>`;
}

// Claude Code titles a session once, from its first exchange, and never updates
// it. In a long-lived session that title is history: demote it under the recap.
const TITLE_STALE_MS = 24 * 3600e3;
const titleIsOld = (s, now) => s.ai_title_at && now - s.ai_title_at > TITLE_STALE_MS && (s.away_summary || s.last_reply);
const md = (ms) => { const d = new Date(ms); return `${d.getMonth() + 1}/${d.getDate()}`; };
function freshTitle(s, now) {
  return s.ai_title && !titleIsOld(s, now) ? `<h3 class="title">${esc(s.ai_title)}</h3>` : "";
}
function oldTitle(s, now) {
  return s.ai_title && titleIsOld(s, now) ? `<p class="title-old" title="Claude Code 只在 session 一開始取一次標題，之後不會更新">最初主題（${esc(md(s.ai_title_at))}）：${esc(s.ai_title)}</p>` : "";
}

// ---- cards (desk, studio) ----------------------------------------------------
function card(s, ctx, i = 0) {
  const now = ctx.now, look = WD.look;
  const att = s.attention || { level: 0, reason: "" };
  const items = linkedItems(s, ctx);
  return `
  <article class="card st-${esc(s.status)} lvl-${Number(att.level) || 0}${isStale(s) ? " stale" : ""}">
    ${look === "studio" ? `<div class="art">${studioArt(s, i)}</div>` : ""}
    <header>
      <span class="name">${esc(nameOf(s))}</span>
      <span class="chip realm realm-${esc(s.realm)}">${esc(s.profile)}</span>
      ${statusBadge(s, now)}
    </header>
    ${att.reason ? `<p class="reason">${esc(att.reason)}</p>` : ""}
    ${isStale(s) ? `<p class="reason">這台機器 ${esc(ago(now - s.source_age_s * 1000, now))}沒回報，狀態可能過時</p>` : ""}
    ${freshTitle(s, now)}
    ${recap(s, now)}
    ${oldTitle(s, now)}
    ${s.last_prompt && (s.away_summary || s.last_reply) ? `<p class="prompt" title="最後一句 prompt">› ${esc(s.last_prompt)}</p>` : ""}
    ${items ? `<div class="items">${items}</div>` : ""}
    ${s.status !== "gone" ? strip(s, now, ctx.hours) : ""}
    <footer><span class="meta">${metaLine(s)}</span>${openLink(s)}</footer>
  </article>`;
}

// the "needs you" lane in the desk look: the wait itself is the headline
function needCard(s, ctx) {
  const d = dur(s.status_since, ctx.now);
  return `
  <article class="need st-${esc(s.status)}">
    <div class="wait" title="${esc(FORMAL[s.status] || "")}"><b>${esc(d.n)}</b><span>${esc(d.u)}</span><em>${esc(word(s.status))}</em></div>
    <div class="body">
      <header><span class="name">${esc(nameOf(s))}</span><span class="chip realm realm-${esc(s.realm)}">${esc(s.host)} · ${esc(s.profile)}</span></header>
      ${freshTitle(s, ctx.now)}
      ${recap(s, ctx.now, 3)}
      ${oldTitle(s, ctx.now)}
      ${s.attention?.reason ? `<p class="reason">${esc(s.attention.reason)}</p>` : ""}
      ${linkedItems(s, ctx) ? `<div class="items">${linkedItems(s, ctx)}</div>` : ""}
    </div>
    <div class="act">${openLink(s, "open big")}</div>
  </article>`;
}

function hostHeader(h, ctx) {
  const srcs = ctx.sources.filter((s) => s.host === h);
  const silent = srcs.length && srcs.every((s) => s.age_s > STALE_S);
  const chips = srcs.map((s) => `<span class="chip realm realm-${esc(s.realm)}" title="${esc([s.account, s.account_org].filter(Boolean).join(" · "))}">${esc(s.profile)}${s.account ? ` · ${esc(shortAccount(s.account))}` : ""}</span>`).join(" ");
  return `<h2>${esc(h)} <small>${chips}${silent ? " · 失聯" : ""}</small></h2>`;
}

function legend(ctx) {
  return `<p class="legend" aria-hidden="true">過去 ${Number(ctx.hours) || 12} 小時 <i class="sw seg-waiting"></i>${esc(FORMAL.waiting)} <i class="sw seg-busy"></i>${esc(FORMAL.busy)} <i class="sw seg-idle"></i>${esc(FORMAL.idle)}</p>`;
}

function goneBlock(ctx) {
  if (!ctx.gone.length) return "";
  return `<details class="gone"><summary>最近結束的 ${ctx.gone.length} 個</summary><div class="list">${ctx.gone.map((s, i) => card(s, ctx, i)).join("")}</div></details>`;
}

function renderCards(ctx) {
  const needs = ctx.live.filter((s) => s.attention.level >= 2);
  const lane = WD.look === "desk"
    ? (needs.length ? `<section class="needs"><h2>需要你 <small>${needs.length}</small></h2><div class="lane">${needs.map((s) => needCard(s, ctx)).join("")}</div></section>` : `<p class="allclear">目前沒有 session 在等你。</p>`)
    : (needs.length ? `<section class="needs"><h2>需要你</h2><div class="list">${needs.map((s, i) => card(s, ctx, i)).join("")}</div></section>` : "");
  const hosts = ctx.hosts.map((h) => {
    const ss = ctx.live.filter((s) => s.host === h && s.attention.level < 2);
    return `<section class="host">${hostHeader(h, ctx)}<div class="list">${ss.map((s, i) => card(s, ctx, i)).join("") || `<p class="empty">沒有其他 session</p>`}</div></section>`;
  }).join("");
  return `${lane}${legend(ctx)}<div class="hosts">${hosts}</div>${goneBlock(ctx)}`;
}

// ---- airport departures board -------------------------------------------------
const flapWas = new Map();
const cjk = (ch) => ch.codePointAt(0) > 0x2e80;
function fit(text, width) {
  let out = "", w = 0;
  for (const ch of String(text || "")) {
    const cw = cjk(ch) ? 2 : 1;
    if (w + cw > width) return out + "…";
    out += ch; w += cw;
  }
  return out;
}
function flaps(key, text, width) {
  const t = fit(text, width), was = flapWas.get(key) ?? "";
  flapWas.set(key, t);
  const prev = Array.from(was);
  return Array.from(t).map((ch, i) => `<span class="f${prev[i] !== ch ? " flip" : ""}" style="--d:${i * 30}ms">${ch === " " ? "&nbsp;" : esc(ch)}</span>`).join("");
}
function renderFlap(ctx) {
  const order = { waiting: 0, busy: 1, idle: 2 };
  const rows = [...ctx.live].sort((a, b) => b.attention.level - a.attention.level || (order[a.status] ?? 3) - (order[b.status] ?? 3) || (a.status_since || 0) - (b.status_since || 0));
  const body = rows.map((s) => `
    <tr class="st-${esc(s.status)}${isStale(s) ? " stale" : ""}">
      <td class="lamp"><i class="dot" aria-hidden="true"></i></td>
      <td class="fl c-name">${flaps(`${s.key}|n`, nameOf(s), 18)}</td>
      <td class="fl c-dest">${flaps(`${s.key}|d`, (titleIsOld(s, ctx.now) ? s.last_prompt || s.ai_title : s.ai_title || s.last_prompt) || "—", 30)}</td>
      <td class="fl c-gate">${flaps(`${s.key}|g`, `${s.host}·${s.profile}`, 24)}</td>
      <td class="fl c-time">${flaps(`${s.key}|t`, ago(s.status_since, ctx.now), 9)}</td>
      <td class="fl c-st" title="${esc(FORMAL[s.status] || "")}">${flaps(`${s.key}|s`, word(s.status), 8)}</td>
      <td class="c-go">${openLink(s, "open")}</td>
    </tr>`).join("");
  const shown = new Set(rows.map((s) => s.key));
  for (const k of flapWas.keys()) if (!shown.has(k.slice(0, k.lastIndexOf("|")))) flapWas.delete(k);
  const waiting = rows.filter((s) => s.attention.level >= 2);
  return `
    <section class="board">
      <h2><span class="en">DEPARTURES</span> 出發 <small>${rows.length} 個 session · ${waiting.length ? `${waiting.length} 個請洽櫃台` : "全部準點"}</small></h2>
      <div class="boardwrap"><table>
        <thead><tr><th></th><th>航班</th><th class="c-dest">目的地</th><th class="c-gate">登機門</th><th>時間</th><th>狀態</th><th></th></tr></thead>
        <tbody>${body || `<tr><td colspan="7" class="empty">沒有航班</td></tr>`}</tbody>
      </table></div>
    </section>
    ${waiting.length ? `<section class="needs"><h2>請洽櫃台</h2><div class="list">${waiting.map((s, i) => card(s, ctx, i)).join("")}</div></section>` : ""}
    ${goneBlock(ctx)}`;
}

// ---- sticky notes on a cork board ----------------------------------------------
const NOTES = ["#fff3bf", "#d3f9d8", "#d0ebff", "#ffdeeb", "#e5dbff", "#ffe8cc"];
function note(s, ctx) {
  const k = hash(nameOf(s)), tilt = ((k % 7) - 3) * 0.6;
  const color = NOTES[hash(s.host) % NOTES.length];
  return `
  <article class="note st-${esc(s.status)}${isStale(s) ? " stale" : ""}" style="--note:${color};--tilt:${tilt}deg">
    <i class="pin" aria-hidden="true"></i>
    <header><span class="name">${esc(nameOf(s))}</span>${statusBadge(s, ctx.now)}</header>
    ${freshTitle(s, ctx.now)}
    ${recap(s, ctx.now, 3)}
    ${oldTitle(s, ctx.now)}
    ${s.attention?.reason ? `<p class="reason">${esc(s.attention.reason)}</p>` : ""}
    <footer><span class="meta">${esc(s.host)} · ${esc(s.profile)}</span>${openLink(s)}</footer>
  </article>`;
}
function renderKanban(ctx) {
  const cols = [
    ["needs", "需要你", ctx.live.filter((s) => s.attention.level >= 2)],
    ["busy", word("busy"), ctx.live.filter((s) => s.attention.level < 2 && s.status === "busy")],
    ["idle", word("idle"), ctx.live.filter((s) => s.attention.level < 2 && s.status !== "busy")],
  ];
  return `<div class="cork">${cols.map(([id, label, ss]) => `
    <section class="col col-${id}"><h2><span>${esc(label)}</span> <small>${ss.length}</small></h2>
      <div class="notes">${ss.map((s) => note(s, ctx)).join("") || `<p class="empty">（空）</p>`}</div>
    </section>`).join("")}</div>${goneBlock(ctx)}`;
}

WD.register({
  id: "sessions",
  label: "Sessions",
  render(el, m) {
    const live = m.sessions.filter((s) => s.status !== "gone");
    const ctx = {
      now: Date.now(),
      hours: m.historyHours || 12,
      itemsById: new Map(m.items.map((it) => [ikey(it.realm, it.id), it])),
      live,
      gone: m.sessions.filter((s) => s.status === "gone"),
      sources: m.sources,
      hosts: [...new Set([...m.sources.map((s) => s.host), ...live.map((s) => s.host)])].sort(),
    };
    const banner = m.offline.length ? `<p class="banner">暫時連不上 ${m.offline.map((s) => esc(s.state?.realm || s.url.replace(/^https?:\/\//, ""))).join("、")}，自動重試中（換網路後通道約 1 分鐘內會自己接回；一直沒好再執行 <code>wd view</code>）</p>` : "";
    if (WD.look === "office") {
      mountOffice(el, ctx, { card, hostHeader, nameOf, banner });
      return;
    }
    const body = WD.look === "flap" ? renderFlap(ctx) : WD.look === "kanban" ? renderKanban(ctx) : renderCards(ctx);
    el.innerHTML = banner + body;
  },
});
