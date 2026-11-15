// Shell: data sources (this hub + peer hubs), merge, view registry.
// Each realm's hub is fetched directly by the browser; no hub ever copies
// another realm's data.

export const WD = {
  views: [],
  register(view) { this.views.push(view); },
  sources: new Map(), // url -> {url, state, error, es}
  active: localStorageGet("wd.view") || "sessions",
};

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function ago(ms, now = Date.now()) {
  if (!ms) return "";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分`;
  if (s < 86400 * 2) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} 小時`;
  return `${Math.round(s / 86400)} 天`;
}

// Merged read model across all reachable hubs.
export function merged() {
  const out = { sessions: [], items: [], sources: [], events: [], realms: [], offline: [] };
  for (const src of WD.sources.values()) {
    if (!src.state) { out.offline.push(src); continue; }
    const st = src.state;
    if (out.realms.includes(st.realm)) continue; // same hub reached via two URLs
    out.realms.push(st.realm);
    out.sessions.push(...st.sessions);
    out.items.push(...st.items.map((it) => ({ ...it, realm: st.realm })));
    out.sources.push(...st.sources);
    out.events.push(...st.events.map((e) => ({ ...e, realm: st.realm })));
  }
  out.sessions.sort((a, b) => b.attention.level - a.attention.level || (a.status === "gone") - (b.status === "gone") || (b.status_since || 0) - (a.status_since || 0));
  return out;
}

function connect(url) {
  const src = { url, state: null, error: null, es: null };
  WD.sources.set(url, src);
  const base = url.replace(/\/$/, "");
  fetch(`${base}/api/state`).then((r) => r.json()).then((st) => { src.state = st; src.error = null; render(); }).catch((e) => { src.error = String(e); render(); });
  const open = () => {
    const es = new EventSource(`${base}/api/events`);
    src.es = es;
    es.addEventListener("state", (ev) => {
      src.state = JSON.parse(ev.data); src.error = null;
      for (const p of src.state.hub?.peers || []) if (!WD.sources.has(p)) connect(p);
      render();
    });
    es.onerror = () => { src.error = "disconnected"; render(); };
  };
  open();
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
  tabs.innerHTML = WD.views.map((v) => `<button role="tab" data-view="${v.id}" aria-selected="${v.id === WD.active}">${esc(v.label)}</button>`).join("");
  tabs.querySelectorAll("button").forEach((b) => b.onclick = () => { WD.active = b.dataset.view; localStorageSet("wd.view", WD.active); render(); });

  document.getElementById("sources").innerHTML = [...WD.sources.values()].map((s) => {
    const realm = s.state?.realm || s.url.replace(/^https?:\/\//, "");
    const cls = s.state && !s.error ? "ok" : "down";
    const title = s.error ? `${s.url}: ${s.error}` : s.url;
    return `<span class="src ${cls} realm-${esc(s.state?.realm || "x")}" title="${esc(title)}">${esc(realm)}</span>`;
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

// Views register themselves on import.
await import("./views/sessions.js");
await import("./views/items.js");

connect(location.origin);
setInterval(render, 15000); // keep relative times fresh
