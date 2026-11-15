// Loads the real frontend modules in Node with a tiny DOM stub and checks that
// (1) the module graph finishes loading (a cycle with top-level await would
// hang forever), and (2) both views render the fixture state.
// Run: node tests/web_smoke.mjs
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, "..", "src", "wd", "web");

const els = {};
const el = (id) => (els[id] ??= { id, innerHTML: "", textContent: "", querySelectorAll: () => [] });
globalThis.document = { getElementById: el, title: "", addEventListener: () => {} };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.location = { origin: "http://127.0.0.1:8740" };

const now = Date.now();
const sess = (realm, pid, status, level, extra = {}) => ({
  key: `h/${realm}/${pid}`, realm, host: "h", profile: realm, pid, session_id: `s${pid}`, name: `n${pid}`,
  status, status_since: now - 60000, attention: { level, reason: level >= 3 ? "等你 1 分鐘" : "" },
  source_age_s: 5, linked_items: [], ...extra,
});
const states = {
  "http://127.0.0.1:8740": {
    realm: "personal", hub: { peers: ["http://127.0.0.1:8741"] },
    sessions: [sess("personal", 1, "waiting", 3, { ai_title: "<b>xss?</b>", linked_items: ["a"] }), sess("personal", 2, "busy", 0)],
    sources: [{ host: "h", profile: "personal", age_s: 5 }], events: [], item_errors: [],
    items: [
      { id: "proj", type: "project", title: "Project P", status: "planned" },
      { id: "a", type: "experiment", title: "Exp A", status: "running", edges: [{ type: "derived_from", to: "proj" }] },
      { id: "b", type: "experiment", title: "Exp B", status: "done", verdict: "refuted", edges: [{ type: "ablation_of", to: "a" }] },
      { id: "c1", type: "experiment", title: "Cycle 1", status: "running", edges: [{ type: "derived_from", to: "c2" }] },
      { id: "c2", type: "experiment", title: "Cycle 2", status: "running", edges: [{ type: "derived_from", to: "c1" }] },
    ],
  },
  "http://127.0.0.1:8741": {
    realm: "work", hub: { peers: [] },
    sessions: [sess("work", 9, "idle", 0, { bridge_session_id: "session_w9", account: "me@corp.example" })],
    sources: [{ host: "h", profile: "work", age_s: 5, account: "me@corp.example", account_org: "Corp" }], events: [], item_errors: [],
    items: [{ id: "a", type: "experiment", title: "Work A (same id, other realm)", status: "running", edges: [{ type: "derived_from", to: "b" }] },
            { id: "b", type: "experiment", title: "Work B", status: "running" }],
  },
};
globalThis.fetch = async (url) => {
  if (url === "http://127.0.0.1:8739/config") {
    return { json: async () => ({ ok: true, realms: { work: { profile: "Profile 2", label: "Work Chrome" } } }) };
  }
  const base = url.replace(/\/api\/state$/, "");
  if (!states[base]) throw new Error(`no fixture for ${url}`);
  return { json: async () => structuredClone(states[base]) };
};

const timeout = setTimeout(() => { console.error("FAIL: module graph did not finish loading (import cycle?)"); process.exit(1); }, 3000);
const { render } = await import(pathToFileURL(path.join(web, "app.js")).href);
const { WD } = await import(pathToFileURL(path.join(web, "core.js")).href);
clearTimeout(timeout);
await new Promise((r) => setTimeout(r, 50));

const fail = (msg) => { console.error("FAIL:", msg); process.exit(1); };
const sessionsHtml = els.view.innerHTML;
if (!sessionsHtml.includes("需要你")) fail("sessions view has no 'needs you' section");
if (!sessionsHtml.includes("n9")) fail("work realm session (peer hub) missing");
if (sessionsHtml.includes("<b>xss?</b>")) fail("ai_title not escaped");
if (!sessionsHtml.includes("Exp A")) fail("linked item title missing");
if (!els.counts.innerHTML.includes("<b>1</b>")) fail("urgent count wrong");
if (!sessionsHtml.includes("me@corp")) fail("account not shown on host header");
await new Promise((r) => setTimeout(r, 50)); // opener probe resolves, re-render
render();
await new Promise((r) => setTimeout(r, 20));
if (!/data-open-realm="work"[^>]*>開啟 ↗ <small>Work Chrome/.test(els.view.innerHTML)) fail("work open link not routed to its browser profile");

WD.active = "items";
render();
await new Promise((r) => setTimeout(r, 20));
const itemsHtml = els.view.innerHTML;
for (const t of ["Exp A", "Exp B", "Cycle 1", "Cycle 2", "Work A (same id, other realm)", "Work B"]) {
  if (!itemsHtml.includes(t)) fail(`items view lost ${t}`);
}
console.log("web smoke: ok");
process.exit(0);
