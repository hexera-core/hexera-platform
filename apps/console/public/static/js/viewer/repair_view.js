// Responsibility: Draw a repaired part in 3D for the repair result - the CAD with every problem pinned where it was
//                 found (fixed / not fixed / left as is), a "how far faces moved" colouring against the allowed change,
//                 and a switch between the repaired part and the original.
// Owns: the repair view's scene, its pins and its two switches.
// Boundaries: it reads the surfaces it is handed (api/v1 repair surfaces, or the geometry check's skin) and the
//             typed problems; every word it shows comes from core/repair_words.js. It decides nothing about a repair.

import { esc } from "../core/format.js";
import { LEGEND, deviationLegend, problemName, statusOf } from "../core/repair_words.js";
import { mountCanvas } from "./scene/canvas.js";
import { makeOverlay } from "./scene/overlay.js";
import { b64f32, b64u32, loadVtk } from "./scene/vtk_load.js";

const BACKGROUND = [0.02, 0.02, 0.02], BODY = [0.77, 0.76, 0.72];
/* the deviation ramp in the viewer's own inks (viewer.js QUALITY HEATMAP): calm stone where nothing moved, amber
   as a move nears the allowed change, crimson at it and deep crimson past it */
const STOPS = [[0, [110, 109, 101]], [0.5, [175, 174, 162]], [0.8, [236, 180, 92]], [1.0, [226, 44, 44]]];
const PAST = [140, 14, 18];

/** The colour of a move of `v` mm against `allowed` mm, into out[o..o+2]. Pure, exported for the browser tier. */
export function deviationColour(v, allowed, out = [0, 0, 0], o = 0) {
  // -1 (or anything negative or missing) is a point the server could not measure: the body's own colour, never "0 mm"
  if (!(Number(v) >= 0)) { for (let k = 0; k < 3; k++) out[o + k] = Math.round(BODY[k] * 255); return out; }
  const lim = allowed > 0 ? allowed : 1;
  const f = Math.max(0, Number(v) || 0) / lim;
  if (f > 1.0001) { out[o] = PAST[0]; out[o + 1] = PAST[1]; out[o + 2] = PAST[2]; return out; }
  let i = 1; while (i < STOPS.length - 1 && f > STOPS[i][0]) i++;
  const [a, ca] = STOPS[i - 1], [b, cb] = STOPS[i], t = Math.min(1, Math.max(0, (f - a) / ((b - a) || 1)));
  for (let k = 0; k < 3; k++) out[o + k] = ca[k] + (cb[k] - ca[k]) * t;
  return out;
}

const GRADIENT = "linear-gradient(0deg," + STOPS.map(([f, c]) => `rgb(${c}) ${(f * 100 / 1.25).toFixed(1)}%`)
  .concat([`rgb(${PAST}) 80.1%`, `rgb(${PAST}) 100%`]).join(",") + ")";

/** The controls and legends the view puts on its canvas. */
export function repairViewChrome(job, repair, { hasBefore = false, hasDeviation = false } = {}) {
  const dl = deviationLegend(repair);
  const seg = (name, items) => `<div class="rpv-seg" role="group" aria-label="${esc(name)}">${items.map(([v, l, on, dis]) =>
    `<button type="button" class="rpv-opt${on ? " on" : ""}" data-v="${esc(v)}" aria-pressed="${on ? "true" : "false"}"`
    + `${dis ? ` disabled title="${esc(dis)}"` : ""}>${esc(l)}</button>`).join("")}</div>`;
  return `<div class="rpv-ctl">
      ${seg("Which part", [["after", "Repaired", true, ""], ["before", "Original", false,
        hasBefore ? "" : "The original part is not available for this run"]])}
      ${seg("Colour", [["pins", "Problems", true, ""], ["dev", "How far faces moved", false,
        hasDeviation ? "" : "No face positions were recorded for this run"]])}
    </div>
    <div class="rpv-legend" role="list" aria-label="Pin colours">${LEGEND.map((l) =>
      `<span role="listitem"><i class="rpv-dot ${l.key}" aria-hidden="true"></i>${esc(l.label)}</span>`).join("")}</div>
    <div class="v-legend rpv-dev" hidden>
      <span class="lg-h"><b>${esc(dl.title)}</b></span>
      <span class="scale"><span class="ticks">
        <span style="bottom:100%">${esc(dl.allowed != null ? `${Number((dl.allowed * 1.25).toPrecision(3))} mm` : "")}</span>
        ${dl.allowed != null ? `<span class="lim" style="bottom:80%">${esc(dl.allowedText)}</span>` : ""}
        <span style="bottom:0">0 mm</span></span>
      <span class="bar" style="background:${GRADIENT}">${dl.allowed != null ? '<i style="bottom:80%"></i>' : ""}</span></span>
      ${dl.maxText ? `<span class="mxv">${esc(dl.maxText)}</span>` : ""}
    </div>
    <div class="v-hint rpv-hint">Drag to turn · scroll to zoom · click a pin to look at it</div>`;
}

/** Where a problem is, in millimetres: its first point, else nothing (it is listed, not pinned). */
export function pinPoint(p) {
  const pts = (((p || {}).location || {}).points || []).filter((q) => Array.isArray(q) && q.length === 3
    && q.every((v) => Number.isFinite(Number(v))));
  return pts.length ? pts[0].map(Number) : null;
}

function polyOf(pt) {
  let pts, polys, normals = null;
  if (pt.polys_b64) {
    pts = b64f32(pt.points_b64); polys = b64u32(pt.polys_b64);
    if (pt.normals_b64) normals = b64f32(pt.normals_b64);
  } else {
    pts = b64f32(pt.positions_b64); const n = pts.length / 9; polys = new Uint32Array(n * 4);
    for (let t = 0; t < n; t++) polys.set([3, t * 3, t * 3 + 1, t * 3 + 2], t * 4);
  }
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
  if (normals && normals.length === pts.length) {
    pd.getPointData().setNormals(vtk.Common.Core.vtkDataArray.newInstance(
      { name: "Normals", values: normals, numberOfComponents: 3 }));
  }
  return { pd, nPts: pts.length / 3 };
}

/**
 * Mount the view in `host`. `surfaces` is `{original, repaired}` (either may be missing); `repair` the typed result.
 * Resolves to a controller {focus(id), set(which), colour(mode), stop()}, or null when nothing can be drawn.
 */
export async function mountRepairView(host, job, repair, surfaces) {
  const original = surfaces && surfaces.original, repaired = surfaces && surfaces.repaired;
  const main = repaired || original;
  if (!main || !(main.patches || []).length) return null;
  await loadVtk();
  const allowed = Number(repair && repair.allowed_change_mm) || Number(repair && repair.max_change_mm) || 1;
  const hasDeviation = !!repaired && (repaired.patches || []).some((p) => p.deviation_b64);
  host.insertAdjacentHTML("beforeend", repairViewChrome(job, repair, { hasBefore: !!(original && repaired), hasDeviation }));
  const canvas = mountCanvas(host, { background: BACKGROUND, ignore: ".rpv-ctl,.rpv-legend,.v-legend,.rp-pin" });
  const { ren, rw, cam } = canvas;
  const overlay = makeOverlay(canvas);

  const draw = (skin, deviation) => (skin && skin.patches || []).map((pt) => {
    const { pd, nPts } = polyOf(pt);
    const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
    const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper);
    const pr = actor.getProperty(); pr.setColor(...BODY); pr.setInterpolationToPhong();
    pr.setAmbient(0.32); pr.setDiffuse(0.76); pr.setSpecular(0.12); pr.setSpecularPower(20);
    ren.addActor(actor);
    let rgb = null;
    if (deviation && pt.deviation_b64) {
      const d = b64f32(pt.deviation_b64);
      // a field that does not match the points is dropped, not trusted: it would colour the wrong faces
      if (d.length === nPts) {
        rgb = new Uint8Array(nPts * 3);
        for (let i = 0; i < nPts; i++) deviationColour(d[i], allowed, rgb, i * 3);
      }
    }
    return { actor, mapper, pd, rgb };
  });
  const after = draw(repaired || original, true);
  const before = repaired && original ? draw(original, false) : [];
  before.forEach((e) => e.actor.setVisibility(false));

  cam.azimuth(45); cam.elevation(25); ren.resetCamera(); rw.render();
  const bounds = ren.computeVisiblePropBounds().slice();
  const diag = Math.max(Math.hypot(bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]), 1e-9);
  const fit = () => { ren.resetCamera(bounds); rw.render(); };
  canvas.fit = fit;

  // THE SKIN'S OWN SCALE: problems are located in millimetres; the skin is in metres as the check read the file,
  // rescaled when the user confirmed another unit (the same correction the review view makes)
  const toSkin = (v) => (v / 1000) * (Number(main.rescaled_by) || 1);
  const probs = (repair && repair.problems) || [];
  const pins = [];
  // where the server pinned each problem (its own point, else the centre of its first face), by id
  const served = new Map(((surfaces && surfaces.pins) || []).map((q) => [String(q.id), q.point]));
  probs.forEach((p, i) => {
    const at = pinPoint(p) || pinPoint({ location: { points: [served.get(String(p.id))] } });
    if (!at) return;
    const s = statusOf(p), n = i + 1;
    const el = document.createElement("button");
    el.type = "button";
    el.className = `v-pin rp-pin ${s}`;
    el.textContent = String(n);
    el.dataset.id = String(p.id ?? n);
    el.setAttribute("aria-label", `Problem ${n}: ${problemName(p.type, p.type_text)}, ${LEGEND.find((l) => l.key === s).label.toLowerCase()}`);
    el.title = `${n}. ${problemName(p.type, p.type_text)} - ${LEGEND.find((l) => l.key === s).label.toLowerCase()}`;
    host.appendChild(el);
    const w = at.map(toSkin);
    overlay.anchor(el, () => w);
    el.onclick = () => focus(el.dataset.id);
    pins.push({ id: el.dataset.id, el, at: w, p });
  });

  function focus(id) {
    const pin = pins.find((q) => q.id === String(id));
    if (!pin) return false;
    pins.forEach((q) => q.el.classList.toggle("sel", q === pin));
    canvas.touched = true;
    const reach = 0.35 * diag, fp = cam.getFocalPoint(), pos = cam.getPosition();
    let dir = [pos[0] - fp[0], pos[1] - fp[1], pos[2] - fp[2]];
    const L = Math.hypot(...dir) || 1; dir = dir.map((v) => v / L);
    cam.setFocalPoint(...pin.at);
    cam.setPosition(pin.at[0] + dir[0] * reach, pin.at[1] + dir[1] * reach, pin.at[2] + dir[2] * reach);
    ren.resetCameraClippingRange(); rw.render(); overlay.tick();
    host.dispatchEvent(new CustomEvent("repair-focus", { detail: { id: pin.id } }));
    return true;
  }

  let which = "after", mode = "pins";
  function paint() {
    const showAfter = which === "after" || !before.length;
    after.forEach((e) => e.actor.setVisibility(showAfter));
    before.forEach((e) => e.actor.setVisibility(!showAfter));
    const dev = mode === "dev" && showAfter;
    after.forEach((e) => {
      if (dev && e.rgb) {
        e.pd.getPointData().setScalars(vtk.Common.Core.vtkDataArray.newInstance(
          { name: "deviation", values: e.rgb, numberOfComponents: 3 }));
        e.mapper.setScalarModeToUsePointData(); e.mapper.setColorModeToDirectScalars(); e.mapper.setScalarVisibility(true);
      } else e.mapper.setScalarVisibility(false);
    });
    const leg = host.querySelector(".rpv-dev");
    if (leg) leg.hidden = !dev;
    host.querySelector(".rpv-legend").hidden = mode === "dev";
    pins.forEach((q) => { q.el.hidden = mode === "dev"; });
    host.querySelectorAll(".rpv-opt").forEach((b) => {
      const on = b.dataset.v === which || b.dataset.v === mode;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    rw.render(); overlay.tick();
  }
  const set = (w) => { which = w === "before" && before.length ? "before" : "after"; paint(); };
  const colour = (m) => { mode = m === "dev" && hasDeviation ? "dev" : "pins"; paint(); };
  host.querySelectorAll(".rpv-opt").forEach((b) => {
    b.onclick = () => { const v = b.dataset.v; if (v === "after" || v === "before") set(v); else colour(v); };
  });
  canvas.setRenderScale(); fit(); paint();

  window._vdbg = window._vdbg || {};
  const api = { focus, set, colour,
    state: () => ({ which, mode, pins: pins.length, visiblePins: pins.filter((q) => !q.el.hidden).length,
      before: before.length, coloured: after.some((e) => e.mapper.getScalarVisibility()),
      focal: cam.getFocalPoint() }),
    stop() { overlay.stop(); canvas.dispose(); delete window._vdbg["repair:" + job]; } };
  window._vdbg["repair:" + job] = api;
  return api;
}
