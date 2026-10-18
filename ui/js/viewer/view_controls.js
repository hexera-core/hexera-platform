// Responsibility: What every 3D view here shares about the camera - which mouse button does what,
// and which axis of the part points up (and remembering that choice).
// Boundaries: it moves a camera and reads or writes one remembered choice; it draws nothing and
// knows nothing about meshes, stickers or heatmaps.

/* THE MOUSE. vtk.js's trackball style turns the part on a left-drag, pans on shift+drag and zooms
   on the wheel, but gives the right and middle buttons nothing. Here both pan, as they do in most
   CAD and CFD tools. The interactor is subscribed to directly: the style only listens to the
   buttons it had handlers for when it was attached. */
export function bindMouse(grw) {
  const inter = grw && grw.getInteractor ? grw.getInteractor() : null;
  const style = inter && inter.getInteractorStyle ? inter.getInteractorStyle() : null;
  if (!style || !style.startPan || !inter.onRightButtonPress) return false;
  const start = () => { style.startPan(); };
  const end = () => { style.endPan(); };
  inter.onRightButtonPress(start); inter.onRightButtonRelease(end);
  if (inter.onMiddleButtonPress) { inter.onMiddleButtonPress(start); inter.onMiddleButtonRelease(end); }
  return true;
}

/** Controls laid over the canvas are the page's, not the camera's: a press or a scroll on them
 *  must not reach vtk's listeners on the container, which would start a turn and take the pointer. */
export function shield(el) {
  if (!el) return;
  ["pointerdown", "wheel"].forEach((t) => el.addEventListener(t, (e) => e.stopPropagation(), { passive: true }));
}

/** What the on-screen hint says about the mouse, everywhere it is said. */
export const MOUSE_HINT = "Rotate: drag · Pan: right-drag · Zoom: scroll";

/* WHICH WAY IS UP. CAD is usually drawn z-up, but not always: a car modelled roof-down, or a part
   drawn y-up, opens upside down or on its side. Each choice is a proper rotation taking +Z to that
   axis (never a mirror), so a view direction given for +Z up turns with it and the part is seen
   from the same quarter, the right way up. */
export const UP_AXES = [["+z", "+Z"], ["-z", "-Z"], ["+y", "+Y"], ["-y", "-Y"], ["+x", "+X"], ["-x", "-X"]];
const TURN = {
  "+z": ([x, y, z]) => [x, y, z],
  "-z": ([x, y, z]) => [x, -y, -z],
  "+y": ([x, y, z]) => [x, z, -y],
  "-y": ([x, y, z]) => [x, -z, y],
  "+x": ([x, y, z]) => [z, y, -x],
  "-x": ([x, y, z]) => [-z, y, x],
};
export const isUpAxis = (a) => Object.prototype.hasOwnProperty.call(TURN, a);

/** The world vector that points up the screen for an axis choice. */
export function upVector(axis) { return (TURN[axis] || TURN["+z"])([0, 0, 1]); }

/** Look along `dir` - given for +Z up - turned for `axis` up, keeping the focal point and the
 *  distance. The caller re-frames afterwards. */
export function orient(cam, axis, dir) {
  const t = TURN[axis] || TURN["+z"], d = t(dir), u = t([0, 0, 1]);
  const f = cam.getFocalPoint(), p = cam.getPosition();
  const dist = Math.hypot(p[0] - f[0], p[1] - f[1], p[2] - f[2]) || 1;
  const L = Math.hypot(d[0], d[1], d[2]) || 1;
  cam.setPosition(f[0] + d[0] / L * dist, f[1] + d[1] / L * dist, f[2] + d[2] / L * dist);
  cam.setViewUp(u[0], u[1], u[2]);
}

/* the choice is remembered per job (the mesh viewer) or per session (the geometry check), in
   this browser only; storage that is blocked or full simply means the part opens z-up again */
const key = (scope, id) => `hexera.view-up.${scope}.${id}`;
export function savedUp(scope, id) {
  try { const v = window.localStorage.getItem(key(scope, id)); return isUpAxis(v) ? v : null; }
  catch { return null; }
}
export function saveUp(scope, id, axis) {
  try { window.localStorage.setItem(key(scope, id), axis); } catch { /* not remembered, still applied */ }
}

/** The "Up" selector's markup: a label and a small select. */
export function upSelectHtml(id, axis) {
  const opts = UP_AXES.map(([v, l]) => `<option value="${v}"${v === axis ? " selected" : ""}>${l}</option>`).join("");
  return `<label class="v-up" title="Which axis of the part points up"><span>Up</span>`
    + `<select id="${id}" aria-label="Which axis points up">${opts}</select></label>`;
}
