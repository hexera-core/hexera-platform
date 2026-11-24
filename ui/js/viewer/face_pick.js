// Responsibility: Face picking on the settled intake model - a plain click on the part asks the server which face is
// there, lights that face and puts one chip under the box the user used last; a second click on it, or its ✕, drops it.
// Owns: the faces picked and their highlight in every view the part is drawn in (the stage, the parts model over it).
// Boundaries: which face a point is on is the server's (application/face_pick through endpoints.faceAt); the chip
// row is shell/chips.js; when a click is a pick is the caller's (geometry_stage.js, parts_view.js). NOTE-17.

import { faceAt } from "../api/endpoints.js";
import { say } from "../shell/attach.js";
import * as chips from "../shell/chips.js";
import { firstHit } from "./open_ends.js";
import { b64f32, b64u32 } from "./scene/vtk_load.js";
import { themeRgb } from "./toolbar.js";

/* WHAT THE USER SEES. Nothing new until a face is clicked; then the face in the product's one accent and one chip,
 * "Face 12 · round Ø15 mm ✕", under the chat box (or the notes box when that was the last one used). Up to 20 faces.
 * A click that lands on no face does nothing. The chips go when the message or note is sent - the light goes with
 * them. Only once the stage is settled (after Proceed): before that a click on the part means what it always did. */

export const MAX_FACES = 20;
export const TOO_MANY_FACES = `${MAX_FACES} faces is the most.`;
export const CLICK_PX = 6;                       // a press that moves further than this turned the model: no pick
const ACCENT = [0.886, 0.173, 0.173];            // the stage's own selection colour, when the theme gives none

let stage = null;                                // {sessionId, box}: the settled stage the faces are picked on
const views = new Set();                         // {ren, rw}: where the picked faces are drawn
const lit = new Map();                           // chip key -> {id, box, pd, actors: Map(view -> actor)}
let asking = false, unwatch = null;

/** A press and its release are one click (not a turn of the model). */
export function isClick(down, up) {
  return !!down && Math.hypot(up[0] - down[0], up[1] - down[1]) <= CLICK_PX;
}

/** The stage the faces are picked on (geometry_stage's scene, CFD and FEA): picking is on once its box is settled. */
export function bindStage(sessionId, box) { stage = { sessionId, box }; }

/** A click on the part is a face pick now: the stage is drawn and settled. */
export function ready() {
  return !!(stage && stage.box && stage.box.isConnected && stage.box.classList.contains("iv-plain"));
}

/** A view the picked faces are drawn in. Returns its detach (call it before the view is disposed). */
export function attachView(view) {
  views.add(view);
  for (const f of lit.values()) drawIn(f, view);
  if (lit.size) view.rw.render();
  return () => {
    for (const f of lit.values()) { const a = f.actors.get(view); if (a) { view.ren.removeActor(a); f.actors.delete(view); } }
    views.delete(view);
  };
}

/** The ray through a point on a canvas, from the camera into the scene (the stage's own rule). */
export function rayThrough(host, ren, apiRW, cam, clientX, clientY) {
  const rect = host.getBoundingClientRect(), size = apiRW.getSize();
  const x = (clientX - rect.left) / rect.width, y = 1 - (clientY - rect.top) / rect.height;
  const aspect = size[0] / Math.max(size[1], 1);
  const a = ren.normalizedDisplayToWorld(x, y, 0, aspect), b = ren.normalizedDisplayToWorld(x, y, 1, aspect);
  const dir = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], L = Math.hypot(dir[0], dir[1], dir[2]) || 1;
  const cp = cam.getPosition();
  const back = ((a[0] - cp[0]) * dir[0] + (a[1] - cp[1]) * dir[1] + (a[2] - cp[2]) * dir[2]) / L;
  return { origin: [a[0] - dir[0] / L * back, a[1] - dir[1] / L * back, a[2] - dir[2] / L * back], dir };
}

/** Where a ray meets the part (open_ends' click index): {point, normal} with the normal toward the viewer, or null. */
export function hitOn(index, ray) {
  if (!index || !ray) return null;
  const L = Math.hypot(ray.dir[0], ray.dir[1], ray.dir[2]) || 1, d = ray.dir.map((v) => v / L);
  const h = firstHit(index, ray.origin, d);
  if (!h) return null;
  const n = h.normal || [0, 0, 0];
  const s = n[0] * d[0] + n[1] * d[1] + n[2] * d[2] > 0 ? -1 : 1;
  return { point: h.point.slice(), normal: n.map((v) => s * v) };
}

function chipOf(id) {
  for (const box of ["chat", "notes"]) {
    const it = chips.items(box).find((i) => i.kind === "face" && i.value && i.value.id === id);
    if (it) return { box, key: it.key };
  }
  return null;
}

/** A click at `hit` (the point on the drawn part, metres): the face there is picked, or dropped when it already was.
 *  Resolves to {picked: id} | {dropped: id} | {full: true} | null (no face there, or not ready). */
export async function pickAt(hit) {
  if (!ready() || !hit || asking) return null;
  asking = true;
  let f = null;
  try { f = await faceAt(stage.sessionId, hit.point, hit.normal); }
  catch (e) { console.warn("face pick: the face could not be read", e && e.message); }
  finally { asking = false; }
  if (!f || !f.id || !ready()) return null;
  const had = chipOf(f.id);
  if (had) { chips.remove(had.box, had.key); return { dropped: f.id }; }
  const box = chips.active();
  if (chips.items(box).filter((i) => i.kind === "face").length >= MAX_FACES) { say(box, TOO_MANY_FACES); return { full: true }; }
  const label = String(f.label || f.id).slice(0, 80);
  const key = chips.add(box, { kind: "face", label, title: label, value: { id: f.id, label }, onRemove: (it) => unlight(it.key) });
  light(key, box, f);
  return { picked: f.id };
}

function light(key, box, f) {
  const h = f.highlight || {};
  if (!h.points_b64 || !h.tris_b64) { lit.set(key, { id: f.id, box, pd: null, actors: new Map() }); keepWatch(); return; }
  const pts = b64f32(h.points_b64), tris = b64u32(h.tris_b64), n = tris.length / 3;
  const polys = new Uint32Array(n * 4);
  for (let t = 0; t < n; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = tris[t * 3]; polys[t * 4 + 2] = tris[t * 3 + 1]; polys[t * 4 + 3] = tris[t * 3 + 2]; }
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  pd.getPoints().setData(pts, 3); pd.getPolys().setData(polys);
  const entry = { id: f.id, box, pd, actors: new Map() };
  lit.set(key, entry);
  for (const v of views) { drawIn(entry, v); v.rw.render(); }
  keepWatch();
}

function drawIn(entry, view) {
  if (!entry.pd || entry.actors.has(view)) return;
  const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(entry.pd);
  // drawn on the part's own surface: pulled a little toward the eye so it is never lost in the body
  if (mapper.setRelativeCoincidentTopologyPolygonOffsetParameters) mapper.setRelativeCoincidentTopologyPolygonOffsetParameters(-2, -2);
  if (mapper.setResolveCoincidentTopology) mapper.setResolveCoincidentTopology(1);
  const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper); actor.setPickable(false);
  const pr = actor.getProperty(), c = themeRgb("--accent", ACCENT);
  pr.setColor(c[0], c[1], c[2]); pr.setAmbient(0.45); pr.setDiffuse(0.6); pr.setSpecular(0); pr.setOpacity(0.92);
  view.ren.addActor(actor);
  entry.actors.set(view, actor);
}

function unlight(key) {
  const e = lit.get(key);
  if (!e) return;
  lit.delete(key);
  for (const [view, actor] of e.actors) { view.ren.removeActor(actor); view.rw.render(); }
  if (!lit.size && unwatch) { unwatch(); unwatch = null; }
}

/* A send clears the chip row without a word to the chips (what was sent stays sent): the light follows the row.
 * A refused send keeps the row, so the face stays lit. */
function keepWatch() {
  if (unwatch) return;
  unwatch = chips.listen((box, items) => {
    for (const [key, e] of [...lit]) if (e.box === box && !items.some((i) => i.key === key)) unlight(key);
  });
}

/** The faces picked now (ids), in the order picked. */
export function picked() { return [...lit.values()].map((e) => e.id); }

/** The intake is over (the run started, or another part was drawn): every picked face and its chip goes. */
export function resetFacePick() {
  for (const box of ["chat", "notes"]) chips.items(box).filter((i) => i.kind === "face").forEach((i) => chips.remove(box, i.key));
  for (const key of [...lit.keys()]) unlight(key);
  stage = null; views.clear();
}

/* support/debug hook - the browser tier reads the picked faces and their light */
window._vdbg = window._vdbg || {};
window._vdbg.facepick = { picked, ready, lit: () => [...lit.values()].reduce((n, e) => n + e.actors.size, 0), pickAt };
