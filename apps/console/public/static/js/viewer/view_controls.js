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

/** Controls laid over the canvas are the page's, not the camera's: no pointer event on them may
 *  reach vtk's listeners on the container. Stopping the press alone was not enough. vtk reads
 *  the held buttons off every pointermove, and a move with the left button down that it never saw
 *  pressed counts as a fresh press: dragging the Section slider (or across any overlay) turned the
 *  part with the drag (dev-areen 2026-11-13, a Camaro). So the moves, the release and the cancel
 *  stop here too. A press made here and dragged off onto the part is the same drag: until it is
 *  released, its moves outside `el` are held back from the page, so the part does not turn under
 *  a slider the hand overshot. A drag that starts on the part is unaffected: vtk captures the
 *  pointer on the container, so its moves never target an overlay. */
export function shield(el) {
  if (!el) return;
  const stop = (e) => e.stopPropagation();
  ["pointerdown", "pointermove", "pointerup", "pointercancel", "wheel"].forEach(
    (t) => el.addEventListener(t, stop, { passive: true }));
  el.addEventListener("pointerdown", (down) => {
    const id = down.pointerId;
    const hold = (e) => { if (e.pointerId === id && !el.contains(e.target)) e.stopPropagation(); };
    const end = (e) => {
      if (e.pointerId !== id) return;
      window.removeEventListener("pointermove", hold, true);
      window.removeEventListener("pointerup", end, true);
      window.removeEventListener("pointercancel", end, true);
    };
    window.addEventListener("pointermove", hold, { capture: true, passive: true });
    window.addEventListener("pointerup", end, true);
    window.addEventListener("pointercancel", end, true);
  }, { passive: true });
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

/* THE SET VIEWS, each given for +Z up as where the camera sits from the part's centre and which
   way is up on the screen; they turn with the chosen up axis like everything else. Front looks
   along +Y (the camera on the -Y side), Side looks along -X, Top looks straight down with +Y up
   the screen, and Corner is the front-quarter view every viewer opens with. */
export const VIEW_PRESETS = {
  front: { label: "Front", from: [0, -1, 0], up: [0, 0, 1] },
  top: { label: "Top", from: [0, 0, 1], up: [0, 1, 0] },
  side: { label: "Side", from: [1, 0, 0], up: [0, 0, 1] },
  corner: { label: "Corner (3D)", from: [1, -1, 0.7], up: [0, 0, 1] },
};
/** Turn the camera to a set view for an up axis, keeping the focal point and the distance. The
 *  caller re-frames afterwards. */
export function presetView(cam, axis, key) {
  const v = VIEW_PRESETS[key] || VIEW_PRESETS.corner, t = TURN[axis] || TURN["+z"];
  const d = t(v.from), u = t(v.up);
  const f = cam.getFocalPoint(), p = cam.getPosition();
  const dist = Math.hypot(p[0] - f[0], p[1] - f[1], p[2] - f[2]) || 1;
  const L = Math.hypot(d[0], d[1], d[2]) || 1;
  cam.setPosition(f[0] + d[0] / L * dist, f[1] + d[1] / L * dist, f[2] + d[2] / L * dist);
  cam.setViewUp(u[0], u[1], u[2]);
}

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

/* ONE CHOICE PER CONVERSATION, NOT ONE PER RUN. The geometry check saved the way up against its
   session; the delivered-mesh viewer saved it against the job. So a user who stood the CAD part
   +y up got the mesh of that same file back +z up, and setting it again on the mesh did not
   reach the part. The session is the right scope: it is the one thing the part and every mesh cut
   from it share.
   `upScope` answers with the scope a caller should read and write, and migrates a choice saved
   under the old job key into the session once, so nobody loses a preference to this change. The
   old key is left where it is - a run opened by link knows no conversation and still has only
   its job to go on, and that is the scope it gets. */
export function upScope(job, sessionId) {
  if (!sessionId) return { scope: "job", id: job };
  if (job && !savedUp("session", sessionId)) {
    const fromJob = savedUp("job", job);
    if (fromJob) saveUp("session", sessionId, fromJob);
  }
  return { scope: "session", id: sessionId };
}

/** The "Up" selector's markup: a label and a small select. */
export function upSelectHtml(id, axis) {
  const opts = UP_AXES.map(([v, l]) => `<option value="${v}"${v === axis ? " selected" : ""}>${l}</option>`).join("");
  return `<label class="v-up" title="Which axis of the part points up"><span>Up</span>`
    + `<select id="${id}" aria-label="Which axis points up">${opts}</select></label>`;
}
