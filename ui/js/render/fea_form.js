// Responsibility: Build the FEA part check - the part's size in the file's unit, the unit box, how
// many bodies the file has and what the clean check found, and the one question "Is this your part
// and are the units right?" - and read the answer back out of it. Below them, what the measurement
// found (FEA build step 3): the thinnest wall and the hot spots the mesh will be refined at, shown
// only (version 1 refines them all automatically; nothing here edits them). For an ASSEMBLY (a file
// of several bodies): its parts, where they touch or pass into each other (marked on the part), and
// its one question, "How are the parts connected?" - bonded (the default) or contact.
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
const assemblySig = (a) => `${(a.parts || []).length}/${(a.interfaces || []).length}/${(a.notes || []).length}/${a.measured === false ? 0 : 1}`;

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
  return true;
}

/** An assembly's parts, its interfaces and its one question (bonded the default). `a` is the
 *  server's `fea.assembly`; nothing for a single part. */
export function assemblyHtml(a) {
  if (!a) return "";
  const parts = (a.parts || []).map((p) => `<li class="fea-part-row" data-part="${esc(p.id)}">${partLabel(p)}</li>`).join("");
  const rows = (a.interfaces || []).map((i) => {
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
      <div class="gc-note">One answer for every joint for now; each face is still named, so you can change any joint in your solver.</div>
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
    const acts = root.querySelector(".gc-tools") || root.querySelector(".gc-actions");
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
 * SHSTAGE-2..4: "MESH AS: Auto (recommended) / Shell / Solid".
 *
 * A shell is the part's mid-surface meshed with plate elements; a solid is its volume meshed with
 * tetrahedra. Auto recommends one from the measured walls, with the reason in plain words. What the
 * user said in the chat opens checked and is never overridden; a click is theirs too. While the walls
 * are still measured the stage says so, and what Proceed does then (Auto: a solid). Below: what stays
 * solid if it is meshed as a shell, where the plates do not meet (red on the part), and the legend of
 * the mid-surface's colours - the wall in mm.
 * ====================================================================================== */

/** The mid-surface's colours: a muted sequential ramp (cividis-like), thin walls dark blue to thick
 *  walls sand. Stops are [r, g, b] in 0..1. */
export const SHELL_RAMP = [[0.15, 0.24, 0.42], [0.30, 0.36, 0.45], [0.48, 0.48, 0.47], [0.68, 0.62, 0.45], [0.86, 0.79, 0.47]];

/** The colour of a wall `t` between `lo` and `hi` (mm), on SHELL_RAMP. */
export function wallColour(t, lo, hi) {
  const span = Number(hi) - Number(lo);
  const u = !(span > 1e-9) ? 0.5 : Math.min(1, Math.max(0, (Number(t) - Number(lo)) / span));
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
  return `<div class="fea-shell-legend" aria-label="Wall thickness on the mid-surface">
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

/** What Auto means now, in the option's own words. */
function autoWords(s) {
  if (s.state === "measuring") return "measuring the walls…";
  if (s.recommend === "shell") return "Shell";
  if (s.recommend === "solid") return "Solid";
  return "";
}

/** One string for what a "Mesh as" view shows: a later view that says more is drawn again. */
const meshAsSig = (s) => `${s.state}/${s.recommend || ""}/${(s.not_shelled || []).length}/${s.said || ""}/${s.overlay ? 1 : 0}`;

/** The effective choice the stage shows: the picked option, Auto read as its recommendation. */
export function effectiveMeshAs(s, picked) {
  const k = picked || (s && s.default) || "auto";
  return k === "auto" ? ((s && s.recommend) || "") : k;
}

function meshAsBody(s, picked) {
  const pick = picked || s.default || "auto";
  const opts = (s.options || []).map((o) => {
    const off = o.key === "shell" && s.shell_ok === false;
    const label = o.key === "auto" ? `${esc(o.label)}${autoWords(s) ? `: <span class="fea-meshas-auto">${esc(autoWords(s))}</span>` : ""}`
      : `<b>${esc(o.label)}</b>${o.words ? ` <span class="fea-conn-words">${esc(o.words)}</span>` : ""}`;
    return `<label class="fea-meshas-opt${off ? " off" : ""}"${off ? ` title="${esc(s.shell_off || "")}"` : ""}><input type="radio" name="fea-mesh-as" value="${esc(o.key)}"${o.key === pick ? " checked" : ""}${off ? " disabled" : ""}> ${label}</label>`;
  }).join("");
  const said = s.said_words ? `<div class="gc-note fea-meshas-said">${esc(s.said_words)}</div>` : "";
  const measuring = s.state === "measuring"
    ? `<div class="gc-note fea-meshas-measuring"><span class="gc-spin" aria-hidden="true"></span> ${esc(s.measuring_words || "")} ${esc(s.early_proceed || "")}</div>` : "";
  const reason = s.reason ? `<div class="gc-note fea-meshas-why">${esc(s.reason)}</div>` : "";
  const rows = (s.not_shelled || []).map((r) => r.id
    ? `<li class="fea-spot-row fea-shell-row ${esc(r.kind)}" data-spot="${esc(r.id)}"><span class="fea-spot-id">${esc(r.id)}</span><span>${esc(r.words)}</span></li>`
    : `<li class="fea-shell-head ${esc(r.kind)}">${esc(r.words)}</li>`).join("");
  const eff = effectiveMeshAs(s, pick);
  const list = rows ? `<div class="fea-shell-list"${eff === "shell" ? "" : " hidden"}><div class="fea-spot-head">If it is meshed as a shell</div><ul class="fea-spots fea-shell-rows" aria-label="What is not shelled">${rows}</ul></div>` : "";
  const legend = s.overlay ? `<div class="fea-shell-show"${eff === "shell" ? "" : " hidden"}>${shellLegendHtml(s)}<div class="gc-note">The coloured surface on the part is the mid-surface the shell is meshed on.</div></div>` : "";
  return `<div class="rc-row"><div class="rc-k">${esc(s.question || "Mesh as")}</div><div class="rc-v"><div class="fea-meshas-opts" role="radiogroup" aria-label="${esc(s.question || "Mesh as")}">${opts}</div></div></div>
    ${said}${measuring}${reason}${list}${legend}`;
}

/** "Mesh as" for the panel; nothing without a `shell` view (a server from before it). */
export function meshAsHtml(s) {
  if (!s) return "";
  return `<div class="fea-meshas" data-sig="${esc(meshAsSig(s))}">${meshAsBody(s)}</div>`;
}

/** The radio the user has checked ("auto", "shell", "solid"), or "". */
export function pickedMeshAs(root) {
  const r = root && root.querySelector ? root.querySelector('input[name="fea-mesh-as"]:checked') : null;
  return r ? r.value : "";
}

/** Show the list and the legend that belong to the effective choice. */
function showForPick(root, s) {
  const eff = effectiveMeshAs(s, pickedMeshAs(root));
  root.querySelectorAll(".fea-shell-list,.fea-shell-show").forEach((el) => { el.hidden = eff !== "shell"; });
  return eff;
}

/** Wire "Mesh as": `changed(effective)` runs when the pick changes (the stage shows or hides the
 *  mid-surface). */
export function bindMeshAs(root, s, changed) {
  const box = root.querySelector(".fea-meshas");
  if (!box || !s) return;
  box.querySelectorAll('input[name="fea-mesh-as"]').forEach((r) => {
    r.addEventListener("change", () => { const eff = showForPick(box, s); if (changed) changed(eff); });
  });
}

/** THE SHELL CHECK LANDS AFTER THE PANEL WAS DRAWN (it takes minutes): "Mesh as" is drawn again in
 *  place, the user's pick kept. True when it changed. */
export function followMeshAs(root, s, changed) {
  const box = root.querySelector(".fea-meshas");
  if (!box || !s || box.dataset.sig === meshAsSig(s)) return false;
  const picked = pickedMeshAs(box);
  box.dataset.sig = meshAsSig(s);
  box.innerHTML = meshAsBody(s, picked);
  bindMeshAs(root, s, changed);
  showForPick(box, s);
  return true;
}

/** The panel's body for one check: `f` is the server's `fea` view, `p` the proposal (size, unit). */
export function feaPanelHtml(f, p) {
  const blocked = f.single_part_only || "";
  const why = f.clean === false
    ? `<div class="gc-note">The part can still go ahead: the mesh is made from the file as it is.</div>` : "";
  const how = f.shell ? "meshed as a shell or a solid (below)" : "meshed as itself, as a solid";
  return `<div class="v-row v-titlerow"><b class="v-title">Your part</b><span class="v-meta">for structural analysis (FEA)</span></div>
    <div class="gc-lead">${p.part ? `<div class="gc-part"><b>${esc(p.part)}</b></div>` : ""}
      <p>${f.assembly ? `The parts are ${f.shell ? "meshed together, as a shell or a solid (below)" : "meshed as solids, together"}. Every face of every part is kept as a named group (Part_k_Face_j), so you set the supports, loads and materials in your own solver.` : `The part is ${how}. Every face is kept as a named group, so you set the supports and loads in your own solver.`}</p></div>
    <div class="gc-form fea-form">
      <div class="rc-row"><div class="rc-k">Size</div><div class="rc-v fea-size">${sizeHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">The file's unit</div><div class="rc-v">${unitBoxHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">Bodies</div><div class="rc-v fea-bodies">${esc(bodiesHtml(f))}</div></div>
      <div class="rc-row"><div class="rc-k">Clean check</div><div class="rc-v">${cleanHtml(f)}</div></div>
      ${why}
      ${measureHtml(f.measure, p)}
      ${meshAsHtml(f.shell)}
      ${assemblyHtml(f.assembly)}
      ${blocked ? `<div class="fea-blocked" role="alert">${esc(blocked)}</div>` : ""}
      <div class="gc-tools"><button class="gc-measure v-btn" type="button" title="measure between two points on the part">Measure</button><span class="gc-tools-hint">click two points to check a length</span></div>
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
  // SHSTAGE-5: the pick as it stands; the server settles it (Auto while measuring: a solid)
  const meshAs = pickedMeshAs(root);
  if (meshAs) body.fea_mesh_as = meshAs;
  const upSel = root.closest ? root.closest(".gc-stage")?.querySelector(".v-up select") : null;
  const up = (upSel && upSel.value) || p.up_axis;
  if (up) body.up_axis = up;
  return body;
}

/** Wire the panel: the unit box follows every size shown, and Proceed confirms - after the unit is
 *  picked when the size makes it doubtful. `done()` runs once the server has the answer. */
export function bindFeaPanel(root, f, p, { confirm, done, meshAsChanged }) {
  bindUnit(root, p);
  const form = root.querySelector(".fea-form") || root;
  bindMeasure(form);
  bindMeshAs(form, f.shell, meshAsChanged);
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
