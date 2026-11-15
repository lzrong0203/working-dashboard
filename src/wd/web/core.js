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
