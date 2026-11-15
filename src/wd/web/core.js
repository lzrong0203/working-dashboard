// Shared state and helpers. Imported by app.js AND by every view, and imports
// nothing itself -- views must never import app.js (that would be a cycle, and
// with top-level await in the graph a cycle never finishes loading).

export function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
export function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

// Looks: one set of data, several worlds. Each look commits to one colour scheme
// and its own words for the states; the formal word is always in a title tooltip.
export const LOOKS = [
  { id: "desk", label: "控制台" },
  { id: "studio", label: "像素工作室" },
  { id: "flap", label: "機場看板" },
  { id: "kanban", label: "便利貼" },
];
const WORDS = {
  desk: { waiting: "等你", busy: "執行中", idle: "閒置", gone: "已結束", unknown: "未知" },
  studio: { waiting: "舉手中", busy: "打字中", idle: "打瞌睡", gone: "下班了", unknown: "未知" },
  flap: { waiting: "請洽櫃台", busy: "飛行中", idle: "已抵達", gone: "已結束", unknown: "未知" },
  kanban: { waiting: "等你", busy: "進行中", idle: "閒置", gone: "完成", unknown: "未知" },
};
export const FORMAL = WORDS.desk;

const docEl = typeof document !== "undefined" ? document.documentElement : null;
export const WD = {
  views: [],
  register(view) { this.views.push(view); },
  sources: new Map(), // url -> {url, state, error, es}
  active: lsGet("wd.view") || "sessions",
  look: docEl?.dataset?.look || "desk",
};

export function setLook(id) {
  if (!LOOKS.some((l) => l.id === id)) return;
  WD.look = id;
  if (docEl?.dataset) docEl.dataset.look = id;
  lsSet("wd.look", id);
}

export const word = (status) => (WORDS[WD.look] || WORDS.desk)[status] || status;

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Item ids are only unique within a realm; always key by both.
export const ikey = (realm, id) => `${realm}\u0000${id}`;

// FNV-1a: a name always maps to the same look (character, note colour, tilt),
// independent of who else is on the board.
export function hash(str) {
  let h = 0x811c9dc5;
  for (const ch of String(str)) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
  return h >>> 0;
}

export function ago(ms, now = Date.now()) {
  if (!ms || Number.isNaN(ms)) return "";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分`;
  if (s < 86400 * 2) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} 小時`;
  return `${Math.round(s / 86400)} 天`;
}

// Big-number form of a duration: {n: "2.1", u: "小時"}
export function dur(ms, now = Date.now()) {
  const s = Math.max(0, Math.round((now - (ms || now)) / 1000));
  if (s < 60) return { n: String(s), u: "秒" };
  if (s < 3600) return { n: String(Math.round(s / 60)), u: "分" };
  if (s < 86400 * 2) return { n: (s / 3600).toFixed(s < 36000 ? 1 : 0), u: "小時" };
  return { n: String(Math.round(s / 86400)), u: "天" };
}

const hhmm = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// Status timeline: one thin strip per session, segments separated by a 2px
// surface gap, colour by state (validated palette, see style.css), hover title
// per segment, and an aria-label with the same facts. Unknown time stays empty.
export function strip(s, now = Date.now(), hours = 12) {
  const h = s.history;
  if (!h) return "";
  const t0 = now - hours * 3600e3;
  const W = 300, H = 8, GAP = 2;
  const x = (t) => ((Math.max(t, t0) - t0) / (now - t0)) * W;
  const segs = [];
  let cur = h.start, at = Math.max(t0, h.since || t0);
  for (const [ts, st] of h.changes || []) {
    if (ts > at) segs.push([at, ts, cur]);
    cur = st;
    at = Math.max(ts, t0);
  }
  if (now > at) segs.push([at, now, cur]);
  const marks = segs
    .filter(([, , st]) => st === "waiting" || st === "busy" || st === "idle")
    .map(([a, b, st]) => {
      const x0 = x(a), w = x(b) - x0 - GAP;
      if (w < 0.6) return "";
      return `<rect class="seg seg-${st}" x="${x0.toFixed(1)}" y="0" width="${w.toFixed(1)}" height="${H}" rx="2"><title>${esc(FORMAL[st])} ${hhmm(a)}–${hhmm(b)}</title></rect>`;
    }).join("");
  const label = segs.filter(([, , st]) => FORMAL[st]).map(([a, b, st]) => `${FORMAL[st]} ${hhmm(a)}–${hhmm(b)}`).join("，");
  return `<svg class="strip" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="過去 ${hours} 小時：${esc(label)}">${marks}</svg>`;
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
