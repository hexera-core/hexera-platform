// Responsibility: Show the geometry check as a stage the user can orient in - the part in 3D drawn
// the way a CAD viewer draws it, a numbered sticker on every opening, and the panel beside it
// where names and roles are confirmed, stickers added or removed.
// Owns: its vtk.js scene (the skin, its edges, the lights, the sticker balls and the pins that
// follow them), the naming state of the panel, and the takeover of the workbench while the
// check is open.
// Boundaries: it decides nothing about the geometry - positions, sizes and proposals arrive from
// the check - and it starts no run; confirming is the callback it was handed.

/* THE GEOMETRY STAGE.
 *
 * The stage opens the moment the part is measured, with the measuring step's own labels greyed
 * out under a "naming" banner, and fills in the model's labels when they arrive - the user is
 * already turning the part while the model thinks. Proceeding hands the workbench back, and the
 * conversation carries on where it was.
 */
import { getGeometrySkin } from "../api/endpoints.js";
import { esc } from "../core/format.js";
import { applyFlow, formHtml, markConfirmed, mm, readExternal, readForm, rowHtml, setNaming }
  from "../render/geometry_form.js";

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

/* the console's own palette: the mesh viewer's dark background, a light grey part with soft
   highlights, darker hairlines on the sharp corners; stickers in the yellow of the pictures, the
   selected one in the product's orange, which nothing else on the stage wears */
const BACKGROUND = [0.035, 0.055, 0.075];
const BODY = [0.74, 0.78, 0.84], EDGE = [0.30, 0.34, 0.40];
const STICKER = [1.0, 0.83, 0.0], SEL = [1.0, 0.31, 0.0];

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

function leadHtml(p) {
  const size = (p.size_mm || []).map((v) => mm(v)).join(" x ");
  return `<b>${esc(p.part || "the part")}</b>${size ? ` · ${esc(size)} mm` : ""}<br>
    Turn the part and click a sticker to select its row. Fix any name or role, add or remove a
    sticker, then proceed: the questions that follow skip everything confirmed here.`;
}

/** Open the check as a stage. `d` is the check the API served (its `proposal` is what is shown;
 *  `named: false` opens the stage in naming mode, labels greyed out under a banner until
 *  `update` brings the model's), `confirm(body)` records the answer. `opts.fallback()` is called
 *  instead when the stage cannot be drawn - no skin, no WebGL - so the user still gets the card. */
export async function openGeometryStage(sessionId, d, confirm, opts) {
  opts = opts || {};
  const p = d.proposal || {};
  const id = "gstage-" + sessionId;
  document.getElementById(id)?.remove();
  const box = document.createElement("div"); box.className = "viewer gc-stage"; box.id = id;
  box.innerHTML = `<div class="v-bar gc-panel">
      <div class="v-row"><b>Geometry check</b><span class="v-meta">what Hexera sees</span></div>
      <div class="gc-banner" hidden><span class="gc-spin" aria-hidden="true"></span><span class="gc-banner-text"></span></div>
      <div class="gc-lead">${leadHtml(p)}</div>
      <div class="gc-form">${formHtml(p)}</div>
    </div>
    <div class="v-canvas gc-canvas" id="gs-canvas-${sessionId}">
      <div class="v-loading" id="gs-load-${sessionId}"><div>Loading the part…</div></div>
      <svg class="gc-axes" viewBox="0 0 84 84" aria-hidden="true">
        ${["x", "y", "z"].map((k) => `<line class="ax ax-${k}" x1="42" y1="42" x2="42" y2="42"/><text class="ax-l ax-${k}" x="42" y="42">${k.toUpperCase()}</text>`).join("")}
      </svg>
    </div>`;
  const release = takeOver(box, opts.anchorEl);
  const panel = box.querySelector(".gc-panel");
  const form = panel.querySelector(".gc-form");
  applyFlow(form);
  const banner = panel.querySelector(".gc-banner");
  function showBanner(text, cls) {
    banner.hidden = !text; banner.className = "gc-banner" + (cls ? " " + cls : "");
    banner.querySelector(".gc-banner-text").textContent = text || "";
  }
  // THE BANNER SAYS WHAT IS REALLY HAPPENING: the model only starts naming once the user has
  // answered the question in the chat; before that the stage is waiting on them, not on it
  const bannerFor = (d1) => (d1 && d1.naming_requested
    ? "Naming the openings… you can turn the part meanwhile"
    : "Answer the question in the chat and I'll name the openings - you can turn the part meanwhile");
  let naming = d.named === false;
  if (naming) { showBanner(bannerFor(d)); setNaming(form, true); }

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

  function bindProceed() {
    const btn = form.querySelector(".gc-proceed"), hint = form.querySelector(".gc-hint");
    btn.onclick = async () => {
      // an opening placed off any measured face has no size until the user types one; the
      // confirm never receives a mouth of no size
      const blank = [...form.querySelectorAll(".gc-dia")].find((i) => !(Number(i.value) > 0));
      if (blank) {
        const tr = blank.closest("tr");
        hint.textContent = `Opening ${tr ? tr.dataset.id : ""} needs a size first: type how many mm across.`;
        blank.focus();
        return;
      }
      const body = readForm(form, p);
      btn.disabled = true; btn.textContent = "Confirming…";
      try {
        await confirm(body);
        markConfirmed(form);
        scene.stop();
        release();                       // the workbench goes back; the conversation carries on
      } catch (e) {
        btn.disabled = false; btn.textContent = "Looks right, proceed";
        hint.textContent = "Could not confirm: " + ((e && e.message) || "try again");
      }
    };
  }
  bindProceed();

  /** The model's labels arrived (or gave up): the form is re-drawn from the new proposal, the
   *  banner goes, the form opens for editing. Positions and sizes are the code's and do not
   *  move; the stickers stay where they are. */
  function update(d2) {
    if (d2 && d2.status === "scouted") {              // still measuring-step labels: only the banner moves
      if (naming) showBanner(bannerFor(d2));
      return;
    }
    const p2 = (d2 && d2.proposal) || {};
    const failed = d2 && d2.status === "failed";
    if (!failed) {
      // keep the code's measurements, take the model's words - IN PLACE: the scene holds the
      // same object, and what the user adds or removes there is what Proceed reads
      const byId = new Map((p2.openings || []).map((o) => [o.id, o]));
      const merged = (p.openings || []).map((o) => {
        const m = byId.get(o.id); return m ? { ...o, name: m.name, role: m.role, confidence: m.confidence } : o; });
      Object.assign(p, p2, { openings: merged });
      panel.querySelector(".gc-lead").innerHTML = leadHtml(p);
      form.innerHTML = formHtml(p);
      applyFlow(form); bindProceed(); scene.rebind();
      showBanner("");
    } else {
      showBanner("The naming step gave up; the names below are the measuring step's own.", "warn");
      setTimeout(() => { if (!naming) showBanner(""); }, 8000);
    }
    naming = false;
    setNaming(form, false);
    scene.refresh();
  }

  return { release: () => { scene.stop(); release(); }, update, isNaming: () => naming };
}

function initScene(sessionId, box, surf, p) {
  const host = document.getElementById("gs-canvas-" + sessionId);
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: BACKGROUND });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  function setRenderScale() { const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR))); }
  grw.resize();
  host.addEventListener("contextmenu", (e) => e.preventDefault());

  /* THE LIGHTS: a key from the upper left, a fill from the right and a faint rim from behind,
     all riding with the camera, so a turned part is always lit the way a CAD viewer lights it */
  if (vtk.Rendering.Core.vtkLight) {
    [[-0.7, 0.9, 1.0, 0.62], [0.9, 0.25, 0.7, 0.28], [0.0, -0.4, -1.0, 0.14]].forEach(([x, y, z, i]) => {
      const l = vtk.Rendering.Core.vtkLight.newInstance();
      if (l.setLightTypeToCameraLight) l.setLightTypeToCameraLight();
      l.setPosition(x, y, z); l.setFocalPoint(0, 0, 0); l.setIntensity(i); l.setColor(1, 1, 1);
      ren.addLight(l);
    });
  }

  /* THE SKIN - shared vertices, smooth normals broken at sharp corners, from the worker; the
     older soup form still draws, flat */
  const skins = [];
  (surf.patches || []).forEach((pt) => {
    let pts, polys, normals = null;
    if (pt.polys_b64) { pts = b64f32(pt.points_b64); polys = b64u32(pt.polys_b64);
      if (pt.normals_b64) normals = b64f32(pt.normals_b64); }
    else { pts = b64f32(pt.positions_b64); const n = pts.length / 9;
      polys = new Uint32Array(n * 4);
      for (let t = 0; t < n; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = t * 3; polys[t * 4 + 2] = t * 3 + 1; polys[t * 4 + 3] = t * 3 + 2; } }
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
    if (normals && normals.length === pts.length) {
      pd.getPointData().setNormals(vtk.Common.Core.vtkDataArray.newInstance({ name: "Normals", values: normals, numberOfComponents: 3 }));
    }
    const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
    const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper); actor.setPickable(true);
    const pr = actor.getProperty();
    pr.setColor(BODY[0], BODY[1], BODY[2]); pr.setEdgeVisibility(false);
    pr.setInterpolationToPhong();
    pr.setAmbient(0.32); pr.setDiffuse(0.76); pr.setSpecular(0.12); pr.setSpecularPower(20);
    ren.addActor(actor);
    skins.push({ actor, pd, pts, polys, offsets: null });
  });
  /* THE SHARP EDGES - dark hairlines where faces meet at an angle, and along open rims */
  let edgeCount = 0;
  if (surf.edges && surf.edges.lines_b64) {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(b64f32(surf.edges.points_b64), 3);
    const lines = b64u32(surf.edges.lines_b64); pd.getLines().setData(lines); edgeCount = lines.length / 3;
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(...EDGE); pp.setLineWidth(1); pp.setLighting(false); pp.setOpacity(0.9);
    ren.addActor(ac);
  }
  const cam = ren.getActiveCamera();
  cam.azimuth(45); cam.elevation(25);
  ren.resetCamera(); rw.render();
  const bs = ren.computeVisiblePropBounds();
  const diag = Math.max(Math.hypot(bs[1] - bs[0], bs[3] - bs[2], bs[5] - bs[4]), 1e-9);
  // THE FIT IS THE PART'S. The far-field box, when drawn, is many part-lengths wide; framing on
  // it would leave the part a speck in the middle.
  const partBounds = bs.slice();
  const centre = [(bs[0] + bs[1]) / 2, (bs[2] + bs[3]) / 2, (bs[4] + bs[5]) / 2];

  // THE FRAMING FOLLOWS THE CANVAS. The workbench grid gives the canvas its size a tick after
  // the stage is appended, so the first fit is made against a placeholder box; every size change
  // until the user has moved the camera re-fits, and after that only "Fit" does.
  let touched = false;
  function fit() {
    ren.resetCamera(partBounds);
    // resetCamera fits the part to the view's HEIGHT; a canvas taller than it is wide (the
    // drawer open on a small window) would crop the sides, so back off by the aspect
    const [w, h] = apiRW.getSize(), a = w / Math.max(h, 1);
    if (a < 1) { cam.dolly(a); ren.resetCameraClippingRange(); }
    rw.render(); }
  /* THE OPENING VIEW: the drawing-office three-quarter view, framed on the part */
  function iso() {
    cam.setPosition(centre[0] - diag, centre[1] - diag, centre[2] + 0.8 * diag);
    cam.setFocalPoint(...centre); cam.setViewUp(0, 0, 1);
    fit();
  }
  const ro = new ResizeObserver(() => { setRenderScale(); if (!touched) fit(); else rw.render(); });
  ro.observe(host);
  host.addEventListener("pointerdown", () => { touched = true; }, true);

  /* THE STICKERS - a ball on every opening, at the position the check measured, and an HTML pin
     with its number that follows it every frame. The pin is the thing to click: it is never
     hidden by the part, only dimmed when its mouth faces away from the camera. */
  const pins = [];
  let sel = null;
  const panel = box.querySelector(".gc-panel");
  const form = panel.querySelector(".gc-form");
  function centreOf(o) {
    return o.centroid_m && o.centroid_m.length === 3 ? o.centroid_m : (o.centroid_mm || [0, 0, 0]).map((v) => v / 1000);
  }
  function addPin(o) {
    const c = centreOf(o), n = o.normal || [0, 0, 1];
    const mp = vtk.Rendering.Core.vtkMapper.newInstance();
    mp.setInputData(spherePd(c[0], c[1], c[2], 0.012 * diag));
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp); ac.setPickable(false);
    const pp = ac.getProperty(); pp.setColor(STICKER[0], STICKER[1], STICKER[2]);
    pp.setAmbient(0.55); pp.setDiffuse(0.6); pp.setSpecular(0.2);
    ren.addActor(ac);
    const el = document.createElement("div"); el.className = "v-pin gc-pin"; el.textContent = String(o.id);
    el.title = "opening " + o.id + " - click to select";
    el.onclick = (ev) => { ev.stopPropagation(); select(o.id); };
    host.appendChild(el);
    pins.push({ id: o.id, o: { id: o.id, c, n }, actor: ac, el });
  }
  function removePin(id) {
    const k = pins.findIndex((pn) => pn.id === id); if (k < 0) return;
    ren.removeActor(pins[k].actor); pins[k].el.remove(); pins.splice(k, 1);
    if (sel === id) sel = null;
    rw.render();
  }
  function rows() { return [...form.querySelectorAll(".gc-table tbody tr")]; }
  function select(id) {
    sel = sel === id ? null : id;
    pins.forEach((pn) => { const on = pn.id === sel;
      const pp = pn.actor.getProperty();
      pp.setColor(on ? SEL[0] : STICKER[0], on ? SEL[1] : STICKER[1], on ? SEL[2] : STICKER[2]);
      pn.el.classList.toggle("sel", on); });
    rows().forEach((tr) => { const on = Number(tr.dataset.id) === sel; tr.classList.toggle("sel", on);
      if (on) { tr.scrollIntoView({ block: "nearest" }); const inp = tr.querySelector(".gc-name"); if (inp && !inp.disabled) inp.focus({ preventScroll: true }); } });
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

  /* ADDING A STICKER: click the part where the opening is. The click is snapped to the nearest
     flat face the measuring step found, so the new opening carries a real size and position;
     off any measured face it lands on the picked spot with the size left for the user. */
  const selector = apiRW.getSelector ? apiRW.getSelector() : null;
  if (selector) { selector.setCaptureZValues(false); selector.setFieldAssociation(1); }
  let addMode = false, downXY = null;
  function setAddMode(on) {
    addMode = on;
    const b = form.querySelector(".gc-add"); if (b) { b.classList.toggle("armed", on); b.textContent = on ? "Click the part…" : "Add an opening"; }
    host.style.cursor = on ? "crosshair" : "";
    const th = form.querySelector(".gc-tools-hint");
    if (th) th.textContent = on ? "click the spot on the part where the opening is (dragging still turns it)" : "then click the part where it is";
  }
  function cellOffsets(sk) {
    if (!sk.offsets) { const n = sk.polys.length; const off = []; let q = 0; while (q < n) { off.push(q); q += sk.polys[q] + 1; } sk.offsets = off; }
    return sk.offsets;
  }
  function cellCentreNormal(sk, cid) {
    const o = cellOffsets(sk)[cid], n = sk.polys[o];
    let x = 0, y = 0, z = 0;
    for (let j = 1; j <= n; j++) { const q = sk.polys[o + j] * 3; x += sk.pts[q]; y += sk.pts[q + 1]; z += sk.pts[q + 2]; }
    let nx = 0, ny = 0, nz = 0;
    for (let j = 1; j <= n; j++) { const a = sk.polys[o + j] * 3, b = sk.polys[o + (j % n) + 1] * 3;
      nx += (sk.pts[a + 1] - sk.pts[b + 1]) * (sk.pts[a + 2] + sk.pts[b + 2]);
      ny += (sk.pts[a + 2] - sk.pts[b + 2]) * (sk.pts[a] + sk.pts[b]);
      nz += (sk.pts[a] - sk.pts[b]) * (sk.pts[a + 1] + sk.pts[b + 1]); }
    const L = Math.hypot(nx, ny, nz) || 1;
    return { c: [x / n, y / n, z / n], n: [nx / L, ny / L, nz / L] };
  }
  function pick(e, cb) {
    if (!selector) return;
    const rect = host.getBoundingClientRect(), size = apiRW.getSize();
    const x = Math.round((e.clientX - rect.left) * (size[0] / rect.width));
    const y = Math.round((rect.height - (e.clientY - rect.top)) * (size[1] / rect.height));
    selector.getSourceDataAsync(ren, x, y, x, y).then((src) => {
      if (!src) return;
      const sels = src.generateSelection(x, y, x, y);
      if (!sels || !sels.length) return;
      const pr = sels[0].getProperties();
      const sk = skins.find((s) => s.actor === pr.prop); if (!sk) return;
      const ids = (pr.selectionList && pr.selectionList.length) ? pr.selectionList
               : (pr.attributeID != null && pr.attributeID >= 0 ? [pr.attributeID] : []);
      const cid = Number(ids[0]);
      if (cid >= 0) cb(cellCentreNormal(sk, cid));
    }).catch(() => {});
  }
  /** A new opening at a point on the part: snapped to the measured flat face the click lands
   *  on, else the point itself with no size. Of concentric faces - a coaxial fitting's inner
   *  and outer port share a centre - the smallest one that still reaches the click is the one
   *  clicked; a face turned away from the clicked surface is never it. Returns the opening. */
  function add(point, normal) {
    const faces = p.faces || [];
    let best = null, bestHalf = Infinity, bestD = Infinity;
    faces.forEach((f) => {
      const fn = f.normal || [0, 0, 1];
      if (normal && (fn[0] * normal[0] + fn[1] * normal[1] + fn[2] * normal[2]) < 0.5) return;
      const fc = f.centroid_m || (f.centroid_mm || [0, 0, 0]).map((v) => v / 1000);
      const half = Math.max(f.width_mm || 0, f.height_mm || 0, f.diameter_mm || 0) / 1000 / 2;
      const dd = Math.hypot(point[0] - fc[0], point[1] - fc[1], point[2] - fc[2]);
      if (dd > 1.1 * half + 0.003) return;                // the click is not on this face (a tenth of slack)
      if (half < bestHalf || (half === bestHalf && dd < bestD)) { best = f; bestHalf = half; bestD = dd; }
    });
    const nextId = Math.max(0, ...(p.openings || []).map((o) => Number(o.id) || 0)) + 1;
    const o = best
      ? { id: nextId, name: `opening_${nextId}`, role: "outlet", shape: best.shape, kind: best.kind, confidence: 0.5,
          centroid_m: best.centroid_m, centroid_mm: best.centroid_mm, normal: best.normal,
          diameter_mm: best.diameter_mm, width_mm: best.width_mm, height_mm: best.height_mm, added: true, snapped: true }
      : { id: nextId, name: `opening_${nextId}`, role: "outlet", shape: "unknown", kind: "picked", confidence: 0.3,
          centroid_m: point.slice(), centroid_mm: point.map((v) => Math.round(v * 1000 * 100) / 100), normal: normal.slice(), added: true };
    p.openings = [...(p.openings || []), o];
    const tbody = form.querySelector(".gc-table tbody");
    if (tbody) { tbody.insertAdjacentHTML("beforeend", rowHtml(o)); }
    else {
      // the first opening on a part that had none: the table takes the "no openings" note's
      // place, and the Add button beside it stays
      const table = `<table class="gc-table"><thead><tr><th>#</th><th>name</th><th>role</th><th>size</th><th class="gc-pos">position</th><th>sure</th><th></th></tr></thead><tbody>${rowHtml(o)}</tbody></table>`;
      const note = form.querySelector(".gc-int .gc-note");
      if (note) note.outerHTML = table; else form.querySelector(".gc-int").insertAdjacentHTML("afterbegin", table);
    }
    addPin(o); rebind(); select(o.id); rw.render();
    return o;
  }
  function remove(id) {
    p.openings = (p.openings || []).filter((o) => Number(o.id) !== Number(id));
    form.querySelector(`.gc-table tr[data-id="${id}"]`)?.remove();
    removePin(Number(id));
  }
  host.addEventListener("pointerdown", (e) => { downXY = [e.clientX, e.clientY]; }, true);
  host.addEventListener("pointerup", (e) => {
    if (!addMode || !downXY) return;
    if (e.target && e.target.classList && e.target.classList.contains("gc-pin")) return;
    if (Math.hypot(e.clientX - downXY[0], e.clientY - downXY[1]) > 6) return;
    pick(e, ({ c, n }) => { add(c, n); setAddMode(false); });
  }, true);

  /* the form is re-drawn when the model's labels arrive, so its controls are re-bound then */
  function rebind() {
    rows().forEach((tr) => {
      const n = tr.querySelector(".gc-n"); if (n) { n.title = "click to look into this opening"; n.onclick = () => look(Number(tr.dataset.id)); }
      const x = tr.querySelector(".gc-x"); if (x) x.onclick = () => remove(Number(tr.dataset.id));
      if (!tr.dataset.bound) { tr.dataset.bound = "1"; tr.addEventListener("focusin", () => { const id = Number(tr.dataset.id); if (sel !== id) select(id); }); }
    });
    const addBtn = form.querySelector(".gc-add"); if (addBtn) addBtn.onclick = () => setAddMode(!addMode);
    // pins follow the rows: a row without a pin gets one, a pin without a row goes
    const ids = new Set(rows().map((tr) => Number(tr.dataset.id)));
    pins.slice().forEach((pn) => { if (!ids.has(pn.id)) removePin(pn.id); });
    (p.openings || []).forEach((o) => { if (ids.has(Number(o.id)) && !pins.some((pn) => pn.id === Number(o.id))) addPin(o); });
  }
  rebind();

  /* A BODY IN A FLOW: an arrow for the way the fluid travels and the far-field box the mesh
     will be built in, both drawn from the form and redrawn as the user changes it. */
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
  function refreshExternal() {
    clearDecor();
    const flowSel = form.querySelector(".gc-flow");
    const external = !!flowSel && flowSel.value === "external";
    pins.forEach((pn) => { pn.actor.setVisibility(!external); if (external) pn.el.style.display = "none"; });
    if (!external) { rw.render(); return; }
    const ex = readExternal(form);
    const axis = ex.flow_axis && ex.flow_axis !== "unknown" ? ex.flow_axis : "+x";
    const k = { x: 0, y: 1, z: 2 }[axis[1]], sign = axis[0] === "-" ? -1 : 1;
    const dir = [0, 0, 0]; dir[k] = sign;
    const size = [partBounds[1] - partBounds[0], partBounds[3] - partBounds[2], partBounds[5] - partBounds[4]];
    const L = (ex.reference_length_mm || 0) > 0 ? ex.reference_length_mm / 1000 : (size[k] || diag);
    const c = centre;
    const half = 0.7 * Math.max(size[k], 0.3 * diag);
    const tail = c.map((v, i) => v - dir[i] * half), tip = c.map((v, i) => v + dir[i] * half);
    const u = k === 2 ? [1, 0, 0] : [0, 0, 1], w = [dir[1] * u[2] - dir[2] * u[1], dir[2] * u[0] - dir[0] * u[2], dir[0] * u[1] - dir[1] * u[0]];
    const h = 0.12 * half;
    const back = tip.map((v, i) => v - dir[i] * h * 2);
    const pts = [...tail, ...tip, ...back.map((v, i) => v + u[i] * h), ...back.map((v, i) => v - u[i] * h),
                 ...back.map((v, i) => v + w[i] * h), ...back.map((v, i) => v - w[i] * h)];
    lineActor(pts, [2, 0, 1, 2, 1, 2, 2, 1, 3, 2, 1, 4, 2, 1, 5], SEL, 3);
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
    lineActor(P.flat(), E.flatMap(([a, b]) => [2, a, b]), [0.45, 0.6, 0.8], 1);
    if (ex.grounded) {
      const g = lo[vert], Q = P.filter((q) => q[vert] === g);
      lineActor(Q.flat(), [2, 0, 2, 2, 1, 3], [0.45, 0.6, 0.8], 1);
    }
    rw.render();
  }
  panel.addEventListener("change", (ev) => { if (ev.target.closest(".gc-flow,.gc-axis,.gc-ground,.gc-ext")) refreshExternal(); });
  panel.addEventListener("input", (ev) => { if (ev.target.closest(".gc-ref,.gc-extents")) refreshExternal(); });
  refreshExternal();

  /* THE AXES - bottom right, the X, Y and Z of the part turning with it, so the user always
     knows which way the part's coordinates run. Drawn as a small overlay from the camera's
     frame: an axis pointing away from the viewer is dimmed. */
  const axesEl = box.querySelector(".gc-axes");
  const axesLines = axesEl ? ["x", "y", "z"].map((k) => [axesEl.querySelector("line.ax-" + k), axesEl.querySelector("text.ax-" + k)]) : [];
  let axesNow = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] };
  function drawAxes() {
    if (!axesEl) return;
    const dir = cam.getDirectionOfProjection(), up = cam.getViewUp();
    const right = [dir[1] * up[2] - dir[2] * up[1], dir[2] * up[0] - dir[0] * up[2], dir[0] * up[1] - dir[1] * up[0]];
    const rl = Math.hypot(right[0], right[1], right[2]) || 1;
    right[0] /= rl; right[1] /= rl; right[2] /= rl;
    const upp = [right[1] * dir[2] - right[2] * dir[1], right[2] * dir[0] - right[0] * dir[2], right[0] * dir[1] - right[1] * dir[0]];
    const C = 42, L = 28;
    axesNow = {};
    ["x", "y", "z"].forEach((k, i) => {
      // the world axis i on the screen: its parts along the camera's right, up and out
      const sx = right[i], sy = upp[i], sz = -dir[i];
      axesNow[k] = [sx, sy, sz];
      const [line, label] = axesLines[i];
      line.setAttribute("x2", (C + L * sx).toFixed(1)); line.setAttribute("y2", (C - L * sy).toFixed(1));
      label.setAttribute("x", (C + (L + 9) * sx).toFixed(1)); label.setAttribute("y", (C - (L + 9) * sy).toFixed(1));
      line.classList.toggle("back", sz < 0); label.classList.toggle("back", sz < 0);
    });
  }

  let alive = true;
  (function pinLoop() {
    if (!alive || !document.body.contains(host)) return;
    drawAxes();
    const size = apiRW.getSize(), aspect = size[0] / size[1], rect = host.getBoundingClientRect();
    const cp = cam.getPosition();
    const externalNow = (form.querySelector(".gc-flow") || {}).value === "external";
    pins.forEach((pn) => {
      if (externalNow) { pn.el.style.display = "none"; return; }
      const [x, y, z] = pn.o.c;
      const nd = ren.worldToNormalizedDisplay(x, y, z, aspect);
      const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
      pn.el.style.display = ok ? "" : "none";
      if (!ok) return;
      pn.el.style.left = (nd[0] * rect.width) + "px";
      pn.el.style.top = ((1 - nd[1]) * rect.height) + "px";
      const n = pn.o.n, facing = (n[0] * (x - cp[0]) + n[1] * (y - cp[1]) + n[2] * (z - cp[2])) < 0;
      pn.el.classList.toggle("back", !facing);
    });
    requestAnimationFrame(pinLoop);
  })();

  setRenderScale(); iso();

  /* support/debug hook - the browser tier drives the stage through it, without pixel picking */
  window._vdbg = window._vdbg || {};
  window._vdbg["gstage:" + sessionId] = {
    pins: () => pins.length, selected: () => sel, select, look, diag, add, remove, iso,
    openings: () => (p.openings || []).map((o) => Number(o.id)),
    external: () => ({ arrow: decor.length >= 1, box: decor.length >= 2, actors: decor.length }),
    visible: () => pins.filter((pn) => pn.el.style.display !== "none").length,
    camera: () => cam.getPosition(), cam: () => cam, edges: () => edgeCount, axes: () => { drawAxes(); return axesNow; },
    smooth: () => skins.some((s) => !!s.pd.getPointData().getNormals()),
    render: () => rw.render(),
  };
  return {
    stop: () => { alive = false; ro.disconnect(); clearDecor(); delete window._vdbg["gstage:" + sessionId]; },
    rebind, refresh: refreshExternal,
  };
}
