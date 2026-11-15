// office look: a top-down pixel office per machine, one person per session.
// Where a person is and what they do is the session's state:
//   busy      sits at their desk; the screen shows what the last tool call was
//             for (edit: code, read: a page, run: a terminal, delegate: a
//             bubble, plan: notes)
//   waiting   stands up beside the desk with a "!" bubble and the wait time
//   idle      fresh result -> at the coffee machine; a while -> asleep on the
//             sofa (or at the desk); parked for hours -> gone, jacket on chair
// People walk between spots along the aisles when their state changes.
// All art is drawn here, pixel by pixel; no external sprite sheets.
// Imports core.js only (views never import app.js).
import { WD, esc, dur, word, hash, lsGet, lsSet } from "../core.js";

const SKINS = ["#f6d5b8", "#eec39a", "#d9a066", "#b9773e", "#8d5524"];
const HAIRS = ["#2b2b2b", "#5c3d2e", "#8d5524", "#e0b46c", "#6c757d", "#a61e4d", "#364fc7"];
const SHIRTS = ["#e64980", "#7950f2", "#1c7ed6", "#0ca678", "#f59f00", "#e8590c", "#5c940d", "#c2255c", "#1098ad"];
const PANTS = ["#343a40", "#364fc7", "#5f3dc4", "#495057", "#2b8a3e"];
const SCREEN = { edit: "#1b2a4a", read: "#f1f3f5", run: "#0b1a0f", delegate: "#2a1b3d", plan: "#fff3bf", tool: "#1e1e2e", "": "#1b2a4a" };
const PARK_MS = 12 * 3600e3;
const TICK_MS = 100;

const DESK_W = 48, DESK_H = 64, WALL_H = 40, LOUNGE_W = 104, MARGIN = 16;

const st = { chars: new Map(), rooms: [], sel: lsGet("wd.office.sel") || "", timer: null, frame: 0, sig: "" };
const reduced = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function looks(name) {
  const k = hash(String(name).toLowerCase());
  return {
    skin: SKINS[k % SKINS.length], hair: HAIRS[(k >>> 4) % HAIRS.length], shirt: SHIRTS[(k >>> 8) % SHIRTS.length],
    pants: PANTS[(k >>> 12) % PANTS.length], style: (k >>> 16) % 4, glasses: ((k >>> 19) & 3) === 0,
  };
}

// ---- layout ------------------------------------------------------------------
function layout(n) {
  const perRow = Math.max(1, Math.min(4, n)), rows = Math.max(1, Math.ceil(n / perRow));
  const deskArea = MARGIN + perRow * DESK_W + 8;
  const W = deskArea + LOUNGE_W, H = Math.max(WALL_H + 8 + rows * DESK_H + 16, WALL_H + 120);
  const desks = Array.from({ length: n }, (_, i) => {
    const x = MARGIN + (i % perRow) * DESK_W, y = WALL_H + 8 + Math.floor(i / perRow) * DESK_H;
    return { x, y, seat: { x: x + 18, y: y + 22 }, stand: { x: x + 34, y: y + 22 }, aisle: y + 50 };
  });
  const lx = deskArea;
  return {
    W, H, perRow, rows, desks, lx, corridor: lx - 6,
    coffee: [0, 1, 2].map((k) => ({ x: lx + 30 + k * 16, y: WALL_H + 14 })),
    sofa: [0, 1, 2].map((k) => ({ x: lx + 14 + k * 26, y: WALL_H + 70 })),
    door: { x: 2, y: H - 22 },
  };
}

// ---- static background (walls, floor, furniture), drawn once per room -------
function px(g, S) { return (x, y, w, h, c) => { g.fillStyle = c; g.fillRect(Math.round(x * S), Math.round(y * S), Math.round(w * S), Math.round(h * S)); }; }
function background(L, S) {
  const c = document.createElement("canvas");
  c.width = L.W * S; c.height = L.H * S;
  const g = c.getContext("2d"), r = px(g, S);
  // floor: wood planks, carpet under the lounge
  for (let y = WALL_H; y < L.H; y += 8) for (let x = 0; x < L.W; x += 32) {
    const off = ((y / 8) % 2) * 16;
    r(x - off, y, 32, 8, ((x + off) / 32 + y / 8) % 3 ? "#d9b98c" : "#d2b083");
    r(x - off, y + 7, 32, 1, "#c29e70");
  }
  r(L.lx, WALL_H, L.W - L.lx, L.H - WALL_H, "#b8c7d9");
  for (let y = WALL_H; y < L.H; y += 4) r(L.lx, y, L.W - L.lx, 1, "#aebdd0");
  // wall, baseboard, windows, clock, whiteboard
  r(0, 0, L.W, WALL_H, "#e7ece7");
  r(0, WALL_H - 4, L.W, 4, "#9c7a54");
  for (let x = MARGIN; x + 34 < L.lx - 20; x += 64) {
    r(x, 8, 34, 20, "#ffffff"); r(x + 2, 10, 30, 16, "#a5d8ff"); r(x + 2, 10, 30, 5, "#d0ebff"); r(x + 16, 10, 2, 16, "#ffffff");
  }
  r(L.lx - 40, 9, 30, 18, "#f8f9fa"); r(L.lx - 40, 9, 30, 1, "#868e96"); r(L.lx - 38, 13, 14, 1, "#4263eb"); r(L.lx - 38, 17, 20, 1, "#e8590c"); r(L.lx - 38, 21, 10, 1, "#2b8a3e");
  // lounge: bookshelf, coffee machine, sofa, plants, rug
  const lx = L.lx;
  r(lx + 6, 6, 22, 30, "#8a5a33"); for (let k = 0; k < 3; k++) { r(lx + 7, 8 + k * 9, 20, 7, "#5c3a1e"); for (let b = 0; b < 5; b++) r(lx + 8 + b * 4, 9 + k * 9, 3, 6, ["#e64980", "#1c7ed6", "#f59f00", "#0ca678", "#7950f2"][(b + k) % 5]); }
  r(lx + 34, WALL_H - 14, 14, 18, "#495057"); r(lx + 36, WALL_H - 12, 10, 6, "#212529"); r(lx + 44, WALL_H - 10, 2, 2, "#fa5252"); r(lx + 38, WALL_H - 2, 6, 3, "#adb5bd");
  r(lx + 60, WALL_H - 10, 16, 14, "#868e96"); r(lx + 62, WALL_H - 8, 12, 4, "#dee2e6");
  r(lx + 8, WALL_H + 76, 84, 18, "#4c6ef5"); r(lx + 8, WALL_H + 72, 84, 6, "#5c7cfa"); r(lx + 8, WALL_H + 72, 4, 22, "#3b5bdb"); r(lx + 88, WALL_H + 72, 4, 22, "#3b5bdb");
  for (const [x, y] of [[L.W - 14, WALL_H + 2], [lx + 2, L.H - 22]]) { r(x, y + 8, 10, 8, "#a0522d"); r(x - 2, y, 14, 9, "#2f9e44"); r(x + 2, y - 4, 6, 6, "#40c057"); }
  // door
  r(0, L.H - 30, 4, 26, "#9c7a54");
  // desks: top, monitor frame, keyboard, chair
  for (const d of L.desks) {
    r(d.x + 4, d.y + 4, 40, 16, "#a8743f"); r(d.x + 4, d.y + 18, 40, 3, "#7a5230");
    r(d.x + 16, d.y + 1, 16, 11, "#2f3442"); r(d.x + 22, d.y + 12, 4, 3, "#2f3442");
    r(d.x + 17, d.y + 15, 14, 3, "#ced4da");
    r(d.x + 36, d.y + 7, 5, 6, "#ffe066");
  }
  return c;
}

// ---- people --------------------------------------------------------------------
function drawPerson(r, c, f) {
  const L = c.look, x = c.x, y = c.y, P = (dx, dy, w, h, col) => r(x + dx, y + dy, w, h, col);
  const step = c.pose === "walk" ? f % 2 : 0;
  if (c.pose === "sleep") {  // lying on the sofa
    P(0, 4, 7, 6, L.skin); P(0, 3, 7, 3, L.hair); P(7, 5, 12, 6, L.shirt); P(19, 6, 6, 4, L.pants);
    if (!reduced()) { const z = f % 20; P(4 + (z >> 3), -2 - (z >> 2), 2, 2, "#748ffc"); }
    return;
  }
  if (c.pose === "sit" || c.pose === "doze") {  // from behind, at the desk
    P(3, 0, 6, 6, L.hair); P(2, 6, 8, 6, L.shirt);
    if (c.pose === "doze") { P(3, 2, 6, 6, L.hair); P(1, 6, 10, 2, L.skin); return; }
    const t = reduced() ? 0 : f % 4 < 2 ? 0 : 1;
    P(0, 5 - t, 2, 3, L.shirt); P(10, 5 - (1 - t), 2, 3, L.shirt); P(0, 4 - t, 2, 1, L.skin); P(10, 4 - (1 - t), 2, 1, L.skin);
    return;
  }
  // standing / walking, front or back
  const back = c.facing === "up";
  P(3, 0, 6, 2, L.hair); P(2, 1, 8, 2, L.hair);
  if (back) P(2, 3, 8, 4, L.hair); else {
    P(3, 3, 6, 4, L.skin); P(2, 3, 1, L.style === 1 ? 6 : 3, L.hair); P(9, 3, 1, L.style === 1 ? 6 : 3, L.hair);
    if (L.style === 2) P(5, -2, 2, 2, L.hair);
    P(4, 4, 1, 1, "#212529"); P(7, 4, 1, 1, "#212529");
    if (L.glasses) { P(3, 4, 3, 1, "#495057"); P(6, 4, 3, 1, "#495057"); }
  }
  P(2, 7, 8, 6, L.shirt);
  if (c.wave) {  // hand up, waving
    const w = reduced() ? 0 : f % 6 < 3 ? 0 : 1;
    P(1, 8, 1, 4, L.shirt); P(1, 12, 1, 1, L.skin); P(10 + w, 2, 1, 6, L.shirt); P(10 + w, 1, 1, 1, L.skin);
  } else {
    P(1, 8 + step, 1, 4, L.shirt); P(10, 8 + (1 - step), 1, 4, L.shirt); P(1, 12 + step, 1, 1, L.skin); P(10, 12 + (1 - step), 1, 1, L.skin);
  }
  P(3, 13, 2, step ? 3 : 4, L.pants); P(7, 13, 2, step ? 4 : 3, L.pants);
  P(3, step ? 16 : 17, 2, 1, "#343a40"); P(7, step ? 17 : 16, 2, 1, "#343a40");
  if (c.mug) {
    P(10, 9, 3, 3, "#ffffff"); P(13, 10, 1, 1, "#adb5bd");
    if (!reduced()) { const s = f % 10; P(11, 7 - (s >> 2), 1, 1, "#ced4da"); P(12, 5 - (s >> 3), 1, 1, "#e9ecef"); }
  }
}

function drawScreen(r, d, act, f, on) {
  const x = d.x + 17, y = d.y + 2;
  r(x, y, 14, 9, on ? SCREEN[act] || SCREEN[""] : "#11141b");
  if (!on) return;
  const k = reduced() ? 3 : f % 12;
  if (act === "read") { for (let i = 0; i < 4; i++) r(x + 2, y + 1 + ((i * 2 + k) % 8), 9 - (i % 3) * 2, 1, "#868e96"); return; }
  if (act === "run") { for (let i = 0; i < 3; i++) r(x + 1, y + 1 + i * 3, Math.min(12, ((k + i * 4) % 12) + 1), 1, "#51cf66"); return; }
  if (act === "plan") { for (let i = 0; i < 3; i++) r(x + 2, y + 2 + i * 2, 8 - i * 2, 1, "#e67700"); return; }
  if (act === "delegate") { r(x + 3, y + 3, 2, 2, "#cc5de8"); r(x + 7, y + 3, 2, 2, "#cc5de8"); r(x + 11, y + 3, 1, 2, "#cc5de8"); return; }
  for (let i = 0; i < 3; i++) r(x + 1 + (i % 2) * 2, y + 1 + i * 3, Math.min(11, ((k + i * 3) % 11) + 2), 1, i === 1 ? "#ffa94d" : "#74c0fc");
}

// ---- who goes where -------------------------------------------------------------
function goal(s, now, desk, L, slots) {
  if (s.status === "waiting") return { ...desk.stand, pose: "stand", facing: "down", wave: true };
  if (s.status === "busy") return { ...desk.seat, pose: "sit", facing: "up" };
  if (s.status === "idle") {
    const age = now - (s.status_since || now);
    if (age > PARK_MS) return { ...L.door, pose: "gone", facing: "left" };
    if ((s.attention?.level || 0) >= 2 && slots.coffee.length) return { ...slots.coffee.shift(), pose: "stand", facing: "down", mug: true };
    if (slots.sofa.length) return { ...slots.sofa.shift(), pose: "sleep", facing: "down" };
    return { ...desk.seat, pose: "doze", facing: "up" };
  }
  return { ...desk.seat, pose: "sit", facing: "up" };
}

function route(c, t, L) {
  // along the aisle under the current row, the corridor by the lounge, then up
  const aisle = (y) => { const d = L.desks.find((dk) => y <= dk.aisle && y >= dk.y - 4); return d ? d.aisle : Math.min(y, L.H - 14); };
  const a1 = aisle(c.y), a2 = aisle(t.y), pts = [{ x: c.x, y: a1 }];
  if (a1 !== a2) pts.push({ x: L.corridor, y: a1 }, { x: L.corridor, y: a2 });
  pts.push({ x: t.x, y: a2 }, { x: t.x, y: t.y });
  return pts;
}

function retarget(room, now) {
  const L = room.L, slots = { coffee: [...L.coffee], sofa: [...L.sofa] };
  room.sessions.forEach((s, i) => {
    const desk = L.desks[i], t = goal(s, now, desk, L, slots);
    let c = st.chars.get(s.key);
    if (!c) {
      const start = t.pose === "gone" ? t : { x: t.x, y: t.y };
      c = { key: s.key, x: start.x, y: start.y, path: [], look: looks(s.name || s.session_id), pose: t.pose, facing: t.facing };
      st.chars.set(s.key, c);
    }
    c.s = s; c.desk = desk; c.room = room.host;
    const moved = !c.goal || c.goal.x !== t.x || c.goal.y !== t.y;
    c.goal = t; c.mug = t.mug; c.wave = t.wave;
    if (moved && (c.x !== t.x || c.y !== t.y)) {
      if (reduced()) { c.x = t.x; c.y = t.y; c.path = []; } else c.path = route(c, t, L);
    }
  });
}

function stepChar(c) {
  if (!c.path.length) {
    c.pose = c.goal.pose; c.facing = c.goal.facing;
    return;
  }
  const p = c.path[0], dx = p.x - c.x, dy = p.y - c.y, d = Math.hypot(dx, dy), v = 2.5;
  c.pose = "walk";
  c.facing = Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? "left" : "right") : dy < 0 ? "up" : "down";
  if (d <= v) { c.x = p.x; c.y = p.y; c.path.shift(); } else { c.x += (dx / d) * v; c.y += (dy / d) * v; }
}

// ---- frame -----------------------------------------------------------------------
function drawRoom(room, f) {
  const { canvas, S, L, bg } = room;
  if (!canvas.isConnected) return false;
  const g = canvas.getContext("2d"), r = px(g, S);
  g.imageSmoothingEnabled = false;
  g.drawImage(bg, 0, 0);
  const people = room.sessions.map((s) => st.chars.get(s.key)).filter(Boolean);
  for (const c of people) {
    const seated = !c.path.length && (c.goal.pose === "sit" || c.goal.pose === "doze");
    drawScreen(r, c.desk, c.s.activity, f, c.s.status === "busy" && seated);
    if (c.goal.pose === "gone" && !c.path.length) { r(c.desk.x + 17, c.desk.y + 24, 14, 8, c.look.shirt); r(c.desk.x + 17, c.desk.y + 24, 14, 2, "rgba(0,0,0,.15)"); }
  }
  for (const d of L.desks) { r(d.x + 17, d.y + 31, 14, 6, "#5c677d"); }  // chair seats
  people.sort((a, b) => a.y - b.y);
  for (const c of people) {
    if (c.goal.pose === "gone" && !c.path.length) continue;
    if (c.s.source_age_s > 180) g.globalAlpha = 0.45;
    drawPerson(r, c, f);
    g.globalAlpha = 1;
    if (c.pose === "sit" || c.pose === "doze") r(c.desk.x + 16, c.desk.y + 34, 16, 4, "#4c566a");  // chair back over the legs
    if (c.goal.wave && !c.path.length) {  // "!" bubble
      r(c.x - 1, c.y - 12, 14, 9, "#ffffff"); r(c.x + 4, c.y - 3, 3, 2, "#ffffff");
      r(c.x + 5, c.y - 11, 2, 4, "#e8590c"); r(c.x + 5, c.y - 6, 2, 2, "#e8590c");
    }
    if (c.s.activity === "delegate" && c.s.status === "busy" && !c.path.length) { r(c.x + 8, c.y - 9, 10, 6, "#ffffff"); r(c.x + 10, c.y - 7, 1, 1, "#7950f2"); r(c.x + 12, c.y - 7, 1, 1, "#7950f2"); r(c.x + 14, c.y - 7, 1, 1, "#7950f2"); }
    if (c.key === st.sel) { const b = reduced() ? 0 : f % 8 < 4 ? 0 : 1; r(c.x + 4, c.y - 16 - b, 4, 2, "#fab005"); r(c.x + 5, c.y - 14 - b, 2, 2, "#fab005"); }
  }
  // HTML labels: names (crisp CJK text) and wait times
  const k = canvas.clientWidth / canvas.width || 1;
  for (const c of people) {
    const lab = room.labels.get(c.key);
    if (!lab) continue;
    const hidden = c.goal.pose === "gone" && !c.path.length;
    lab.style.display = hidden ? "none" : "";
    lab.style.transform = `translate(${((c.x + 6) * S * k).toFixed(1)}px, ${((c.y + (c.pose === "sleep" ? 12 : 19)) * S * k).toFixed(1)}px)`;
  }
  return true;
}

function tick() {
  st.frame++;
  if (typeof document !== "undefined" && document.hidden) return;
  if (WD.look !== "office") { stop(); return; }
  for (const c of st.chars.values()) if (c.goal) stepChar(c);
  let alive = false;
  for (const room of st.rooms) alive = drawRoom(room, st.frame) || alive;
  if (!alive) stop();
}
function stop() { if (st.timer) { clearInterval(st.timer); st.timer = null; } }

// ---- mount -------------------------------------------------------------------------
// Called on every board render. The room DOM (canvases) is rebuilt only when the
// set of machines or sessions changes, so people keep walking between updates.
export function mountOffice(el, ctx, h) {
  const now = ctx.now;
  if (st.sel && !ctx.live.some((s) => s.key === st.sel)) st.sel = "";
  const hosts = ctx.hosts.map((host) => ({
    host, sessions: ctx.live.filter((s) => s.host === host).sort((a, b) => (a.name || a.session_id).localeCompare(b.name || b.session_id) || a.key.localeCompare(b.key)),
  })).filter((x) => x.sessions.length);
  const width = Math.round((el.clientWidth || 1200) / 100);
  const sig = JSON.stringify([width, hosts.map((x) => [x.host, x.sessions.map((s) => s.key)])]);
  const needs = ctx.live.filter((s) => s.attention.level >= 2);
  const reception = needs.length ? `<b>需要你</b>${needs.map((s) => `<button class="rchip st-${esc(s.status)}" data-desk="${esc(s.key)}">${esc(h.nameOf(s))}<span>${esc(s.attention.reason || word(s.status))}</span></button>`).join("")}` : "";
  const sel = ctx.live.find((s) => s.key === st.sel);
  const names = (x) => x.sessions.map((s) => `<span class="pxname" data-key="${esc(s.key)}"><b>${esc(h.nameOf(s))}</b><i></i></span>`).join("");
  if (typeof el.querySelector !== "function" || typeof document.createElement !== "function") {  // no DOM (tests)
    el.innerHTML = `<div class="pxwrap">${hosts.map((x) => `<section class="pxroom">${names(x)}</section>`).join("")}</div>`;
    return;
  }

  if (st.sig !== sig || !el.querySelector(":scope > .pxwrap")) {
    st.sig = sig;
    el.innerHTML = `<div class="pxwrap"><div class="pxbanner"></div><div class="reception"></div>
      <div class="pxoffice"><div class="pxrooms">${hosts.map((x) => `
        <section class="pxroom" data-host="${esc(x.host)}">${h.hostHeader(x.host, ctx)}
          <div class="pxstage"><div class="pxcanvas"><canvas aria-label="${esc(x.host)} 的辦公室"></canvas><div class="pxlabels">${names(x)}</div></div></div>
        </section>`).join("")}</div><aside class="odetail"></aside></div></div>`;
    st.rooms = hosts.map((x) => {
      const sec = [...el.querySelectorAll(".pxroom")].find((n) => n.dataset.host === x.host);
      const L = layout(x.sessions.length);
      const avail = sec.clientWidth || 900;
      const S = Math.max(1, Math.min(3, Math.floor(avail / L.W) || 1, Math.floor(360 / L.H) || 1));
      const canvas = sec.querySelector("canvas");
      canvas.width = L.W * S; canvas.height = L.H * S;
      canvas.style.width = `${Math.min(L.W * S, avail)}px`;
      const labels = new Map([...sec.querySelectorAll(".pxname")].map((n) => [n.dataset.key, n]));
      return { host: x.host, sessions: x.sessions, L, S, canvas, bg: background(L, S), labels };
    });
    for (const room of st.rooms) room.canvas.addEventListener("click", (ev) => pick(room, ev));
    for (const k of [...st.chars.keys()]) if (!ctx.live.some((s) => s.key === k)) st.chars.delete(k);
  } else {
    for (const room of st.rooms) room.sessions = hosts.find((x) => x.host === room.host)?.sessions || room.sessions;
  }
  el.querySelector(".pxbanner").innerHTML = h.banner || "";
  const rec = el.querySelector(".reception");
  rec.innerHTML = reception; rec.hidden = !reception;
  el.querySelector(".odetail").innerHTML = sel ? h.card(sel, ctx) : `<p class="pxhint">點一個人，看他在做什麼。</p>`;
  for (const room of st.rooms) {
    retarget(room, now);
    for (const s of room.sessions) {
      const lab = room.labels.get(s.key);
      if (!lab) continue;
      const d = dur(s.status_since, now), c = st.chars.get(s.key);
      lab.classList.toggle("wait", s.status === "waiting");
      lab.classList.toggle("on", s.key === st.sel);
      lab.querySelector("i").textContent = s.status === "waiting" ? `等你 ${d.n}${d.u}` : c?.goal?.pose === "gone" ? "" : `${word(s.status)} · ${d.n}${d.u}`;
    }
  }
  if (!el._pxWired && el.addEventListener) {
    el._pxWired = true;
    el.addEventListener("click", (ev) => {
      const b = ev.target.closest?.("[data-desk]");
      if (!b || WD.look !== "office") return;
      select(b.dataset.desk);
    });
  }
  for (const room of st.rooms) drawRoom(room, st.frame);
  if (!st.timer) st.timer = setInterval(tick, TICK_MS);
}

function select(key) {
  st.sel = st.sel === key ? "" : key;
  lsSet("wd.office.sel", st.sel);
  WD.rerender?.();
  if (st.sel && typeof window !== "undefined" && window.innerWidth < 900) setTimeout(() => document.querySelector(".odetail")?.scrollIntoView({ block: "nearest", behavior: "smooth" }), 40);
}

function pick(room, ev) {
  const rect = room.canvas.getBoundingClientRect();
  const x = ((ev.clientX - rect.left) / rect.width) * room.L.W, y = ((ev.clientY - rect.top) / rect.height) * room.L.H;
  let best = null, bd = 1e9;
  room.sessions.forEach((s) => {
    const c = st.chars.get(s.key);
    const cands = [c && c.goal?.pose !== "gone" ? { x: c.x + 6, y: c.y + 8 } : null, { x: c.desk.x + 24, y: c.desk.y + 16 }].filter(Boolean);
    for (const p of cands) { const d = Math.hypot(p.x - x, p.y - y); if (d < bd) { bd = d; best = s.key; } }
  });
  if (best && bd < 26) select(best);
}
