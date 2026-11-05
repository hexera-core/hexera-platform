// Responsibility: Show the mesh as it is BEING built - one frame per pointer the run published -
// in a viewport that never claims to be the delivered mesh.
// Owns: its vtk.js scene, the one panel per run, and the record of which pointers were drawn.
// Boundaries: it fetches its own frame and draws it; it decides nothing about the run, enables no
// download, opens no result panel, and never sets the delivered-mesh state.

/* THE MESH-SO-FAR.
 *
 * A long mesh used to leave the screen dead. The mesher now publishes a pointer event
 * (`mesh_partial`) the moment a phase's boundary is readable, and the API serves that frame from
 * the key the run itself published. This module is what turns the pointer into something seen.
 *
 * Four rules it keeps, in this order:
 *
 *  1. THE CAMERA DOES NOT MOVE. A frame of a growing mesh has different bounds, and possibly a
 *     different unit, from the content it replaces - so the promise is the one that can be kept:
 *     the viewing DIRECTION and the up vector are held, and the new content is re-framed against
 *     its own bounds. vtk's resetCamera keeps the direction of projection, which is exactly the
 *     weaker, honest promise (design 2026-11-04 §4).
 *  2. ONE DRAW PER POINTER. The durable event log is replayed on every reconnect and on every
 *     page load, so the same `object_key` arrives again and again. A key is recorded BEFORE the
 *     fetch, so a replay neither re-reads the route nor stacks a second set of actors.
 *  3. A PARTIAL IS NOT THE MESH. The payload says so (`is_mesh: false`, `partial: true`) and a
 *     payload that says otherwise is refused here. Nothing in this file touches the result
 *     surface, the deliverables or the run's terminal state, and the caption says in words that
 *     this is the mesh being built.
 *  4. FAILURE IS SILENT. 404 (the flag is off, nothing is published yet, the exchange was tidied)
 *     and 503 leave whatever is on screen alone. This is a progress nicety: it may add nothing,
 *     and it may never break a run.
 *
 * The delivered mesh wins: `dropMeshPartial` is called on the terminal path, which takes the
 * panel down and makes every later pointer for that run a no-op.
 */
import { getMeshPartial } from "../api/endpoints.js";
import { b64f32, b64u32, loadVtk } from "./geometry_stage.js";
import { MOUSE_HINT, bindMouse, isUpAxis, orient, savedUp, shield } from "./view_controls.js";
import { themeRgb } from "./toolbar.js";

/* the same palette the delivered mesh and the geometry check are drawn in (tokens.css
   --viewer-canvas, --part-body); these values are only the fallback when no theme is loaded */
const BACKGROUND_FALLBACK = [0.035, 0.055, 0.075], BODY_FALLBACK = [0.49, 0.51, 0.54];
const EDGE = [0.27, 0.25, 0.22];
const AMBIENT = 0.22, DIFFUSE = 0.80, SPECULAR = 0.06;
// the front-quarter view every viewer here opens with, given for +Z up
const VIEW_DIR = [1, -1, 0.7];

/** What is known about one run: the pointers already taken, the panel, and whether the delivered
 *  mesh has arrived (after which no partial is drawn, however late the pointer). */
const _runs = new Map();
function runState(job) {
  let st = _runs.get(job);
  if (!st) { st = { keys: new Set(), panel: null, scene: null, done: false, draws: 0 }; _runs.set(job, st); }
  return st;
}

/* THE WORKBENCH TAKEOVER, the move the geometry check and the delivered mesh both make: with a
   workbench on the page the viewport takes its column and the conversation stays beside it;
   without one (an embedding page) the panel opens after its anchor. */
function takeOver(box, anchorEl) {
  const wb = document.getElementById("workbench"), app = document.getElementById("app");
  if (!(wb && app)) { if (anchorEl) anchorEl.after(box); return; }
  wb.querySelectorAll(".viewer").forEach((v) => { v.hidden = true; });
  wb.appendChild(box);
  app.classList.add("wb"); app.classList.remove("wb-collapsed");
  const tg = document.getElementById("wb-toggle");
  if (tg) {
    tg.hidden = false;
    tg.onclick = () => {
      const on = app.classList.toggle("wb-collapsed");
      tg.textContent = on ? "Show conversation" : "Hide conversation";
      tg.setAttribute("aria-pressed", on ? "true" : "false");
    };
  }
}

/** The words under the frame. It names the phase the mesher is in and says plainly that this is
 *  not the finished mesh - no cell count, because the count of a mesh still being built is not
 *  the run's and the payload ships no statistics for it. */
export function partialCaption(phase) {
  const p = String(phase || "").trim();
  return (p ? `Mesh being built · ${p}` : "Mesh being built") + " · not the finished mesh";
}

/** One scene for the run's partials: content swaps inside it, the camera does not. */
function initScene(job, host, upAxis) {
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({
    background: themeRgb("--viewer-canvas", BACKGROUND_FALLBACK) });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const cam = ren.getActiveCamera();
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  function setRenderScale() {
    const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR)));
  }
  grw.resize(); setRenderScale();
  // a resize changes the canvas, never the camera: re-framing here would undo the user's own zoom
  const ro = new ResizeObserver(() => { setRenderScale(); rw.render(); });
  ro.observe(host);
  host.addEventListener("contextmenu", (e) => e.preventDefault());
  bindMouse(grw);
  let actors = [], faces = 0, first = true;

  /* RE-FRAME, ASPECT-CORRECT. resetCamera fits the bounding sphere to the view's HEIGHT, so in a
     canvas narrower than it is tall the content ran off both sides; step back by the shortfall
     (the same correction viewer.js makes). It keeps the direction of projection and the view up,
     which is what holds the user's framing across a content swap. */
  function frame() {
    ren.resetCamera();
    const s = apiRW.getSize && apiRW.getSize();
    if (s && s[0] > 0 && s[1] > s[0]) {
      const k = s[1] / s[0];
      if (cam.getParallelProjection()) cam.setParallelScale(cam.getParallelScale() * k);
      else cam.dolly(1 / k);
    }
    ren.resetCameraClippingRange();
  }

  function clear() { actors.forEach((a) => ren.removeActor(a)); actors = []; faces = 0; }

  /** Draw one frame of the mesh-so-far. The FIRST one opens on the front quarter for the way up
   *  this run is being looked at; every one after it keeps the camera exactly as the user left it
   *  and only re-frames against the new bounds. */
  function show(surf) {
    clear();
    const colour = themeRgb("--part-body", BODY_FALLBACK);
    (surf.patches || []).forEach((p) => {
      let pts, polys, nCells;
      if (p.polys_b64) {
        pts = b64f32(p.points_b64); polys = b64u32(p.polys_b64);
        // count the polygons ACTUALLY in the connectivity array - a decimated frame does not ship
        // one per mesh face, and trusting face_count walks the offsets off the end
        nCells = 0; for (let q = 0; q < polys.length; q += polys[q] + 1) nCells++;
      } else if (p.positions_b64) {
        pts = b64f32(p.positions_b64); nCells = pts.length / 9;      // triangle soup
        polys = new Uint32Array(nCells * 4);
        for (let t = 0; t < nCells; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = t * 3; polys[t * 4 + 2] = t * 3 + 1; polys[t * 4 + 3] = t * 3 + 2; }
      } else return;
      const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
      pd.getPoints().setData(pts, 3);
      pd.getPolys().setData(polys);
      const mapper = vtk.Rendering.Core.vtkMapper.newInstance();
      mapper.setInputData(pd);
      const actor = vtk.Rendering.Core.vtkActor.newInstance();
      actor.setMapper(mapper);
      const pr = actor.getProperty();
      pr.setColor(colour[0], colour[1], colour[2]);
      pr.setEdgeVisibility(true); pr.setEdgeColor(EDGE[0], EDGE[1], EDGE[2]); pr.setLineWidth(1);
      pr.setAmbient(AMBIENT); pr.setDiffuse(DIFFUSE);
      pr.setSpecular(SPECULAR); pr.setSpecularPower(18); pr.setSpecularColor(1, 1, 1);
      // an OpenFOAM boundary face points out of the FLUID, so nothing here may cull a back face
      if (pr.setBackfaceCulling) pr.setBackfaceCulling(false);
      ren.addActor(actor);
      actors.push(actor); faces += nCells;
    });
    if (first) { orient(cam, upAxis, VIEW_DIR); first = false; }
    frame();
    rw.render();
  }

  const dist = () => {
    const f = cam.getFocalPoint(), q = cam.getPosition();
    return Math.hypot(q[0] - f[0], q[1] - f[1], q[2] - f[2]);
  };
  /* support/debug hook - the browser tier reads the camera and the content through it */
  window._vdbg = window._vdbg || {};
  window._vdbg["partial:" + job] = {
    actors: () => actors.length, faces: () => faces, dist,
    view: () => ({ focal: cam.getFocalPoint().slice(), position: cam.getPosition().slice(),
                   dir: cam.getDirectionOfProjection().slice(), up: cam.getViewUp().slice() }),
    size: () => apiRW.getSize(), render: () => rw.render(), cam: () => cam,
  };
  return { show, stop: () => { ro.disconnect(); clear(); delete window._vdbg["partial:" + job]; } };
}

/** The panel the frames are drawn in: a viewport and nothing else. No deliverables, no verdict,
 *  no result panel - there is nothing to decide about a mesh that is still growing. */
function openPanel(job, opts) {
  const id = "mesh-partial-" + job;
  document.getElementById(id)?.remove();
  const box = document.createElement("div");
  box.className = "viewer mp-view"; box.id = id;
  box.innerHTML = `<div class="v-canvas" id="mp-canvas-${job}">
      <div class="v-hint">${MOUSE_HINT}</div>
      <div class="v-count" id="mp-caption-${job}" role="status"></div></div>`;
  takeOver(box, opts.anchorEl);
  box.querySelectorAll(".v-hint,.v-count").forEach(shield);
  return box;
}

/** Which way up this run is being looked at: the choice the user made for this job, else the one
 *  the geometry check proposed for its session, else the CAD convention. */
function upAxisFor(job, surf, sessionId) {
  const forJob = savedUp("job", job);
  if (forJob) return forJob;
  if (isUpAxis(surf && surf.up_axis)) return surf.up_axis;
  const forSession = sessionId ? savedUp("session", sessionId) : null;
  return forSession || "+z";
}

/** A `mesh_partial` pointer arrived for the run on screen. Fetches that one frame and draws it.
 *  Resolves to true when something was drawn, false in every other case - including every case
 *  this must survive: an already-seen pointer, a run that has already delivered, a 404 or 503
 *  from the route, a payload that is not a partial, and a browser that cannot load vtk.js. */
export async function showMeshPartial(job, phase, objectKey, opts) {
  opts = opts || {};
  if (!job) return false;
  const st = runState(job);
  if (st.done) return false;                       // the delivered mesh has already won
  const key = String(objectKey || "");
  // ONE DRAW PER POINTER, recorded before the fetch: a reconnect replays the durable log, and a
  // pointer with no key is nothing this can be idempotent about
  if (!key || st.keys.has(key)) return false;
  st.keys.add(key);
  try {
    const surf = await getMeshPartial(job);
    // the route has nothing (the flag is off, nothing is published yet, the exchange was tidied)
    if (!surf) return false;
    // A PARTIAL SAYS SO. Anything else is refused rather than shown as the mesh being built.
    if (surf.partial !== true || surf.is_mesh === true) return false;
    await loadVtk();
    if (st.done) return false;                     // it delivered while this frame was in flight
    if (!st.panel || !document.getElementById(st.panel.id)) {
      st.panel = openPanel(job, opts);
      st.scene = initScene(job, document.getElementById("mp-canvas-" + job),
                           upAxisFor(job, surf, opts.sessionId));
    }
    st.scene.show(surf);
    st.draws += 1;
    const cap = document.getElementById("mp-caption-" + job);
    if (cap) cap.textContent = partialCaption(phase || surf.stage);
    return true;
  } catch (e) {
    // never fatal: the run is unaffected and the screen keeps what it had
    console.warn("the mesh-so-far could not be shown:", (e && e.message) || e);
    return false;
  }
}

/** The run delivered (or ended). The partial content is dropped and every later pointer for this
 *  run is a no-op, so a frame still in flight can never land over the delivered mesh. */
export function dropMeshPartial(job) {
  if (!job) return;
  const st = runState(job);
  st.done = true;
  if (st.scene) st.scene.stop();
  if (st.panel) st.panel.remove();
  st.scene = null; st.panel = null;
}
