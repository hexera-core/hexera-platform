// Responsibility: Build the FEA part check - the "Your part" panel beside the part: a header (the part's or
// file's name and a purpose tag), a few key facts (size, the unit box, how many parts, the clean check in one
// line, the thinnest wall), then sections that are always shown: the hot spots the mesh is refined at (shown
// only: version 1 refines them all automatically), the shell (only when the user's words asked for one) and,
// for an ASSEMBLY, its joins - the one Bonded / Contact answer in the section's header, each pair with its own
// type to pick (PLAN-fea-19/20), and the small-gap switch once with its gaps listed once. Only the extra detail
// folds: each hot-spot type's list, a pair's reach and which part keeps an overlap, the parts list, the notes,
// what is not in the shell. Proceed sits at the foot. It also reads the answer back out of the panel.
// Boundaries: markup and a reading of it. No fluid, no openings, no flow axis and no stickers: an FEA part is
// meshed as itself. It fetches nothing and starts no run; the stage and the card both use it, and confirming
// is the callback they were handed.
import { esc } from "../core/format.js";
import { bindUnit, shown, unitBoxHtml, unitChoiceHint, unitChoiceNeeded, unitOf } from "./geometry_form.js";

/* LENGTHS AS PEOPLE READ THEM: a part under about a metre in a file drawn in metres is shown in
   millimetres ("36.5 mm", not "0.0365 m"); the unit box still says the file's own unit. The served
   lengths are millimetres of the reading, so millimetres need no conversion. */
const MM_BELOW_MM = 1000;
function fmt3(v) { const a = Math.abs(v); return a >= 100 ? String(Math.round(v)) : a === 0 ? "0" : String(Number(v.toPrecision(3))); }
function inMm(p) {
  const longest = Math.max(0, ...(p.size_mm || []).map(Number).filter(Number.isFinite));
  return unitOf(p) === "m" && longest > 0 && longest < MM_BELOW_MM;
}
const lenUnit = (p) => (inMm(p) ? "mm" : unitOf(p));
const len = (v, p) => (v == null || isNaN(v) ? "?" : inMm(p) ? fmt3(Number(v)) : shown(v, p));

/** Text for an attribute in double quotes (esc() leaves quotes alone: a reason or a JSON map has them). */
const attr = (s) => esc(s).replace(/"/g, "&quot;");
/** A number as the stage says it: three figures at most. */
const n3 = (v) => String(Number(Number(v).toPrecision(3)));
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

/* ======================================================================================
 * FOLDS: a header that opens its body on a click, closed at first. Every fold is an element with
 * `data-fold="<key>"`, its toggle a `.fea-fold-btn` and its body a direct `.fea-fold-body` child. The key
 * keeps a fold open when the stage draws its part again (a later poll says more).
 * ====================================================================================== */
const CHEV = `<span class="fea-chev" aria-hidden="true"></span>`;

/** A section that is always shown: its header and its body, no toggle. The main facts live in these; only the
 *  extra detail inside them folds. */
function secHtml(head, body, { aside = "", cls = "" } = {}) {
  return `<section class="fea-sec fea-sec-shown ${cls}">
      <div class="fea-sec-head"><div class="fea-sec-title">${head}</div>${aside}</div>
      <div class="fea-sec-body">${body}</div></section>`;
}

/** A small fold inside a section: a muted line that opens its rows (extra detail). */
function subFoldHtml(key, head, body, cls = "") {
  return `<div class="fea-subfold ${cls}" data-fold="${attr(key)}">
      <button type="button" class="fea-fold-btn fea-sub-h" aria-expanded="false">${CHEV}${head}</button>
      <div class="fea-fold-body" hidden>${body}</div></div>`;
}

const bodyOf = (w) => w && w.querySelector(":scope > .fea-fold-body");
const btnOf = (w) => [...w.querySelectorAll(".fea-fold-btn")].find((b) => b.closest("[data-fold]") === w);

/** Open or close one fold. */
export function setFold(w, open) {
  const body = bodyOf(w);
  if (!body) return;
  body.hidden = !open;
  w.classList.toggle("open", !!open);
  const b = btnOf(w);
  if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
}

/** Open every fold an element sits in (a mark on the part was clicked: its line is shown). */
function reveal(el) {
  for (let w = el.parentElement && el.parentElement.closest("[data-fold]"); w;
    w = w.parentElement && w.parentElement.closest("[data-fold]")) setFold(w, true);
}

/** The folds open now, by key; and open them again after a redraw. */
const openFolds = (root) => new Set([...root.querySelectorAll("[data-fold].open")].map((w) => w.dataset.fold));
function reopen(root, keys) {
  root.querySelectorAll("[data-fold]").forEach((w) => { if (keys.has(w.dataset.fold)) setFold(w, true); });
}

/* ======================================================================================
 * KEY FACTS: size, unit, parts, the clean check.
 * ====================================================================================== */

/** The size row: the three sides, the unit beside them. */
function sizeHtml(p) {
  const s = (p.size_mm || []).map((v) => len(v, p));
  return s.length === 3 ? `${esc(s.join(" × "))} <span class="gc-u">${esc(lenUnit(p))}</span>` : "not measured";
}

function bodiesHtml(f) {
  const n = f.bodies;
  // a file of loose surfaces (an IGES of trimmed faces) has no solids to count: its bodies are the pieces
  // its faces sew into (cad/fea_measure.sewn_bodies)
  if (!n && f.bodies_sewn > 0) return `${f.bodies_sewn} (sewn from surfaces)`;
  // the clean check counts the pieces a solid is made of: one solid of two overlapping pieces is
  // said as such, so the two lines never disagree
  if (n === 1 && f.pieces > 1) return `1 solid, ${f.pieces} pieces that pass into each other`;
  if (n === 1) return "1";
  if (n > 1) return `${n} (an assembly)`;
  return "not counted";
}

/** What the clean check found, in one line ("✓ Clean", "⚠ Part_7 may have self-crossing faces - we'll still
 *  try"); the detail opens under it on a click. */
const cleanSig = (f) => `${f.clean}/${f.clean_short || ""}/${(f.clean_details || []).length}/${f.clean_summary || ""}`;
function cleanHtml(f) {
  const warned = (f.warnings || []).length > 0;
  const cls = f.clean === false || warned ? "bad" : f.clean === true ? "ok" : "unk";
  const mark = cls === "ok" ? "✓" : cls === "bad" ? "⚠" : "?";
  const short = f.clean_short
    || (cls === "ok" ? "Clean" : f.clean === false || warned ? "Not clean - we'll still try" : "Not checked");
  const details = (f.clean_details || (cls === "ok" ? [] : [f.clean_summary])).filter((d) => d && String(d).trim());
  const line = `<span class="ic">${mark}</span><span class="fea-clean-short">${esc(short)}</span>`;
  if (!details.length) return `<span class="fea-clean ${cls}" data-sig="${attr(cleanSig(f))}">${line}</span>`;
  return `<span class="fea-clean ${cls}" data-sig="${attr(cleanSig(f))}" data-fold="clean">
      <button type="button" class="fea-fold-btn fea-clean-btn" aria-expanded="false">${line}${CHEV}</button>
      <span class="fea-clean-more fea-fold-body" hidden>${details.map((d) => `<span class="fea-line">${esc(d)}</span>`).join("")}</span></span>`;
}

/* ======================================================================================
 * THE ASSEMBLY: its parts and joins, the one answer in the fold's header.
 * ====================================================================================== */

/** An assembly's interfaces as marks on the part, in the hot spots' shape: one per place two parts
 *  touch (or pass into each other), tagged I1, I2, ... */
export function interfaceMarks(a) {
  return ((a && a.interfaces) || []).filter((i) => i.at_mm && i.at_mm.length === 3).map((i) => ({
    id: String(i.id || "").replace("Interface_", "I"), type: "interface_" + (i.kind || "touching"),
    shape: i.kind === "overlap" ? "parts overlap" : i.kind === "gap" ? "parts across a small gap" : "parts touch",
    at_mm: i.at_mm }));
}

/** What an assembly view shows, in one string: a later view that says more is drawn again. */
const assemblySig = (a) => `${(a.parts || []).length}/${(a.interfaces || []).length}/${(a.notes || []).length}/${a.measured === false ? 0 : 1}/${(a.pairs || []).length}`;

/** A length in millimetres as the stage says it: "0.19 mm". */
const mmWords = (v) => (v == null || isNaN(v) ? "a small gap" : `${Number(Number(v).toPrecision(2))} mm`);

/** PLAN-fea-19: what was measured of one pair, in words: "touch over 120 mm²", "sit 0.2 mm apart over
 *  45 mm²", "pass into each other (12% of the smaller part, about 0.4 mm deep)". */
export function pairMeasured(r) {
  const area = r.area_mm2 == null ? "" : ` over ${n3(r.area_mm2)} mm²`;
  if (r.kind === "gap") return `sit ${mmWords(r.gap_mm)} apart${area}`;
  if (r.kind === "overlap") {
    const bits = [];
    if (r.overlap_share != null) bits.push(`${n3(100 * r.overlap_share)}% of the smaller part`);
    if (r.depth_mm != null) bits.push(`about ${n3(r.depth_mm)} mm deep`);
    return `pass into each other${bits.length ? ` (${bits.join(", ")})` : ""}`;
  }
  return `touch${area}`;
}

/** The same in a word or two, for the pair's one line: "touch", "0.2 mm gap", "overlap 18%". */
function pairKind(r) {
  if (r.kind === "gap") return `${mmWords(r.gap_mm)} gap`;
  if (r.kind === "overlap") return `overlap${r.overlap_share != null ? ` ${Math.round(100 * r.overlap_share)}%` : ""}`;
  return "touch";
}

/** The type a pair takes by rule under the stage's answers (the server's `defaults`, by
 *  "bonded|on" = Bonded with small gaps joined, ...). */
export function pairDefault(r, conn, gapOn) {
  const k = `${conn === "contact" ? "contact" : "bonded"}|${gapOn === false ? "off" : "on"}`;
  return ((r.defaults || {})[k]) || r.type || "bonded";
}

/** One pair's row: its tag, its two parts, what was measured in a word, its type to pick; behind the row
 *  (a click opens it) what was measured in full, the reason, which part keeps an overlap and the reach. */
function pairRowHtml(r, tag, label, types, k0) {
  const [a, b] = r.parts || ["?", "?"];
  const name = (id) => label[id] || id;
  const opts = types.map((t) => `<option value="${attr(t.key)}"${t.key === r.type ? " selected" : ""}>${esc(t.label)}</option>`).join("");
  const keep = r.keep ? `<select class="fea-pair-keep" data-key="${attr(r.key)}" aria-label="${attr(`Which part keeps its full shape where ${a} and ${b} overlap`)}">
        ${[a, b].map((p) => `<option value="${attr(p)}"${p === r.keep ? " selected" : ""}>${esc(name(p))} keeps its shape</option>`).join("")}</select>` : "";
  // THE WAY ON FROM AN OPEN PAIR (PLAN-fea-21): a reach of the user's own, for a bonded pair; blank keeps the
  // measured reach (a small gap) or the solver's default (parts that touch)
  const reach = `<label class="fea-pair-reach-l"${r.type === "bonded" ? "" : " hidden"}>reach <input class="fea-pair-reach" type="number" min="0" step="any" data-key="${attr(r.key)}" aria-label="${attr(`Reach for ${a} and ${b}, in mm`)}" title="${attr(r.reach_mm != null ? `Blank: the measured reach, ${n3(r.reach_mm)} mm` : "Blank: the solver's own default")}" placeholder="${attr(r.reach_mm != null ? n3(r.reach_mm) : "auto")}"${r.type === "bonded" ? "" : " hidden"}> mm</label>`;
  return `<li class="fea-spot-row fea-itf-row fea-pair ${attr(r.kind || "touching")}" data-spot="${attr(tag)}" data-key="${attr(r.key)}" data-fold="pair:${attr(r.key)}"
      data-defaults="${attr(JSON.stringify(r.defaults || {}))}" data-whys="${attr(JSON.stringify(r.why || {}))}">
      <div class="fea-pair-line"><span class="fea-spot-id">${esc(tag)}</span>
        <span class="fea-pair-name" title="${attr(`${name(a)} and ${name(b)}`)}">${esc(name(a))} – ${esc(name(b))}</span>
        <span class="fea-pair-kind">${esc(pairKind(r))}</span>
        <select class="fea-pair-type" data-key="${attr(r.key)}" aria-label="${attr(`How ${a} and ${b} are joined`)}">${opts}</select>
        <button type="button" class="fea-fold-btn fea-pair-btn" aria-expanded="false" aria-label="${attr(`More about ${tag}`)}">${CHEV}</button></div>
      <div class="fea-pair-more fea-fold-body" hidden>
        <span class="fea-pair-what">${esc(name(a))} and ${esc(name(b))} ${esc(pairMeasured(r))}</span>
        <span class="fea-pair-why">${esc(((r.why || {})[k0]) || "")}</span>
        ${r.keep_why ? `<span class="fea-pair-why fea-keep-why">${esc(r.keep_why)}</span>` : ""}
        <span class="fea-pair-set">${keep}${reach}</span></div></li>`;
}

/** Show a pair's reach box only while it is bonded. */
function showReach(row, bonded) {
  row.querySelectorAll(".fea-pair-reach, .fea-pair-reach-l").forEach((el) => { el.hidden = !bonded; });
}

/** THE STAGE'S ANSWERS CHANGED (the whole-assembly question or the small-gap switch): every pair the
 *  user has not set follows the rule again, its reason with it. */
export function followPairDefaults(root) {
  const conn = (root.querySelector('input[name="fea-connection"]:checked') || {}).value || "bonded";
  const gapBox = root.querySelector('input[name="fea-gap-joins"]');
  const gapOn = gapBox ? !!gapBox.checked : true;
  const k = `${conn === "contact" ? "contact" : "bonded"}|${gapOn ? "on" : "off"}`;
  root.querySelectorAll(".fea-pair").forEach((row) => {
    const sel = row.querySelector(".fea-pair-type");
    if (!sel) return;
    let defaults = {}, whys = {};
    try { defaults = JSON.parse(row.dataset.defaults || "{}"); whys = JSON.parse(row.dataset.whys || "{}"); } catch (e) { /* the row keeps its value */ }
    if (sel.dataset.touched !== "1" && defaults[k]) sel.value = defaults[k];
    const why = row.querySelector(".fea-pair-why");
    if (why) why.textContent = sel.dataset.touched === "1" ? "Set by you for this pair." : (whys[k] || "");
    showReach(row, sel.value === "bonded");
  });
  // the line under the answer says what it means
  const said = root.querySelector(".fea-conn-said");
  const opt = root.querySelector('input[name="fea-connection"]:checked');
  if (said && opt) said.textContent = opt.dataset.words || "";
}

/** The pairs as the user left them: {key: {type, keep?, reach_mm?}} (the server keeps only the
 *  ones that differ from the rule). */
export function readPairs(root) {
  const out = {};
  root.querySelectorAll(".fea-pair").forEach((row) => {
    const key = row.dataset.key;
    const sel = row.querySelector(".fea-pair-type");
    if (!key || !sel) return;
    const r = { type: sel.value };
    const keep = row.querySelector(".fea-pair-keep");
    if (keep) r.keep = keep.value;
    const reach = row.querySelector(".fea-pair-reach");
    const mm = reach && !reach.hidden && reach.value !== "" ? Number(reach.value) : NaN;
    if (Number.isFinite(mm) && mm > 0) r.reach_mm = mm;
    out[key] = r;
  });
  return out;
}

/** AN ASSEMBLY MEASURED AFTER THE PANEL WAS DRAWN (the stage opened while the check was still
 *  measuring): its parts and interfaces are drawn in place, the answer already picked kept. True
 *  when they changed. */
export function followAssembly(root, a) {
  if (!a) return false;
  const box = root.querySelector(".fea-assembly");
  if (box && box.dataset.sig === assemblySig(a)) return false;
  const picked = root.querySelector('input[name="fea-connection"]:checked');
  const gapBox = root.querySelector('input[name="fea-gap-joins"]');
  const gapOn = gapBox ? gapBox.checked : null;
  const folds = openFolds(root);
  // THE PAIRS THE USER SET are kept too, by pair key
  const set = {};
  root.querySelectorAll(".fea-pair").forEach((row) => {
    const sel = row.querySelector(".fea-pair-type");
    const keep = row.querySelector(".fea-pair-keep");
    const reach = row.querySelector(".fea-pair-reach");
    set[row.dataset.key] = { type: sel && sel.dataset.touched === "1" ? sel.value : null,
      keep: keep && keep.dataset.touched === "1" ? keep.value : null, reach: reach ? reach.value : "" };
  });
  const html = assemblyHtml(a);
  if (box) box.outerHTML = html;
  else {
    const before = root.querySelector(".fea-blocked") || root.querySelector(".gc-actions");
    if (!before) return false;
    before.insertAdjacentHTML("beforebegin", html);
  }
  if (picked) {
    const again = root.querySelector(`input[name="fea-connection"][value="${picked.value}"]`);
    if (again) again.checked = true;
  }
  const gapAgain = root.querySelector('input[name="fea-gap-joins"]');
  if (gapAgain && gapOn !== null) gapAgain.checked = gapOn;
  root.querySelectorAll(".fea-pair").forEach((row) => {
    const was = set[row.dataset.key];
    if (!was) return;
    const sel = row.querySelector(".fea-pair-type");
    if (sel && was.type) { sel.value = was.type; sel.dataset.touched = "1"; }
    const keep = row.querySelector(".fea-pair-keep");
    if (keep && was.keep) { keep.value = was.keep; keep.dataset.touched = "1"; }
    const reach = row.querySelector(".fea-pair-reach");
    if (reach && was.reach) reach.value = was.reach;
  });
  reopen(root, folds);
  followPairDefaults(root);
  return true;
}

/** An assembly's fold: its parts, its joins and its one answer (bonded the default) in the header. `a` is the
 *  server's `fea.assembly`; nothing for a single part. */
export function assemblyHtml(a) {
  if (!a) return "";
  const label = {};
  (a.parts || []).forEach((p) => { if (p.name) label[p.id] = p.name; });
  const nParts = (a.parts || []).length;
  const parts = (a.parts || []).map((p) => `<li class="fea-part-row" data-part="${attr(p.id)}" title="${attr(p.id)}">${esc(p.name || p.id)}</li>`).join("");
  const types = a.pair_types || [{ key: "bonded", label: "Bonded" }, { key: "contact", label: "Contact" }, { key: "off", label: "Off" }];
  const k0 = `${a.default === "contact" ? "contact" : "bonded"}|${a.gap_default === false ? "off" : "on"}`;
  // PLAN-fea-18: THE SMALL GAPS are listed ONCE - as their own pairs, under the one switch that joins them
  const gaps = a.gap_joins || [];
  const gapIds = new Set(gaps.map((g) => g.id));
  const pairs = a.pairs || [];
  const row = (r) => pairRowHtml(r, String(r.id || "").replace("Interface_", "I"), label, types, k0);
  const joinRows = pairs.filter((r) => !gapIds.has(r.id)).map(row).join("");
  const gapRows = pairs.filter((r) => gapIds.has(r.id)).map(row).join("");
  // no pairs served (an older view): the interfaces themselves, one line each
  const itfRows = pairs.length ? "" : (a.interfaces || []).filter((i) => !gapIds.has(i.id)).map((i) => {
    const tag = String(i.id || "").replace("Interface_", "I");
    const ps = (i.parts || []).map((id) => label[id] || id);
    return `<li class="fea-spot-row fea-itf-row ${attr(i.kind || "touching")}" data-spot="${attr(tag)}"><span class="fea-spot-id">${esc(tag)}</span><span class="fea-pair-name">${esc(ps.join(" – "))}</span><span class="fea-pair-kind">${esc(pairKind(i))}</span></li>`;
  }).join("");
  const gapList = gapRows || gaps.map((g) => `<li class="fea-spot-row fea-itf-row gap" data-spot="${attr(String(g.id || "").replace("Interface_", "I"))}"><span class="fea-spot-id">${esc(String(g.id || "").replace("Interface_", "I"))}</span><span>${esc(g.words)}</span></li>`).join("");
  const nJoins = pairs.length || (a.interfaces || []).length;
  const joins = joinRows || itfRows;
  const joinsHtml = joins ? `<div class="fea-sub-h">Joins <span class="fea-spot-n">${nJoins - gaps.length}</span></div>
      <ul class="fea-spots fea-itfs" aria-label="Where the parts touch">${joins}</ul>` : "";
  const gapHtml = gaps.length ? `<div class="fea-gaps">
      <label class="fea-gap-opt fea-sub-h" title="${attr(a.gap_question || "")}"><input type="checkbox" name="fea-gap-joins"${a.gap_default === false ? "" : " checked"}> Join small gaps <span class="fea-spot-n">${gaps.length}</span> <span class="fea-muted">welds and threads the CAD leaves out</span></label>
      <ul class="fea-spots fea-itfs fea-gap-rows" aria-label="Small gaps">${gapList}</ul></div>` : "";
  // NOT MEASURED YET is not "nothing touches" (the stage can open before the measurement lands)
  const none = joins || gaps.length ? ""
    : `<div class="fea-line fea-muted">${a.measured === false ? "Where the parts touch is still being measured…" : "No two parts touch."}</div>`;
  const free = (a.free_parts || []).length
    ? `<div class="fea-line fea-muted">${esc(a.free_parts.map((id) => label[id] || id).join(", "))}: touches no other part.</div>` : "";
  // the measurement's own notes, under one fold: the small-gap note is the gap list again, so it is left out
  const notes = [...(a.join_notes || []).filter((n) => !(gaps.length && /across a small gap/.test(n))),
    ...(a.overlap_note ? [a.overlap_note] : [])];
  const notesHtml = notes.length ? `<div class="fea-notes" data-fold="asm-notes">
      <button type="button" class="fea-fold-btn fea-sub-h fea-notes-btn" aria-expanded="false">${CHEV}Notes <span class="fea-spot-n">${notes.length}</span></button>
      <div class="fea-fold-body" hidden>${notes.map((n) => `<div class="fea-line fea-muted">${esc(n)}</div>`).join("")}</div></div>` : "";
  const dflt = a.default || "bonded";
  const optsList = a.options || [];
  const seg = optsList.length ? `<div class="fea-conn fea-seg" role="radiogroup" aria-label="${attr(a.question || "How are the parts connected?")}">${optsList.map((o) =>
    `<label class="fea-conn-opt" title="${attr(o.words)}"><input type="radio" name="fea-connection" value="${attr(o.key)}" data-words="${attr(`${o.label}: ${o.words}.`)}"${o.key === dflt ? " checked" : ""}><span>${esc(o.label)}</span></label>`).join("")}</div>` : "";
  const picked = optsList.find((o) => o.key === dflt);
  const head = `<span class="fea-sec-t">Parts &amp; joins</span><span class="fea-sec-sum">${esc(plural(nParts || parseInt(a.summary, 10) || 0, "part"))} · ${esc(plural(nJoins, "join"))}</span>`;
  const body = `${picked ? `<div class="fea-line fea-muted fea-conn-said">${esc(`${picked.label}: ${picked.words}.`)}</div>` : ""}
      ${joinsHtml}${gapHtml}${none}${free}
      ${subFoldHtml("asm-parts", `Parts <span class="fea-spot-n">${nParts}</span>`, `<ul class="fea-parts" aria-label="The parts of the assembly">${parts}</ul>`, "fea-parts-fold")}
      ${notesHtml}`;
  return `<div class="fea-assembly" data-sig="${attr(assemblySig(a))}">${secHtml(head, body, { aside: seg, cls: "fea-sec-asm" })}</div>`;
}

/* ======================================================================================
 * THE HOT SPOTS: shown only - version 1 refines every one automatically.
 * ====================================================================================== */

/** One hot spot in a few words, its size in the file's numbers: "90° corner", "hole · r 7.5 mm". */
function spotWords(h, p) {
  const u = `<span class="gc-u">${esc(lenUnit(p))}</span>`;
  if (h.type === "sharp_corner") return `${esc(String(Math.round(h.angle_deg)))}° corner`;
  const name = { hole: "hole", fillet: "fillet", notch: "notch", groove: "groove" }[h.shape] || "spot";
  return `${esc(name)} · r ${esc(len(h.radius_mm, p))} ${u}`;
}

/** One type's spots in one line: "160 · 14° to 149°, refinement capped" / "29 · radius 1.2 to 15 mm". */
export function groupSummary(type, rows, p) {
  const n = rows.length;
  if (type === "sharp_corner") {
    const a = rows.map((h) => Math.round(Number(h.angle_deg))).filter((v) => !isNaN(v));
    if (!a.length) return `${n}`;
    const lo = Math.min(...a), hi = Math.max(...a);
    return `${n} · ${lo === hi ? `${lo}°` : `${lo}° to ${hi}°`}, refinement capped`;
  }
  const r = rows.map((h) => Number(h.radius_mm)).filter((v) => !isNaN(v));
  if (!r.length) return `${n}`;
  const lo = Math.min(...r), hi = Math.max(...r);
  const u = lenUnit(p);
  return `${n} · radius ${lo === hi ? len(lo, p) : `${len(lo, p)} to ${len(hi, p)}`} ${u}`;
}

const measureState = (m) => (!m ? "none" : m.measured ? "measured" : "unmeasured");
/** The hot spots are listed in groups, one per type, in the order the server sends them (sharpest
 *  and smallest first). */
const GROUP_WORDS = { sharp_corner: "Sharp inside corners", notch: "Notches and grooves", fillet: "Fillets", hole: "Holes" };

/** The hot spots in groups by type: [[type, [spot, ...]], ...], in the order the types first come. */
function spotGroups(spots) {
  const groups = new Map();
  spots.forEach((h) => {
    const k = GROUP_WORDS[h.type] ? h.type : "other";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(h);
  });
  return [...groups.entries()];
}

/** What the measurement found (cad/fea_measure): the thinnest wall (a key fact) and the hot spots' fold, each
 *  type its own sub-row with its count and range. `m` is the server's `fea.measure`; nothing at all before the
 *  part was measured. */
export function measureHtml(m, p) {
  return `<div class="fea-measure" data-state="${measureState(m)}">${measureBody(m, p)}</div>`;
}

function measureBody(m, p) {
  if (!m) return "";
  if (!m.measured) {
    return `<div class="rc-row"><div class="rc-k">Hot spots</div><div class="rc-v fea-unmeasured">Not measured: ${esc(m.reason || "")}</div></div>`;
  }
  const u = `<span class="gc-u">${esc(lenUnit(p))}</span>`;
  // no wall measured: no row (a "not measured" line beside a shell card that names its walls reads as a contradiction)
  const wallRow = m.thickness_min_mm == null ? ""
    : `<div class="rc-row"><div class="rc-k">Thinnest wall</div><div class="rc-v fea-wall">${esc(len(m.thickness_min_mm, p))} ${u}${m.thickness_face ? ` <span class="fea-where">${esc(m.thickness_face)}</span>` : ""}</div></div>`;
  const contacts = m.contacts ? `<div class="fea-line fea-muted fea-contacts">${esc(plural(Number(m.contacts), "place"))} where parts touch (not corners)</div>` : "";
  const spots = m.hot_spots || [];
  if (!spots.length) {
    return `${wallRow}<div class="rc-row"><div class="rc-k">Hot spots</div><div class="rc-v fea-spots-sum">none found</div></div>${contacts}`;
  }
  // EVERY hot spot is listed, in a group per type; each group opens on a click (a spot left out looks the same
  // as one never found)
  const list = spotGroups(spots).map(([type, rows]) => {
    const lines = rows.map((h) =>
      `<li class="fea-spot-row" data-spot="${attr(h.id)}"><span class="fea-spot-id">${esc(h.id)}</span><span>${spotWords(h, p)}</span></li>`).join("");
    const sum = groupSummary(type, rows, p).replace(/^\d+( · )?/, "").replace(", refinement capped", ", capped");
    return `<li class="fea-spot-group" data-type="${attr(type)}" data-fold="spots:${attr(type)}">
      <button type="button" class="fea-fold-btn fea-grp-btn" aria-expanded="false">${CHEV}<span class="fea-grp-t">${esc(GROUP_WORDS[type] || "Other hot spots")}</span><span class="fea-spot-n">${rows.length}</span><span class="fea-spot-sum">${esc(sum)}</span></button>
      <ul class="fea-spot-rows fea-fold-body" hidden>${lines}</ul></li>`;
  }).join("");
  // only a part past the record's own limit (thousands of hot spots) has any the server did not send
  const unsent = m.more ? `<div class="fea-line fea-muted fea-spot-unsent">${m.more} more found than the measurement keeps</div>` : "";
  const capped = spots.some((h) => h.capped)
    ? `<div class="fea-line fea-muted">Sharp corners: refinement stops at a set level (their stress never settles).</div>` : "";
  const head = `<span class="fea-sec-t">Hot spots</span><span class="fea-sec-n">${spots.length + (m.more || 0)}</span><span class="fea-sec-sum">refined automatically</span>`;
  return `${wallRow}${secHtml(head, `<ul class="fea-spots" aria-label="Hot spots the mesh will be refined at">${list}</ul>${unsent}${capped}${contacts}`, { cls: "fea-sec-spots" })}`;
}

/** Wire the hot-spot list (and every other tagged row): `light(id)` (if given) runs when a line is clicked, and
 *  `light(id, "hover")` while it is pointed at (`light(null, "hover")` when the pointer leaves): pointing shows
 *  the line's mark on the part, a click chooses it. */
export function bindMeasure(root, light) {
  if (!light) return;
  root.querySelectorAll(".fea-spot-row").forEach((r) => {
    if (r.dataset.lit === "1") return;
    r.dataset.lit = "1";
    r.addEventListener("mouseenter", () => light(r.dataset.spot, "hover"));
    r.addEventListener("mouseleave", () => light(null, "hover"));
    r.addEventListener("click", () => light(r.dataset.spot));
  });
}

/** Light one tagged line, opening every fold it sits in first (a mark on the part was clicked). */
export function lightSpotRow(root, id) {
  root.querySelectorAll(".fea-spot-row").forEach((r) => {
    const on = r.dataset.spot === id;
    if (on && r.closest("[hidden]")) { reveal(r); if (r.scrollIntoView) r.scrollIntoView({ block: "nearest" }); }
    r.classList.toggle("sel", on);
  });
}

/** A measurement that lands after the panel was drawn (the stage opened while the check was
 *  still naming): the wall and hot-spot lines are drawn in place. True when they changed. */
export function followMeasure(root, m, p, light) {
  const box = root.querySelector(".fea-measure");
  if (!box || box.dataset.state === measureState(m)) return false;
  const folds = openFolds(root);
  box.dataset.state = measureState(m);
  box.innerHTML = measureBody(m, p);
  reopen(box, folds);
  bindMeasure(box, light);
  return true;
}

/** THE FULL READING LANDED AFTER THE PANEL WAS DRAWN (the stage opened on the quick preview of a
 *  big file): its bodies and its clean check are said in place. True when they changed. */
export function followClean(root, f) {
  if (!f) return false;
  let moved = false;
  const bodies = root.querySelector(".fea-bodies");
  if (bodies && bodies.textContent !== bodiesHtml(f)) { bodies.textContent = bodiesHtml(f); moved = true; }
  const clean = root.querySelector(".fea-clean");
  if (clean && clean.dataset.sig !== cleanSig(f)) { clean.outerHTML = cleanHtml(f); moved = true; }
  // A STOP THE FULL READING FOUND (a surface, not a closed solid): said, Proceed off, the way on
  const btn = root.querySelector(".gc-proceed"), hint = root.querySelector(".gc-hint");
  const blocked = f.single_part_only || "";
  let box = root.querySelector(".fea-blocked");
  if (blocked && !box) {
    const acts = root.querySelector(".gc-actions");
    if (acts) acts.insertAdjacentHTML("beforebegin", `<div class="fea-blocked" role="alert">${esc(blocked)}</div>`);
    box = root.querySelector(".fea-blocked");
    moved = true;
  } else if (box && !blocked) { box.remove(); moved = true; }
  else if (box && box.textContent !== blocked) { box.textContent = blocked; moved = true; }
  if (btn && btn.textContent !== "Confirming…") btn.disabled = !!blocked;
  if (hint && blocked) hint.textContent = f.way_out || "Upload a single part to go on.";
  return moved;
}

/* ======================================================================================
 * SHSTAGE-2..4: A SHELL, ONLY WHEN THE USER ASKED FOR ONE IN WORDS.
 *
 * A shell is the part's mid-surface meshed with plate elements. The stage never offers or suggests one:
 * the server sends a `shell` view only when the user's words asked for a shell, and the panel then shows
 * one fold - its header says how much of the part is a shell, or that it is still being made, or that no
 * shell can be made (the part is meshed solid); inside, what is not in the shell, where plates do not meet
 * (red on the part) and the legend of the mid-surface's colours, the wall in mm. No view: nothing at all.
 * ====================================================================================== */

/** The mid-surface's colours: a muted sequential ramp (cividis-like), thin walls dark blue to thick
 *  walls sand. Stops are [r, g, b] in 0..1. */
export const SHELL_RAMP = [[0.15, 0.24, 0.42], [0.30, 0.36, 0.45], [0.48, 0.48, 0.47], [0.68, 0.62, 0.45], [0.86, 0.79, 0.47]];

/** The colour of a wall `t` between `lo` and `hi` (mm), on SHELL_RAMP. A part of one wall takes the ramp's
 *  light end: the ramp's middle is the part's own grey, and its mid-surface could not be told from it. */
export function wallColour(t, lo, hi) {
  const span = Number(hi) - Number(lo);
  const u = !(span > 1e-9) ? 1 : Math.min(1, Math.max(0, (Number(t) - Number(lo)) / span));
  const x = u * (SHELL_RAMP.length - 1), i = Math.min(SHELL_RAMP.length - 2, Math.floor(x)), f = x - i;
  return SHELL_RAMP[i].map((a, k) => a + (SHELL_RAMP[i + 1][k] - a) * f);
}

const rgb = (c) => `rgb(${c.map((v) => Math.round(v * 255)).join(",")})`;
const mmText = (v) => (v == null || isNaN(v) ? "?" : `${Number(Number(v).toPrecision(3))} mm`);

/** The legend: the ramp from the thinnest wall to the thickest, in mm. */
export function shellLegendHtml(s) {
  const [lo, hi] = (s && s.legend_mm) || [];
  if (lo == null || hi == null) return "";
  const stops = SHELL_RAMP.map((c, i) => `${rgb(c)} ${Math.round(100 * i / (SHELL_RAMP.length - 1))}%`).join(", ");
  const one = Math.abs(hi - lo) <= 1e-6;
  return `<div class="fea-shell-legend" aria-label="Wall thickness on the mid-surface" title="The coloured surface on the part is the mid-surface the shell is meshed on">
      <span class="fea-shell-legend-k">Wall</span>
      <span class="fea-shell-ramp" style="background:linear-gradient(90deg, ${one ? `${rgb(wallColour(lo, lo, hi))}, ${rgb(wallColour(lo, lo, hi))}` : stops})"></span>
      <span class="fea-shell-legend-v">${one ? esc(mmText(lo)) : `${esc(mmText(lo))} – ${esc(mmText(hi))}`}</span>
    </div>`;
}

/** The marks on the part for what stays solid (S1, S2 ...) and where plates do not meet (O1, O2 ...),
 *  in the hot spots' shape. */
export function shellMarks(s) {
  return ((s && s.not_shelled) || []).filter((r) => r.id && r.at_mm && r.at_mm.length === 3).map((r) => ({
    id: r.id, type: r.kind === "open" ? "shell_open" : "shell_solid",
    shape: r.kind === "open" ? "plates do not meet" : "stays solid", at_mm: r.at_mm }));
}

/** One string for what a shell view shows: a later view that says more is drawn again. */
const meshAsSig = (s) => `${s.state}/${s.mesh_as || ""}/${(s.not_shelled || []).length}/${s.reason || ""}/${s.overlay ? 1 : 0}`;

/** Whether the choice draws the mid-surface: a shell (or a shell + solid confirmed before it left the stage). */
export const showsShell = (eff) => eff === "shell" || eff === "mixed";

/** What the part is meshed as, by the server's view: "shell", "solid" (a shell asked for that cannot be made),
 *  or "" with no view (a plain solid). */
export function effectiveMeshAs(s) {
  return (s && s.mesh_as) || "";
}

/** The shell fold's header line: how much of the part is a shell. */
function shellSummary(s) {
  if (!showsShell(effectiveMeshAs(s))) return "not possible · meshed solid";
  if (s.state === "measuring") return `<span class="gc-spin" aria-hidden="true"></span> making the mid-surface…`;
  const bodies = s.bodies || [];
  const solid = (s.not_shelled || []).filter((r) => r.kind === "solid" && r.id).length;
  const open = Number(s.open_edges) > 0 ? (s.not_shelled || []).filter((r) => r.kind === "open" && r.id).length || Number(s.open_edges) : 0;
  const bits = [];
  if (bodies.length > 1) {
    const sh = bodies.filter((b) => b.verdict !== "solid").length;
    bits.push(`${sh} of ${bodies.length} parts as shell`, `${bodies.length - sh} stay solid`);
  } else bits.push(solid ? `${plural(solid, "region")} stay${solid === 1 ? "s" : ""} solid` : "whole part");
  if (open) bits.push(`${open} open`);
  return esc(bits.join(" · "));
}

function meshAsBody(s) {
  const shell = showsShell(effectiveMeshAs(s));
  const measuring = shell && s.state === "measuring"
    ? `<div class="fea-line fea-muted fea-meshas-measuring"><span class="gc-spin" aria-hidden="true"></span> ${esc(s.measuring_words || "")}</div>` : "";
  const reason = s.reason ? `<div class="fea-line fea-muted fea-meshas-why">${esc(s.reason)}</div>` : "";
  const rows = shell ? (s.not_shelled || []).map((r) => r.id
    ? `<li class="fea-spot-row fea-shell-row ${attr(r.kind)}" data-spot="${attr(r.id)}" title="${attr(r.words)}"><span class="fea-spot-id">${esc(r.id)}</span><span>${esc(r.short || r.words)}</span></li>`
    : `<li class="fea-shell-head ${attr(r.kind)}" title="${attr(r.words)}">${esc(r.short || r.words)}</li>`).join("") : "";
  const nRows = shell ? (s.not_shelled || []).filter((r) => r.id).length : 0;
  const list = rows ? subFoldHtml("shell-not", `Not in the shell${nRows ? ` <span class="fea-spot-n">${nRows}</span>` : ""}`,
    `<ul class="fea-spots fea-shell-rows" aria-label="Not in the shell">${rows}</ul>`, "fea-shell-list") : "";
  const legend = shell && s.overlay ? `<div class="fea-shell-show">${shellLegendHtml(s)}</div>` : "";
  const said = shell && s.said_words ? ` <span class="fea-muted fea-meshas-said" title="${attr(s.said_words)}">· as you asked</span>` : "";
  const head = `<span class="fea-sec-t">Shell</span><span class="fea-sec-sum">${shellSummary(s)}</span>`;
  return secHtml(head,`<div class="fea-line fea-meshas-v"><b>${esc(s.label || "")}</b>${said}</div>
    ${measuring}${reason}${legend}${list}`, { cls: "fea-sec-shell" });
}

/** The shell fold for the panel; nothing without a `shell` view (a solid, as before shell existed). */
export function meshAsHtml(s) {
  if (!s) return "";
  return `<div class="fea-meshas" data-sig="${attr(meshAsSig(s))}">${meshAsBody(s)}</div>`;
}

/** The purpose tag beside the title: "FEA", "FEA · shell", "FEA · solid" (a shell asked for that cannot be made). */
function purposeTag(s) {
  const eff = effectiveMeshAs(s);
  return showsShell(eff) ? "FEA · shell" : s ? "FEA · solid" : "FEA";
}

/** THE SHELL CHECK LANDS AFTER THE PANEL WAS DRAWN (it takes minutes): the shell fold is drawn again in place (or
 *  added, for a shell asked for after the stage opened). True when it changed. */
export function followMeshAs(root, s, changed) {
  if (!root || !s) return false;
  let box = root.querySelector(".fea-meshas");
  const folds = openFolds(root);
  if (!box) {
    const at = root.querySelector(".fea-assembly") || root.querySelector(".fea-blocked") || root.querySelector(".gc-actions");
    if (!at) return false;
    at.insertAdjacentHTML("beforebegin", meshAsHtml(s));
    box = root.querySelector(".fea-meshas");
  } else if (box.dataset.sig === meshAsSig(s)) return false;
  else {
    box.dataset.sig = meshAsSig(s);
    box.innerHTML = meshAsBody(s);
    reopen(box, folds);
  }
  const tag = root.querySelector(".fea-tag");
  if (tag) tag.textContent = purposeTag(s);
  if (changed) changed(effectiveMeshAs(s));
  return true;
}

/* ======================================================================================
 * THE PANEL
 * ====================================================================================== */

/** The panel's title: the part's own name; for an assembly the file's (a part's name would read as the whole). */
function titleOf(f, p) {
  if (f.title) return f.title;
  if (f.assembly || f.bodies > 1) return "Your part";
  return p.part || "Your part";
}

/** The panel's body for one check: `f` is the server's `fea` view, `p` the proposal (size, unit). */
export function feaPanelHtml(f, p) {
  const blocked = f.single_part_only || "";
  const title = titleOf(f, p);
  return `<div class="v-row v-titlerow fea-head"><b class="v-title fea-title" title="${attr(title)}">${esc(title)}</b>
      <span class="fea-tag" title="Every face is kept as a named group, so you set the supports, loads and materials in your own solver.">${esc(purposeTag(f.shell))}</span></div>
    <div class="gc-form fea-form">
      <div class="fea-facts">
        <div class="rc-row"><div class="rc-k">Size</div><div class="rc-v fea-size">${sizeHtml(p)}</div></div>
        <div class="rc-row fea-unit-row"><div class="rc-k">Unit</div><div class="rc-v">${unitBoxHtml(p)}</div></div>
        <div class="rc-row"><div class="rc-k">Parts</div><div class="rc-v fea-bodies">${esc(bodiesHtml(f))}</div></div>
        <div class="rc-row fea-clean-row"><div class="rc-k">Check</div><div class="rc-v">${cleanHtml(f)}</div></div>
        ${measureHtml(f.measure, p)}
      </div>
      ${meshAsHtml(f.shell)}
      ${assemblyHtml(f.assembly)}
      ${blocked ? `<div class="fea-blocked" role="alert">${esc(blocked)}</div>` : ""}
      <div class="gc-actions fea-foot"><button class="gc-proceed" type="button"${blocked ? " disabled" : ""} title="${attr(f.question || "Is this your part and are the units right?")}">Yes, proceed</button>
        <span class="gc-hint">${blocked ? esc(f.way_out || "Upload a single part to go on.") : "Right part, right unit?"}</span></div>
    </div>`;
}

/** What the user confirmed: the unit they say the file is in, and the numbers it was read under.
 *  The server sets everything else for an FEA part (a solid body, its bodies and clean check). */
export function readFeaForm(root, p) {
  const body = { part: p.part || "", size_mm: p.size_mm || null, unit: unitOf(p),
                 scale_to_m: p.scale_to_m == null ? null : Number(p.scale_to_m) };
  const conn = root.querySelector ? root.querySelector('input[name="fea-connection"]:checked') : null;
  if (conn) body.fea_connection = conn.value;
  const gapBox = root.querySelector ? root.querySelector('input[name="fea-gap-joins"]') : null;
  if (gapBox) body.fea_gap_joins = !!gapBox.checked;
  // PLAN-fea-19/20: each pair as it is set (the server keeps the ones that differ from the rule)
  if (root.querySelector && root.querySelector(".fea-pair")) body.fea_pairs = readPairs(root);
  const upSel = root.closest ? root.closest(".gc-stage")?.querySelector(".v-up select") : null;
  const up = (upSel && upSel.value) || p.up_axis;
  if (up) body.up_axis = up;
  return body;
}

/** Wire the panel: the folds open on a click, the unit box follows every size shown, and Proceed confirms -
 *  after the unit is picked when the size makes it doubtful. `done()` runs once the server has the answer. */
export function bindFeaPanel(root, f, p, { confirm, done }) {
  bindUnit(root, p);
  const form = root.querySelector(".fea-form") || root;
  // THE FOLDS, one listener for the form, so a fold drawn again later opens too: a fold's button, or a pair's
  // line anywhere but its own controls
  form.addEventListener("click", (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    let btn = t.closest(".fea-fold-btn");
    if (!btn) {
      const line = t.closest(".fea-pair-line");
      if (line && !t.closest("select,input,label")) btn = line.querySelector(".fea-fold-btn");
    }
    if (!btn || !form.contains(btn)) return;
    const w = btn.closest("[data-fold]");
    if (w) setFold(w, !w.classList.contains("open"));
  });
  // THE PAIRS (PLAN-fea-19/20), one listener for the form, so a list drawn again later is wired too: a
  // pair the user sets is theirs; the whole-assembly answer and the small-gap switch move the rest
  form.addEventListener("change", (e) => {
    const t = e.target;
    if (!t || !t.classList) return;
    if (t.classList.contains("fea-pair-type") || t.classList.contains("fea-pair-keep")) {
      t.dataset.touched = "1";
      const row = t.closest(".fea-pair");
      if (row && t.classList.contains("fea-pair-keep")) {
        const why = row.querySelector(".fea-keep-why");
        if (why) why.textContent = `${t.value} keeps its full shape there (set by you).`;
      }
      followPairDefaults(form);
    } else if (t.name === "fea-connection" || t.name === "fea-gap-joins") {
      followPairDefaults(form);
    }
  });
  const btn = form.querySelector(".gc-proceed"), hint = form.querySelector(".gc-hint");
  if (!btn) return;
  btn.onclick = async () => {
    if (f.single_part_only) return;
    if (unitChoiceNeeded(p)) {
      hint.textContent = unitChoiceHint(p);
      const pick = form.querySelector(".gc-pick"); if (pick) pick.focus();
      return;
    }
    btn.disabled = true; btn.textContent = "Confirming…";
    try {
      await confirm(readFeaForm(form, p), "");
      form.querySelectorAll("input,select,button:not(.fea-fold-btn)").forEach((el) => { el.disabled = true; });
      const acts = form.querySelector(".gc-actions");
      if (acts) acts.innerHTML = '<span class="gc-done">✓ Confirmed. Next: what you will analyze.</span>';
      if (done) done();
    } catch (e) {
      btn.disabled = false; btn.textContent = "Yes, proceed";
      hint.textContent = "Could not confirm: " + ((e && e.message) || "try again");
    }
  };
}
