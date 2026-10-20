// Responsibility: Build the FEA part check - the part's size in the file's unit, the unit box, how
// many bodies the file has and what the clean check found, and the one question "Is this your part
// and are the units right?" - and read the answer back out of it. Below them, what the measurement
// found (FEA build step 3): the thinnest wall and the hot spots the mesh will be refined at, shown
// only (version 1 refines them all automatically; nothing here edits them).
// Boundaries: markup and a reading of it. No fluid, no openings, no flow axis and no stickers: an
// FEA part is meshed as itself. It fetches nothing and starts no run; the stage and the card both
// use it, and confirming is the callback they were handed.
import { esc } from "../core/format.js";
import { bindUnit, shown, unitBoxHtml, unitChoiceHint, unitChoiceNeeded, unitOf } from "./geometry_form.js";

/** The size row: the three sides in the file's own numbers, the unit beside them. */
function sizeHtml(p) {
  const s = (p.size_mm || []).map((v) => shown(v, p));
  return s.length === 3 ? `${esc(s.join(" × "))} <span class="gc-u">${esc(unitOf(p))}</span>` : "not measured";
}

function bodiesHtml(f) {
  const n = f.bodies;
  if (n === 1) return "1 (a single part)";
  if (n > 1) return `${n} separate bodies`;
  return "not counted";
}

function cleanHtml(f) {
  const mark = f.clean === true ? "✓" : f.clean === false ? "!" : "?";
  const cls = f.clean === true ? "ok" : f.clean === false ? "bad" : "unk";
  return `<span class="fea-clean ${cls}"><span class="ic">${mark}</span>${esc(f.clean_summary || "")}</span>`;
}

/** One hot spot in words, its size in the file's numbers: "Hole · radius 7.5 mm". */
function spotWords(h, p) {
  const u = `<span class="gc-u">${esc(unitOf(p))}</span>`;
  const name = { hole: "Hole", fillet: "Fillet", notch: "Notch", groove: "Groove", "inside corner": "Sharp inside corner" }[h.shape] || "Hot spot";
  const size = h.type === "sharp_corner"
    ? `${esc(String(Math.round(h.angle_deg)))}°, refinement capped`
    : `radius ${esc(shown(h.radius_mm, p))} ${u}`;
  return `<b>${esc(name)}</b> · ${size}`;
}

/** What the measurement found (cad/fea_measure): the thinnest wall and the hot spots the mesh will
 *  be refined at. Shown only: version 1 refines every one automatically. `m` is the server's
 *  `fea.measure`; nothing at all before the part was measured. */
export function measureHtml(m, p) {
  if (!m) return "";
  if (!m.measured) {
    return `<div class="rc-row"><div class="rc-k">Hot spots</div><div class="rc-v fea-unmeasured">Not measured: ${esc(m.reason || "")}</div></div>`;
  }
  const u = `<span class="gc-u">${esc(unitOf(p))}</span>`;
  const wall = m.thickness_min_mm == null ? "not measured"
    : `${esc(shown(m.thickness_min_mm, p))} ${u}${m.thickness_face ? ` <span class="fea-where">(${esc(m.thickness_face)})</span>` : ""}`;
  const spots = m.hot_spots || [];
  const list = spots.slice(0, 8).map((h) =>
    `<li class="fea-spot-row" data-spot="${esc(h.id)}"><span class="fea-spot-id">${esc(h.id)}</span><span>${spotWords(h, p)}</span></li>`).join("");
  const rest = Math.max(0, spots.length - 8) + (m.more || 0);
  const more = rest ? `<li class="fea-spot-more">and ${rest} more</li>` : "";
  const capped = spots.some((h) => h.capped)
    ? `<div class="gc-note">A perfectly sharp inside corner's stress never settles however fine the mesh, so refinement there stops at a set level.</div>` : "";
  return `<div class="rc-row"><div class="rc-k">Thinnest wall</div><div class="rc-v fea-wall">${wall}</div></div>
    <div class="rc-row"><div class="rc-k">Hot spots</div><div class="rc-v fea-spots-sum">${esc(m.summary || "")}</div></div>
    ${spots.length ? `<ul class="fea-spots" aria-label="Hot spots the mesh will be refined at">${list}${more}</ul>
    <div class="gc-note">The mesh is refined at these automatically.</div>${capped}` : ""}`;
}

/** The panel's body for one check: `f` is the server's `fea` view, `p` the proposal (size, unit). */
export function feaPanelHtml(f, p) {
  const blocked = f.single_part_only || "";
  const why = f.clean === false
    ? `<div class="gc-note">The part can still go ahead: the mesh is made from the file as it is.</div>` : "";
  return `<div class="v-row v-titlerow"><b class="v-title">Your part</b><span class="v-meta">for structural analysis (FEA)</span></div>
    <div class="gc-lead">${p.part ? `<div class="gc-part"><b>${esc(p.part)}</b></div>` : ""}
      <p>The part is meshed as itself, as a solid. Every face is kept as a named group, so you set the supports and loads in your own solver.</p></div>
    <div class="gc-form fea-form">
      <div class="rc-row"><div class="rc-k">Size</div><div class="rc-v fea-size">${sizeHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">The file's unit</div><div class="rc-v">${unitBoxHtml(p)}</div></div>
      <div class="rc-row"><div class="rc-k">Bodies</div><div class="rc-v fea-bodies">${esc(bodiesHtml(f))}</div></div>
      <div class="rc-row"><div class="rc-k">Clean check</div><div class="rc-v">${cleanHtml(f)}</div></div>
      ${why}
      ${measureHtml(f.measure, p)}
      ${blocked ? `<div class="fea-blocked" role="alert">${esc(blocked)}</div>` : ""}
      <div class="gc-tools"><button class="gc-measure v-btn" type="button" title="measure between two points on the part">Measure</button><span class="gc-tools-hint">click two points to check a length</span></div>
      <p class="fea-q">${esc(f.question || "Is this your part and are the units right?")}</p>
      <div class="gc-actions"><button class="gc-proceed" type="button"${blocked ? " disabled" : ""}>Yes, proceed</button>
        <span class="gc-hint">${blocked ? "Upload a single part to go on." : "Change the unit first if the size is wrong."}</span></div>
    </div>`;
}

/** What the user confirmed: the unit they say the file is in, and the numbers it was read under.
 *  The server sets everything else for an FEA part (a solid body, its bodies and clean check). */
export function readFeaForm(root, p) {
  const body = { part: p.part || "", size_mm: p.size_mm || null, unit: unitOf(p),
                 scale_to_m: p.scale_to_m == null ? null : Number(p.scale_to_m) };
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
