// Responsibility: Draw the marks a stage puts on a part - openings, hot spots, joins, what is not in the
// shell - as small flat marks of a fixed size on screen, and merge the ones that would overlap at the
// current zoom into one mark with a count, which splits again as the user zooms in.
// Owns: where each mark element sits each frame, which marks are merged, the merged marks' elements,
// the hovered and the chosen mark, and the depth cue on a mark whose surface faces away.
// Boundaries: it decides nothing about what a mark means or whether it should be shown: the caller
// creates each element (its classes, its words, its click) and says which ones are shown now.

/* WHY THIS EXISTS. The stages drew every mark twice: a 3D ball sized to the part, and an HTML pin
 * with a white ring and a drop shadow on top of it. The ball grew with the zoom until it covered
 * the very hole it marked, and on a busy part (a welded frame: 100 hot spots, 36 joins) the pins
 * piled on top of each other. Here a mark is one small flat element of a fixed size on screen, and
 * marks closer together on screen than a few pixels are drawn as one mark with their count.
 *
 * THE WORK IS DONE ONLY WHEN SOMETHING MOVED: the camera, the canvas size, or what is shown
 * (`bump()`). A still camera costs one string compare per frame.
 */

/** Marks closer than this on screen, in CSS pixels, are drawn as one. */
export const MERGE_PX = 18;

/**
 * @param {object} canvas   what `mountCanvas` returned (`host`, `ren`, `cam`, `rw`).
 * @param {object} overlay  what `makeOverlay` returned: the marks are placed from its frame loop.
 * @param {object} opts
 *   - `diag`: the part's diagonal, in the scene's units (a merged mark zooms in by it).
 *   - `shown(id)`: whether a mark is shown now; a hovered or chosen mark is shown regardless.
 * @returns {object} `add`, `remove`, `clear`, `select`, `hover`, `bump`, `layout`, `state`.
 */
export function makeMarks(canvas, overlay, opts = {}) {
  const { host, ren, cam } = canvas;
  const marks = new Map();          // id -> {id, el, at, normal, hidden, order}
  const pool = [];                  // the merged marks' elements, reused frame to frame
  let shown = opts.shown || (() => true);
  let chosen = null, hovered = new Set();
  let version = 0, last = "", order = 0, groups = [];

  /** Put a mark on the part. `m`: {id, el, at: [x,y,z] or a function giving it, normal?: same,
   *  hidden?: () => bool}. The element is the caller's; it is appended to the canvas here. */
  function add(m) {
    remove(m.id);
    const at = typeof m.at === "function" ? m.at : () => m.at;
    const normal = typeof m.normal === "function" ? m.normal : m.normal ? () => m.normal : null;
    m.el.classList.add("vm");
    m.el.style.display = "none";          // placed on the next frame, never drawn at the corner first
    host.appendChild(m.el);
    marks.set(String(m.id), { id: String(m.id), el: m.el, at, normal, hidden: m.hidden || null, order: order++ });
    bump();
  }
  function remove(id) {
    const m = marks.get(String(id));
    if (!m) return;
    m.el.remove(); marks.delete(String(id));
    if (chosen === String(id)) chosen = null;
    bump();
  }
  function clear() { [...marks.keys()].forEach(remove); }
  function bump() { version++; }
  /** The chosen mark (one; null for none): drawn on its own, never merged. */
  function select(id) { chosen = id == null ? null : String(id); bump(); }
  /** The marks pointed at in the panel (an id, a list of them, or null): shown, never merged. */
  function hover(ids) {
    hovered = new Set((ids == null ? [] : Array.isArray(ids) ? ids : [ids]).map(String));
    marks.forEach((m) => m.el.classList.toggle("hot", hovered.has(m.id)));
    bump();
  }
  function setShown(fn) { shown = fn || (() => true); bump(); }

  function chip(i) {
    if (pool[i]) return pool[i];
    const el = document.createElement("div");
    el.className = "vm-group";
    el.addEventListener("click", (ev) => { ev.stopPropagation(); zoomTo(el._members || []); });
    host.appendChild(el);
    pool[i] = el;
    return el;
  }

  /** A merged mark was clicked: the camera moves in on its marks until they come apart. */
  function zoomTo(members) {
    const pts = members.map((m) => m.at()).filter((p) => p && p.length === 3);
    if (!pts.length) return;
    const c = [0, 1, 2].map((k) => pts.reduce((s, p) => s + p[k], 0) / pts.length);
    const f = cam.getFocalPoint(), p = cam.getPosition(), d = [c[0] - f[0], c[1] - f[1], c[2] - f[2]];
    cam.setFocalPoint(c[0], c[1], c[2]);
    cam.setPosition(p[0] + d[0], p[1] + d[1], p[2] + d[2]);
    cam.dolly(2.5);
    ren.resetCameraClippingRange();
    canvas.touched = true;
    canvas.rw.render();
  }

  /** Place every shown mark and merge the ones that would overlap. Called from the frame loop; it
   *  does nothing unless the camera, the canvas or what is shown changed since the last time. */
  function layout(aspect, rect, force) {
    const key = `${cam.getMTime()}|${rect.width}|${rect.height}|${version}`;
    if (!force && key === last) return;
    last = key;
    const cp = cam.getPosition();
    const free = [], alone = [];
    marks.forEach((m) => {
      const own = m.id === chosen || hovered.has(m.id);
      if ((m.hidden && m.hidden()) || !(own || shown(m.id))) { m.el.style.display = "none"; return; }
      const p = m.at();
      if (!p || p.length !== 3) { m.el.style.display = "none"; return; }
      const nd = ren.worldToNormalizedDisplay(p[0], p[1], p[2], aspect);
      if (!(nd[2] > 0 && nd[2] < 1 && nd[0] >= 0 && nd[0] <= 1 && nd[1] >= 0 && nd[1] <= 1)) {
        m.el.style.display = "none"; return;
      }
      const item = { m, x: nd[0] * rect.width, y: (1 - nd[1]) * rect.height, back: false };
      if (m.normal) {
        const n = m.normal();
        item.back = !!n && (n[0] * (p[0] - cp[0]) + n[1] * (p[1] - cp[1]) + n[2] * (p[2] - cp[2])) >= 0;
      }
      (own ? alone : free).push(item);
    });
    // GREEDY, IN THE ORDER THE MARKS CAME: each mark not yet taken gathers every free mark within
    // MERGE_PX of it, found through a grid of that size
    free.sort((a, b) => a.m.order - b.m.order);
    const grid = new Map(), cell = (x, y) => `${Math.floor(x / MERGE_PX)},${Math.floor(y / MERGE_PX)}`;
    free.forEach((it) => { const k = cell(it.x, it.y); if (!grid.has(k)) grid.set(k, []); grid.get(k).push(it); });
    const taken = new Set();
    groups = [];
    for (const it of free) {
      if (taken.has(it)) continue;
      taken.add(it);
      const g = [it], cx = Math.floor(it.x / MERGE_PX), cy = Math.floor(it.y / MERGE_PX);
      for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
        for (const o of grid.get(`${cx + i},${cy + j}`) || []) {
          if (!taken.has(o) && Math.hypot(o.x - it.x, o.y - it.y) < MERGE_PX) { taken.add(o); g.push(o); }
        }
      }
      groups.push(g);
    }
    const put = (it) => {
      const s = it.m.el.style;
      s.display = ""; s.left = it.x + "px"; s.top = it.y + "px";
      it.m.el.classList.toggle("back", it.back);
    };
    alone.forEach(put);
    let used = 0;
    groups.forEach((g) => {
      if (g.length === 1) { put(g[0]); return; }
      g.forEach((it) => { it.m.el.style.display = "none"; });
      const el = chip(used++);
      const x = g.reduce((s, it) => s + it.x, 0) / g.length, y = g.reduce((s, it) => s + it.y, 0) / g.length;
      el._members = g.map((it) => it.m);
      el.textContent = String(g.length);
      el.title = `${g.length} marks here: click to zoom in`;
      // a merged mark holding a problem says so by its edge; one whose marks all face away is dimmed
      el.classList.toggle("warn", g.some((it) => it.m.el.classList.contains("k-problem")));
      el.classList.toggle("back", g.every((it) => it.back));
      el.style.display = ""; el.style.left = x + "px"; el.style.top = y + "px";
    });
    for (let i = used; i < pool.length; i++) pool[i].style.display = "none";
  }

  const off = overlay.each((aspect, rect) => layout(aspect, rect, false));

  return {
    add, remove, clear, select, hover, bump, setShown,
    /** Place everything now (a test, or a caller that cannot wait a frame). */
    layout: () => { overlay.tick(); },
    /** What is drawn: each merged mark's count, and how many marks stand alone. */
    state: () => ({
      marks: marks.size,
      single: [...marks.values()].filter((m) => m.el.style.display !== "none").length,
      merged: groups.filter((g) => g.length > 1).map((g) => g.length),
    }),
    stop: () => { off(); clear(); pool.forEach((el) => el.remove()); pool.length = 0; },
  };
}
