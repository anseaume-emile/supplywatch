"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtN = v => v == null ? "—" : Number(v).toLocaleString("de-CH");
const fmtLead = d => d == null ? "—" : (d >= 14 ? `${Math.round(d / 7)} wk` : `${d} d`);
const fmtDate = s => s ? new Date(s).toLocaleString("de-CH", {dateStyle: "medium", timeStyle: "short"}) : "—";
const fmtPrice = bp => bp ? `${Number(bp.price).toLocaleString("de-CH", {maximumFractionDigits: 4})} ${esc(bp.currency)}` : "—";

let DATA = null, STATUS = null;
const ui = {q: "", project: "", flag: "", coo: "", sort: "risk", dir: 1};
try { Object.assign(ui, JSON.parse(localStorage.getItem("sw-ui") || "{}")); } catch (e) {}
const saveUi = () => { try { localStorage.setItem("sw-ui", JSON.stringify(ui)); } catch (e) {} };

async function api(path, opts = {}) {
  const o = {...opts, headers: {"X-Requested-With": "supplywatch", ...(opts.headers || {})}};
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => t.hidden = true, 4000); }

const chips = flags => `<div class="chips">${flags.map(f => {
  const d = DATA.flags[f] || {label: f, level: "info", help: ""};
  return `<span class="chip ${d.level}" title="${esc(d.help)}">${esc(d.label)}</span>`;
}).join("")}</div>`;
const normMpn = m => String(m || "").toUpperCase().replace(/\s+/g, "");  // mirrors db.norm_mpn
const expanded = new Set();  // disclosure groups opened in this session ("part:<key>", "line:<project>:<i>")
const disclose = (id, n) => n ? `<button class="disclose" data-expand="${esc(id)}" aria-expanded="${expanded.has(id)}">${n} alternate${n === 1 ? "" : "s"}</button>` : "";
const NO_COO = "__none";  // ui.coo value for parts with no known country of origin
const projName = id => { const p = DATA.projects.find(x => String(x.id) === String(id)); return p ? p.name + (p.variant && p.variant !== "[No Variations]" ? ` · ${p.variant}` : "") : id; };

function coverage(p) {
  if (p.stock == null || !p.required) return "";
  const r = p.stock / p.required;
  const cls = r < 1 ? "critical" : r < DATA.thresholds.low_stock_multiple ? "warning" : "";
  return `<div class="bar" title="${r.toFixed(1)}× demand"><i class="${cls}" style="width:${Math.min(100, r / DATA.thresholds.low_stock_multiple * 100)}%"></i></div>`;
}

// ------------------------------------------------------------------------------ views
function viewOverview() {
  const active = DATA.projects.filter(p => p.active);
  const parts = DATA.parts.filter(p => !p.is_alternate_only);
  const count = f => parts.filter(p => p.flags.includes(f)).length;
  const crit = parts.filter(p => p.flags.some(f => DATA.flags[f].level === "critical"));
  const withCoo = parts.filter(p => p.coo).length;
  const u = STATUS.nexar_usage;
  const coo = {}; parts.forEach(p => { const k = p.coo || "Unknown"; coo[k] = (coo[k] || 0) + 1; });
  const cooRows = Object.entries(coo).sort((a, b) => (a[0] === "Unknown") - (b[0] === "Unknown") || b[1] - a[1]);
  const buckets = [["Under 4 weeks", d => d < 28], ["4–12 weeks", d => d >= 28 && d < 84], ["12–26 weeks", d => d >= 84 && d < 182], ["26 weeks or more", d => d >= 182]];
  const lead = buckets.map(([l, fn]) => [l, parts.filter(p => p.lead_days != null && fn(p.lead_days)).length]);
  lead.push(["Unknown", parts.filter(p => p.lead_days == null).length]);
  const max = Math.max(1, ...cooRows.map(r => r[1]), ...lead.map(r => r[1]));
  const hbar = (rows, coo) => rows.map(([k, v]) => {
    const inner = `<span>${esc(k)}</span><div class="bar"><i style="width:${v / max * 100}%"></i></div><span class="num">${v}</span>`;
    return coo ? `<a class="hbar" href="#parts" data-coo="${esc(k === "Unknown" ? NO_COO : k)}" title="List ${esc(k)} parts">${inner}</a>` : `<div class="hbar">${inner}</div>`;
  }).join("");
  const last = STATUS.runs.find(r => r.kind === "supply_refresh" && r.status !== "running");
  return `
  <div class="row spread"><h1>Overview</h1><span class="muted small">Supply data ${last ? "refreshed " + fmtDate(last.finished) : "not refreshed yet"} · <a href="/api/export/parts.csv">Export parts (CSV)</a></span></div>
  <div class="tiles">
    <a class="tile" href="#projects"><b>${active.length}</b><span>Active projects</span></a>
    <a class="tile" href="#parts"><b>${parts.length}</b><span>Unique parts</span></a>
    <a class="tile crit" href="#parts" data-flag="shortage"><b>${count("shortage")}</b><span>Shortages</span></a>
    <a class="tile crit" href="#parts" data-flag="lifecycle"><b>${count("lifecycle")}</b><span>Lifecycle risk</span></a>
    <a class="tile warn" href="#parts" data-flag="long_lead"><b>${count("long_lead")}</b><span>Lead ≥ ${Math.round(DATA.thresholds.long_lead_days / 7)} weeks</span></a>
    <a class="tile" href="#parts" data-flag="no_coo"><b>${parts.length ? Math.round(withCoo / parts.length * 100) : 0}%</b><span>COO known</span></a>
    ${STATUS.nexar_supply.enabled ? `<a class="tile" href="#admin"><b>${fmtN(u.parts)} <small>/ ${fmtN(u.budget)}</small></b><span>Octopart quota ${esc(u.month)}</span></a>` : ""}
  </div>
  <div class="grid2">
    <section class="panel"><h2>Needs attention</h2>${crit.length ? `<div class="tablebox"><table><thead><tr><th>MPN</th><th class="num">Required</th><th class="num">Stock</th><th>Issues</th></tr></thead><tbody>
      ${crit.slice(0, 12).map(p => `<tr class="link" data-part="${esc(p.key)}"><td><div class="mono">${esc(p.mpn)}</div><div class="muted small">${esc(p.manufacturer)}</div></td><td class="num">${fmtN(p.required)}</td><td class="num">${fmtN(p.stock)}</td><td>${chips(p.flags.filter(f => DATA.flags[f].level === "critical"))}</td></tr>`).join("")}
      </tbody></table></div>${crit.length > 12 ? `<a href="#parts" data-flag="critical" class="small">All ${crit.length} parts with critical issues</a>` : ""}` : `<p class="muted">No critical issues across active projects.</p>`}</section>
    <section class="panel"><h2>Lead time</h2>${hbar(lead)}<p class="muted small">Best known factory lead time per part.</p>
      <h2>Country of origin</h2>${hbar(cooRows, true)}<p class="muted small">Click a country to list its parts.</p></section>
  </div>`;
}

// Top-level rows are BOM parts (first choice on a line); approved alternates hang under them, like Altium's ActiveBOM.
const scopedParts = () => DATA.parts.filter(p => !p.is_alternate_only && (!ui.project || p.used_in[ui.project] != null));
const partByKey = () => Object.fromEntries(DATA.parts.map(x => [x.key, x]));

function filteredParts() {
  const q = ui.q.toLowerCase(), byKey = partByKey();
  let rows = scopedParts().filter(p => (!ui.coo || (ui.coo === NO_COO ? !p.coo : p.coo === ui.coo))
    && (!ui.flag || (ui.flag === "critical" ? p.flags.some(f => DATA.flags[f].level === "critical") : p.flags.includes(ui.flag)))
    && (!q || [p.mpn, p.manufacturer, p.description, p.coo, ...p.lines, ...p.alternates.map(k => byKey[k]?.mpn || k)].join(" ").toLowerCase().includes(q)));
  const sev = p => p.flags.some(f => DATA.flags[f].level === "critical") ? 0 : p.flags.some(f => DATA.flags[f].level === "warning") ? 1 : 2;
  const key = {risk: p => [sev(p), p.mpn], mpn: p => [p.mpn], required: p => [-p.required], stock: p => [p.stock ?? -1],
    lead: p => [-(p.lead_days ?? -1)], coo: p => [p.coo || "~"], suppliers: p => [p.suppliers]}[ui.sort] || (p => [p.mpn]);
  rows.sort((a, b) => { const x = key(a), y = key(b); for (let i = 0; i < x.length; i++) { if (x[i] < y[i]) return -ui.dir; if (x[i] > y[i]) return ui.dir; } return 0; });
  return rows;
}

// One Parts-table row. With `primary`, `p` is shown as that part's alternate: indented, judged against its demand.
// RoHS / REACH: labels come from supplywatch/compliance.py; anything else is a raw value no rule recognised.
const COMPLIANCE = {
  "Compliant": ["okc", "✓"], "Compliant (exemption)": ["info", "exempt"],
  "Not compliant": ["critical", "✗"], "Contains SVHC": ["warning", "SVHC"],
};
function complianceChip(kind, status, source = "") {
  if (!status) return `<span class="chip soft" title="${kind}: unknown">${kind} ?</span>`;
  const [cls, short] = COMPLIANCE[status] || ["info", "?"];
  return `<span class="chip ${cls}" title="${esc(`${kind}: ${status}${source ? ` (from ${source})` : ""}`)}">${kind} ${short}</span>`;
}
const complianceText = (status, source) => status
  ? `<span class="chip ${(COMPLIANCE[status] || ["info"])[0]}">${esc(status)}</span> ${source ? `<span class="muted small">from ${esc(source)}</span>` : ""}`
  : `<span class="muted">Unknown</span>`;

function partRow(p, primary = null, nAlts = 0) {
  const req = primary ? primary.required : p.required, grp = `part:${primary ? primary.key : p.key}`;
  return `<tr class="link${primary ? " alt" : ""}" data-part="${esc(p.key)}" ${primary ? `data-group="${esc(grp)}" ${expanded.has(grp) ? "" : "hidden"}` : ""}>
    <td>${primary ? `<span class="chip info">Alternate</span> ` : ""}<span class="mono">${esc(p.mpn)}</span><div class="muted small">${esc(p.manufacturer)}</div>${disclose(primary ? "" : grp, nAlts)}</td>
    <td class="small">${primary ? `<span class="muted">for ${esc(primary.mpn)}</span>` : Object.entries(p.used_in).map(([id, q]) => `${esc(projName(id))} <span class="muted">×${q}</span>`).join("<br>") || "—"}</td>
    <td class="num">${primary ? `<span class="muted">${fmtN(req)}</span>` : fmtN(req)}</td><td class="num">${fmtN(p.stock)}</td><td>${coverage({stock: p.stock, required: req})}</td>
    <td class="num">${p.suppliers}</td><td class="num" title="${esc(p.lead_source)}">${fmtLead(p.lead_days)}</td>
    <td class="small">${esc(p.lifecycle || "—")}</td>
    <td class="small">${esc(p.coo || "—")}${p.coo_source === "manual" ? ` <span class="muted">(manual)</span>` : ""}</td>
    <td><div class="chips">${complianceChip("RoHS", p.rohs, p.rohs_source)}${complianceChip("REACH", p.reach, p.reach_source)}</div></td>
    <td class="num small">${fmtPrice(p.best_price)}</td><td>${chips(primary ? p.flags.filter(f => f !== "single_source") : p.flags)}</td></tr>`;
}

function viewParts() {
  const opts = DATA.projects.filter(p => p.active).map(p => `<option value="${p.id}" ${String(ui.project) === String(p.id) ? "selected" : ""}>${esc(projName(p.id))}</option>`).join("");
  const fopts = [["", "All parts"], ["critical", "Any critical issue"], ...Object.entries(DATA.flags).filter(([k]) => k !== "no_mpn").map(([k, v]) => [k, v.label])]
    .map(([k, l]) => `<option value="${k}" ${ui.flag === k ? "selected" : ""}>${esc(l)}</option>`).join("");
  const cooCount = {}; let noCoo = 0;
  scopedParts().forEach(p => p.coo ? cooCount[p.coo] = (cooCount[p.coo] || 0) + 1 : noCoo++);
  if (ui.coo && ui.coo !== NO_COO && !(ui.coo in cooCount)) cooCount[ui.coo] = 0;
  const copts = [["", "All countries"], ...Object.keys(cooCount).sort().map(k => [k, `${k} (${cooCount[k]})`]), [NO_COO, `Unknown (${noCoo})`]]
    .map(([k, l]) => `<option value="${esc(k)}" ${ui.coo === k ? "selected" : ""}>${esc(l)}</option>`).join("");
  const th = (k, l, cls = "") => `<th class="${cls}"><button data-sort="${k}">${l}${ui.sort === k ? (ui.dir > 0 ? " ↑" : " ↓") : ""}</button></th>`;
  const rows = filteredParts(), byKey = partByKey();
  const altsOf = p => p.alternates.map(k => byKey[k]).filter(Boolean);
  const nAlts = rows.reduce((n, p) => n + altsOf(p).length, 0);
  return `
  <div class="row spread"><h1>Parts</h1><a class="btn" href="/api/export/parts.csv">Export CSV</a></div>
  <div class="row">
    <input type="search" id="f-q" placeholder="Search MPN, manufacturer, CMP number, COO…" value="${esc(ui.q)}" aria-label="Search" style="min-width:260px">
    <select id="f-project" aria-label="Project"><option value="">All active projects</option>${opts}</select>
    <select id="f-flag" aria-label="Issue">${fopts}</select>
    <select id="f-coo" aria-label="Country of origin">${copts}</select>
    ${nAlts ? `<button class="btn small" id="f-alts">${rows.some(p => altsOf(p).length && !expanded.has(`part:${p.key}`)) ? "Expand" : "Collapse"} all alternates</button>` : ""}
    <span class="muted small">${rows.length} part${rows.length === 1 ? "" : "s"}${nAlts ? ` · ${nAlts} alternate${nAlts === 1 ? "" : "s"}` : ""}</span>
  </div>
  <div class="tablebox">${rows.length ? `<table><thead><tr>
    ${th("mpn", "Part")}<th>Used in</th>${th("required", "Required", "num")}${th("stock", "Auth. stock", "num")}<th>Coverage</th>
    ${th("suppliers", "Suppliers", "num")}${th("lead", "Lead", "num")}<th>Lifecycle</th>${th("coo", "COO")}<th>RoHS / REACH</th><th class="num">Best price</th>${th("risk", "Issues")}
  </tr></thead><tbody>${rows.map(p => { const al = altsOf(p); return partRow(p, null, al.length) + al.map(x => partRow(x, p)).join(""); }).join("")}</tbody></table>`
    : `<div class="empty">${DATA.parts.length ? "No parts match these filters." : "No parts yet. Run a BOM sync on the Admin page, or upload a BOM there."}</div>`}</div>`;
}

async function viewPart(key) {
  const d = await api(`/api/parts/${encodeURIComponent(key)}`);
  PART_HISTORY = d.stock_history || [];
  const p = d.part, cur = DATA.currency;
  const src = d.sources || {};
  const srcRows = Object.entries(src).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v.lifecycle || "—")}</td><td class="num">${fmtLead(v.lead_days)}</td><td>${esc(v.coo || "—")}</td><td class="mono">${esc(v.hts || "—")}</td><td class="mono">${esc(v.eccn || "—")}</td><td class="small">${esc(v.rohs || "—")}</td><td class="small">${esc(v.reach || "—")}</td></tr>`).join("");
  const price = o => (o.prices || []).slice(0, 6).map(x => `${fmtN(x.qty)}: ${Number(x.price).toLocaleString("de-CH", {maximumFractionDigits: 4})}`).join("<br>") + (o.prices?.length ? ` <span class="muted">${esc(o.prices[0].currency || cur)}</span>` : "");
  return `
  <div class="row"><a href="#parts" class="small">← Parts</a></div>
  <div class="row spread"><div><h1 class="mono" style="font-size:20px">${esc(p.mpn)}</h1><div class="muted">${esc(p.manufacturer || "")} · ${esc(p.description || "")}</div></div>
    <div class="row">${d.octopart_url ? `<a class="btn" href="${esc(d.octopart_url)}" target="_blank" rel="noopener">Octopart</a>` : ""}${d.datasheet ? `<a class="btn" href="${esc(d.datasheet)}" target="_blank" rel="noopener">Datasheet</a>` : ""}<button class="btn" id="refresh-part">Refresh this part</button></div></div>
  ${p.flags?.length ? chips(p.flags) : ""}
  <div class="grid2">
    <section class="panel"><h2>Demand</h2><dl class="kv">
      <dt>Required</dt><dd>${fmtN(p.required)} <span class="muted small">(${fmtN(p.per_board)} per board set)</span></dd>
      <dt>Used in</dt><dd>${Object.entries(p.used_in || {}).map(([id, q]) => `<a href="#project/${id}">${esc(projName(id))}</a> ×${q} · build ${d.projects[id]?.build_qty ?? "?"}`).join("<br>") || "Alternate only"}</dd>
      ${p.alternate_for?.length ? `<dt>Alternate for</dt><dd class="mono">${p.alternate_for.map(m => `<a href="#part/${encodeURIComponent(m)}">${esc(m)}</a>`).join(", ")}</dd>` : ""}
      <dt>CMP numbers</dt><dd class="mono">${esc((p.lines || []).join(", ") || "—")}</dd>
      <dt>Authorised stock</dt><dd>${fmtN(p.stock)} across ${p.suppliers ?? 0} supplier(s)</dd>
      <dt>Lead time</dt><dd>${fmtLead(p.lead_days)} ${p.lead_source ? `<span class="muted small">from ${esc(p.lead_source)}</span>` : ""}</dd>
      <dt>Lifecycle</dt><dd>${esc(p.lifecycle || "—")}</dd>
      <dt>HTS / ECCN</dt><dd class="mono">${esc(p.hts || "—")} / ${esc(p.eccn || "—")}</dd>
      <dt>RoHS</dt><dd>${complianceText(p.rohs, p.rohs_source)}</dd>
      <dt>REACH</dt><dd>${complianceText(p.reach, p.reach_source)}</dd>
      <dt>Refreshed</dt><dd>${fmtDate(p.refreshed_at)}</dd></dl></section>
    <section class="panel"><h2>Country of origin</h2>
      <p>${p.coo ? `<b>${esc(p.coo)}</b> <span class="muted small">${p.coo_source === "manual" ? "entered manually" : "from " + esc(p.coo_source)}</span>` : `<span class="muted">Unknown. Distributor APIs rarely publish it; enter it from the supplier's CoC or datasheet.</span>`}</p>
      <form id="coo-form" class="row">
        <input type="text" id="coo" placeholder="e.g. Japan, Malaysia" value="${esc(d.coo_override?.coo || "")}" aria-label="Country of origin" maxlength="100">
        <input type="text" id="coo-note" placeholder="Source or note (optional)" value="${esc(d.coo_override?.note || "")}" aria-label="Note" style="flex:1;min-width:180px" maxlength="1000">
        <button class="btn primary" type="submit">Save</button>
      </form>
      ${d.coo_override ? `<p class="muted small">Set by ${esc(d.coo_override.by)} on ${fmtDate(d.coo_override.at)}. Clear the field and save to remove it.</p>` : ""}
      ${srcRows ? `<h2>By data source</h2><div class="tablebox"><table><thead><tr><th>Source</th><th>Lifecycle</th><th class="num">Lead</th><th>COO</th><th>HTS</th><th>ECCN</th><th>RoHS</th><th>REACH</th></tr></thead><tbody>${srcRows}</tbody></table></div>` : ""}
    </section>
  </div>
  ${alternatesPanel(p, d.alternates || [])}
  ${stockHistoryPanel()}
  <section class="panel"><h2>Suppliers and offers</h2>
  ${d.offers.length ? `
    ${offerGroup("Trusted sources", "Authorised distributors of the manufacturer.", d.offers.filter(o => o.authorized === true), price)}
    ${offerGroup("Non-trusted sources", "Brokers and sellers that are not authorised, or whose authorisation is unknown. Their stock does not count towards authorised stock.", d.offers.filter(o => o.authorized !== true), price)}`
    : `<div class="empty">No offers yet. Run a supply refresh.</div>`}
  ${d.offers.some(o => o.source === "trustedparts") ? `<p class="muted small">Distributor data partly from <a href="https://www.trustedparts.com" target="_blank" rel="noopener">TrustedParts.com</a> (ECIA authorised distributors).</p>` : ""}</section>`;
}

const WAREHOUSE_BASIS = {
  api: "Warehouse reported by the source",
  typical: "Distributor's main distribution centre (not reported per offer)",
  seller_country: "Seller's home country; the shipping warehouse may differ",
};
function warehouseCell(o) {
  if (!o.warehouse) return `<span class="muted">—</span>`;
  const note = o.warehouse_basis === "api" ? "" : ` <span class="muted small">${o.warehouse_basis === "typical" ? "(typical)" : "(seller country)"}</span>`;
  return `<span title="${esc(WAREHOUSE_BASIS[o.warehouse_basis] || "")}">${esc(o.warehouse)}</span>${note}`;
}

function offerGroup(title, hint, offers, price) {
  return `<div class="row spread"><h3>${title} <span class="muted">${offers.length}</span></h3><span class="muted small">${hint}</span></div>
  <div class="tablebox">${offers.length ? `<table><thead><tr><th>Supplier</th><th>Warehouse</th><th>SKU</th><th class="num">Stock</th><th class="num">On order</th><th class="num">MOQ</th><th class="num">Lead</th><th>Packaging</th><th class="num">Price breaks</th><th>Source</th><th>Updated</th></tr></thead><tbody>
    ${offers.map(o => `<tr><td>${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.seller)}</a>` : esc(o.seller)}${o.authorized === false ? ` <span class="chip warning">Not authorised</span>` : o.authorized == null ? ` <span class="chip info">Authorisation unknown</span>` : ""}</td>
      <td class="small">${warehouseCell(o)}</td>
      <td class="mono">${esc(o.sku || "—")}</td><td class="num">${o.stock == null ? `<span class="muted small">${esc(o.stock_note || "—")}</span>` : fmtN(o.stock)}</td><td class="num">${fmtN(o.on_order)}</td><td class="num">${fmtN(o.moq)}</td>
      <td class="num">${fmtLead(o.lead_days)}</td><td class="small">${esc(o.packaging || "—")}</td><td class="num small">${price(o) || "—"}</td><td class="small muted">${esc(o.source)}</td><td class="small muted">${fmtDate(o.updated)}</td></tr>`).join("")}
  </tbody></table>` : `<div class="empty">None.</div>`}</div>`;
}

// ------------------------------------------------------------------------------ stock history
// One line per region; a snapshot is written at every supply refresh. A region missing from a snapshot had no stock.
const REGIONS = [["Americas", "--s1"], ["Europe", "--s2"], ["Asia-Pacific", "--s3"], ["Rest of world", "--s4"], ["Unlocated", "--s-none"]];
let PART_HISTORY = [];
let historyScope = (() => { try { return localStorage.getItem("sw-history") || "trusted"; } catch (e) { return "trusted"; } })();
const fmtCompact = v => Math.abs(v) >= 1e6 ? `${+(v / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e4 ? `${Math.round(v / 1e3)}K` : Math.abs(v) >= 1e3 ? `${+(v / 1e3).toFixed(1)}K` : String(v);
const fmtDay = t => new Date(t).toLocaleDateString("de-CH", {day: "numeric", month: "short"});

function historySeries() {
  const byAt = new Map();
  for (const r of PART_HISTORY) {
    if (!byAt.has(r.at)) byAt.set(r.at, {});  // every snapshot keeps its timestamp, so lines drop to zero when stock goes
    if (historyScope === "trusted" && !r.trusted) continue;
    const m = byAt.get(r.at);
    m[r.region] = (m[r.region] || 0) + r.stock;
  }
  const times = [...byAt.keys()].sort();
  const regions = REGIONS.filter(([name]) => times.some(t => byAt.get(t)[name] > 0));
  return {times, regions, value: (t, name) => byAt.get(t)[name] || 0};
}

function stockHistoryPanel() {
  const {times, regions, value} = historySeries();
  const scopeBtn = (k, label) => `<button class="seg-btn" data-history="${k}" aria-pressed="${historyScope === k}">${label}</button>`;
  const body = times.length < 2
    ? `<div class="empty">${times.length ? "One snapshot so far. The chart appears after the next supply refresh." : "No history yet. A snapshot is recorded at every supply refresh."}</div>`
    : !regions.length ? `<div class="empty">No stock in any snapshot${historyScope === "trusted" ? " from trusted sources" : ""}.</div>`
    : `<div class="legend" aria-hidden="true">${regions.map(([n, c]) => `<span><i style="background:var(${c})"></i>${esc(n)}</span>`).join("")}</div>
       <div class="chart" id="stock-chart" role="img" aria-label="Stock by region over time; the table below lists the values"></div>`;
  const table = times.length && regions.length ? `<details class="small"><summary>Show as table</summary><div class="tablebox"><table>
    <thead><tr><th>Snapshot</th>${regions.map(([n]) => `<th class="num">${esc(n)}</th>`).join("")}<th class="num">Total</th></tr></thead><tbody>
    ${times.slice().reverse().map(t => `<tr><td>${fmtDate(t)}</td>${regions.map(([n]) => `<td class="num">${fmtN(value(t, n))}</td>`).join("")}<td class="num"><b>${fmtN(regions.reduce((a, [n]) => a + value(t, n), 0))}</b></td></tr>`).join("")}
    </tbody></table></div></details>` : "";
  return `<section class="panel" id="stock-history"><div class="row spread"><h2>Stock history by region</h2>
    <div class="seg" role="group" aria-label="Sellers included">${scopeBtn("trusted", "Trusted sources")}${scopeBtn("all", "All sources")}</div></div>
    <p class="muted small">Stock at each supply refresh, by warehouse region. Where a source doesn't report the warehouse, the distributor's main distribution centre or the seller's country is used.</p>
    ${body}${table}</section>`;
}

function renderStockChart() {
  const box = $("#stock-chart");
  if (!box) return;
  const {times, regions, value} = historySeries();
  const css = getComputedStyle(box), color = v => css.getPropertyValue(v).trim();
  const W = Math.max(box.clientWidth, 280), H = 240, m = {l: 48, r: 104, t: 12, b: 26};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const ts = times.map(t => Date.parse(t)), t0 = ts[0], t1 = ts[ts.length - 1];
  const max = Math.max(1, ...times.flatMap(t => regions.map(([n]) => value(t, n))));
  const raw = max / 4, mag = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 2.5, 5, 10].map(k => k * mag).find(s => s >= raw));
  const top = Math.ceil(max / step) * step;
  const x = t => m.l + (t1 === t0 ? iw / 2 : (t - t0) / (t1 - t0) * iw), y = v => m.t + ih - v / top * ih;
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, parent) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent?.appendChild(e); return e; };
  const svg = el("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`});
  for (let v = 0; v <= top; v += step) {
    el("line", {x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), class: v ? "grid" : "axis"}, svg);
    el("text", {x: m.l - 8, y: y(v) + 4, "text-anchor": "end", class: "tick"}, svg).textContent = fmtCompact(v);
  }
  const nx = Math.min(iw < 360 ? 3 : 5, ts.length), seen = new Set();
  for (let i = 0; i < nx; i++) {
    const t = t0 + (t1 - t0) * (nx === 1 ? 0 : i / (nx - 1)), label = fmtDay(t);
    if (seen.has(label)) continue;
    seen.add(label);
    el("text", {x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === nx - 1 ? "end" : "middle", class: "tick"}, svg).textContent = label;
  }
  const dots = ts.length <= 40, ends = [];
  for (const [name, v] of regions) {
    const c = color(v), pts = times.map((t, i) => [x(ts[i]), y(value(t, name))]);
    el("polyline", {points: pts.map(p => p.join(",")).join(" "), fill: "none", stroke: c, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
    if (dots) pts.forEach(([px, py]) => el("circle", {cx: px, cy: py, r: 4, fill: c, class: "dot"}, svg));
    ends.push({name, y: pts[pts.length - 1][1], v: value(times[times.length - 1], name)});
  }
  // direct end labels, largest first, skipping any that would collide; the legend and tooltip carry the rest
  const kept = [];
  ends.sort((a, b) => b.v - a.v).forEach(e => { if (kept.every(k => Math.abs(k.y - e.y) >= 14)) kept.push(e); });
  kept.forEach(e => { el("text", {x: m.l + iw + 8, y: e.y + 4, class: "endlabel"}, svg).textContent = `${e.name} ${fmtCompact(e.v)}`; });
  const hair = el("line", {y1: m.t, y2: m.t + ih, class: "hair", visibility: "hidden"}, svg);
  const hit = el("rect", {x: m.l - 12, y: 0, width: iw + 24, height: H, fill: "transparent", tabindex: 0, focusable: "true",
                          "aria-label": "Snapshots: use the arrow keys to read values"}, svg);
  const tip = document.createElement("div");
  tip.className = "charttip";
  tip.hidden = true;
  box.replaceChildren(svg, tip);
  let cur = -1;
  const show = i => {
    cur = i;
    const px = x(ts[i]);
    hair.setAttribute("x1", px); hair.setAttribute("x2", px); hair.setAttribute("visibility", "visible");
    tip.replaceChildren();
    const head = document.createElement("div"); head.className = "muted"; head.textContent = fmtDate(times[i]); tip.appendChild(head);
    const row = (key, val, label) => {
      const r = document.createElement("div"); r.className = "tiprow";
      const k = document.createElement("i"); if (key) k.style.background = key; else k.style.visibility = "hidden";
      const b = document.createElement("b"); b.textContent = fmtN(val);
      const s = document.createElement("span"); s.className = "muted"; s.textContent = label;
      r.append(k, b, s); tip.appendChild(r);
    };
    regions.forEach(([n, v]) => row(color(v), value(times[i], n), n));
    if (regions.length > 1) row(null, regions.reduce((a, [n]) => a + value(times[i], n), 0), "Total");
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${px + 12 + tw > W ? Math.max(0, px - 12 - tw) : px + 12}px`;
    tip.style.top = `${m.t}px`;
  };
  const hide = () => { hair.setAttribute("visibility", "hidden"); tip.hidden = true; };
  const nearest = px => ts.reduce((best, t, i) => Math.abs(x(t) - px) < Math.abs(x(ts[best]) - px) ? i : best, 0);
  hit.addEventListener("pointermove", e => show(nearest(e.clientX - svg.getBoundingClientRect().left)));
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("blur", hide);
  hit.addEventListener("focus", () => show(cur < 0 ? ts.length - 1 : cur));
  hit.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      show(Math.min(ts.length - 1, Math.max(0, (cur < 0 ? ts.length - 1 : cur) + (e.key === "ArrowLeft" ? -1 : 1))));
    }
    if (e.key === "Escape") hide();
  });
}

function bindStockHistory() {
  const panel = $("#stock-history");
  if (!panel) return;
  panel.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    historyScope = b.dataset.history;
    try { localStorage.setItem("sw-history", historyScope); } catch (e) {}
    panel.outerHTML = stockHistoryPanel();
    bindStockHistory();
  }));
  renderStockChart();
  const box = $("#stock-chart");
  if (box && window.ResizeObserver) {
    let w = box.clientWidth;
    new ResizeObserver(() => { if (box.isConnected && box.clientWidth !== w) { w = box.clientWidth; renderStockChart(); } }).observe(box);
  }
}

const RELATION = {primary: ["Primary", "cu"], alternate: ["Alternate", "info"], sibling: ["Other alternate", "info"]};
function alternatesPanel(p, alts) {
  const hint = p.is_alternate_only || p.alternate_for?.length
    ? "The primary part this one stands in for, and the other approved alternates on the same BOM lines."
    : "Approved alternates on the BOM lines that use this part.";
  return `<section class="panel"><div class="row spread"><h2>Alternates${alts.length ? ` <span class="muted">${alts.length}</span>` : ""}</h2><span class="muted small">${alts.length ? hint : ""}</span></div>
  ${alts.length ? `<ul class="alts">${alts.map(a => { const [rl, rc] = RELATION[a.relation];
    return `<li><a href="#part/${encodeURIComponent(a.key)}"><span class="mono">${esc(a.mpn)}</span> <span class="muted small">${esc(a.manufacturer)}</span></a>
      <span class="chip ${rc}">${rl}</span>
      <span class="muted small">${a.stock == null ? "stock unknown" : `${fmtN(a.stock)} in stock`}${a.need && a.stock != null ? ` (${a.stock >= a.need ? "covers" : "below"} need of ${fmtN(a.need)})` : ""} · lead ${fmtLead(a.lead_days)}${a.lifecycle ? ` · ${esc(a.lifecycle)}` : ""}</span>
      ${chips(a.flags.filter(f => f !== "single_source"))}</li>`; }).join("")}</ul>`
    : `<div class="empty">No approved alternates on the BOM lines that use this part.${p.flags?.includes("single_source") ? " It is single-sourced." : ""}</div>`}</section>`;
}

function viewProjects() {
  const admin = STATUS.user.admin;
  const ps = DATA.projects;
  return `
  <div class="row spread"><h1>Projects</h1><span class="muted small">Build quantity drives the required quantities and shortage checks.</span></div>
  <div class="tablebox">${ps.length ? `<table><thead><tr><th>Project</th><th>Variant</th><th>Source</th><th class="num">Lines</th><th class="num">Placements</th><th>Build qty</th><th>Issues</th><th>Synced</th><th></th></tr></thead><tbody>
  ${ps.map(p => `<tr><td><a href="#project/${p.id}">${esc(p.name)}</a>${p.active ? "" : ` <span class="chip info">Excluded</span>`}</td><td class="small">${esc(p.variant || "—")}</td><td class="small muted" title="${esc(p.location || "")}">${p.source === "git" ? `Git <span class="mono">${esc(p.revision || "")}</span>` : p.source === "a365" ? "Altium 365" : "Upload"}</td>
    <td class="num">${p.line_count}</td><td class="num">${p.placements}</td>
    <td><input type="number" min="0" step="1" value="${p.build_qty}" data-build="${p.id}" aria-label="Build quantity for ${esc(p.name)}"></td>
    <td>${chips(Object.keys(p.flags).filter(f => ["critical", "warning"].includes(DATA.flags[f]?.level)))}</td><td class="small muted">${fmtDate(p.synced_at)}</td>
    <td><div class="row" style="flex-wrap:nowrap">${admin ? `<button class="btn small" data-toggle="${p.id}" data-active="${p.active}">${p.active ? "Exclude" : "Include"}</button>` : ""}<a class="btn small" href="/api/export/projects/${p.id}.csv">CSV</a></div></td></tr>`).join("")}
  </tbody></table>` : `<div class="empty">No projects yet. ${admin ? "Set GIT_REPOS and run a BOM sync on the Admin page, or upload a BOM there." : "Ask an admin to run a BOM sync."}</div>`}</div>`;
}

function viewProject(id) {
  const p = DATA.projects.find(x => String(x.id) === String(id));
  if (!p) return `<p>Project not found. <a href="#projects">Back to projects</a></p>`;
  const parts = Object.fromEntries(DATA.parts.map(x => [x.key, x]));
  return `
  <div class="row"><a href="#projects" class="small">← Projects</a></div>
  <div class="row spread"><div><h1>${esc(p.name)}</h1><div class="muted">${esc(p.variant)} · ${p.line_count} lines · ${p.placements} placements · build ${p.build_qty}${p.revision ? ` · <span class="mono">${esc(p.revision)}</span>` : ""}</div>${p.location ? `<div class="muted small mono">${esc(p.location)}</div>` : ""}</div><a class="btn" href="/api/export/projects/${p.id}.csv">Export CSV</a></div>
  <div class="tablebox"><table><thead><tr><th>Component</th><th>Designators</th><th class="num">Qty</th><th class="num">Need</th><th>MPN</th><th class="num">Auth. stock</th><th class="num">Lead</th><th>COO</th><th>Issues</th></tr></thead><tbody>
  ${p.lines.map((l, i) => { const pp = parts[l.primary] || {}; const ch = l.choices, grp = `line:${p.id}:${i}`;
    return `<tr ${l.primary ? `class="link" data-part="${esc(l.primary)}"` : ""}><td><div class="mono">${esc(l.component)}</div><div class="muted small">${esc(l.description)}</div></td>
    <td class="mono small" style="max-width:220px">${esc(l.designators.join(", "))}</td><td class="num">${l.quantity}</td><td class="num">${fmtN(l.need)}</td>
    <td>${ch.length ? `<div class="mono">${esc(ch[0].mpn)}</div><div class="muted small">${esc(ch[0].mfr)}${l.mpn_source === "activebom" ? ` · <span title="MPN from the project's ActiveBOM, not schematic parameters">ActiveBOM</span>` : ""}</div>${disclose(grp, ch.length - 1)}` : "—"}</td>
    <td class="num">${fmtN(pp.stock)}</td><td class="num">${fmtLead(pp.lead_days)}</td><td class="small">${esc(pp.coo || "—")}</td><td>${chips(l.flags)}</td></tr>`
    + ch.slice(1).map(c => { const k = normMpn(c.mpn), ap = parts[k] || {};
      return `<tr class="link alt" data-part="${esc(k)}" data-group="${esc(grp)}" ${expanded.has(grp) ? "" : "hidden"}><td colspan="4" class="small muted">Alternate for ${esc(l.component)}</td>
      <td><span class="chip info">Alternate</span> <span class="mono">${esc(c.mpn)}</span><div class="muted small">${esc(c.mfr || ap.manufacturer || "")}</div></td>
      <td class="num">${fmtN(ap.stock)}</td><td class="num">${fmtLead(ap.lead_days)}</td><td class="small">${esc(ap.coo || "—")}</td><td>${chips((ap.flags || []).filter(f => f !== "single_source"))}</td></tr>`; }).join(""); }).join("")}
  </tbody></table></div>`;
}

function viewAdmin() {
  const s = STATUS, c = s.bom_sources, admin = s.user.admin;
  const dot = on => `<span class="dot ${on ? "on" : "off"}"></span>`;
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  return `
  <div class="row spread"><h1>Admin</h1>${s.running ? `<span class="chip info">Sync running…</span>` : ""}</div>
  ${params.get("error") ? `<div class="panel critical">Altium 365 sign-in returned: ${esc(params.get("error"))}</div>` : ""}
  ${!admin ? `<p class="muted">You can view status. Admin actions need ADMIN_USERS or ADMIN_GROUP.</p>` : ""}
  <div class="grid2">
    <section class="panel"><h2>BOM sources</h2><dl class="kv">
      <dt>Git / folders</dt><dd>${dot(s.bom_sources.git.configured)}${s.bom_sources.git.configured ? `${s.bom_sources.git.repos} repo(s), ${s.bom_sources.git.folders} folder(s) <span class="muted small">filter <span class="mono">${esc(s.bom_sources.git.filter || "none")}</span></span>` : "Not configured (GIT_REPOS / PROJECT_DIRS)"}</dd>
      ${c.altium365.configured ? `<dt>Altium 365</dt><dd>${dot(c.altium365.connected)}${c.altium365.connected ? "Connected" : "Not signed in"}${c.altium365.workspace ? ` <span class="muted small">${esc(c.altium365.workspace)}</span>` : ""}</dd>` : ""}
      <dt>Schedule</dt><dd class="small">BOM sync <span class="mono">${esc(s.schedule.bom_sync)}</span> · supply <span class="mono">${esc(s.schedule.supply_refresh)}</span>${Object.entries(s.schedule.next).map(([k, v]) => `<br><span class="muted">next ${esc(k === "bom_sync" ? "BOM sync" : "supply refresh")}: ${fmtDate(v)}</span>`).join("")}</dd>
    </dl>
    <h2>Supply sources</h2><dl class="kv">
      ${s.supply_sources.map(x => `<dt>${esc(x.label)}</dt><dd>${dot(x.configured)}${x.configured ? "Configured" : "Not configured"}</dd>`).join("")}
      ${s.nexar_supply.enabled ? `<dt>Octopart quota</dt><dd>${fmtN(s.nexar_usage.parts)} of ${fmtN(s.nexar_usage.budget)} matched parts in ${esc(s.nexar_usage.month)}${s.nexar_supply.refused_fields.length ? `<div class="muted small">Refused by plan: ${esc(s.nexar_supply.refused_fields.join(", "))}</div>` : ""}</dd>` : ""}
    </dl>
    ${admin ? `<div class="row">
      <button class="btn primary" id="run-bom" ${s.bom_sources.git.configured || c.altium365.connected ? "" : "disabled"}>Sync BOMs now</button>
      <button class="btn" id="run-supply">Refresh due parts</button>
      <button class="btn" id="run-supply-force">Refresh all parts</button>
      ${c.altium365.configured ? (c.altium365.connected ? `<button class="btn" id="a365-off">Disconnect Altium 365</button>` : `<a class="btn" href="/admin/nexar/login">Sign in to Altium 365</a>`) : ""}
    </div>` : ""}
    </section>
    ${admin ? `<section class="panel"><h2>Upload a BOM</h2><p class="muted small">For projects that are not in a synced repository. Use an Altium BOM export (CSV) with Designator, Quantity and Manufacturer Part Number columns. Uploading the same name and variant replaces it.</p>
      <form id="upload" class="row"><input type="text" name="name" placeholder="Project name" required aria-label="Project name"><input type="text" name="variant" placeholder="Variant (optional)" aria-label="Variant">
      <input type="file" name="file" accept=".csv,text/csv" required aria-label="BOM file"><button class="btn primary" type="submit">Upload</button></form></section>` : ""}
  </div>
  <section class="panel"><h2>Recent runs</h2><div class="tablebox"><table><thead><tr><th>Started</th><th>Job</th><th>Status</th><th>Summary</th><th>By</th><th></th></tr></thead><tbody>
  ${s.runs.map(r => `<tr><td class="small">${fmtDate(r.started)}</td><td class="small">${r.kind === "bom_sync" ? "BOM sync" : "Supply refresh"}</td><td class="status-${r.status}">${esc(r.status)}</td><td class="small">${esc(r.summary)}</td><td class="small muted">${esc(r.by)}</td><td><button class="btn small" data-log="${r.id}">Log</button></td></tr>`).join("") || `<tr><td colspan="6" class="empty">No runs yet.</td></tr>`}
  </tbody></table></div><pre class="log" id="log" hidden></pre></section>`;
}

// ------------------------------------------------------------------------------ router
async function load() { [DATA, STATUS] = await Promise.all([api("/api/data"), api("/api/status")]); $("#who").textContent = STATUS.user.email === "anonymous" ? "No login" : STATUS.user.email + (STATUS.user.admin ? " · admin" : ""); }

async function route() {
  const [path] = location.hash.slice(1).split("?");
  const [name, arg] = (path || "overview").split("/");
  document.querySelectorAll("#nav a").forEach(a => a.setAttribute("aria-current", a.dataset.r === (name === "part" ? "parts" : name === "project" ? "projects" : name) ? "page" : "false"));
  const v = $("#view");
  try {
    if (!DATA) await load();
    const html = name === "parts" ? viewParts() : name === "part" ? await viewPart(decodeURIComponent(arg)) : name === "projects" ? viewProjects()
      : name === "project" ? viewProject(arg) : name === "admin" ? viewAdmin() : viewOverview();
    v.innerHTML = html;
    bind(name, arg);
  } catch (e) { v.innerHTML = `<div class="panel"><h2>Could not load</h2><p>${esc(e.message)}</p></div>`; }
}

function bind(name, arg) {
  const v = $("#view");
  v.querySelectorAll("[data-expand]").forEach(b => b.addEventListener("click", () => {
    const id = b.dataset.expand, open = !expanded.has(id);
    open ? expanded.add(id) : expanded.delete(id);
    b.setAttribute("aria-expanded", open);
    v.querySelectorAll("[data-group]").forEach(tr => { if (tr.dataset.group === id) tr.hidden = !open; });
  }));
  v.querySelectorAll("[data-part]").forEach(tr => tr.addEventListener("click", e => { if (e.target.closest("a,button,input")) return; location.hash = "#part/" + encodeURIComponent(tr.dataset.part); }));
  v.querySelectorAll("a[data-flag]").forEach(a => a.addEventListener("click", () => { ui.flag = a.dataset.flag; ui.q = ""; ui.project = ""; ui.coo = ""; saveUi(); }));
  v.querySelectorAll("a[data-coo]").forEach(a => a.addEventListener("click", () => { ui.coo = a.dataset.coo; ui.flag = ""; ui.q = ""; ui.project = ""; saveUi(); }));
  if (name === "parts") {
    const rerender = () => { saveUi(); v.innerHTML = viewParts(); bind("parts"); const q = $("#f-q"); if (document.activeElement !== q && ui._focus) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); } };
    $("#f-q").addEventListener("input", e => { ui.q = e.target.value; ui._focus = true; clearTimeout(ui._t); ui._t = setTimeout(rerender, 150); });
    $("#f-project").addEventListener("change", e => { ui.project = e.target.value; ui._focus = false; rerender(); });
    $("#f-flag").addEventListener("change", e => { ui.flag = e.target.value; ui._focus = false; rerender(); });
    $("#f-coo").addEventListener("change", e => { ui.coo = e.target.value; ui._focus = false; rerender(); });
    $("#f-alts")?.addEventListener("click", () => {
      const ids = [...v.querySelectorAll("[data-expand]")].map(b => b.dataset.expand), open = ids.some(id => !expanded.has(id));
      ids.forEach(id => open ? expanded.add(id) : expanded.delete(id)); ui._focus = false; rerender(); });
    v.querySelectorAll("[data-sort]").forEach(b => b.addEventListener("click", () => { const k = b.dataset.sort; ui.dir = ui.sort === k ? -ui.dir : 1; ui.sort = k; ui._focus = false; rerender(); }));
  }
  if (name === "part") {
    bindStockHistory();
    $("#coo-form").addEventListener("submit", async e => { e.preventDefault();
      try { await api(`/api/parts/${encodeURIComponent(arg)}/coo`, {method: "PUT", json: {coo: $("#coo").value, note: $("#coo-note").value}}); toast($("#coo").value ? "Country of origin saved" : "Country of origin cleared"); await load(); route(); } catch (err) { toast(err.message); } });
    $("#refresh-part").addEventListener("click", async () => { try { await api(`/api/parts/${encodeURIComponent(arg)}/refresh`, {method: "POST"}); toast("Refreshing this part. Reloading in a few seconds…"); setTimeout(async () => { await load(); route(); }, 8000); } catch (err) { toast(err.message); } });
  }
  if (name === "projects") {
    v.querySelectorAll("[data-build]").forEach(inp => inp.addEventListener("change", async () => {
      try { await api(`/api/projects/${inp.dataset.build}`, {method: "PATCH", json: {build_qty: Math.max(0, parseInt(inp.value || "0", 10))}}); toast("Build quantity saved"); await load(); } catch (err) { toast(err.message); } }));
    v.querySelectorAll("[data-toggle]").forEach(b => b.addEventListener("click", async () => {
      try { await api(`/api/projects/${b.dataset.toggle}`, {method: "PATCH", json: {active: b.dataset.active !== "true"}}); await load(); route(); } catch (err) { toast(err.message); } }));
  }
  if (name === "admin") {
    const run = (id, url, msg) => { const b = $(id); if (b) b.addEventListener("click", async () => { try { await api(url, {method: "POST"}); toast(msg); setTimeout(async () => { await load(); route(); }, 1500); } catch (err) { toast(err.message); } }); };
    run("#run-bom", "/api/runs/bom-sync", "BOM sync started; supply refresh follows");
    run("#run-supply", "/api/runs/supply-refresh", "Supply refresh started");
    run("#run-supply-force", "/api/runs/supply-refresh?force=true", "Full supply refresh started");
    run("#a365-off", "/admin/nexar/disconnect", "Altium 365 disconnected");
    const up = $("#upload");
    if (up) up.addEventListener("submit", async e => { e.preventDefault();
      try { const r = await api("/api/upload", {method: "POST", body: new FormData(up)}); toast(`Imported ${r.lines} lines (${r.with_mpn} with MPN)`); up.reset(); await load(); route(); } catch (err) { toast(err.message); } });
    v.querySelectorAll("[data-log]").forEach(b => b.addEventListener("click", async () => { const r = await api(`/api/runs/${b.dataset.log}`); const pre = $("#log"); pre.hidden = false; pre.textContent = `${r.kind} #${r.id} · ${r.status}\n${r.summary}\n\n${(r.log || []).join("\n")}`; }));
    if (STATUS.running) setTimeout(async () => { if (location.hash.startsWith("#admin")) { await load(); route(); } }, 5000);
  }
}

window.addEventListener("hashchange", route);
route();
