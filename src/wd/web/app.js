// Shell: data sources (this hub + peer hubs), merge, chrome, routing.
// Each realm's hub is fetched directly by the browser; no hub ever copies
// another realm's data.
import { WD, LOOKS, esc, lsSet, setLook, word, opener, probeOpener, openInProfile, inRealm, realmLabel, setRealm } from "./core.js";
import "./views/sessions.js"; // views register themselves on import
import "./views/items.js";
import "./views/research.js";

// Merged read model across all reachable hubs.
export function merged() {
  const out = { sessions: [], items: [], sources: [], events: [], realms: [], offline: [], itemErrors: [], research: [], historyHours: 12 };
  for (const src of WD.sources.values()) {
    if (!src.state) { out.offline.push(src); continue; }
    const st = src.state;
    if (out.realms.includes(st.realm)) continue; // same hub reached via two URLs
    out.realms.push(st.realm);
    out.sessions.push(...(st.sessions || []));
    out.items.push(...(st.items || []).map((it) => ({ ...it, realm: st.realm })));
    out.sources.push(...(st.sources || []).map((s) => ({ ...s, realm: st.realm })));
    out.events.push(...(st.events || []).map((e) => ({ ...e, realm: st.realm })));
    out.research.push(...(st.research || []).map((r) => ({ ...r, realm: st.realm, base: src.url })));
    out.itemErrors.push(...(st.item_errors || []).map((e) => `${st.realm}: ${e}`));
    if (st.history_hours) out.historyHours = st.history_hours;
  }
  out.sessions.sort((a, b) => b.attention.level - a.attention.level || (a.status === "gone") - (b.status === "gone") || (b.status_since || 0) - (a.status_since || 0));
  return out;
}

function connect(url) {
  const src = { url, state: null, error: null, es: null };
  WD.sources.set(url, src);
  const base = url.replace(/\/$/, "");
  const accept = (st) => {
    src.state = st; src.error = null;
    for (const p of st.hub?.peers || []) if (!WD.sources.has(p)) connect(p);
    render();
  };
  fetch(`${base}/api/state`).then((r) => r.json()).then(accept).catch((e) => { src.error = String(e); render(); });
  if (typeof EventSource !== "undefined") {
    const es = new EventSource(`${base}/api/events`);
    src.es = es;
    es.addEventListener("state", (ev) => accept(JSON.parse(ev.data)));
    es.onerror = () => { src.error = "disconnected"; render(); };
  }
}

function renderRealmFilter(m) {
  const box = document.getElementById("realmfilter");
  if (!box) return;
  const realms = [...new Set([...m.realms, ...(WD.realm ? [WD.realm] : [])])].sort();
  if (realms.length < 2 && !WD.realm) { box.innerHTML = ""; return; }
  // a hidden realm still says when something there needs you
  const urgentIn = (r) => m.sessions.filter((s) => s.realm === r && s.attention.level >= 3).length;
  const btn = (r, label) => {
    const n = r && r !== WD.realm ? urgentIn(r) : 0;
    return `<button type="button" data-realm="${esc(r)}" aria-pressed="${WD.realm === r}">${esc(label)}${n ? ` <b class="rf-urgent" title="${n} 個在等你">${n}</b>` : ""}</button>`;
  };
  box.innerHTML = btn("", "全部") + realms.map((r) => btn(r, realmLabel(r))).join("");
  box.querySelectorAll("button").forEach((b) => { b.onclick = () => { setRealm(b.dataset.realm); render(); }; });
}

function renderChrome(m) {
  renderRealmFilter(m);
  const mine = m.sessions.filter((s) => inRealm(s.realm));
  const by = (st) => mine.filter((s) => s.status === st).length;
  const urgent = mine.filter((s) => s.attention.level >= 3).length;
  document.getElementById("counts").innerHTML = `
    <span class="count urgent ${urgent ? "on" : ""}"><b>${urgent}</b>需要你</span>
    <span class="count busy"><b>${by("busy")}</b>${esc(word("busy"))}</span>
    <span class="count idle"><b>${by("idle")}</b>${esc(word("idle"))}</span>`;

  const sel = document.getElementById("looks");
  if (sel && !sel.options.length) {
    sel.innerHTML = LOOKS.map((l) => `<option value="${esc(l.id)}">${esc(l.label)}</option>`).join("");
    sel.onchange = () => { setLook(sel.value); render(); };
  }
  if (sel) sel.value = WD.look;
  // the browser tab counts every realm, filtered or not: a hidden realm must still be able to call you
  const allUrgent = m.sessions.filter((s) => s.attention.level >= 3).length;
  document.title = allUrgent ? `(${allUrgent}) Working Dashboard` : "Working Dashboard";

  const tabs = document.getElementById("tabs");
  const shown = WD.views.filter((v) => !v.visible || v.visible(m));
  // a tab whose data hasn't arrived yet is hidden, not forgotten
  const eff = shown.some((v) => v.id === WD.active) ? WD.active : shown[0]?.id;
  tabs.innerHTML = shown.map((v) => `<button role="tab" data-view="${esc(v.id)}" aria-selected="${v.id === eff}">${esc(v.label)}</button>`).join("");
  tabs.querySelectorAll("button").forEach((b) => { b.onclick = () => { WD.active = b.dataset.view; lsSet("wd.view", WD.active); render(); }; });

  document.getElementById("sources").innerHTML = [...WD.sources.values()].map((s) => {
    const realm = s.state?.realm || s.url.replace(/^https?:\/\//, "");
    const cls = s.state && !s.error ? "ok" : "down";
    const title = s.error ? `${s.url}: ${s.error}` : s.url;
    return `<span class="src ${cls}" title="${esc(title)}">${esc(realm)}</span>`;
  }).join("");
  document.getElementById("clock").textContent = `更新於 ${new Date().toLocaleTimeString()}`;
}

// Coalesce bursts of updates into one render. setTimeout, not
// requestAnimationFrame: rAF never fires without a display link (asleep
// screens, some headless/embedded browsers) and the board would stay blank.
let pending = false;
export function render() {
  if (pending) return;
  pending = true;
  setTimeout(() => {
    pending = false;
    const m = merged();
    renderChrome(m);
    const view = WD.views.find((v) => v.id === WD.active && (!v.visible || v.visible(m))) || WD.views[0];
    if (view) view.render(document.getElementById("view"), m);
  }, 16);
}

// "開啟" links: hand them to the opener (right browser profile) when it serves
// that realm; otherwise the plain link opens in the current profile.
document.addEventListener("click", (ev) => {
  const a = ev.target.closest?.("a[data-open-realm]");
  if (!a || !opener.realms?.[a.dataset.openRealm]) return;
  ev.preventDefault();
  openInProfile(a.dataset.openRealm, a.href).catch(() => window.open(a.href, "_blank", "noopener"));
});

WD.rerender = render; // views call this instead of importing app.js (no cycles)
connect(location.origin);
probeOpener().then(render);
setInterval(render, 15000); // keep relative times fresh
setInterval(() => probeOpener().then(render), 60000);
