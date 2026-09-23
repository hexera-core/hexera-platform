// Responsibility: Show the geometry check as a stage the user can orient in - the part in 3D with a
// numbered sticker on every opening it found, and the panel beside it where names and roles are
// confirmed.
// Owns: its vtk.js scene (the skin, the sticker balls, the pins that follow them) and the
// takeover of the workbench while the check is open.
// Boundaries: it decides nothing about the geometry - positions, sizes and proposals arrive from
// the check - and it starts no run; confirming is the callback it was handed.

/* THE GEOMETRY STAGE.
 *
 * The check used to arrive as a card of pictures in the conversation. Pictures are what the
 * vision model needs; a person wants to turn the part and see where the stickers are. So the
 * same skin the worker drew the pictures from is rendered here with the mesh viewer's own
 * toolkit, and the stage takes the workbench exactly the way a delivered mesh does. Proceeding
 * hands the workbench back, and the conversation carries on where it was.
 */
import { getGeometrySkin } from "../api/endpoints.js";
import { esc } from "../core/format.js";
import { applyFlow, formHtml, markConfirmed, mm, readExternal, readForm } from "../render/geometry_form.js";

let _vtkP = null;
function loadVtk() {
  if (window.vtk) return Promise.resolve();
  if (!_vtkP) _vtkP = new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = "/static/vendor/vtk.js";
    s.onload = res; s.onerror = () => rej(new Error("vtk.js failed to load"));
    document.head.appendChild(s); });
  return _vtkP;
}
function b64f32(b) { const bin = atob(b), n = bin.length, u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = bin.charCodeAt(i); return new Float32Array(u.buffer); }
function b64u32(b) { const bin = atob(b), n = bin.length, u = new Uint8Array(n);
  for (let i = 0; i < n; i++) u[i] = bin.charCodeAt(i); return new Uint32Array(u.buffer); }

/* the part in the drawing-office grey the mesh viewer uses; stickers in the yellow of the
   pictures; the selected one in the product's orange, which nothing else on the stage wears */
const BODY = [0.74, 0.78, 0.84], STICKER = [1.0, 0.83, 0.0], SEL = [1.0, 0.31, 0.0];
const HINT = "rotate: drag · zoom: wheel or right-drag · pan: shift+drag · click a sticker to select it";

function spherePd(cx, cy, cz, r) {
  const la = 12, lo = 16, pts = [], polys = [];
  for (let i = 0; i <= la; i++) { const th = Math.PI * i / la;
    for (let j = 0; j <= lo; j++) { const ph = 2 * Math.PI * j / lo;
      pts.push(cx + r * Math.sin(th) * Math.cos(ph), cy + r * Math.cos(th), cz + r * Math.sin(th) * Math.sin(ph)); } }
  const W = lo + 1;
  for (let i = 0; i < la; i++) for (let j = 0; j < lo; j++) { const a = i * W + j; polys.push(4, a, a + 1, a + W + 1, a + W); }
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  pd.getPoints().setData(Float32Array.from(pts), 3);
  pd.getPolys().setData(Uint32Array.from(polys));
  return pd;
}

/* THE WORKBENCH TAKEOVER, the same move the mesh viewer makes: with a workbench on the page the
   stage fills it and the conversation becomes the drawer; without one it opens inline. Releasing
   gives the page back exactly as it was unless another viewer is still waiting in the workbench. */
function takeOver(box, anchorEl) {
  const wb = document.getElementById("workbench"), app = document.getElementById("app");
  if (!(wb && app)) { if (anchorEl) anchorEl.after(box); return () => box.remove(); }
  wb.querySelectorAll(".viewer").forEach((v) => { v.hidden = true; });
  wb.appendChild(box);
  app.classList.add("wb"); app.classList.remove("wb-collapsed");
  const tg = document.getElementById("wb-toggle");
  if (tg) { tg.hidden = false;
    tg.onclick = () => { const on = app.classList.toggle("wb-collapsed");
      tg.textContent = on ? "Show conversation" : "Hide conversation";
      tg.setAttribute("aria-pressed", on ? "true" : "false"); }; }
  return () => {
    box.remove();
    const left = [...wb.querySelectorAll(".viewer")];
    if (left.length) { left[left.length - 1].hidden = false; return; }
    app.classList.remove("wb", "wb-collapsed");
    if (tg) { tg.hidden = true; tg.textContent = "Hide conversation"; tg.setAttribute("aria-pressed", "false"); }
  };
}

/** Open the check as a stage. `d` is the check the API served (its `proposal` is what is shown),
 *  `confirm(body)` records the answer. `opts.fallback()` is called instead when the stage cannot
 *  be drawn - no skin, no WebGL - so the user still gets the picture card. */
export async function openGeometryStage(sessionId, d, confirm, opts) {
  opts = opts || {};
  const p = d.proposal || {};
  const id = "gstage-" + sessionId;
  document.getElementById(id)?.remove();
  const box = document.createElement("div"); box.className = "viewer gc-stage"; box.id = id;
  const size = (p.size_mm || []).map((v) => mm(v)).join(" x ");
  box.innerHTML = `<div class="v-bar gc-panel">
      <div class="v-row"><b>Geometry check</b><span class="v-meta">what Hexera sees</span></div>
      <div class="gc-lead"><b>${esc(p.part || "the part")}</b>${size ? ` · ${esc(size)} mm` : ""}<br>
        Turn the part and click a sticker to select its row. Fix any name or role, then proceed:
        the questions that follow skip everything confirmed here.</div>
      ${formHtml(p)}
    </div>
    <div class="v-canvas gc-canvas" id="gs-canvas-${sessionId}">
      <div class="v-loading" id="gs-load-${sessionId}"><div>Loading the part…</div></div>
      <div class="v-hint" id="gs-hint-${sessionId}">${HINT}</div>
      <button class="v-btn gc-fit" id="gs-fit-${sessionId}" type="button">Fit</button>
    </div>`;
  const release = takeOver(box, opts.anchorEl);
  const panel = box.querySelector(".gc-panel");
  applyFlow(panel);
  const btn = box.querySelector(".gc-proceed");
  const hint = box.querySelector(".gc-hint");

  let scene = null;
  try {
    await loadVtk();
    const surf = await getGeometrySkin(sessionId);
    scene = initScene(sessionId, box, surf, p);
    document.getElementById("gs-load-" + sessionId)?.remove();
  } catch (e) {
    // the stage is a better way to answer the same form, never the only way
    console.warn("geometry stage unavailable, showing the card instead:", e && e.message);
    release();
    if (opts.fallback) opts.fallback();
    return null;
  }

  btn.onclick = async () => {
    const body = readForm(panel, p);
    btn.disabled = true; btn.textContent = "Confirming…";
    try {
      await confirm(body);
      markConfirmed(panel);
      scene.stop();
      release();                       // the workbench goes back; the conversation carries on
    } catch (e) {
      btn.disabled = false; btn.textContent = "Looks right, proceed";
      hint.textContent = "Could not confirm: " + ((e && e.message) || "try again");
    }
  };
  return { release: () => { scene.stop(); release(); } };
}

function initScene(sessionId, box, surf, p) {
  const host = document.getElementById("gs-canvas-" + sessionId);
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: [0.035, 0.055, 0.075] });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  function setRenderScale() { const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR))); }
  grw.resize();
  // THE FRAMING FOLLOWS THE CANVAS. The workbench grid gives the canvas its size a tick after
  // the stage is appended, so the first fit is made against a placeholder box; every size change
  // until the user has moved the camera re-fits, and after that only "Fit" does.
  let touched = false, fits = 0;
  function fit() {
    cam.setViewUp(0, 0, 1); ren.resetCamera(partBounds);
    // resetCamera fits the part to the view's HEIGHT; a canvas taller than it is wide (the
    // drawer open on a small window) would crop the sides, so back off by the aspect
    const [w, h] = apiRW.getSize(), a = w / Math.max(h, 1);
    if (a < 1) { cam.dolly(a); ren.resetCameraClippingRange(); }
    rw.render(); }
  const ro = new ResizeObserver(() => { setRenderScale(); if (!touched && fits++ < 6) fit(); else rw.render(); });
  ro.observe(host);
  host.addEventListener("pointerdown", () => { touched = true; }, true);
  host.addEventListener("contextmenu", (e) => e.preventDefault());

  /* THE SKIN - one actor per patch the payload carries, in either shape the mesh viewer reads */
  (surf.patches || []).forEach((pt) => {
    let pts, polys;
    if (pt.polys_b64) { pts = b64f32(pt.points_b64); polys = b64u32(pt.polys_b64); }
    else { pts = b64f32(pt.positions_b64); const n = pts.length / 9;
      polys = new Uint32Array(n * 4);
      for (let t = 0; t < n; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = t * 3; polys[t * 4 + 2] = t * 3 + 1; polys[t * 4 + 3] = t * 3 + 2; } }
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
    const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
    const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper); actor.setPickable(false);
    const pr = actor.getProperty();
    pr.setColor(BODY[0], BODY[1], BODY[2]); pr.setEdgeVisibility(false);
    pr.setAmbient(0.32); pr.setDiffuse(0.76); pr.setSpecular(0.12); pr.setSpecularPower(20);
    ren.addActor(actor);
  });
  const cam = ren.getActiveCamera();
  cam.azimuth(45); cam.elevation(25);
  ren.resetCamera(); rw.render();
  const bs = ren.computeVisiblePropBounds();
  const diag = Math.max(Math.hypot(bs[1] - bs[0], bs[3] - bs[2], bs[5] - bs[4]), 1e-9);
  // THE FIT IS THE PART'S. The far-field box, when drawn, is many part-lengths wide; framing on
  // it would leave the part a speck in the middle.
  const partBounds = bs.slice();

  /* THE STICKERS - a ball on every opening, at the position the check measured, and an HTML pin
     with its number that follows it every frame. The pin is the thing to click: it is never
     hidden by the part, only dimmed when its mouth faces away from the camera. */
  const openings = (p.openings || []).map((o) => {
    const c = o.centroid_m && o.centroid_m.length === 3 ? o.centroid_m
            : (o.centroid_mm || [0, 0, 0]).map((v) => v / 1000);
    const n = o.normal || [0, 0, 1];
    return { id: o.id, c, n };
  });
  const pins = [];
  let sel = null;
  const rows = [...box.querySelectorAll(".gc-table tbody tr")];
  openings.forEach((o) => {
    const mp = vtk.Rendering.Core.vtkMapper.newInstance();
    mp.setInputData(spherePd(o.c[0], o.c[1], o.c[2], 0.012 * diag));
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(STICKER[0], STICKER[1], STICKER[2]);
    pp.setAmbient(0.55); pp.setDiffuse(0.6); pp.setSpecular(0.2);
    ren.addActor(ac);
    const el = document.createElement("div"); el.className = "v-pin gc-pin"; el.textContent = String(o.id);
    el.title = "opening " + o.id + " - click to select";
    el.onclick = (ev) => { ev.stopPropagation(); select(o.id); };
    host.appendChild(el);
    pins.push({ id: o.id, o, actor: ac, el });
  });

  function select(id) {
    sel = sel === id ? null : id;
    pins.forEach((pn) => { const on = pn.id === sel;
      const pp = pn.actor.getProperty();
      pp.setColor(on ? SEL[0] : STICKER[0], on ? SEL[1] : STICKER[1], on ? SEL[2] : STICKER[2]);
      pn.el.classList.toggle("sel", on); });
    rows.forEach((tr) => { const on = Number(tr.dataset.id) === sel; tr.classList.toggle("sel", on);
      if (on) { tr.scrollIntoView({ block: "nearest" }); tr.querySelector(".gc-name")?.focus({ preventScroll: true }); } });
    rw.render();
  }
  /* LOOK INTO A MOUTH - the close-up the pictures had, now a camera move: from outside, straight
     down the opening's normal, framed on it */
  function look(id) {
    const pn = pins.find((x) => x.id === id); if (!pn) return;
    const { c, n } = pn.o;
    cam.setFocalPoint(c[0], c[1], c[2]);
    cam.setPosition(c[0] + n[0] * 0.9 * diag, c[1] + n[1] * 0.9 * diag, c[2] + n[2] * 0.9 * diag);
    cam.setViewUp(...(Math.abs(n[2]) < 0.9 ? [0, 0, 1] : [0, 1, 0]));
    ren.resetCameraClippingRange(); cam.zoom(1.6); rw.render();
    touched = true;
    if (sel !== id) select(id);
  }
  rows.forEach((tr) => {
    const n = tr.querySelector(".gc-n"); if (!n) return;
    n.title = "click to look into this opening";
    n.onclick = () => look(Number(tr.dataset.id));
    tr.addEventListener("focusin", () => { const id = Number(tr.dataset.id); if (sel !== id) select(id); });
  });
  document.getElementById("gs-fit-" + sessionId).onclick = fit;

  /* A BODY IN A FLOW: an arrow for the way the fluid travels and the far-field box the mesh
     will be built in, both drawn from the form and redrawn as the user changes it. */
  const panel = box.querySelector(".gc-panel");
  let decor = [];
  function clearDecor() { decor.forEach((a) => ren.removeActor(a)); decor = []; }
  function lineActor(pts, lines, color, width) {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts), 3); pd.getLines().setData(Uint32Array.from(lines));
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(...color); pp.setLineWidth(width); pp.setLighting(false);
    ren.addActor(ac); decor.push(ac); return ac;
  }
  const hintEl = document.getElementById("gs-hint-" + sessionId);
  function refreshExternal() {
    clearDecor();
    const flowSel = panel.querySelector(".gc-flow");
    const external = !!flowSel && flowSel.value === "external";
    pins.forEach((pn) => { pn.actor.setVisibility(!external); if (external) pn.el.style.display = "none"; });
    if (hintEl) hintEl.textContent = external
      ? "rotate: drag · zoom: wheel or right-drag · the arrow is the flow, the box is the far field"
      : HINT;
    if (!external) { rw.render(); return; }
    const ex = readExternal(panel);
    const axis = ex.flow_axis && ex.flow_axis !== "unknown" ? ex.flow_axis : "+x";
    const k = { x: 0, y: 1, z: 2 }[axis[1]], sign = axis[0] === "-" ? -1 : 1;
    const dir = [0, 0, 0]; dir[k] = sign;
    const size = [partBounds[1] - partBounds[0], partBounds[3] - partBounds[2], partBounds[5] - partBounds[4]];
    const L = (ex.reference_length_mm || 0) > 0 ? ex.reference_length_mm / 1000 : (size[k] || diag);
    const c = [(partBounds[0] + partBounds[1]) / 2, (partBounds[2] + partBounds[3]) / 2, (partBounds[4] + partBounds[5]) / 2];
    // the arrow: a shaft through the part's centre and a head of three short strokes
    const half = 0.7 * Math.max(size[k], 0.3 * diag);
    const tail = c.map((v, i) => v - dir[i] * half), tip = c.map((v, i) => v + dir[i] * half);
    const u = k === 2 ? [1, 0, 0] : [0, 0, 1], w = [dir[1] * u[2] - dir[2] * u[1], dir[2] * u[0] - dir[0] * u[2], dir[0] * u[1] - dir[1] * u[0]];
    const h = 0.12 * half;
    const back = tip.map((v, i) => v - dir[i] * h * 2);
    const pts = [...tail, ...tip, ...back.map((v, i) => v + u[i] * h), ...back.map((v, i) => v - u[i] * h),
                 ...back.map((v, i) => v + w[i] * h), ...back.map((v, i) => v - w[i] * h)];
    lineActor(pts, [2, 0, 1, 2, 1, 2, 2, 1, 3, 2, 1, 4, 2, 1, 5], SEL, 3);
    // the far-field box: upstream and downstream along the flow, sideways on the other
    // horizontal axis, up on the vertical one; the floor is the ground or a margin below
    const e = ex.extents || {};
    const lo = partBounds.filter((_, i) => i % 2 === 0), hi = partBounds.filter((_, i) => i % 2 === 1);
    const up = (sign > 0 ? e.upstream : e.downstream) || 5, down = (sign > 0 ? e.downstream : e.upstream) || 5;
    lo[k] -= up * L; hi[k] += down * L;
    const vert = k === 2 ? 1 : 2, side = [0, 1, 2].find((i) => i !== k && i !== vert);
    lo[side] -= (e.lateral || 5) * L; hi[side] += (e.lateral || 5) * L;
    hi[vert] += (e.vertical || 5) * L;
    if (!ex.grounded) lo[vert] -= (e.vertical || 5) * L;
    const P = [[lo[0], lo[1], lo[2]], [hi[0], lo[1], lo[2]], [hi[0], hi[1], lo[2]], [lo[0], hi[1], lo[2]],
               [lo[0], lo[1], hi[2]], [hi[0], lo[1], hi[2]], [hi[0], hi[1], hi[2]], [lo[0], hi[1], hi[2]]];
    const E = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
    lineActor(P.flat(), E.flatMap(([a, b]) => [2, a, b]), [0.55, 0.72, 0.9], 1);
    if (ex.grounded) {   // the ground drawn as a faint plate under the part
      const g = lo[vert], Q = P.filter((p) => p[vert] === g);
      lineActor(Q.flat(), [2, 0, 2, 2, 1, 3], [0.55, 0.72, 0.9], 1);
    }
    rw.render();
  }
  panel.addEventListener("change", (ev) => { if (ev.target.closest(".gc-flow,.gc-axis,.gc-ground,.gc-ext")) refreshExternal(); });
  panel.addEventListener("input", (ev) => { if (ev.target.closest(".gc-ref,.gc-extents")) refreshExternal(); });
  refreshExternal();

  let alive = true;
  (function pinLoop() {
    if (!alive || !document.body.contains(host)) return;
    const size = apiRW.getSize(), aspect = size[0] / size[1], rect = host.getBoundingClientRect();
    const cp = cam.getPosition();
    const externalNow = (panel.querySelector(".gc-flow") || {}).value === "external";
    pins.forEach((pn) => {
      if (externalNow) { pn.el.style.display = "none"; return; }
      const [x, y, z] = pn.o.c;
      const nd = ren.worldToNormalizedDisplay(x, y, z, aspect);
      const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
      pn.el.style.display = ok ? "" : "none";
      if (!ok) return;
      pn.el.style.left = (nd[0] * rect.width) + "px";
      pn.el.style.top = ((1 - nd[1]) * rect.height) + "px";
      // a mouth whose outward normal points away from the camera is on the far side
      const n = pn.o.n, facing = (n[0] * (x - cp[0]) + n[1] * (y - cp[1]) + n[2] * (z - cp[2])) < 0;
      pn.el.classList.toggle("back", !facing);
    });
    requestAnimationFrame(pinLoop);
  })();

  setRenderScale(); fit();

  /* support/debug hook - the browser tier drives the stage through it, without pixel picking */
  window._vdbg = window._vdbg || {};
  window._vdbg["gstage:" + sessionId] = {
    pins: () => pins.length, selected: () => sel, select, look, diag,
    external: () => ({ arrow: decor.length >= 1, box: decor.length >= 2, actors: decor.length }),
    visible: () => pins.filter((pn) => pn.el.style.display !== "none").length,
    render: () => rw.render(),
  };
  return { stop: () => { alive = false; ro.disconnect(); clearDecor(); delete window._vdbg["gstage:" + sessionId]; } };
}
