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
const views = new Set();                         // {ren, rw, skin}: where the picked faces are drawn; skin() = the
                                                 // view's own surfaces (vtkPolyData), the light is made of them
const lit = new Map();                           // chip key -> {id, box, hl: {pts, tris}, actors: Map(view -> [actor])}
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
    for (const f of lit.values()) { const a = f.actors.get(view); if (a) { for (const x of a) view.ren.removeActor(x); f.actors.delete(view); } }
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
  const hl = h.points_b64 && h.tris_b64 ? { pts: b64f32(h.points_b64), tris: b64u32(h.tris_b64) } : null;
  const entry = { id: f.id, box, hl, actors: new Map() };
  lit.set(key, entry);
  if (hl) for (const v of views) { drawIn(entry, v); v.rw.render(); }
  keepWatch();
}

/* WHY THE LIGHT IS THE SKIN'S OWN TRIANGLES. The server's face is tessellated apart from the skin (a CAD face's
 * triangles are not the skin's): drawn as they are, the two surfaces cross each other a hair apart, and on a curved
 * face, at a grazing angle, the grey wins in stripes. So the light is made of the skin triangles that lie on the
 * server's face - the very triangles the part is drawn with - drawn after the skin at the same depth, so the depth
 * test lets it win at every angle and never shows it through anything in front of it. */
const ON_FACE = 0.3;      // a skin corner is on the face within this share of the face triangle's shortest edge
const FACING = 0.3;       // ... and the skin triangle faces the same way within ~72 degrees (a wall at 90 is not)

function d2tri(px, py, pz, T, o) {            // squared distance from a point to triangle o of T (9 floats each)
  const ax = T[o], ay = T[o + 1], az = T[o + 2];
  const abx = T[o + 3] - ax, aby = T[o + 4] - ay, abz = T[o + 5] - az, acx = T[o + 6] - ax, acy = T[o + 7] - ay, acz = T[o + 8] - az;
  const apx = px - ax, apy = py - ay, apz = pz - az;
  const d1 = abx * apx + aby * apy + abz * apz, d2 = acx * apx + acy * apy + acz * apz;
  const at = (v, w) => { const x = apx - v * abx - w * acx, y = apy - v * aby - w * acy, z = apz - v * abz - w * acz; return x * x + y * y + z * z; };
  if (d1 <= 0 && d2 <= 0) return at(0, 0);
  const d3 = abx * (apx - abx) + aby * (apy - aby) + abz * (apz - abz), d4 = acx * (apx - abx) + acy * (apy - aby) + acz * (apz - abz);
  if (d3 >= 0 && d4 <= d3) return at(1, 0);
  const vc = d1 * d4 - d3 * d2;
  if (vc <= 0 && d1 >= 0 && d3 <= 0) return at(d1 / (d1 - d3), 0);
  const d5 = abx * (apx - acx) + aby * (apy - acy) + abz * (apz - acz), d6 = acx * (apx - acx) + acy * (apy - acy) + acz * (apz - acz);
  if (d6 >= 0 && d5 <= d6) return at(0, 1);
  const vb = d5 * d2 - d1 * d6;
  if (vb <= 0 && d2 >= 0 && d6 <= 0) return at(0, d2 / (d2 - d6));
  const va = d3 * d6 - d5 * d4;
  if (va <= 0 && d4 - d3 >= 0 && d5 - d6 >= 0) { const w = (d4 - d3) / (d4 - d3 + d5 - d6); return at(1 - w, w); }
  const s = 1 / (va + vb + vc);
  return at(vb * s, vc * s);
}

/** The skin's cells (`skin`: the view's surfaces, vtkPolyData) that lie on the face the server lit (`pts` xyz,
 *  `tris` vertex ids): every corner on it, the cell facing its way. [{skin, polys (VTK cell form)}], or null. */
export function onSkin(skin, pts, tris) {
  const n = tris.length / 3;
  if (!skin || !skin.length || !n) return null;
  const T = new Float64Array(n * 9), N = new Float64Array(n * 3), tol = new Float64Array(n), longest = [];
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let t = 0; t < n; t++) {
    for (let v = 0; v < 3; v++) for (let k = 0; k < 3; k++) {
      const x = pts[tris[t * 3 + v] * 3 + k]; T[t * 9 + v * 3 + k] = x;
      if (x < lo[k]) lo[k] = x; if (x > hi[k]) hi[k] = x;
    }
    const o = t * 9, e = [0, 1, 2].map((v) => { const w = (v + 1) % 3;
      return Math.hypot(T[o + w * 3] - T[o + v * 3], T[o + w * 3 + 1] - T[o + v * 3 + 1], T[o + w * 3 + 2] - T[o + v * 3 + 2]); });
    tol[t] = ON_FACE * Math.min(...e); longest.push(Math.max(...e));
    const ux = T[o + 3] - T[o], uy = T[o + 4] - T[o + 1], uz = T[o + 5] - T[o + 2], vx = T[o + 6] - T[o], vy = T[o + 7] - T[o + 1], vz = T[o + 8] - T[o + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, L = Math.hypot(nx, ny, nz) || 1;
    N[t * 3] = nx / L; N[t * 3 + 1] = ny / L; N[t * 3 + 2] = nz / L;
  }
  const diag = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) || 1;
  let reach = 0;
  for (let t = 0; t < n; t++) { tol[t] += 1e-6 * diag; reach = Math.max(reach, tol[t]); }
  // the face's triangles in a grid of cells about one triangle across, each in every cell it reaches
  longest.sort((a, b) => a - b);
  const h = Math.max(longest[longest.length >> 1], diag / 64), cells = new Map();
  const cell = (x, k) => Math.floor((x - lo[k] + reach) / h);
  for (let t = 0; t < n; t++) {
    const a = [0, 1, 2].map((k) => cell(Math.min(T[t * 9 + k], T[t * 9 + 3 + k], T[t * 9 + 6 + k]) - tol[t], k));
    const b = [0, 1, 2].map((k) => cell(Math.max(T[t * 9 + k], T[t * 9 + 3 + k], T[t * 9 + 6 + k]) + tol[t], k));
    for (let i = a[0]; i <= b[0]; i++) for (let j = a[1]; j <= b[1]; j++) for (let k = a[2]; k <= b[2]; k++) {
      const key = i + "," + j + "," + k, list = cells.get(key);
      if (list) list.push(t); else cells.set(key, [t]);
    }
  }
  const onFace = (x, y, z) => {
    const list = cells.get(cell(x, 0) + "," + cell(y, 1) + "," + cell(z, 2));
    if (list) for (const t of list) if (d2tri(x, y, z, T, t * 9) <= tol[t] * tol[t]) return t;
    return -1;
  };
  const out = [];
  for (const sp of skin) {
    const S = sp.getPoints().getData(), C = sp.getPolys().getData(), keep = [];
    const inBox = (v) => S[v * 3] >= lo[0] - reach && S[v * 3] <= hi[0] + reach && S[v * 3 + 1] >= lo[1] - reach
      && S[v * 3 + 1] <= hi[1] + reach && S[v * 3 + 2] >= lo[2] - reach && S[v * 3 + 2] <= hi[2] + reach;
    for (let q = 0; q < C.length; q += C[q] + 1) {
      const m = C[q];
      let t = -1, ok = m >= 3;
      for (let j = 1; ok && j <= m; j++) ok = inBox(C[q + j]);
      for (let j = 1; ok && j <= m; j++) { const v = C[q + j], f = onFace(S[v * 3], S[v * 3 + 1], S[v * 3 + 2]); if (f < 0) ok = false; else if (t < 0) t = f; }
      if (!ok) continue;
      const a = C[q + 1] * 3, b = C[q + 2] * 3, c = C[q + 3] * 3;
      const ux = S[b] - S[a], uy = S[b + 1] - S[a + 1], uz = S[b + 2] - S[a + 2], vx = S[c] - S[a], vy = S[c + 1] - S[a + 1], vz = S[c + 2] - S[a + 2];
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, L = Math.hypot(nx, ny, nz);
      if (!L || Math.abs(nx * N[t * 3] + ny * N[t * 3 + 1] + nz * N[t * 3 + 2]) / L < FACING) continue;
      for (let j = 0; j <= m; j++) keep.push(C[q + j]);
    }
    if (keep.length) out.push({ skin: sp, polys: Uint32Array.from(keep) });
  }
  return out.length ? out : null;
}

/** The light as a surface: the skin's own points and normals with only the face's cells (`polys`), so it is shaded
 *  exactly as the part is - or, with no skin, the server's triangles as they came. */
function surfaceOf(part, hl) {
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  if (part) {
    pd.getPoints().setData(part.skin.getPoints().getData(), 3); pd.getPolys().setData(part.polys);
    const nrm = part.skin.getPointData().getNormals();
    if (nrm) pd.getPointData().setNormals(nrm);
    return pd;
  }
  const n = hl.tris.length / 3, polys = new Uint32Array(n * 4);
  for (let t = 0; t < n; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = hl.tris[t * 3]; polys[t * 4 + 2] = hl.tris[t * 3 + 1]; polys[t * 4 + 3] = hl.tris[t * 3 + 2]; }
  pd.getPoints().setData(hl.pts, 3); pd.getPolys().setData(polys);
  return pd;
}

function drawIn(entry, view) {
  if (!entry.hl || entry.actors.has(view)) return;
  let parts = null;
  try { parts = view.skin ? onSkin(view.skin(), entry.hl.pts, entry.hl.tris) : null; }
  catch (e) { console.warn("face pick: the skin could not be read", e && e.message); }
  // no skin to match (or nothing on it matched): the server's own triangles, as they are
  const actors = (parts || [null]).map((part) => lightActor(surfaceOf(part, entry.hl)));
  for (const a of actors) view.ren.addActor(a);
  entry.actors.set(view, actors);
}

function lightActor(pd) {
  const mapper = vtk.Rendering.Core.vtkMapper.newInstance(); mapper.setInputData(pd);
  // the skin's own triangles, drawn after the skin, at its very depth sample for sample: the depth test (vtk's
  // "less or equal") lets the light win everywhere on the face and nowhere else. No depth offset at all: vtk makes
  // one by writing the pixel's depth from the shader, one value for the whole pixel, which at a grazing angle loses
  // to the skin's own per-sample depths (a dim 25% red); a slope-scaled one pokes through what is in front where a
  // wall is seen edge-on. vtk's default polygon factor 2 is taken back to 0 here.
  if (mapper.setRelativeCoincidentTopologyPolygonOffsetParameters) mapper.setRelativeCoincidentTopologyPolygonOffsetParameters(-2, 0);
  if (mapper.setResolveCoincidentTopology) mapper.setResolveCoincidentTopology(1);
  const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper); actor.setPickable(false);
  const pr = actor.getProperty(), c = themeRgb("--accent", ACCENT);
  // mostly unlit: the accent holds at 80% from every angle (a wall turned from the light is still clearly the
  // picked red, never a dim maroon), with a little lit shading on top so the shape still reads
  pr.setColor(c[0], c[1], c[2]); pr.setAmbient(0.8); pr.setDiffuse(0.25); pr.setSpecular(0); pr.setOpacity(1);  return actor;
}

function unlight(key) {
  const e = lit.get(key);
  if (!e) return;
  lit.delete(key);
  for (const [view, actors] of e.actors) { for (const a of actors) view.ren.removeActor(a); view.rw.render(); }
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
