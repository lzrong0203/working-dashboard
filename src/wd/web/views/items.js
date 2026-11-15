// Items view (M1): experiments with their lineage, hypothesis and verdict;
// other work items as a simple list. The lineage graph comes in M2.
import { WD, esc, ago } from "../app.js";

const LINEAGE = new Set(["derived_from", "ablation_of", "variant_of", "scaled_from", "rerun_of"]);
const REL_LABEL = { derived_from: "衍生自", ablation_of: "消融自", variant_of: "變體自", scaled_from: "規模變化自", rerun_of: "重跑自", evaluates: "評估", supersedes: "取代", informed_by: "受啟發於", part_of: "屬於" };
const VERDICT_LABEL = { supported: "成立", refuted: "不成立", inconclusive: "不確定", "n/a": "—" };
const STATUS_LABEL = { planned: "規劃中", running: "進行中", done: "完成", failed: "失敗", abandoned: "放棄", superseded: "被取代" };

function edgeText(it, byId) {
  return (it.edges || []).map((e) => {
    const t = byId.get(e.to);
    return `<span class="edge ${LINEAGE.has(e.type) ? "lineage" : ""}">${esc(REL_LABEL[e.type] || e.type)} <b>${esc(t?.title || e.to)}</b>${e.note ? ` <i>（${esc(e.note)}）</i>` : ""}</span>`;
  }).join("");
}

// depth along lineage, so children indent under parents
function depth(it, byId, seen = new Set()) {
  const p = (it.edges || []).find((e) => LINEAGE.has(e.type));
  if (!p || seen.has(it.id) || !byId.has(p.to)) return 0;
  seen.add(it.id);
  return 1 + depth(byId.get(p.to), byId, seen);
}

function ordered(exps, byId) {
  // parents before children: DFS from roots
  const kids = new Map();
  const roots = [];
  for (const it of exps) {
    const p = (it.edges || []).find((e) => LINEAGE.has(e.type));
    if (p && byId.has(p.to)) (kids.get(p.to) || kids.set(p.to, []).get(p.to)).push(it);
    else roots.push(it);
  }
  const out = [];
  const walk = (it) => { out.push(it); (kids.get(it.id) || []).sort((a, b) => String(a.created).localeCompare(String(b.created))).forEach(walk); };
  roots.sort((a, b) => String(b.updated).localeCompare(String(a.updated))).forEach(walk);
  return out;
}

WD.register({
  id: "items",
  label: "實驗與工作",
  render(el, m) {
    const byId = new Map(m.items.map((it) => [it.id, it]));
    const exps = m.items.filter((it) => it.type === "experiment" || it.type === "eval");
    const other = m.items.filter((it) => !(it.type === "experiment" || it.type === "eval"));
    const rows = ordered(exps, byId).map((it) => `
      <tr class="st-${esc(it.status)} v-${esc(it.verdict || "none")}">
        <td class="t" style="padding-left:${0.6 + depth(it, byId) * 1.4}rem">
          <span class="title">${esc(it.title)}</span>
          <span class="id">${esc(it.slug || it.id)} · ${esc(it.realm)}${it.project ? " · " + esc(it.project) : ""}</span>
          ${edgeText(it, byId)}
        </td>
        <td><span class="chip status">${esc(STATUS_LABEL[it.status] || it.status)}</span></td>
        <td>${it.verdict ? `<span class="chip verdict v-${esc(it.verdict)}">${esc(VERDICT_LABEL[it.verdict] || it.verdict)}</span>` : ""}</td>
        <td class="h">${esc(it.hypothesis || "")}${it.expected ? `<br><small>預期：${esc(it.expected)}</small>` : ""}</td>
        <td class="h">${esc(it.conclusion || "")}</td>
        <td class="n">${(it.sessions || []).length || ""}</td>
      </tr>`).join("");
    el.innerHTML = `
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
