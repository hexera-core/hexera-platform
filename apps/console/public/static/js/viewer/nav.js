// Responsibility: How every 3D view here is moved and framed - the camera controller (orbit, pan, zoom,
// damping, zoom toward the cursor, touch), the animated view changes, the ViewCube, the bottom toolbar,
// the section plane, the light scene (background and contact shadow) and the lighter drawing while moving.
// Boundaries: it moves a camera and draws its own controls; it knows nothing about meshes, stickers or
// heatmaps. A view hands it what it needs (the renderer, its surfaces, how to frame) and keeps the rest.

import { upVector } from "./view_controls.js";

/* THE SCENE: a soft light grey, the way a CAD viewer shows a part */
export const SCENE_BG = [0.925, 0.925, 0.93];
/* A SELECTED PART: a light blue, see-through, so its holes and inner edges show */
export const SELECT_TINT = [0.62, 0.74, 0.98];
export const SELECT_OPACITY = 0.55;

const ANIM_MS = 750;
const ORBIT_DEG_PER_PX = 0.35;
const ZOOM_STEP = 1.0018;            // per wheel delta unit
const INERTIA_DECAY_MS = 140;        // how fast a released turn slows down
const IDLE_MS = 160;                 // still for this long: draw full detail again

/* ---------------------------------------------------------------- vector maths ---- */
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = (a) => Math.hypot(a[0], a[1], a[2]);
const unit = (a) => { const l = len(a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
/** rotate v about the unit axis k by angle a (Rodrigues) */
function rot(v, k, a) {
  const c = Math.cos(a), s = Math.sin(a), kv = cross(k, v), kd = dot(k, v);
  return [v[0] * c + kv[0] * s + k[0] * kd * (1 - c), v[1] * c + kv[1] * s + k[1] * kd * (1 - c),
          v[2] * c + kv[2] * s + k[2] * kd * (1 - c)];
}
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/* quaternions [x,y,z,w] for the eased turn between two views */
function quatOf(r, u, b) {                       // columns r, u, b of a rotation matrix
  const m00 = r[0], m01 = u[0], m02 = b[0], m10 = r[1], m11 = u[1], m12 = b[1], m20 = r[2], m21 = u[2], m22 = b[2];
  const tr = m00 + m11 + m22;
  let x, y, z, w;
  if (tr > 0) { const s = Math.sqrt(tr + 1) * 2; w = s / 4; x = (m21 - m12) / s; y = (m02 - m20) / s; z = (m10 - m01) / s; }
  else if (m00 > m11 && m00 > m22) { const s = Math.sqrt(1 + m00 - m11 - m22) * 2; w = (m21 - m12) / s; x = s / 4; y = (m01 + m10) / s; z = (m02 + m20) / s; }
  else if (m11 > m22) { const s = Math.sqrt(1 + m11 - m00 - m22) * 2; w = (m02 - m20) / s; x = (m01 + m10) / s; y = s / 4; z = (m12 + m21) / s; }
  else { const s = Math.sqrt(1 + m22 - m00 - m11) * 2; w = (m10 - m01) / s; x = (m02 + m20) / s; y = (m12 + m21) / s; z = s / 4; }
  return [x, y, z, w];
}
function slerp(a, b, t) {
  let d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
  if (d < 0) { b = b.map((v) => -v); d = -d; }
  if (d > 0.9995) { const q = a.map((v, i) => v + (b[i] - v) * t); const l = Math.hypot(...q); return q.map((v) => v / l); }
  const th = Math.acos(d), s = Math.sin(th), wa = Math.sin((1 - t) * th) / s, wb = Math.sin(t * th) / s;
  return a.map((v, i) => v * wa + b[i] * wb);
}
function quatCols(q) {
  const [x, y, z, w] = q;
  return [[1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)],
          [2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w)],
          [2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]];
}

/** The camera as plain numbers. */
export function viewOf(cam) {
  return { focal: cam.getFocalPoint().slice(), position: cam.getPosition().slice(), up: cam.getViewUp().slice() };
}

/* ------------------------------------------------------------------ the cube ---- */
/* Faces in the z-up frame: normal n, the face's right a and its down d (d = a x n keeps the labels
   readable from outside). An up axis other than +Z turns all three the same way. */
const FACES = [
  ["TOP", [0, 0, 1], [1, 0, 0]], ["BOTTOM", [0, 0, -1], [1, 0, 0]],
  ["FRONT", [0, -1, 0], [1, 0, 0]], ["BACK", [0, 1, 0], [-1, 0, 0]],
  ["RIGHT", [1, 0, 0], [0, 1, 0]], ["LEFT", [-1, 0, 0], [0, -1, 0]],
];
const TURNS = {
  "+z": ([x, y, z]) => [x, y, z], "-z": ([x, y, z]) => [x, -y, -z], "+y": ([x, y, z]) => [x, z, -y],
  "-y": ([x, y, z]) => [x, -z, y], "+x": ([x, y, z]) => [z, y, -x], "-x": ([x, y, z]) => [-z, y, x],
};
const keyOf = (v) => v.map((x) => Math.round(x)).join(",");
const m3d = (cols, t) => `matrix3d(${cols[0][0]},${cols[0][1]},${cols[0][2]},0,${cols[1][0]},${cols[1][1]},${cols[1][2]},0,`
  + `${cols[2][0]},${cols[2][1]},${cols[2][2]},0,${t[0]},${t[1]},${t[2]},1)`;

const ICON = {
  home: '<path d="M4 11.5 12 5l8 6.5"/><path d="M6.5 10v8.5h11V10"/><path d="M10.5 18.5v-4.5h3v4.5"/>',
  fit: '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/><circle cx="12" cy="12" r="1.6"/>',
  pan: '<path d="M12 3v18M3 12h18"/><path d="M9.5 5.5 12 3l2.5 2.5M9.5 18.5 12 21l2.5-2.5M5.5 9.5 3 12l2.5 2.5M18.5 9.5 21 12l-2.5 2.5"/>',
  zoom: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 5 5"/>',
  orbit: '<path d="M19 12a7 7 0 1 1-2.1-5"/><path d="M17.5 3.5 17 7l-3.5-.5"/>',
  section: '<path d="M4 8.5 12 4l8 4.5v7L12 20l-8-4.5z"/><path d="M4 8.5l8 4.5 8-4.5M12 13v7"/><path d="M2.5 13.5h19" stroke-dasharray="2 2"/>',
};
const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICON[name]}</svg>`;

/**
 * Attach the navigation to a vtk.js view.
 *
 * o: { host, grw, ren, rw, cam, apiRW, dpr,
 *      upAxis: () => "+z" | ...,           which axis of the part points up
 *      frame: () => void,                  frame the part INSTANTLY, keeping the direction
 *      home: () => void,                   the opening view, INSTANTLY
 *      surfaces: () => [vtkActor],         what the section cuts and the shadow sits under
 *      afterMove: () => void,              the view's own clamp, after every move
 *      onMotion: (moving) => void,         lighter drawing while the camera moves, full when still
 *      fitButton, toolbar, cube, shadow, section: booleans / element }
 */
export function attachNav(o) {
  const { host, ren, rw, cam } = o;
  const inter = o.grw && o.grw.getInteractor ? o.grw.getInteractor() : null;
  // the controller below owns the pointer: vtk's own style would turn the camera a second time
  if (inter && inter.setInteractorStyle) { try { inter.setInteractorStyle(null); } catch { /* older vtk */ } }
  host.classList.add("nav-host");
  host.style.touchAction = "none";

  /* ------------------------------------------------------------ drawing on demand ---- */
  let dirty = false, raf = 0, moving = false, idleTimer = 0, alive = true;
  const listeners = [];
  function after() { if (o.afterMove) o.afterMove(); ren.resetCameraClippingRange(); if (groundRen) groundRen.resetCameraClippingRange(); }
  function motion() {
    if (!moving) { moving = true; if (o.onMotion) o.onMotion(true); }
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { moving = false; if (o.onMotion) o.onMotion(false); draw(); }, IDLE_MS);
  }
  function draw() { dirty = true; if (!raf) raf = requestAnimationFrame(frame); }
  function frame(t) {
    raf = 0;
    if (!alive) return;
    let more = false;
    if (anim) more = stepAnim(t) || more;
    if (spin) more = stepSpin(t) || more;
    if (zoomLeft) more = stepZoom() || more;
    if (dirty) { dirty = false; after(); rw.render(); syncCube(); }
    if (more) { motion(); raf = requestAnimationFrame(frame); }
  }

  /* ------------------------------------------------------------------ the camera ---- */
  const up = () => upVector(o.upAxis ? o.upAxis() : "+z");
  function basis() {
    const f = cam.getFocalPoint(), p = cam.getPosition();
    const b = unit(sub(p, f)), u0 = cam.getViewUp(), r = unit(cross(u0, b)), u = cross(b, r);
    return { f, p, b, u, r, d: len(sub(p, f)) };
  }
  function place(f, b, u, d) {
    cam.setFocalPoint(f[0], f[1], f[2]);
    cam.setPosition(f[0] + b[0] * d, f[1] + b[1] * d, f[2] + b[2] * d);
    cam.setViewUp(u[0], u[1], u[2]);
  }
  /* ORBIT: a turntable about the part's up axis, then a tilt about the screen's right - the part
     never rolls, and it stops a hair short of looking straight down so it cannot flip over */
  function orbit(dxDeg, dyDeg) {
    const { f, b, u, d } = basis(), U = up();
    let nb = rot(b, U, -dxDeg * Math.PI / 180), nu = rot(u, U, -dxDeg * Math.PI / 180);
    const r = unit(cross(nu, nb));
    const tilt = -dyDeg * Math.PI / 180;
    const tb = rot(nb, r, tilt), tu = rot(nu, r, tilt);
    if (Math.abs(dot(tb, U)) < 0.9995 || Math.abs(dot(tb, U)) < Math.abs(dot(nb, U))) { nb = tb; nu = tu; }
    place(f, unit(nb), unit(nu), d);
    draw();
  }
  function pan(dxPx, dyPx) {
    const { f, b, u, r, d } = basis();
    const h = host.clientHeight || 1, k = 2 * d * Math.tan(cam.getViewAngle() * Math.PI / 360) / h;
    const off = add(mul(r, -dxPx * k), mul(u, dyPx * k));
    place(add(f, off), b, u, d);
    draw();
  }
  /* ZOOM TOWARD THE CURSOR: the point under the pointer stays under it */
  function zoomAt(factor, cx, cy) {
    const { f, b, u, r, d } = basis();
    const rect = host.getBoundingClientRect();
    const nx = rect.width ? ((cx - rect.left) / rect.width) * 2 - 1 : 0;
    const ny = rect.height ? 1 - ((cy - rect.top) / rect.height) * 2 : 0;
    const hh = d * Math.tan(cam.getViewAngle() * Math.PI / 360), hw = hh * (rect.width / Math.max(rect.height, 1));
    const P = add(f, add(mul(r, nx * hw), mul(u, ny * hh)));
    const nf = add(P, mul(sub(f, P), 1 / factor));
    place(nf, b, u, d / factor);
    draw();
  }

  /* ------------------------------------------------------- animated view changes ---- */
  let anim = null;
  /** Turn and glide to a view {focal, position, up} over about three quarters of a second. */
  function animateTo(to, ms = ANIM_MS) {
    stopSpin();
    const from = basis();
    const tb = unit(sub(to.position, to.focal)), tr = unit(cross(to.up, tb)), tu = cross(tb, tr);
    const td = len(sub(to.position, to.focal));
    const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    anim = { t0: performance.now(), ms: calm ? 1 : ms, qa: quatOf(from.r, from.u, from.b), qb: quatOf(tr, tu, tb),
             fa: from.f, fb: to.focal, da: from.d, db: td };
    return new Promise((res) => { anim.done = res; draw(); });
  }
  function stepAnim(t) {
    const a = anim, k = Math.min(1, (t - a.t0) / a.ms), e = ease(k);
    const c = quatCols(slerp(a.qa, a.qb, e));
    const d = a.da * Math.pow(a.db / a.da, e);
    place(lerp(a.fa, a.fb, e), unit(c[2]), unit(c[1]), d);
    dirty = true;
    if (k >= 1) { anim = null; a.done(); return false; }
    return true;
  }
  function stopAnim() { if (anim) { const a = anim; anim = null; a.done(); } }
  /** The view `frame` would give, without showing the jump. */
  function framed(fn) {
    const was = viewOf(cam);
    fn();
    const to = viewOf(cam);
    place(was.focal, unit(sub(was.position, was.focal)), was.up, len(sub(was.position, was.focal)));
    return to;
  }
  const fit = () => animateTo(framed(o.frame));
  let homeView = null;
  function setHome() { homeView = framed(o.home); }
  const home = () => { if (!homeView) setHome(); return animateTo(homeView); };
  /** Look along `back` (from the focal point toward the camera) with `upv` up, framed. */
  function lookFrom(back, upv) {
    const was = viewOf(cam);
    const to = framed(() => { place(was.focal, unit(back), unit(upv), len(sub(was.position, was.focal))); o.frame(); });
    return animateTo(to);
  }

  /* -------------------------------------------------------------- the pointer ---- */
  let tool = "orbit";
  const pts = new Map();
  let drag = null, spin = null, pinch = null, zoomLeft = 0, zoomAtXY = [0, 0];
  const hist = [];
  function stopSpin() { spin = null; }
  function stepSpin(t) {
    const dt = Math.min(48, t - (spin.t || t)); spin.t = t;
    spin.vx *= Math.exp(-dt / INERTIA_DECAY_MS); spin.vy *= Math.exp(-dt / INERTIA_DECAY_MS);
    if (Math.abs(spin.vx) + Math.abs(spin.vy) < 0.002) { spin = null; return false; }
    orbit(spin.vx * dt, spin.vy * dt);
    return true;
  }
  function stepZoom() {
    const part = zoomLeft * 0.28;
    zoomAt(Math.pow(ZOOM_STEP, -part), zoomAtXY[0], zoomAtXY[1]);
    zoomLeft -= part;
    if (Math.abs(zoomLeft) < 0.5) { zoomLeft = 0; return false; }
    return true;
  }
  const onControls = (e) => !!(e.target && e.target.closest && e.target.closest(".nav-ui,.v-tools,.v-legend,.v-probe,.gc-pin,.v-flag,.fea-spot"));
  function down(e) {
    if (onControls(e)) return;
    stopAnim(); stopSpin(); zoomLeft = 0;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    try { host.setPointerCapture(e.pointerId); } catch { /* fine */ }
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), m: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] };
      drag = null; return;
    }
    const mode = e.button === 2 || e.button === 1 || e.shiftKey ? "pan" : tool;
    drag = { mode, x: e.clientX, y: e.clientY };
    hist.length = 0; hist.push([performance.now(), 0, 0]);
  }
  function move(e) {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (pinch && pts.size >= 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]), m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      if (pinch.d > 0 && d > 0) zoomAt(d / pinch.d, m[0], m[1]);
      pan(m[0] - pinch.m[0], m[1] - pinch.m[1]);
      pinch = { d, m }; motion(); return;
    }
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY;
    if (!dx && !dy) return;
    if (drag.mode === "orbit") {
      orbit(dx * ORBIT_DEG_PER_PX, dy * ORBIT_DEG_PER_PX);
      hist.push([performance.now(), dx, dy]); if (hist.length > 6) hist.shift();
    } else if (drag.mode === "pan") pan(dx, dy);
    else if (drag.mode === "zoom") { const r = host.getBoundingClientRect(); zoomAt(Math.pow(1.006, -dy), r.left + r.width / 2, r.top + r.height / 2); }
    motion();
  }
  function up_(e) {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (drag && drag.mode === "orbit" && pts.size === 0) {
      // A RELEASED TURN CARRIES ON and slows down, as a weighted turntable does
      const now = performance.now(), recent = hist.filter((h) => now - h[0] < 80);
      if (recent.length >= 2) {
        const span = Math.max(16, now - recent[0][0]);
        const vx = recent.reduce((s, h) => s + h[1], 0) * ORBIT_DEG_PER_PX / span;
        const vy = recent.reduce((s, h) => s + h[2], 0) * ORBIT_DEG_PER_PX / span;
        if (Math.abs(vx) + Math.abs(vy) > 0.02) { spin = { vx, vy, t: 0 }; draw(); }
      }
    }
    if (pts.size === 0) drag = null;
  }
  function wheel(e) {
    if (onControls(e)) return;
    e.preventDefault();
    stopAnim(); stopSpin();
    const k = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1;
    zoomLeft += Math.max(-400, Math.min(400, e.deltaY * k));
    zoomAtXY = [e.clientX, e.clientY];
    draw();
  }
  const on = (el, t, fn, opts) => { el.addEventListener(t, fn, opts); listeners.push([el, t, fn, opts]); };
  on(host, "pointerdown", down);
  on(host, "pointermove", move);
  on(host, "pointerup", up_);
  on(host, "pointercancel", up_);
  on(host, "wheel", wheel, { passive: false });
  on(host, "contextmenu", (e) => e.preventDefault());
  // a camera moved by anyone else (a fly to a hot spot, the Up selector) still turns the cube
  const sub1 = cam.onModified ? cam.onModified(() => { if (!dirty) scheduleCube(); }) : null;

  /* -------------------------------------------------------------------- the cube ---- */
  let cubeEl = null, cubeBody = null, arrowsEl = null, cubeRaf = 0;
  const cells = [];
  function scheduleCube() { if (!cubeRaf) cubeRaf = requestAnimationFrame(() => { cubeRaf = 0; syncCube(); }); }
  function buildCube() {
    cubeEl = document.createElement("div");
    cubeEl.className = "nav-ui nav-cube";
    cubeEl.innerHTML = `<button type="button" class="nav-home" title="Home view" aria-label="Home view">${svg("home")}</button>
      <div class="nav-cube-view"><div class="nav-cube-body"></div></div>
      <div class="nav-arrows" hidden>
        <button type="button" class="nav-arr up" data-step="up" aria-label="Turn to the face above"></button>
        <button type="button" class="nav-arr down" data-step="down" aria-label="Turn to the face below"></button>
        <button type="button" class="nav-arr left" data-step="left" aria-label="Turn to the face on the left"></button>
        <button type="button" class="nav-arr right" data-step="right" aria-label="Turn to the face on the right"></button>
        <button type="button" class="nav-roll ccw" data-roll="-1" aria-label="Roll the view 90 degrees anticlockwise"><svg viewBox="0 0 24 24"><path d="M19 13a7 7 0 0 0-12.5-4.3"/><path d="M5 4.5v4.5h4.5"/></svg></button>
        <button type="button" class="nav-roll cw" data-roll="1" aria-label="Roll the view 90 degrees clockwise"><svg viewBox="0 0 24 24"><path d="M5 13a7 7 0 0 1 12.5-4.3"/><path d="M19 4.5v4.5h-4.5"/></svg></button>
      </div>`;
    cubeBody = cubeEl.querySelector(".nav-cube-body");
    arrowsEl = cubeEl.querySelector(".nav-arrows");
    cubeEl.querySelector(".nav-home").onclick = () => home();
    arrowsEl.querySelectorAll("[data-step]").forEach((b) => { b.onclick = () => step(b.dataset.step); });
    arrowsEl.querySelectorAll("[data-roll]").forEach((b) => { b.onclick = () => roll(Number(b.dataset.roll)); });
    host.appendChild(cubeEl);
    buildFaces();
  }
  function buildFaces() {
    cubeBody.innerHTML = ""; cells.length = 0;
    const T = TURNS[o.upAxis ? o.upAxis() : "+z"] || TURNS["+z"];
    FACES.forEach(([label, n0, a0]) => {
      const n = T(n0), a = T(a0), d = cross(a, n);
      const face = document.createElement("div");
      face.className = "nav-face"; face.dataset.face = label;
      face.style.transform = m3d([a, d, n], mul(n, 30));
      for (let j = -1; j <= 1; j++) for (let i = -1; i <= 1; i++) {
        const dir = add(n, add(mul(a, i), mul(d, j)));
        const c = document.createElement("div");
        c.className = "nav-cell" + (i || j ? (i && j ? " corner" : " edge") : " centre");
        c.dataset.dir = keyOf(dir);
        c.addEventListener("pointerenter", () => hover(c.dataset.dir));
        c.addEventListener("pointerleave", () => hover(null));
        c.addEventListener("click", () => goDir(dir));
        face.appendChild(c); cells.push(c);
      }
      const t = document.createElement("span"); t.className = "nav-label"; t.textContent = label;
      face.appendChild(t);
      cubeBody.appendChild(face);
    });
    syncCube();
  }
  function hover(k) { cells.forEach((c) => c.classList.toggle("hot", !!k && c.dataset.dir === k)); }
  function syncCube() {
    if (!cubeBody) return;
    const { r, u, b } = basis();
    // world -> screen: right, down (CSS y), toward the viewer
    const cols = [[r[0], -u[0], b[0]], [r[1], -u[1], b[1]], [r[2], -u[2], b[2]]];
    cubeBody.style.transform = m3d(cols, [0, 0, 0]);
    const T = TURNS[o.upAxis ? o.upAxis() : "+z"] || TURNS["+z"];
    const facing = FACES.some(([, n0]) => dot(T(n0), b) > 0.9995);
    if (arrowsEl) arrowsEl.hidden = !facing;
  }
  const axisSnap = (v) => { const a = v.map(Math.abs), i = a.indexOf(Math.max(...a)); const s = [0, 0, 0]; s[i] = Math.sign(v[i]) || 1; return s; };
  /** Look from a cube face, edge or corner: the part's up stays up, unless looking along it -
   *  then the screen's up is the horizontal axis nearest the one shown now, so the turn is short. */
  function goDir(dir) {
    const back = unit(dir), U = up();
    let upv;
    if (Math.abs(dot(back, U)) < 0.999) upv = sub(U, mul(back, dot(U, back)));
    else {
      // straight down (or up): the TOP label reads upright, the front of the part at the bottom
      const T = TURNS[o.upAxis ? o.upAxis() : "+z"] || TURNS["+z"];
      upv = T(dot(back, U) > 0 ? [0, 1, 0] : [0, -1, 0]);
    }
    return lookFrom(back, upv);
  }
  function step(where) {
    const { b, u, r } = basis();
    const B = axisSnap(b), Uu = axisSnap(u), R = axisSnap(r);
    const to = { right: [R, Uu], left: [mul(R, -1), Uu], up: [Uu, mul(B, -1)], down: [mul(Uu, -1), B] }[where];
    return lookFrom(to[0], to[1]);
  }
  /* a clockwise roll brings what was on the left of the screen to the top */
  function roll(sign) {
    const { b, r } = basis();
    return lookFrom(axisSnap(b), axisSnap(sign > 0 ? mul(r, -1) : r));
  }

  /* ------------------------------------------------------------------ the toolbar ---- */
  let barEl = null, secEl = null;
  function buildBar() {
    barEl = document.createElement("div");
    barEl.className = "nav-ui nav-bar";
    barEl.setAttribute("role", "toolbar"); barEl.setAttribute("aria-label", "View");
    const btn = (id, label, extra = "") => `<button type="button" class="nav-btn" data-nav="${id}" ${extra}>${svg(id)}<span>${label}</span></button>`;
    barEl.innerHTML = btn("home", "Home") + '<span class="nav-fit-slot"></span>'
      + btn("pan", "Pan", 'aria-pressed="false"') + btn("zoom", "Zoom", 'aria-pressed="false"')
      + btn("orbit", "Orbit", 'aria-pressed="true"')
      + (o.section !== false ? btn("section", "Section", 'aria-pressed="false"') : "");
    // THE FIT BUTTON IS THE VIEW'S OWN, moved here: its id and its listeners stay as they were
    const slot = barEl.querySelector(".nav-fit-slot");
    let fb = o.fitButton;
    if (!fb) { fb = document.createElement("button"); fb.type = "button"; }
    fb.className = "nav-btn"; fb.dataset.nav = "fit"; fb.removeAttribute("title");
    fb.innerHTML = `${svg("fit")}<span>Fit</span>`; fb.setAttribute("aria-label", "Fit the part to the view");
    fb.onclick = () => fit();
    slot.replaceWith(fb);
    barEl.querySelector('[data-nav="home"]').onclick = () => home();
    ["pan", "zoom", "orbit"].forEach((t) => { barEl.querySelector(`[data-nav="${t}"]`).onclick = () => setTool(t); });
    const sb = barEl.querySelector('[data-nav="section"]');
    if (sb) sb.onclick = () => setSection(!section);
    host.appendChild(barEl);
  }
  function setTool(t) {
    tool = t;
    if (barEl) ["pan", "zoom", "orbit"].forEach((k) => {
      const b = barEl.querySelector(`[data-nav="${k}"]`); if (b) b.setAttribute("aria-pressed", String(k === t)); });
  }

  /* ------------------------------------------------------------------ the section ---- */
  let section = false, plane = null, secAxis = 0, secAt = 0.5, secFlip = false;
  function bounds() {
    const b = [Infinity, -Infinity, Infinity, -Infinity, Infinity, -Infinity];
    (o.surfaces ? o.surfaces() : []).forEach((a) => { if (!a.getVisibility()) return; const q = a.getBounds();
      for (let i = 0; i < 6; i += 2) { b[i] = Math.min(b[i], q[i]); b[i + 1] = Math.max(b[i + 1], q[i + 1]); } });
    return b[0] <= b[1] ? b : null;
  }
  function applySection() {
    const acts = o.surfaces ? o.surfaces() : [];
    acts.forEach((a) => { const m = a.getMapper(); if (m && m.removeAllClippingPlanes) m.removeAllClippingPlanes(); });
    if (section && plane) acts.forEach((a) => { const m = a.getMapper(); if (m && m.addClippingPlane) m.addClippingPlane(plane); });
    rw.render();
  }
  function placePlane() {
    const b = bounds(); if (!b || !plane) return;
    const n = [0, 0, 0]; n[secAxis] = secFlip ? 1 : -1;
    const org = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2];
    org[secAxis] = b[secAxis * 2] + (b[secAxis * 2 + 1] - b[secAxis * 2]) * secAt;
    plane.setNormal(...n); plane.setOrigin(...org);
  }
  function setSection(onNow) {
    section = !!onNow;
    if (section && !plane && window.vtk && vtk.Common.DataModel.vtkPlane) plane = vtk.Common.DataModel.vtkPlane.newInstance();
    if (section) {
      // the cut faces the viewer: along the axis the camera looks most nearly down
      const bb = basis().b, a = bb.map(Math.abs);
      secAxis = a.indexOf(Math.max(...a)); secFlip = bb[secAxis] < 0; secAt = 0.5;
    }
    if (barEl) { const sb = barEl.querySelector('[data-nav="section"]'); if (sb) sb.setAttribute("aria-pressed", String(section)); }
    if (!secEl && section) {
      secEl = document.createElement("div");
      secEl.className = "nav-ui nav-section";
      secEl.innerHTML = `<div class="nav-seg" role="group" aria-label="Cut across">${["X", "Y", "Z"].map((x, i) =>
        `<button type="button" data-ax="${i}">${x}</button>`).join("")}</div>
        <input type="range" min="0" max="1000" value="500" aria-label="Where the cut is">
        <button type="button" class="nav-flip" aria-label="Show the other side">Flip</button>`;
      secEl.querySelectorAll("[data-ax]").forEach((b) => { b.onclick = () => { secAxis = Number(b.dataset.ax); secAt = 0.5; syncSection(); }; });
      const rng = secEl.querySelector("input");
      rng.oninput = () => { secAt = Number(rng.value) / 1000; placePlane(); rw.render(); };
      secEl.querySelector(".nav-flip").onclick = () => { secFlip = !secFlip; placePlane(); rw.render(); };
      host.appendChild(secEl);
    }
    if (secEl) secEl.hidden = !section;
    syncSection();
  }
  function syncSection() {
    if (secEl) {
      secEl.querySelectorAll("[data-ax]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.ax) === secAxis)));
      secEl.querySelector("input").value = String(Math.round(secAt * 1000));
    }
    placePlane(); applySection();
  }

  /* ------------------------------------------------------------- the contact shadow ---- */
  /* THE GROUND IS ITS OWN LAYER: a renderer drawn first, behind the part, sharing its camera. It
     never widens a fit or the clipping range, and the part always sits on top of it. */
  let shadow = null, groundRen = null, shadowAt = null;
  function ground() {
    if (groundRen || !window.vtk) return groundRen;
    groundRen = vtk.Rendering.Core.vtkRenderer.newInstance({ background: o.background || SCENE_BG });
    groundRen.setActiveCamera(cam);
    if (groundRen.setInteractive) groundRen.setInteractive(false);
    rw.removeRenderer(ren); rw.addRenderer(groundRen); rw.addRenderer(ren);
    if (rw.setNumberOfLayers) rw.setNumberOfLayers(2);
    groundRen.setLayer(0); ren.setLayer(1);
    if (ren.setPreserveColorBuffer) ren.setPreserveColorBuffer(true);
    return groundRen;
  }
  function buildShadow() {
    if (shadow) { groundRen.removeActor(shadow); shadow = null; }
    const b = bounds(); if (!b || !window.vtk) return;
    const U = up(), ax = U.map(Math.abs).indexOf(1), lo = U[ax] > 0 ? b[ax * 2] : b[ax * 2 + 1];
    const others = [0, 1, 2].filter((i) => i !== ax);
    const c = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2];
    const span = Math.hypot(b[1] - b[0], b[3] - b[2], b[5] - b[4]) || 1;
    c[ax] = lo - Math.sign(U[ax]) * span * 0.002;
    const ex = others.map((i) => (b[i * 2 + 1] - b[i * 2]) / 2 * 1.55 + span * 0.05);
    const RINGS = 18, SEG = 64, pts = [], col = [], polys = [];
    for (let i = 0; i <= RINGS; i++) {
      const t = i / RINGS, f = Math.min(1, Math.max(0, (1 - t) / 0.5)), alpha = Math.round(78 * f * f * (3 - 2 * f));
      for (let j = 0; j < SEG; j++) {
        const a = 2 * Math.PI * j / SEG, p = c.slice();
        p[others[0]] += Math.cos(a) * ex[0] * t; p[others[1]] += Math.sin(a) * ex[1] * t;
        pts.push(...p); col.push(28, 30, 36, alpha);
      }
    }
    for (let i = 0; i < RINGS; i++) for (let j = 0; j < SEG; j++) {
      const a = i * SEG + j, bq = i * SEG + (j + 1) % SEG;
      polys.push(4, a, bq, bq + SEG, a + SEG);
    }
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from(pts), 3);
    pd.getPolys().setData(Uint32Array.from(polys));
    pd.getPointData().setScalars(vtk.Common.Core.vtkDataArray.newInstance({ numberOfComponents: 4, values: Uint8Array.from(col), name: "shadow" }));
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    if (mp.setColorModeToDirectScalars) mp.setColorModeToDirectScalars();
    if (mp.setScalarModeToUsePointData) mp.setScalarModeToUsePointData();
    shadow = vtk.Rendering.Core.vtkActor.newInstance(); shadow.setMapper(mp); shadow.setPickable(false);
    const pr = shadow.getProperty(); pr.setLighting(false); pr.setOpacity(0.999);
    if (pr.setBackfaceCulling) pr.setBackfaceCulling(false);
    shadowAt = { centre: c, axis: ax, sign: Math.sign(U[ax]) };
    ground().addActor(shadow);
    groundRen.resetCameraClippingRange();
  }
  function syncShadow() {
    if (!shadow) return;
    const s = shadowAt, p = cam.getPosition();
    // seen from below, the ground would hide the part's underside
    shadow.setVisibility((p[s.axis] - s.centre[s.axis]) * s.sign > 0);
  }

  /* ------------------------------------------------------------------------ build ---- */
  if (o.toolbar !== false) buildBar();
  if (o.cube !== false) buildCube();
  if (o.shadow) buildShadow();
  const sub2 = cam.onModified ? cam.onModified(syncShadow) : null;
  syncShadow();

  return {
    animateTo, fit, home, setHome, lookFrom, goDir, step, roll, setTool, tool: () => tool,
    setSection, section: () => section, orbit, pan, zoomAt, draw,
    /** the part's up axis changed: the cube's labels and the shadow follow */
    rebuild() { if (cubeBody) buildFaces(); if (o.shadow) buildShadow(); syncShadow(); setHome(); },
    refreshShadow() { if (o.shadow) { buildShadow(); syncShadow(); } },
    animating: () => !!anim || !!spin || !!zoomLeft,
    destroy() {
      alive = false; clearTimeout(idleTimer); if (raf) cancelAnimationFrame(raf);
      listeners.forEach(([el, t, fn, opts]) => el.removeEventListener(t, fn, opts));
      [sub1, sub2].forEach((s) => s && s.unsubscribe && s.unsubscribe());
      [cubeEl, barEl, secEl].forEach((el) => el && el.remove());
      if (groundRen) { groundRen.removeAllViewProps && groundRen.removeAllViewProps(); }
    },
  };
}
