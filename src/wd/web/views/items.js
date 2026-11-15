// Items view (M1): experiments with their lineage, hypothesis and verdict;
// other work items as a simple list. The lineage graph comes in M2.
import { WD, esc, ago, ikey } from "../core.js";

const LINEAGE = new Set(["derived_from", "ablation_of", "variant_of", "scaled_from", "rerun_of"]);
const REL_LABEL = { derived_from: "衍生自", ablation_of: "消融自", variant_of: "變體自", scaled_from: "規模變化自", rerun_of: "重跑自", evaluates: "評估", supersedes: "取代", informed_by: "受啟發於", part_of: "屬於" };
const VERDICT_LABEL = { supported: "成立", refuted: "不成立", inconclusive: "不確定", "n/a": "—" };
const STATUS_LABEL = { planned: "規劃中", running: "進行中", done: "完成", failed: "失敗", abandoned: "放棄", superseded: "被取代" };
const isExp = (it) => it.type === "experiment" || it.type === "eval";
const edgesOf = (it) => (Array.isArray(it.edges) ? it.edges.filter((e) => e && typeof e === "object") : []);
const lineageOf = (it) => edgesOf(it).find((e) => LINEAGE.has(e.type));

function edgeText(it, byKey) {
  return edgesOf(it).map((e) => {
    const t = byKey.get(ikey(it.realm, e.to));
    return `<span class="edge ${LINEAGE.has(e.type) ? "lineage" : ""}">${esc(REL_LABEL[e.type] || e.type)} <b>${esc(t?.title || e.to)}</b>${e.note ? ` <i>（${esc(e.note)}）</i>` : ""}</span>`;
  }).join("");
}

// Parents before children (DFS from roots), each row with its depth. A child
// whose lineage parent isn't shown here (other type, other realm, missing)
// becomes a root, so nothing disappears. `seen` guards against cycles from
// hand-edited cards.
function ordered(rows) {
  const shown = new Map(rows.map((it) => [ikey(it.realm, it.id), it]));
  const kids = new Map();
  const roots = [];
  for (const it of rows) {
    const p = lineageOf(it);
    const pk = p && ikey(it.realm, p.to);
    if (pk && shown.has(pk) && pk !== ikey(it.realm, it.id)) {
      if (!kids.has(pk)) kids.set(pk, []);
      kids.get(pk).push(it);
    } else roots.push(it);
  }
  const out = [];
  const seen = new Set();
  const walk = (it, depth) => {
    const k = ikey(it.realm, it.id);
    if (seen.has(k)) return;
    seen.add(k);
    out.push({ it, depth });
    (kids.get(k) || []).sort((a, b) => String(a.created).localeCompare(String(b.created))).forEach((c) => walk(c, depth + 1));
  };
  roots.sort((a, b) => String(b.updated).localeCompare(String(a.updated))).forEach((r) => walk(r, 0));
  for (const it of rows) walk(it, 0); // anything only reachable through a cycle
  return out;
}

WD.register({
  id: "items",
  label: "實驗與工作",
  render(el, m) {
    const byKey = new Map(m.items.map((it) => [ikey(it.realm, it.id), it]));
    const exps = m.items.filter(isExp);
    const other = m.items.filter((it) => !isExp(it));
    const rows = ordered(exps).map(({ it, depth }) => `
      <tr class="st-${esc(it.status)} v-${esc(it.verdict || "none")}">
        <td class="t" style="padding-left:${0.6 + Math.min(depth, 12) * 1.4}rem">
          <span class="title">${esc(it.title)}</span>
          <span class="id">${esc(it.slug || it.id)} · ${esc(it.realm)}${it.project ? " · " + esc(it.project) : ""}</span>
          ${edgeText(it, byKey)}
        </td>
        <td><span class="chip status">${esc(STATUS_LABEL[it.status] || it.status)}</span></td>
        <td>${it.verdict ? `<span class="chip verdict v-${esc(it.verdict)}">${esc(VERDICT_LABEL[it.verdict] || it.verdict)}</span>` : ""}</td>
        <td class="h">${esc(it.hypothesis || "")}${it.expected ? `<br><small>預期：${esc(it.expected)}</small>` : ""}</td>
        <td class="h">${esc(it.conclusion || "")}</td>
        <td class="n">${Array.isArray(it.sessions) && it.sessions.length ? it.sessions.length : ""}</td>
      </tr>`).join("");
    el.innerHTML = `
      ${m.itemErrors.length ? `<p class="banner">有 ${m.itemErrors.length} 張卡片讀不進來：${m.itemErrors.map(esc).join("；")}</p>` : ""}
      <section class="exps">
        <h2>實驗 <small>${exps.length} 個 · 用 <code>wd exp new</code> 記錄假設，<code>wd exp close</code> 記錄結論</small></h2>
        ${exps.length ? `<div class="tablewrap"><table>
          <thead><tr><th>實驗與譜系</th><th>狀態</th><th>結論</th><th>假設</th><th>結果說明</th><th>Sessions</th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : `<p class="empty">還沒有實驗卡片。</p>`}
      </section>
      ${other.length ? `<section class="others"><h2>其他工作</h2><ul>${other.map((it) => `<li><span class="chip">${esc(it.type)}</span> ${esc(it.title)} <small>${esc(STATUS_LABEL[it.status] || it.status)} · ${esc(ago(Date.parse(it.updated)))}前</small></li>`).join("")}</ul></section>` : ""}
    `;
  },
});
