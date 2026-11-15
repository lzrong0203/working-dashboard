// Shell: data sources (this hub + peer hubs), merge, chrome, routing.
// Each realm's hub is fetched directly by the browser; no hub ever copies
// another realm's data.
import { WD, esc, lsSet, opener, probeOpener, openInProfile } from "./core.js";
import "./views/sessions.js"; // views register themselves on import
import "./views/items.js";

// Merged read model across all reachable hubs.
export function merged() {
  const out = { sessions: [], items: [], sources: [], events: [], realms: [], offline: [], itemErrors: [] };
  for (const src of WD.sources.values()) {
    if (!src.state) { out.offline.push(src); continue; }
    const st = src.state;
    if (out.realms.includes(st.realm)) continue; // same hub reached via two URLs
    out.realms.push(st.realm);
    out.sessions.push(...(st.sessions || []));
    out.items.push(...(st.items || []).map((it) => ({ ...it, realm: st.realm })));
    out.sources.push(...(st.sources || []).map((s) => ({ ...s, realm: st.realm })));
    out.events.push(...(st.events || []).map((e) => ({ ...e, realm: st.realm })));
    out.itemErrors.push(...(st.item_errors || []).map((e) => `${st.realm}: ${e}`));
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

function renderChrome(m) {
  const by = (st) => m.sessions.filter((s) => s.status === st).length;
  const urgent = m.sessions.filter((s) => s.attention.level >= 3).length;
  document.getElementById("counts").innerHTML = `
    <span class="count urgent ${urgent ? "on" : ""}">需要你 <b>${urgent}</b></span>
    <span class="count busy">執行中 <b>${by("busy")}</b></span>
    <span class="count idle">閒置 <b>${by("idle")}</b></span>`;
  document.title = urgent ? `(${urgent}) Working Dashboard` : "Working Dashboard";

  const tabs = document.getElementById("tabs");
  tabs.innerHTML = WD.views.map((v) => `<button role="tab" data-view="${esc(v.id)}" aria-selected="${v.id === WD.active}">${esc(v.label)}</button>`).join("");
  tabs.querySelectorAll("button").forEach((b) => { b.onclick = () => { WD.active = b.dataset.view; lsSet("wd.view", WD.active); render(); }; });

  document.getElementById("sources").innerHTML = [...WD.sources.values()].map((s) => {
    const realm = s.state?.realm || s.url.replace(/^https?:\/\//, "");
    const cls = s.state && !s.error ? "ok" : "down";
    const title = s.error ? `${s.url}: ${s.error}` : s.url;
    return `<span class="src ${cls}" title="${esc(title)}">${esc(realm)}</span>`;
  }).join("");
  document.getElementById("clock").textContent = `更新於 ${new Date().toLocaleTimeString()}`;
}

let pending = false;
export function render() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    const m = merged();
    renderChrome(m);
    const view = WD.views.find((v) => v.id === WD.active) || WD.views[0];
    if (view) view.render(document.getElementById("view"), m);
  });
}

// "開啟" links: hand them to the opener (right browser profile) when it serves
// that realm; otherwise the plain link opens in the current profile.
document.addEventListener("click", (ev) => {
  const a = ev.target.closest?.("a[data-open-realm]");
  if (!a || !opener.realms?.[a.dataset.openRealm]) return;
  ev.preventDefault();
  openInProfile(a.dataset.openRealm, a.href).catch(() => window.open(a.href, "_blank", "noopener"));
});

connect(location.origin);
probeOpener().then(render);
setInterval(render, 15000); // keep relative times fresh
setInterval(() => probeOpener().then(render), 60000);
