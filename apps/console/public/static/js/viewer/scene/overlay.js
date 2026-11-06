// Responsibility: Keep HTML elements sitting on top of a 3D canvas glued to points in the scene,
// for every scene, from one animation frame loop.
// Owns: the single requestAnimationFrame loop per canvas, the world-to-pixel projection, the
// on-screen test, and the depth cue that dims an anchor facing away from the camera.
// Boundaries: it creates no elements and styles nothing but `display`, `left`, `top` and the
// `back` class. What an anchor IS - a sticker, a probe, a flag, a defect pin - is the caller's.

/* ONE LOOP, NOT FOUR.
 *
 * Four independent rAF loops projected HTML onto 3D: the flow stage's pins and spots, the thermal
 * stage's probes, the mesh viewer's flags and measure line, and the review view's pin and mate.
 * All four computed the same aspect, the same `worldToNormalizedDisplay`, the same clamp to the
 * unit square with `nd[2]` strictly inside (0,1), and the same `left`/`top` in pixels.
 *
 * THEY ALSO ALL LEAKED. Each stopped only on `!document.body.contains(host)` - and `openViewer`
 * HIDES a previous viewer rather than removing it, so the hidden viewer's loop kept running, kept
 * projecting, and kept the whole scene reachable, forever. An overlay here stops when its host
 * leaves the document OR when its canvas is disposed, and `stop()` ends it outright.
 *
 * THE LOOP IS LAZY. No frame is requested until something is anchored, and the loop ends itself
 * when the last anchor is released, so an overlay with nothing on it costs nothing.
 */

/**
 * @param {object} canvas  what `mountCanvas` returned - `host`, `ren`, `apiRW`, `disposed()`.
 * @returns {object} with `anchor`, `each`, `tick`, `stop`, `running` and `count`.
 */
export function makeOverlay(canvas) {
  const { host, ren, apiRW } = canvas;
  const anchors = new Set();
  const extras = new Set();
  let raf = 0;

  /** Glue `el` to the scene point `at()` returns. `at` may return null to hide the element.
   *
   *  @param {HTMLElement} el
   *  @param {function(): (number[]|null)} at   the world point, each frame.
   *  @param {object} opts
   *    - `hidden()`: truthy to hide the element this frame without projecting it.
   *    - `normal()`: the surface normal at the point. An anchor whose normal faces away from the
   *      camera gets the `back` class - an HTML overlay cannot be occluded by the geometry it
   *      sits on, so without this a far-side marker reads as a near one.
   *  @returns {function(): void} release this anchor.
   */
  function anchor(el, at, opts = {}) {
    const item = { el, at, opts };
    anchors.add(item);
    start();
    return () => { anchors.delete(item); };
  }

  /** Per-frame work that is not a single anchor - an axes gizmo, a two-ended measure line, a
   *  readout that follows the camera. Called with the frame's aspect and host rect.
   *  @returns {function(): void} release. */
  function each(fn) {
    const item = { fn };
    extras.add(item);
    start();
    return () => { extras.delete(item); };
  }

  function place(item, aspect, rect, cp) {
    const { el, opts } = item;
    if (!el) return;
    if (opts.hidden && opts.hidden()) { el.style.display = "none"; return; }
    const p = item.at();
    if (!p || p.length !== 3) { el.style.display = "none"; return; }
    const nd = ren.worldToNormalizedDisplay(p[0], p[1], p[2], aspect);
    const ok = nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1;
    el.style.display = ok ? "" : "none";
    if (!ok) return;
    el.style.left = (nd[0] * rect.width) + "px";
    el.style.top = ((1 - nd[1]) * rect.height) + "px";
    if (opts.normal) {
      const n = opts.normal();
      const facing = n ? (n[0] * (p[0] - cp[0]) + n[1] * (p[1] - cp[1]) + n[2] * (p[2] - cp[2])) < 0 : true;
      el.classList.toggle("back", !facing);
    }
  }

  /** One pass. Exposed so a caller (and a test) can place everything without waiting for a frame -
   *  the scenes all want the overlay correct the instant the camera is moved, not a frame later. */
  function tick() {
    const size = apiRW.getSize(), aspect = size[0] / Math.max(size[1], 1);
    const rect = host.getBoundingClientRect();
    const cp = ren.getActiveCamera().getPosition();
    anchors.forEach((item) => place(item, aspect, rect, cp));
    extras.forEach((item) => item.fn(aspect, rect));
  }

  function frame() {
    raf = 0;
    // the three reasons to stop: the panel went away, the context went away, nothing left to place
    if (!document.body.contains(host) || canvas.disposed()) return;
    if (!anchors.size && !extras.size) return;
    tick();
    start();
  }

  function start() {
    if (raf || canvas.disposed()) return;
    raf = requestAnimationFrame(frame);
  }

  /** End the loop now and forget everything on it. */
  function stop() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    anchors.clear(); extras.clear();
  }

  return { anchor, each, tick, stop,
           running: () => !!raf,
           count: () => anchors.size };
}
