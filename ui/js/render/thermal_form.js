// Responsibility: Build the "Thermal model" panel an ECXML upload gets instead of the geometry check's
// form - the model and who wrote it, the totals, the air box in one line, the parts as the file
// groups them, the fans, vents, heat sources, probes and materials with the file's own numbers,
// warnings only when there are any, and the one choice left: the detail level - and read that
// choice back as the body the confirm sends.
// Owns: the panel's markup, its collapsible groups, search and sort, the selected and hovered part
// as the list shows them, and the detail level picked.
// Boundaries: markup and a reading of it. It fetches nothing, draws no scene and judges nothing:
// every number is the check's `thermal` block (application/thermal_view.py), shown in mm, degC
// and W. Picking a part is a callback; the 3D stage (viewer/geometry_stage.js) and the card both
// use it so the user answers one panel.
import { esc } from "../core/format.js";

const attr = (v) => esc(v).replace(/"/g, "&quot;");

/** The detail levels in the order offered, with the words the panel uses for each. */
export const LEVELS = [["draft", "Quick look"], ["standard", "Standard"], ["max", "Fine"]];
/** Above this many parts the groups start folded and the list gets a search box. */
export const FOLD_ABOVE = 40;
/** Rows a group shows before "Show more". */
export const PAGE_ROWS = 200;

/* ------------------------------------------------------------------ numbers as engineers write them */
/** A number with at most 4 significant digits and no trailing zeros. */
export function num(v) {
  const f = Number(v);
  if (v === null || v === undefined || v === "" || !isFinite(f)) return "";
  if (f === 0) return "0";
  const r = Number(f.toPrecision(4));
  return Math.abs(r) >= 1e6 ? r.toLocaleString("en-US", { maximumFractionDigits: 0 }) : String(r);
}
export function watts(v) {
  const f = Number(v) || 0;
  return f === 0 ? "0 W" : `${num(Math.round(f * 1000) / 1000)} W`;
}
/** A cell count as people say it: 357,973 -> "358k", 2,029,905 -> "2.03M". */
export function cells(n) {
  const f = Number(n) || 0;
  if (f >= 1e6) return `${Number((f / 1e6).toPrecision(3))}M`;
  if (f >= 1e3) return `${Math.round(f / 1e3)}k`;
  return String(Math.round(f));
}
/** A run time: "about 40 s", "about 2 min". */
export function duration(s) {
  const f = Number(s) || 0;
  if (f < 90) return `about ${Math.max(10, Math.round(f / 10) * 10)} s`;
  return `about ${Math.round(f / 60)} min`;
}
/** A length given in mm, in the unit that reads it: "10 nm", "25 µm", "1.6 mm" - never "0 mm". */
export function length(mm) {
  const f = Number(mm);
  if (mm === null || mm === undefined || mm === "" || !isFinite(f)) return "";
  const a = Math.abs(f);
  if (a === 0) return "0 mm";
  if (a < 0.0009995) return `${num(Number((f * 1e6).toPrecision(3)))} nm`;
  if (a < 0.9995) return `${num(Number((f * 1e3).toPrecision(3)))} µm`;
  return `${num(f)} mm`;
}
/** Sizes in mm as a person reads them: "10 × 10 × 0.3 mm", or each in its own unit when one is
 * under a micrometre's worth of millimetres ("10 mm × 10 mm × 10 nm"). */
function dimsText(size) {
  const v = (size || []).filter((x) => x !== null && x !== undefined);
  if (!v.length) return "";
  if (v.every((x) => Math.abs(Number(x)) >= 0.01 || Number(x) === 0)) return `${v.map(num).join(" × ")} mm`;
  return v.map(length).join(" × ");
}
/** A size as the narrow column shows it (mm): three significant digits, no spaces round the "×";
 * a size under 10 µm carries its own unit. */
function dimsShort(size) {
  return (size || []).filter((v) => v !== null && v !== undefined)
    .map((v) => (Math.abs(Number(v)) >= 0.01 || Number(v) === 0
      ? num(Number(Number(v).toPrecision(3))) : length(v).replace(" ", ""))).join("×");
}
function k(v) {
  if (Array.isArray(v)) return `${v.map(num).join(" / ")} W/m·K`;
  if (typeof v === "number") return `${num(v)} W/m·K`;
  return v ? String(v) : "";
}
function rgb(c) {
  return Array.isArray(c) && c.length === 3 ? `rgb(${c.map((x) => Math.round(x * 255)).join(",")})` : "transparent";
}
function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }

/* ------------------------------------------------------------------------------- the parts */
/** The parts as the file groups them: [{key, label, rows}] in file order. A part the file puts
 *  in no assembly sits in the group with an empty key, shown without a header. */
export function groupsOf(tree) {
  const groups = new Map();
  (tree || []).forEach((row, i) => {
    const key = (row.path || []).join(" / ");
    if (!groups.has(key)) groups.set(key, { key, label: (row.path || []).join(" › "), rows: [] });
    groups.get(key).rows.push({ ...row, order: i });
  });
  return [...groups.values()];
}

function partSub(r) {
  return [r.kind, r.detail].filter(Boolean).join(" · ");
}

/** What a hover over a part says: everything the file gives about it, in plain lines. */
export function partDetails(r) {
  const lines = [r.name];
  if ((r.path || []).length) lines.push(`In ${(r.path || []).join(" › ")}`);
  if (partSub(r)) lines.push(partSub(r));
  lines.push(`Material: ${r.material || "none given"}`);
  lines.push(`Power: ${watts(r.power_W)}`);
  if (r.size_mm) lines.push(`Size: ${dimsText(r.size_mm)}`);
  if (r.thinnest_mm) lines.push(`Thinnest layer: ${length(r.thinnest_mm)}`);
  if (r.thin_layer) lines.push(`Carried as a thin-layer resistance, not as cells: ${r.thin_layer}`);
  if (r.left_out) lines.push(`Left out of the mesh: ${r.left_out}`);
  else if (r.clipped) lines.push("Partly outside the air box: only the part inside is meshed");
  return lines.join("\n");
}

function rowHtml(r, maxW) {
  const out = !!r.left_out;
  const share = maxW > 0 && r.power_W > 0 ? Math.max(4, Math.round(100 * r.power_W / maxW)) : 0;
  const reg = r.region ? ` data-region="${attr(r.region)}" role="button" tabindex="0"` : "";
  return `<div class="tm-row${out ? " out" : ""}"${reg} title="${attr(partDetails(r))}">
      <span class="tm-sw" style="background:${out ? "transparent" : rgb(r.color)}"></span>
      <span class="tm-nm"><b>${esc(r.name)}</b><small>${esc(out ? "left out" : r.thin_layer ? "thin layer, a resistance" : partSub(r))}</small></span>
      <span class="tm-mat">${esc(r.material || "—")}</span>
      <span class="tm-w num">${r.power_W ? esc(watts(r.power_W)) : '<span class="tm-zero">—</span>'}${share ? `<i class="tm-heat" title="share of the hottest part"><i style="width:${share}%"></i></i>` : ""}</span>
      <span class="tm-sz num">${esc(dimsShort(r.size_mm))}</span>
      <span class="tm-th num">${r.thinnest_mm ? esc(length(r.thinnest_mm)) : ""}</span>
    </div>`;
}

function groupHtml(g, folded, byPower, maxW) {
  const rows = byPower ? g.rows.slice().sort((a, b) => (b.power_W || 0) - (a.power_W || 0)) : g.rows;
  const power = g.rows.reduce((s, r) => s + (r.left_out ? 0 : (r.power_W || 0)), 0);
  const shown = rows.slice(0, PAGE_ROWS).map((r) => rowHtml(r, maxW)).join("");
  const more = rows.length > PAGE_ROWS
    ? `<button class="tm-more" type="button" data-more="${attr(g.key)}">Show ${rows.length - PAGE_ROWS} more</button>` : "";
  if (!g.key) return `<div class="tm-group tm-root" data-group="">${shown}${more}</div>`;
  return `<div class="tm-group${folded ? " folded" : ""}" data-group="${attr(g.key)}">
      <div class="tm-gh"><button class="tm-fold" type="button" aria-expanded="${folded ? "false" : "true"}">
        <span class="tm-caret" aria-hidden="true"></span><span class="tm-gl">${esc(g.label)}</span>
        <span class="tm-gc num">${plural(g.rows.length, "part")}${power ? ` · ${esc(watts(power))}` : ""}</span>
      </button><button class="tm-only" type="button" title="Show only these parts in the view">Show only</button></div>
      <div class="tm-rows">${shown}${more}</div>
    </div>`;
}

function partsHtml(t, state) {
  const groups = groupsOf(t.tree);
  const maxW = Math.max(0, ...(t.tree || []).map((r) => (r.left_out ? 0 : r.power_W || 0)));
  const list = state.byPower
    ? groups.slice().sort((a, b) => Math.max(0, ...b.rows.map((r) => r.power_W || 0)) - Math.max(0, ...a.rows.map((r) => r.power_W || 0)))
    : groups;
  return list.map((g) => groupHtml(g, state.folded, state.byPower, maxW)).join("");
}

/* -------------------------------------------------------------------- fans, vents and the rest */
/** The file's own sentence for a fan or a vent, without its name in front (the name is bold). */
function after(text, name) {
  const s = String(text || ""), pre = `${name}: `;
  return s.startsWith(pre) ? s.slice(pre.length) : s;
}

function itemHtml(name, words, extra) {
  return `<li class="tm-item"${extra || ""}><b>${esc(name)}</b><span>${esc(words)}</span></li>`;
}

function listSection(title, items) {
  if (!items.length) return "";
  return `<section class="tm-sec"><h3 class="tm-h">${esc(title)}<span class="tm-n num">${items.length}</span></h3>
      <ul class="tm-list">${items.join("")}</ul></section>`;
}

function sourceWords(s) {
  const bits = [watts(s.power_W), s.kind === "surface" ? "on a surface" : "through a volume"];
  if (s.where) bits.push(s.where);
  const lands = (s.lands_in || []).map((x) => (x && (x.region || x.name)) || x).filter(Boolean);
  if (lands.length) bits.push(`lands in ${lands.slice(0, 4).join(", ")}${lands.length > 4 ? ` and ${lands.length - 4} more` : ""}`);
  return bits.join(", ");
}

function resistanceWords(r) {
  const bits = [];
  if (r.size_mm) bits.push(dimsText(r.size_mm));
  if ((r.loss_coefficient_xyz || []).length) bits.push(`loss coefficients ${r.loss_coefficient_xyz.map(num).join(" / ")} (x / y / z)`);
  if ((r.open_pct_xyz || []).length) bits.push(`${r.open_pct_xyz.map(num).join(" / ")}% open`);
  return bits.join(", ");
}

function materialWords(m) {
  const bits = [];
  if (k(m.k_W_mK)) bits.push(`k ${k(m.k_W_mK)}`);
  if (m.density_kg_m3) bits.push(`${num(m.density_kg_m3)} kg/m³`);
  bits.push(m.used_by ? plural(m.used_by, "part") : "not used by a part");
  return bits.join(" · ");
}

/* ---------------------------------------------------------------------------- the detail level */
/** One detail level's line under its name: the cells and time it was sized at, or its limits. */
export function levelWords(row) {
  if (!row) return "";
  if (row.fits === false) return row.estimate || "too fine for this level";
  if (row.est_cells) {
    let s = `${cells(row.est_cells)} cells · ${duration(row.est_seconds)}`;
    const short = Number(row.thin_layers_short_count || (row.thin_layers_short || []).length || 0);
    if (short) s += ` · ${plural(short, "layer")} short`;
    const rules = Number(row.rules_short_count || 0);
    if (rules) s += ` · ${plural(rules, "spot")} coarser than asked`;
    return s;
  }
  return `up to ${cells(row.max_cells)} cells · ${row.estimate || "sized when the mesh runs"}`;
}

/** What the chosen level promises, in one line under the choice. */
export function levelPromise(row, label) {
  if (!row) return "";
  const across = Number(row.min_cells_across || 1);
  const gap = Number(row.gap_cells || 0);
  const gaps = gap ? ` and ${gap} across every air gap` : "";
  const aim = `${label}: up to ${cells(row.max_cells)} cells, aiming for ${plural(across, "cell")} through every layer${gaps}`;
  if (row.fits === false) return `${aim}. ${sentenceOf(row.estimate || "This model needs more cells than that")}`;
  const short = Number(row.thin_layers_short_count || (row.thin_layers_short || []).length || 0);
  if (short) return `${aim}; ${plural(short, "thin layer")} ${short === 1 ? "gets" : "get"} fewer.`;
  // the check sized it and every layer gets its cells: the promise is the check's own result
  const rules = Number(row.rules_short_count || 0);
  if (row.est_cells && rules) return `${aim}; ${plural(rules, "spot")} ${rules === 1 ? "gets" : "get"} fewer.`;
  // the counts were measured off the laid-out grid: the promise is the check's own result
  if (row.est_cells && row.rules_measured !== false) return `${label}: up to ${cells(row.max_cells)} cells, at least ${plural(across, "cell")} through every layer${gaps}.`;
  return `${aim}.`;
}
function sentenceOf(text) {
  const t = String(text || "").trim().replace(/\.+$/, "");
  return t ? t.charAt(0).toUpperCase() + t.slice(1) + "." : "";
}

function detailHtml(t, chosen) {
  const det = t.detail || {};
  return `<div class="tm-levels" role="radiogroup" aria-label="Detail level">${LEVELS.map(([key, label]) => {
    const row = det[key] || {}, on = key === chosen, bad = row.fits === false;
    return `<button class="tm-level${on ? " on" : ""}${bad ? " bad" : ""}" type="button" role="radio" aria-checked="${on}" data-level="${key}">
        <b>${esc(row.label || label)}</b><span class="num">${esc(levelWords(row))}</span></button>`;
  }).join("")}</div>
    <p class="tm-promise">${esc(levelPromise(det[chosen], (det[chosen] || {}).label || chosen))}</p>`;
}

/* -------------------------------------------------------------------------------- the panel */
function tile(value, label, sub) {
  return `<div class="tm-tile"><b class="num">${esc(value)}</b><span>${esc(label)}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
}

/** The whole panel's markup for a `thermal` block. `opts.level` is the detail level shown chosen. */
export function thermalPanelHtml(t, opts) {
  t = t || {};
  const o = opts || {};
  const tot = t.totals || {}, amb = t.ambient, dom = t.domain || {};
  const level = o.level || (t.detail || {}).default || "standard";
  const nParts = (t.tree || []).length;
  const state = { folded: nParts > FOLD_ABOVE, byPower: false };
  const from = [t.producer ? `from ${t.producer}` : "", "JEDEC ECXML"].filter(Boolean).join(" · ");
  const left = Number(tot.power_left_out_W || 0);
  const strip = [
    tile(watts(tot.power_W), "total power", left > 0 ? `${watts(left)} left out` : ""),
    tile(String(tot.parts ?? nParts), "parts"),
    tile(String(tot.materials ?? (t.materials || []).length), "materials"),
    amb && amb.temperature_C !== null && amb.temperature_C !== undefined
      ? tile(`${num(amb.temperature_C)} °C`, "room air") : tile("—", "room air", "none in the file"),
  ].join("");
  const fans = (t.fans || []).map((f) => itemHtml(f.name, after(f.text, f.name)));
  const vents = (t.vents || []).map((v) => itemHtml(v.name, after(v.text, v.name)));
  const res = (t.resistances || []).map((r) => itemHtml(r.name, resistanceWords(r)));
  const sources = (t.sources || []).map((s) => itemHtml(s.name, sourceWords(s)));
  const probes = (t.probes || []).map((p) => itemHtml(p.name,
    (p.at_mm || []).length === 3 ? `at ${p.at_mm.map(num).join(", ")} mm` : "", ` data-probe="${attr(p.name)}"`));
  const mats = (t.materials || []).map((m) => itemHtml(m.name, materialWords(m)));
  const warns = (t.warnings || []).map((w) => {
    const regions = new Map((t.tree || []).filter((r) => r.region).map((r) => [r.name, r.region]));
    const chips = (w.parts || []).filter((n) => regions.has(n)).slice(0, 6)
      .map((n) => `<button class="tm-chip" type="button" data-region="${attr(regions.get(n))}">${esc(n)}</button>`).join("");
    return `<li class="tm-warn ${w.level === "info" ? "info" : "warn"}"><span class="tm-mark" aria-hidden="true"></span>
        <div><p>${esc(w.text)}</p>${chips ? `<div class="tm-chips">${chips}</div>` : ""}</div></li>`;
  });
  const report = (t.build_report || []).length
    ? `<details class="tm-report"><summary>Show the build report</summary><ul>${t.build_report.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>` : "";
  const shownNote = t.tree_shown ? `<p class="tm-note">The first ${t.tree_shown} parts are listed; the totals count all ${tot.parts}.</p>` : "";
  return `<div class="tm-head">
      <div class="tm-eyebrow">Thermal model<span>${esc(from)}</span></div>
      <h2 class="tm-title">${esc(t.title || "the thermal model")}</h2>
      <div class="tm-strip">${strip}</div>
    </div>
    ${warns.length ? `<section class="tm-sec tm-warns"><ul>${warns.join("")}</ul></section>` : ""}
    <section class="tm-sec"><h3 class="tm-h">Air around the model</h3><p class="tm-air">${esc(dom.summary || "")}</p></section>
    <section class="tm-sec tm-parts">
      <h3 class="tm-h">Parts<span class="tm-n num">${nParts}</span>
        <span class="tm-sort" role="group" aria-label="Order"><button type="button" class="on" data-sort="file">File order</button><button type="button" data-sort="power">Power</button></span></h3>
      ${nParts > FOLD_ABOVE ? `<input class="tm-search" type="search" placeholder="Find a part or material" aria-label="Find a part or material">` : ""}
      <div class="tm-cols" aria-hidden="true"><span></span><span>Part</span><span>Material</span><span class="r">Power</span><span class="r">Size, mm</span><span class="r">Thinnest</span></div>
      <div class="tm-tree">${partsHtml(t, state)}</div>
      <p class="tm-empty" hidden>No part matches.</p>
      ${shownNote}
    </section>
    ${listSection("Fans", fans)}${listSection("Vents", vents)}${listSection("Flow resistances", res)}
    ${listSection("Heat sources", sources)}${listSection("Probes", probes)}${listSection("Materials", mats)}
    ${report ? `<section class="tm-sec">${report}</section>` : ""}
    <div class="tm-foot">
      <h3 class="tm-h">Detail level</h3>
      <div class="tm-detail">${detailHtml(t, level)}</div>
      <div class="tm-go"><button class="tm-mesh" type="button">Mesh this model</button><span class="tm-hint" aria-live="polite"></span></div>
    </div>`;
}

/** The body the confirm sends for a thermal model: the detail level, and no openings. The server
 *  knows it is a thermal model from its own stored check and fills the rest. */
export function thermalConfirmBody(level) {
  return { detail: LEVELS.some(([key]) => key === level) ? level : "standard", openings: [] };
}

/** Wire a drawn panel. `cb.select(region|null)` and `cb.hover(region|null)` tell the stage which
 *  part the list points at; `cb.probe(name|null)` which probe; `cb.confirm(body)` sends the
 *  answer (resolving when the server holds it). Returns the panel's controls. */
export function bindThermalPanel(root, t, cb) {
  cb = cb || {};
  t = t || {};
  let level = (t.detail || {}).default || "standard", selected = null, byPower = false;
  const tree = root.querySelector(".tm-tree");
  const rowsOf = (region) => [...root.querySelectorAll(".tm-row[data-region]")].filter((r) => r.dataset.region === region);

  function redrawTree() {
    const folded = new Set([...root.querySelectorAll(".tm-group.folded")].map((g) => g.dataset.group));
    const shown = (root.querySelector(".tm-group.sel") || {}).dataset;
    tree.innerHTML = partsHtml(t, { folded: false, byPower });
    tree.querySelectorAll(".tm-group").forEach((g) => {
      if (folded.has(g.dataset.group)) fold(g, true);
      if (shown && g.dataset.group === shown.group) g.classList.add("sel");
    });
    if (!Array.isArray(selected)) mark(selected);
    filter((root.querySelector(".tm-search") || {}).value || "");
  }
  function fold(g, on) {
    g.classList.toggle("folded", on);
    const h = g.querySelector(".tm-fold"); if (h) h.setAttribute("aria-expanded", on ? "false" : "true");
  }
  /** The list's selection follows the stage's: the row lit, its group open, scrolled to. */
  function mark(region) {
    selected = region || null;
    root.querySelectorAll(".tm-row.sel,.tm-group.sel").forEach((r) => r.classList.remove("sel"));
    if (!selected) return;
    if (Array.isArray(selected)) return;
    const rows = rowsOf(selected);
    if (!rows.length) {
      // a part past the first page of a long group: its page is drawn first
      const row = (t.tree || []).find((r) => r.region === selected);
      const g = row && [...root.querySelectorAll(".tm-group")].find((x) => x.dataset.group === (row.path || []).join(" / "));
      const more = g && g.querySelector(".tm-more");
      if (more) { more.click(); return mark(region); }
    }
    rows.forEach((r) => {
      r.classList.add("sel");
      const g = r.closest(".tm-group"); if (g && g.classList.contains("folded")) fold(g, false);
      r.scrollIntoView({ block: "nearest" });
    });
  }
  function choose(region) {
    const next = region && region === selected ? null : region;   // a second click puts it back
    mark(next);
    if (cb.select) cb.select(next);
  }
  function filter(q) {
    const s = String(q || "").trim().toLowerCase();
    // a search reads every part, not only the first page of each group: the rest are drawn first
    if (s) root.querySelectorAll(".tm-more").forEach((m) => m.click());
    let any = false;
    root.querySelectorAll(".tm-group").forEach((g) => {
      let hits = 0;
      g.querySelectorAll(".tm-row").forEach((r) => {
        const hit = !s || r.textContent.toLowerCase().includes(s);
        r.hidden = !hit; if (hit) hits++;
      });
      g.hidden = s ? hits === 0 : false;
      if (s && hits) fold(g, false);
      if (hits) any = true;
    });
    const empty = root.querySelector(".tm-empty"); if (empty) empty.hidden = !s || any;
  }

  root.addEventListener("click", (e) => {
    const only = e.target.closest(".tm-only");
    if (only) {
      // a whole group shown at once: 400 solder balls are one click, not 400
      const g = only.closest(".tm-group"), was = g.classList.contains("sel");
      const regions = (t.tree || []).filter((r) => r.region && (r.path || []).join(" / ") === g.dataset.group).map((r) => r.region);
      mark(null);
      if (!was) { g.classList.add("sel"); selected = regions; }
      if (cb.select) cb.select(was ? null : regions);
      return;
    }
    const gh = e.target.closest(".tm-fold");
    if (gh) { const g = gh.closest(".tm-group"); fold(g, !g.classList.contains("folded")); return; }
    const more = e.target.closest(".tm-more");
    if (more) {
      const g = groupsOf(t.tree).find((x) => x.key === more.dataset.more);
      const maxW = Math.max(0, ...(t.tree || []).map((r) => (r.left_out ? 0 : r.power_W || 0)));
      const rows = byPower ? g.rows.slice().sort((a, b) => (b.power_W || 0) - (a.power_W || 0)) : g.rows;
      more.insertAdjacentHTML("beforebegin", rows.slice(PAGE_ROWS).map((r) => rowHtml(r, maxW)).join(""));
      more.remove();
      filter((root.querySelector(".tm-search") || {}).value || "");   // new rows follow the search
      return;
    }
    const row = e.target.closest(".tm-row[data-region], .tm-chip[data-region]");
    if (row) { choose(row.dataset.region); return; }
    const sort = e.target.closest(".tm-sort button");
    if (sort) {
      byPower = sort.dataset.sort === "power";
      root.querySelectorAll(".tm-sort button").forEach((b) => b.classList.toggle("on", b === sort));
      redrawTree();
      return;
    }
    const lv = e.target.closest(".tm-level");
    if (lv) {
      level = lv.dataset.level;
      root.querySelector(".tm-detail").innerHTML = detailHtml(t, level);
      return;
    }
    const pr = e.target.closest(".tm-item[data-probe]");
    if (pr && cb.probe) cb.probe(pr.dataset.probe);
  });
  root.addEventListener("dblclick", (e) => {
    const row = e.target.closest(".tm-row[data-region]");
    if (row && cb.frame) cb.frame(row.dataset.region);
  });
  root.addEventListener("keydown", (e) => {
    const row = e.target.closest && e.target.closest(".tm-row[data-region]");
    if (row && (e.key === "Enter" || e.key === " ")) {
      // the page's own role=button handler would click the row a second time and undo the choice
      e.preventDefault(); e.stopPropagation(); choose(row.dataset.region);
    }
  });
  let hovered = null;
  root.addEventListener("pointerover", (e) => {
    const row = e.target.closest(".tm-row[data-region]");
    const region = row ? row.dataset.region : null;
    if (region === hovered) return;
    hovered = region;
    if (cb.hover) cb.hover(region);
  });
  root.addEventListener("pointerleave", () => { if (hovered !== null) { hovered = null; if (cb.hover) cb.hover(null); } });
  const search = root.querySelector(".tm-search");
  if (search) search.addEventListener("input", () => filter(search.value));

  const btn = root.querySelector(".tm-mesh"), hint = root.querySelector(".tm-hint");
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = "Sending…"; hint.textContent = "";
    try {
      await cb.confirm(thermalConfirmBody(level));
      btn.textContent = "Sent";
      root.classList.add("tm-sent");
    } catch (e) {
      btn.disabled = false; btn.textContent = "Mesh this model";
      hint.textContent = "Could not send: " + ((e && e.message) || "try again");
    }
  };
  return {
    select: mark,
    hover: (region) => {
      root.querySelectorAll(".tm-row.hov").forEach((r) => r.classList.remove("hov"));
      if (region) rowsOf(region).forEach((r) => r.classList.add("hov"));
    },
    level: () => level,
    setLevel: (lv) => { level = lv; root.querySelector(".tm-detail").innerHTML = detailHtml(t, level); },
    body: () => thermalConfirmBody(level),
    selected: () => (Array.isArray(selected) ? null : selected),
  };
}
