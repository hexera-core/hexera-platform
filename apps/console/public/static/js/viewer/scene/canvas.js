// Responsibility: Boot and own one vtk.js render window on one host element - the render window,
// the device pixel ratio, the render scale, the resize policy, the mouse binding, the lights, and
// the release of all of it.
// Owns: the vtkGenericRenderWindow, its renderer, render window and API-specific render window,
// the ResizeObserver, the two host listeners it installs, and the camera-light rig.
// Boundaries: it draws no geometry, reads no payload and knows nothing about a part, a mesh, a
// defect or a job. It hands back the vtk handles and gets out of the way.

/* ONE BOOT FOR EVERY SCENE.
 *
 * Four verbatim copies of this sixteen-line incantation existed - the flow stage, the thermal
 * stage, the delivered-mesh viewer and the review view - and they had drifted in exactly the
 * places a user can see:
 *
 *   THE LIGHTS. The two geometry stages installed a three-camera-light rig; the mesh viewer and
 *   the review view installed nothing and rode vtk's default headlight, which lights a part flat
 *   from dead ahead and loses every surface that faces away. Unifying on the rig is a VISIBLE
 *   change to the delivered mesh and the review view, and it is the intended one: a part should
 *   be lit the same way wherever it is shown.
 *
 *   THE RESIZE POLICY. The geometry stages re-framed on every size change until the user first
 *   moved the camera (`touched`), the review view used "has a defect been pinned yet" as a proxy
 *   for the same idea, and the mesh viewer never re-framed at all - so a mesh whose canvas was
 *   sized a tick after the first fit (the workbench grid always does this) kept the framing it
 *   got against the placeholder box. `touched` is the policy here: it is the one a user would
 *   predict, and it is the only one of the three that is about the user rather than about an
 *   internal flag that happens to correlate.
 *
 * DISPOSE IS THE POINT. No scene has ever released a WebGL context. `openViewer` deliberately
 * HIDES a previous viewer rather than tearing it down, so a session with three runs holds three
 * live contexts against a browser cap of 8-16 and the fourth run gets a blank canvas. `dispose()`
 * exists here so that there is something to call; nothing calls it yet, on purpose - wiring it is
 * a separate change with its own evidence.
 */

import { bindMouse } from "../view_controls.js";

/* A key from the upper left, a fill from the right and a faint rim from behind, all riding with
   the camera, so a turned part is always lit the way a CAD viewer lights it. */
const LIGHT_RIG = [[-0.7, 0.9, 1.0, 0.62], [0.9, 0.25, 0.7, 0.28], [0.0, -0.4, -1.0, 0.14]];

/* A press on an HTML overlay sitting over the canvas is a click on a control, not an orbit, so it
   must not count as the user taking the camera. Callers with more on-canvas furniture than the
   geometry stage pass their own selector. */
const DEFAULT_IGNORE = ".vt,.v-ov";

/**
 * Mount a render window on `host` and hand back the handles plus the things that own themselves.
 *
 * @param {HTMLElement} host    the element the canvas is appended to; it must be positioned.
 * @param {object} opts
 *   - `background`: the scene colour, as vtk's [r,g,b] in 0..1.
 *   - `lights`: `false` to keep vtk's default headlight instead of the three-light rig.
 *   - `mouse`: `false` to skip `bindMouse` (nothing does this today).
 *   - `ignore`: selector for on-canvas HTML whose presses are not camera moves.
 *
 * The returned object carries `fit` and `touched` as plain MUTABLE fields: a caller assigns its
 * own re-frame function to `canvas.fit` once it knows the part's bounds, and may set or clear
 * `canvas.touched` itself when it moves the camera on the user's behalf (the review view places
 * the camera on a defect, which is a deliberate framing that a resize must not undo).
 */
export function mountCanvas(host, opts = {}) {
  const sceneBg = opts.background || [0, 0, 0];
  const grw = vtk.Rendering.Misc.vtkGenericRenderWindow.newInstance({ background: sceneBg });
  grw.setContainer(host);
  const ren = grw.getRenderer(), rw = grw.getRenderWindow();
  const apiRW = grw.getApiSpecificRenderWindow ? grw.getApiSpecificRenderWindow() : grw.getOpenGLRenderWindow();
  const cam = ren.getActiveCamera();
  // 2x is the cap: a 3x phone screen costs nine times the fragments for a difference nobody sees
  const DPR = Math.min(window.devicePixelRatio || 1, 2);

  function setRenderScale() {
    const r = host.getBoundingClientRect();
    apiRW.setSize(Math.max(2, Math.round(r.width * DPR)), Math.max(2, Math.round(r.height * DPR)));
  }
  grw.resize();

  const noMenu = (e) => e.preventDefault();
  host.addEventListener("contextmenu", noMenu);
  if (opts.mouse !== false) bindMouse(grw);   // left-drag turns, right-drag pans, the wheel zooms

  if (opts.lights !== false && vtk.Rendering.Core.vtkLight) {
    LIGHT_RIG.forEach(([x, y, z, i]) => {
      const l = vtk.Rendering.Core.vtkLight.newInstance();
      if (l.setLightTypeToCameraLight) l.setLightTypeToCameraLight();
      l.setPosition(x, y, z); l.setFocalPoint(0, 0, 0); l.setIntensity(i); l.setColor(1, 1, 1);
      ren.addLight(l);
    });
  }

  let disposed = false;

  const canvas = {
    host, grw, ren, rw, apiRW, cam, sceneBg, DPR,
    setRenderScale,
    /** The aspect the overlay projection needs, from the drawing surface rather than the box. */
    aspect: () => { const s = apiRW.getSize(); return s[0] / Math.max(s[1], 1); },
    render: () => { if (!disposed) rw.render(); },
    disposed: () => disposed,
    /** Assigned by the caller: re-frame the part. Called on resize while `touched` is false. */
    fit: null,
    /** True once the user has taken the camera. Callers may set it themselves; see above. */
    touched: false,
    dispose,
  };

  const ignore = opts.ignore || DEFAULT_IGNORE;
  const onTake = (e) => {
    if (!(e.target && e.target.closest && e.target.closest(ignore))) canvas.touched = true;
  };
  // capture: the vtk interactor stops propagation, so a bubble listener here never fires
  host.addEventListener("pointerdown", onTake, true);
  // THE WHEEL TAKES THE CAMERA TOO. Zoom is a camera move a user makes without ever pressing a
  // button, so keying "the user has taken the camera" to pointerdown alone threw a wheel-only
  // close-up away on the next resize - and a resize is not rare here: the conversation drawer
  // toggles one. The same `ignore` keeps a scroll over the toolbar or an overlay out of it,
  // since those scroll their own content and say nothing about the part.
  host.addEventListener("wheel", onTake, { capture: true, passive: true });

  /* THE FRAMING FOLLOWS THE CANVAS until the user takes the camera. The workbench grid gives a
     panel its real size a tick after the scene is appended, so the first fit is always made
     against a placeholder box. */
  const ro = new ResizeObserver(() => {
    if (disposed) return;
    setRenderScale();
    if (!canvas.touched && canvas.fit) canvas.fit(); else rw.render();
  });
  ro.observe(host);
  // exposed because one caller (the review view) already tore its observer down on close and
  // must keep doing so; `dispose()` disconnects it too
  canvas.ro = ro;

  /** Release everything this module took: the observer, the listeners, the interactor, the
   *  actors, the lights and - the one that matters - the WebGL context and its canvas element.
   *  Idempotent; returns true the first time and false after. */
  function dispose() {
    if (disposed) return false;
    disposed = true;
    ro.disconnect();
    host.removeEventListener("contextmenu", noMenu);
    host.removeEventListener("pointerdown", onTake, true);
    host.removeEventListener("wheel", onTake, true);
    // Each step is independently wrapped: a vtk build that is missing one of these must not
    // prevent the later ones, and the context release is the last and most important of them.
    const step = (fn) => { try { fn(); } catch { /* releasing is best-effort by nature */ } };
    step(() => { const it = rw.getInteractor && rw.getInteractor();
      if (it) { if (it.unbindEvents) it.unbindEvents(); if (it.setContainer) it.setContainer(null); } });
    step(() => ren.removeAllViewProps && ren.removeAllViewProps());
    step(() => ren.removeAllLights && ren.removeAllLights());
    // THE CONTEXT. vtk's delete() detaches the canvas but leaves the GL context for the browser
    // to collect whenever it feels like it, and "whenever it feels like it" is what blows the
    // per-page context cap. Ask for it back explicitly.
    step(() => { const gl = apiRW.get3DContext && apiRW.get3DContext();
      const lose = gl && gl.getExtension && gl.getExtension("WEBGL_lose_context");
      if (lose) lose.loseContext(); });
    // delete() FIRST, then sweep: vtk detaches the canvas itself and complains loudly
    // ("canvas parent node does not match container") if it has already been pulled out.
    step(() => grw.delete());
    step(() => { const el = apiRW.getCanvas && apiRW.getCanvas();
      if (el && el.parentNode) el.parentNode.removeChild(el); });
    return true;
  }

  return canvas;
}
