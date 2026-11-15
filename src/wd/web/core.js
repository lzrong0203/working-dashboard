// Shared state and helpers. Imported by app.js AND by every view, and imports
// nothing itself -- views must never import app.js (that would be a cycle, and
// with top-level await in the graph a cycle never finishes loading).

export const WD = {
  views: [],
  register(view) { this.views.push(view); },
  sources: new Map(), // url -> {url, state, error, es}
  active: lsGet("wd.view") || "sessions",
};

export function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
export function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Item ids are only unique within a realm; always key by both.
export const ikey = (realm, id) => `${realm}\u0000${id}`;

export function ago(ms, now = Date.now()) {
  if (!ms || Number.isNaN(ms)) return "";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分`;
  if (s < 86400 * 2) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} 小時`;
  return `${Math.round(s / 86400)} 天`;
}

// Optional helper on the viewing machine that opens claude.ai links in the
// browser profile configured for each realm (see wd/opener.py).
export const opener = { base: `http://127.0.0.1:${lsGet("wd.opener_port") || 8739}`, realms: null };

export async function probeOpener() {
  try {
    const r = await fetch(`${opener.base}/config`);
    const j = await r.json();
    opener.realms = j.ok ? j.realms : null;
  } catch { opener.realms = null; }
  return opener.realms;
}

export async function openInProfile(realm, url) {
  const r = await fetch(`${opener.base}/open`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-WD-Open": "1" },
    body: JSON.stringify({ realm, url }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "open failed");
}
