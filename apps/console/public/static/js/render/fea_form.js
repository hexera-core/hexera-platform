// Responsibility: Build the FEA part check - the part's size in the file's unit, the unit box, how
// many bodies the file has and what the clean check found, and the one question "Is this your part
// and are the units right?" - and read the answer back out of it. Below them, what the measurement
// found (FEA build step 3): the thinnest wall and the hot spots the mesh will be refined at, shown
// only (version 1 refines them all automatically; nothing here edits them). For an ASSEMBLY (a file
// of several bodies): its parts, where they touch or pass into each other (marked on the part), and
// its one question, "How are the parts connected?" - bonded (the default) or contact - with every
// pair listed under it (PLAN-fea-19/20): what was measured, its type to pick (Bonded / Contact / Off,
// the answer by default) and, where parts pass into each other, which part keeps its full shape.
// Boundaries: markup and a reading of it. No fluid, no openings, no flow axis and no stickers: an
// FEA part is meshed as itself. It fetches nothing and starts no run; the stage and the card both
// use it, and confirming is the callback they were handed.
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

/** The size row: the three sides, the unit beside them. */
function sizeHtml(p) {
  const s = (p.size_mm || []).map((v) => len(v, p));
  return s.length === 3 ? `${esc(s.join(" × "))} <span class="gc-u">${esc(lenUnit(p))}</span>` : "not measured";
}

function bodiesHtml(f) {
  const n = f.bodies;
  // the clean check counts the pieces a solid is made of: one solid of two overlapping pieces is
  // said as such, so the two lines never disagree
  if (n === 1 && f.pieces > 1) return `1 solid, made of ${f.pieces} pieces that pass into each other`;
  if (n === 1) return "1 (a single part)";
  if (n > 1) return `${n} (an assembly: the parts are meshed together)`;
  return "not counted";
}

/** An assembly's interfaces as marks on the part, in the hot spots' shape: one per place two parts
 *  touch (or pass into each other), tagged I1, I2, ... */
export function interfaceMarks(a) {
  return ((a && a.interfaces) || []).filter((i) => i.at_mm && i.at_mm.length === 3).map((i) => ({
    id: String(i.id || "").replace("Interface_", "I"), type: "interface_" + (i.kind || "touching"),
    shape: i.kind === "overlap" ? "parts overlap" : i.kind === "gap" ? "parts across a small gap" : "parts touch",
    at_mm: i.at_mm }));
}

function partLabel(p) {
  return `<b>${esc(p.id)}</b>${p.name ? ` · ${esc(p.name)}` : ""}`;
}

/** What an assembly view shows, in one string: a later view that says more is drawn again. */
const assemblySig = (a) => `${(a.parts || []).length}/${(a.interfaces || []).length}/${(a.notes || []).length}/${a.measured === false ? 0 : 1}/${(a.pairs || []).length}`;

/** Text for an attribute in double quotes (esc() leaves quotes alone: a reason or a JSON map has them). */
const attr = (s) => esc(s).replace(/"/g, "&quot;");

/** A number as the stage says it: three figures at most. */
const n3 = (v) => String(Number(Number(v).toPrecision(3)));

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

/** The type a pair takes by rule under the stage's answers (the server's `defaults`, by
 *  "bonded|on" = Bonded with small gaps joined, ...). */
export function pairDefault(r, conn, gapOn) {
  const k = `${conn === "contact" ? "contact" : "bonded"}|${gapOn === false ? "off" : "on"}`;
  return ((r.defaults || {})[k]) || r.type || "bonded";
}

/** One pair's row: its tag, its two parts, what was measured, its type to pick and - where the
 *  parts pass into each other - which part keeps the shared piece; the reason under it. */
function pairRowHtml(r, tag, label, types, k0) {
  const [a, b] = r.parts || ["?", "?"];
  const name = (id) => label[id] ? `${id} · ${label[id]}` : id;
  const opts = types.map((t) => `<option value="${attr(t.key)}"${t.key === r.type ? " selected" : ""}>${esc(t.label)}</option>`).join("");
  const keep = r.keep ? `<select class="fea-pair-keep" data-key="${attr(r.key)}" aria-label="${attr(`Which part keeps its full shape where ${a} and ${b} overlap`)}">
        ${[a, b].map((p) => `<option value="${attr(p)}"${p === r.keep ? " selected" : ""}>${esc(p)} keeps its shape</option>`).join("")}</select>` : "";
  // THE WAY ON FROM AN OPEN PAIR (PLAN-fea-21): a reach of the user's own, for a bonded pair; blank keeps the
  // measured reach (a small gap) or the solver's default (parts that touch)
  const reach = `<input class="fea-pair-reach" type="number" min="0" step="any" data-key="${attr(r.key)}" aria-label="${attr(`Reach for ${a} and ${b}, in mm`)}" title="${attr(r.reach_mm != null ? `Blank: the measured reach, ${n3(r.reach_mm)} mm` : "Blank: the solver's own default")}" placeholder="${attr(r.reach_mm != null ? `reach ${n3(r.reach_mm)} mm` : "reach, mm")}"${r.type === "bonded" ? "" : " hidden"}>`;
  return `<li class="fea-spot-row fea-itf-row fea-pair ${attr(r.kind || "touching")}" data-spot="${attr(tag)}" data-key="${attr(r.key)}"
      data-defaults="${attr(JSON.stringify(r.defaults || {}))}" data-whys="${attr(JSON.stringify(r.why || {}))}">
      <span class="fea-spot-id">${esc(tag)}</span>
      <span class="fea-pair-body"><span class="fea-pair-what">${esc(name(a))} and ${esc(name(b))} ${esc(pairMeasured(r))}</span>
        <span class="fea-pair-set"><select class="fea-pair-type" data-key="${attr(r.key)}" aria-label="${attr(`How ${a} and ${b} are joined`)}">${opts}</select>${keep}${reach}</span>
        <span class="fea-pair-why">${esc(((r.why || {})[k0]) || "")}</span>
        ${r.keep_why ? `<span class="fea-pair-why fea-keep-why">${esc(r.keep_why)}</span>` : ""}</span></li>`;
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
    const reach = row.querySelector(".fea-pair-reach");
    if (reach) reach.hidden = sel.value !== "bonded";
  });
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

/** A length in millimetres as the stage says it: "0.19 mm". */
const mmWords = (v) => (v == null || isNaN(v) ? "a small gap" : `${Number(Number(v).toPrecision(2))} mm`);

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
    const after = root.querySelector(".fea-measure");
    if (!after) return false;
    after.insertAdjacentHTML("afterend", html);
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
  followPairDefaults(root);
  return true;
}

/** An assembly's parts, its interfaces and its one question (bonded the default). `a` is the
 *  server's `fea.assembly`; nothing for a single part. */
export function assemblyHtml(a) {
  if (!a) return "";
  const parts = (a.parts || []).map((p) => `<li class="fea-part-row" data-part="${esc(p.id)}">${partLabel(p)}</li>`).join("");
  // PLAN-fea-19/20: EVERY PAIR with its type to pick (and who keeps an overlap), when the server sends them
  const label = {};
  (a.parts || []).forEach((p) => { if (p.name) label[p.id] = p.name; });
  const types = a.pair_types || [{ key: "bonded", label: "Bonded" }, { key: "contact", label: "Contact" }, { key: "off", label: "Off" }];
  const k0 = `${a.default === "contact" ? "contact" : "bonded"}|${a.gap_default === false ? "off" : "on"}`;
  const pairRows = (a.pairs || []).map((r) => pairRowHtml(r, String(r.id || "").replace("Interface_", "I"), label, types, k0)).join("");
  const rows = pairRows || (a.interfaces || []).map((i) => {
    const tag = String(i.id || "").replace("Interface_", "I");
    const what = i.kind === "overlap"
      ? (i.overlap_share == null ? "pass into each other"
        : `pass into each other (${esc(String(Math.round(100 * i.overlap_share)))}% of the smaller part)`)
      : i.kind === "gap" ? `sit ${esc(mmWords(i.gap_mm))} apart: joined across the gap` : "touch";
    return `<li class="fea-spot-row fea-itf-row ${esc(i.kind || "touching")}" data-spot="${esc(tag)}"><span class="fea-spot-id">${esc(tag)}</span><span>${esc((i.parts || []).join(" and "))} ${what}</span></li>`;
  }).join("");
  const free = (a.free_parts || []).length
    ? `<div class="gc-note">${esc(a.free_parts.join(", "))}: touches no other part.</div>` : "";
  // NOT MEASURED YET is not "nothing touches" (the stage can open before the measurement lands)
  const none = a.measured === false
    ? `<div class="gc-note">Where the parts touch is still being measured…</div>`
    : `<div class="gc-note">No place where two parts touch was found.</div>`;
  // PLAN-fea-18: THE SMALL-GAP JOINS, a proposal with one switch (on by default)
  const gaps = a.gap_joins || [];
  const gapHtml = gaps.length ? `<div class="fea-gaps">
      <label class="fea-gap-opt"><input type="checkbox" name="fea-gap-joins"${a.gap_default === false ? "" : " checked"}> ${esc(a.gap_question || "Join parts across small gaps")}</label>
      <ul class="fea-gap-list">${gaps.map((g) => `<li>${esc(g.words)}</li>`).join("")}</ul>
    </div>` : "";
  const joinNotes = (a.join_notes || []).map((n) => `<div class="gc-note">${esc(n)}</div>`).join("");
  const opts = (a.options || []).map((o) => `<label class="fea-conn-opt"><input type="radio" name="fea-connection" value="${esc(o.key)}"${o.key === (a.default || "bonded") ? " checked" : ""}> <b>${esc(o.label)}</b> <span class="fea-conn-words">${esc(o.words)}</span></label>`).join("");
  return `<div class="fea-assembly" data-sig="${esc(assemblySig(a))}">
      <div class="rc-row"><div class="rc-k">Parts</div><div class="rc-v fea-parts-sum">${esc(a.summary || "")}</div></div>
      <ul class="fea-parts" aria-label="The parts of the assembly">${parts}</ul>
      ${rows ? `<ul class="fea-spots fea-itfs" aria-label="Where the parts touch">${rows}</ul>` : none}
      ${gapHtml}${joinNotes}${free}${a.overlap_note ? `<div class="gc-note">${esc(a.overlap_note)}</div>` : ""}
      <p class="fea-q fea-conn-q">${esc(a.question || "How are the parts connected?")}</p>
      <div class="fea-conn" role="radiogroup" aria-label="${esc(a.question || "How are the parts connected?")}">${opts}</div>
      <div class="gc-note">${esc(a.pair_note || (pairRows ? "Each pair follows the answer above unless you change it in the list." : "One answer for every joint; each face is still named, so you can change any joint in your solver."))}</div>
    </div>`;
}

function cleanHtml(f) {
  const mark = f.clean === true ? "✓" : f.clean === false ? "!" : "?";
  const cls = f.clean === true ? "ok" : f.clean === false ? "bad" : "unk";
  return `<span class="fea-clean ${cls}"><span class="ic">${mark}</span>${esc(f.clean_summary || "")}</span>`;
}

/** One hot spot in words, its size in the file's numbers: "Hole · radius 7.5 mm". */
function spotWords(h, p) {
  const u = `<span class="gc-u">${esc(lenUnit(p))}</span>`;
  const name = { hole: "Hole", fillet: "Fillet", notch: "Notch", groove: "Groove", "inside corner": "Sharp inside corner" }[h.shape] || "Hot spot";
  const size = h.type === "sharp_corner"
    ? `${esc(String(Math.round(h.angle_deg)))}°, refinement capped`
    : `radius ${esc(len(h.radius_mm, p))} ${u}`;
  return `<b>${esc(name)}</b> · ${size}`;
}

/** What the measurement found (cad/fea_measure): the thinnest wall and the hot spots the mesh will
 *  be refined at. Shown only: version 1 refines every one automatically. `m` is the server's
 *  `fea.measure`; nothing at all before the part was measured. */
export function measureHtml(m, p) {
  return `<div class="fea-measure" data-state="${measureState(m)}">${measureBody(m, p)}</div>`;
}

/** Each type's lines first shown; the rest of a type open with "Show all N", or when a tag on the
 *  part is clicked. */
const FIRST_SPOTS = 8;
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

function measureBody(m, p) {
  if (!m) return "";
  if (!m.measured) {
    return `<div class="rc-row"><div class="rc-k">Hot spots</div><div class="rc-v fea-unmeasured">Not measured: ${esc(m.reason || "")}</div></div>`;
  }
  const u = `<span class="gc-u">${esc(lenUnit(p))}</span>`;
  const wall = m.thickness_min_mm == null ? "not measured"
    : `${esc(len(m.thickness_min_mm, p))} ${u}${m.thickness_face ? ` <span class="fea-where">(${esc(m.thickness_face)})</span>` : ""}`;
  const spots = m.hot_spots || [];
  // EVERY hot spot is listed, in a group per type; a long group shows its first lines and opens
  // whole with "Show all N" (a spot left out looks the same as one never found)
  const list = spotGroups(spots).map(([type, rows]) => {
    const lines = rows.map((h, i) =>
      `<li class="fea-spot-row" data-spot="${esc(h.id)}"${i >= FIRST_SPOTS ? " hidden" : ""}><span class="fea-spot-id">${esc(h.id)}</span><span>${spotWords(h, p)}</span></li>`).join("");
    const folded = rows.length > FIRST_SPOTS
      ? `<li class="fea-spot-more"><button type="button" class="fea-spot-open">Show all ${rows.length}</button></li>` : "";
    return `<li class="fea-spot-group" data-type="${esc(type)}"><div class="fea-spot-head">${esc(GROUP_WORDS[type] || "Other hot spots")} <span class="fea-spot-n">${rows.length}</span></div>
      <ul class="fea-spot-rows">${lines}${folded}</ul></li>`;
  }).join("");
  // only a part past the record's own limit (thousands of hot spots) has any the server did not send
  const unsent = m.more ? `<li class="fea-spot-unsent">${m.more} more found than the measurement keeps</li>` : "";
  const capped = spots.some((h) => h.capped)
    ? `<div class="gc-note">A perfectly sharp inside corner's stress never settles however fine the mesh, so refinement there stops at a set level.</div>` : "";
  return `<div class="rc-row"><div class="rc-k">Thinnest wall</div><div class="rc-v fea-wall">${wall}</div></div>
    <div class="rc-row"><div class="rc-k">Hot spots</div><div class="rc-v fea-spots-sum">${esc(m.summary || "")}</div></div>
    ${spots.length ? `<ul class="fea-spots" aria-label="Hot spots the mesh will be refined at">${list}${unsent}</ul>
    <div class="gc-note">The mesh is refined at these automatically.</div>${capped}` : ""}`;
}

/** Wire the hot-spot list: a group's "Show all N" opens the rest of that group, and `light(id)`
 *  (if given) runs when a line is pointed at or clicked. */
export function bindMeasure(root, light) {
  root.querySelectorAll(".fea-spot-open").forEach((open) => {
    open.onclick = () => {
      const group = open.closest(".fea-spot-rows");
      group.querySelectorAll(".fea-spot-row[hidden]").forEach((r) => { r.hidden = false; });
      open.closest("li").remove();
    };
  });
  if (!light) return;
  root.querySelectorAll(".fea-spot-row").forEach((r) => {
    r.addEventListener("mouseenter", () => light(r.dataset.spot));
    r.addEventListener("click", () => light(r.dataset.spot));
  });
}

/** Light one hot spot's line, opening it first when it is folded away (a tag past its group's
 *  first eight was clicked). */
export function lightSpotRow(root, id) {
  root.querySelectorAll(".fea-spot-row").forEach((r) => {
    const on = r.dataset.spot === id;
    if (on && r.hidden) { r.hidden = false; r.scrollIntoView({ block: "nearest" }); }
    r.classList.toggle("sel", on);
  });
}

/** A measurement that lands after the panel was drawn (the stage opened while the check was
 *  still naming): the wall and hot-spot lines are drawn in place. True when they changed. */
export function followMeasure(root, m, p, light) {
  const box = root.querySelector(".fea-measure");
  if (!box || box.dataset.state === measureState(m)) return false;
  box.dataset.state = measureState(m);
  box.innerHTML = measureBody(m, p);
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
  if (clean) {
    const html = cleanHtml(f);
    if (clean.outerHTML !== html) { clean.outerHTML = html; moved = true; }
  }
  // A STOP THE FULL READING FOUND (a surface, not a closed solid): said, Proceed off, the way on
  const btn = root.querySelector(".gc-proceed"), hint = root.querySelector(".gc-hint");
  const blocked = f.single_part_only || "";
  let box = root.querySelector(".fea-blocked");
  if (blocked && !box) {
    const acts = root.querySelector(".fea-q") || root.querySelector(".gc-actions");
    if (acts) acts.insertAdjacentHTML("beforebegin", `<div class="fea-blocked" role="alert">${esc(blocked)}</div>`);
    box = root.querySelector(".fea-blocked");
    moved = true;
  } else if (box && !blocked) { box.remove(); moved = true; }
  else if (box && box.textContent !== blocked) { box.textContent = blocked; moved = true; }
  if (btn && btn.textContent !== "Confirming…") btn.disabled = !!blocked;
  if (hint && blocked) hint.textContent = f.way_out || "Upload a single part to go on.";
  return moved;
}

/** The panel's body for one check: `f` is the server's `fea` view, `p` the proposal (size, unit). */
export function feaPanelHtml(f, p) {
  const blocked = f.single_part_only || "";
  const why = f.clean === false
    ? `<div class="gc-note">The part can still go ahead: the mesh is made from the file as it is.</div>` : "";
  return `<div class="v-row v-titlerow"><b class="v-title">Your part</b><span class="v-meta">for structural analysis (FEA)</span></div>
    <div class="gc-lead">${p.part ? `<div class="gc-part"><b>${esc(p.part)}</b></div>` : ""}
      <p>${f.assembly ? "The parts are meshed as solids, together. Every face of every part is kept as a named group (Part_k_Face_j), so you set the supports, loads and materials in your own solver." : "The part is meshed as itself, as a solid. Every face is kept as a named group, so you set the supports and loads in your own solver."}</p></div>
    <div class="gc-form fea-form">
      <div class="rc-row"><div class="rc-k">Size</div><div class="rc-v fea-size">${sizeHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">The file's unit</div><div class="rc-v">${unitBoxHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">Bodies</div><div class="rc-v fea-bodies">${esc(bodiesHtml(f))}</div></div>
      <div class="rc-row"><div class="rc-k">Clean check</div><div class="rc-v">${cleanHtml(f)}</div></div>
      ${why}
      ${measureHtml(f.measure, p)}
      ${assemblyHtml(f.assembly)}
      ${blocked ? `<div class="fea-blocked" role="alert">${esc(blocked)}</div>` : ""}
      <p class="fea-q">${esc(f.question || "Is this your part and are the units right?")}</p>
      <div class="gc-actions"><button class="gc-proceed" type="button"${blocked ? " disabled" : ""}>Yes, proceed</button>
        <span class="gc-hint">${blocked ? esc(f.way_out || "Upload a single part to go on.") : "Change the unit first if the size is wrong."}</span></div>
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

/** Wire the panel: the unit box follows every size shown, and Proceed confirms - after the unit is
 *  picked when the size makes it doubtful. `done()` runs once the server has the answer. */
export function bindFeaPanel(root, f, p, { confirm, done }) {
  bindUnit(root, p);
  const form = root.querySelector(".fea-form") || root;
  bindMeasure(form);
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
      form.querySelectorAll("input,select,button").forEach((el) => { el.disabled = true; });
      const acts = form.querySelector(".gc-actions");
      if (acts) acts.innerHTML = '<span class="gc-done">✓ Confirmed. Next: what you will analyze.</span>';
      if (done) done();
    } catch (e) {
      btn.disabled = false; btn.textContent = "Yes, proceed";
      hint.textContent = "Could not confirm: " + ((e && e.message) || "try again");
    }
  };
}
