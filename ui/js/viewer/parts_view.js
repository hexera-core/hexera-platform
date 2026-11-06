// Responsibility: The parts checklist's model in 3D - the user's model, turned by dragging and zoomed by
// scrolling, each part (or area inside a part) in the colour of the note row it belongs to, the rest in the
// theme's matte grey; the number tags of the coloured parts, kept on them as they turn and hidden while they are
// behind other parts; a ring round an area too small to see; which part is under the cursor, for hover and click.
// Owns: its vtk.js scene (one actor for the whole model, coloured per triangle), the visibility pass that says
// which part each pixel shows, the tags, the rings and the cursor tag.
// Boundaries: it decides nothing about the notes: the colours, the lit row and the tags arrive through `set`;
// a hover or a click on a part is handed back through the callbacks it was given. It fetches nothing.
import { b64f32, b64u8, loadVtk } from "./scene/vtk_load.js";
import { mountCanvas } from "./scene/canvas.js";
import { makeOverlay } from "./scene/overlay.js";
import { orient } from "./view_controls.js";
import { parseColour, themeRgb } from "./toolbar.js";

const AMBIENT = 0.24, DIFFUSE = 0.74, SPECULAR = 0.06;
const BODY_FALLBACK = [0.49, 0.51, 0.54];
// a row with more tagged parts in view than this gets one tag per cluster of them, not one each
const MANY = 8, CLUSTER_PX = 64;
// an area drawn smaller than this (css pixels across) gets a ring, so it can be found
const SMALL_PX = 9;

const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(String(h).slice(i, i + 2), 16) / 255);
const mix = (a, b, t) => [0, 1, 2].map((i) => a[i] * (1 - t) + b[i] * t);

/** "9–12" for a run, else "6 parts": what one tag says for a cluster of a row's parts. */
function clusterLabel(labels) {
  if (labels.length === 1) return labels[0];
  const nums = labels.map(Number);
  if (nums.every((n) => Number.isInteger(n))) {
    nums.sort((a, b) => a - b);
    if (nums[nums.length - 1] - nums[0] === nums.length - 1) return `${nums[0]}–${nums[nums.length - 1]}`;
  }
  return `${labels.length} parts`;
}

/**
 * Draw the parts model in `host` (positioned, sized by CSS). `payload` is GET .../check/parts/model;
 * `opts`: `upAxis`, `onHover(id|null)`, `onClick(id)`. Resolves to the view, or throws when it cannot be drawn
 * (no WebGL): the caller shows the pictures instead.
 */
export async function mountPartsView(host, payload, opts = {}) {
  await loadVtk();
  const order = payload.order || [];
  const pts = b64f32(payload.points_b64), nrm = b64f32(payload.normals_b64);
  const tris = new Uint32Array(b64u8(payload.tris_b64).buffer);
  const partOf = new Uint16Array(b64u8(payload.part_b64).buffer);
  const edges = payload.edges_b64 ? new Uint32Array(b64u8(payload.edges_b64).buffer) : new Uint32Array(0);
  const nTri = tris.length / 3, K = order.length;
  const index = new Map(order.map((id, k) => [id, k + 1]));      // part id -> its slot (0 is the grey rest)

  // the card's own background behind the model, so it sits on the card as the picture did
  const card = host.closest(".req-card") || host;
  const bg = parseColour(getComputedStyle(card).backgroundColor) || themeRgb("--viewer-canvas", [0.08, 0.08, 0.08]);
  const BODY = themeRgb("--part-body", BODY_FALLBACK);
  const canvas = mountCanvas(host, { background: bg, ignore: ".pv-tags" });
  const { ren, rw, apiRW, cam } = canvas;
  const overlay = makeOverlay(canvas);

  /* THE MODEL: one actor, coloured per triangle */
  const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
  pd.getPoints().setData(pts, 3);
  const polys = new Uint32Array(nTri * 4);
  for (let t = 0; t < nTri; t++) { polys[t * 4] = 3; polys[t * 4 + 1] = tris[t * 3]; polys[t * 4 + 2] = tris[t * 3 + 1]; polys[t * 4 + 3] = tris[t * 3 + 2]; }
  pd.getPolys().setData(polys);
  if (nrm.length === pts.length) pd.getPointData().setNormals(vtk.Common.Core.vtkDataArray.newInstance({ name: "Normals", values: nrm, numberOfComponents: 3 }));
  const rgb = new Uint8Array(nTri * 3);
  const colours = vtk.Common.Core.vtkDataArray.newInstance({ name: "Colours", values: rgb, numberOfComponents: 3 });
  pd.getCellData().setScalars(colours);
  const mapper = vtk.Rendering.Core.vtkMapper.newInstance();
  mapper.setInputData(pd); mapper.setScalarModeToUseCellData(); mapper.setColorModeToDirectScalars(); mapper.setScalarVisibility(true);
  const actor = vtk.Rendering.Core.vtkActor.newInstance(); actor.setMapper(mapper);
  const pr = actor.getProperty();
  pr.setInterpolationToPhong(); pr.setAmbient(AMBIENT); pr.setDiffuse(DIFFUSE); pr.setSpecular(SPECULAR); pr.setSpecularPower(20);
  ren.addActor(actor);
  // the sharp corners and where two parts meet, as quiet hairlines
  let lines = null;
  if (edges.length) {
    const ld = vtk.Common.DataModel.vtkPolyData.newInstance();
    ld.getPoints().setData(pts, 3);
    const cells = new Uint32Array(edges.length / 2 * 3);
    for (let e = 0; e < edges.length / 2; e++) { cells[e * 3] = 2; cells[e * 3 + 1] = edges[e * 2]; cells[e * 3 + 2] = edges[e * 2 + 1]; }
    ld.getLines().setData(cells);
    const lm = vtk.Rendering.Core.vtkMapper.newInstance(); lm.setInputData(ld);
    lines = vtk.Rendering.Core.vtkActor.newInstance(); lines.setMapper(lm); lines.setPickable(false);
    const lp = lines.getProperty(); lp.setColor(...mix(BODY, [0, 0, 0], 0.55)); lp.setLighting(false); lp.setOpacity(0.55);
    ren.addActor(lines);
  }

  /* THE CAMERA: the drawing-office three-quarter view, framed on the model */
  const bs = pd.getBounds();
  const centre = [(bs[0] + bs[1]) / 2, (bs[2] + bs[3]) / 2, (bs[4] + bs[5]) / 2];
  const diag = Math.max(Math.hypot(bs[1] - bs[0], bs[3] - bs[2], bs[5] - bs[4]), 1e-9);
  const upAxis = opts.upAxis || "+z";
  function fit() {
    ren.resetCamera(bs);
    const [w, h] = apiRW.getSize(), a = w / Math.max(h, 1);
    cam.zoom(a < 1 ? a * 1.15 : 1.15);
    ren.resetCameraClippingRange(); rw.render();
  }
  function iso() {
    cam.setFocalPoint(...centre); cam.setPosition(centre[0] - diag, centre[1] - diag, centre[2] + 0.8 * diag); cam.setViewUp(0, 0, 1);
    orient(cam, upAxis, [-1, -1, 0.8]);
    fit();
  }
  canvas.fit = fit;

  /* WHAT THE USER IS LOOKING AT, from the checklist: set() */
  let st = { colour: {}, active: null, activeColour: null, hover: null, tags: [], focusRow: null, picking: false };
  const slotRgb = new Uint8Array((K + 1) * 3);
  function paint() {
    const grey = BODY, faded = mix(BODY, bg, 0.55);
    for (let s = 0; s <= K; s++) {
      const id = s ? order[s - 1] : null;
      let c;
      if (st.active) c = id && st.active.has(id) ? hexRgb(st.activeColour || st.colour[id] || "#E3A23B") : faded;
      else c = id && st.colour[id] ? hexRgb(st.colour[id]) : grey;
      if (id && st.hover && st.hover.has(id)) c = mix(c, [1, 1, 1], 0.28);
      slotRgb[s * 3] = c[0] * 255; slotRgb[s * 3 + 1] = c[1] * 255; slotRgb[s * 3 + 2] = c[2] * 255;
    }
    for (let t = 0; t < nTri; t++) {
      const s = partOf[t] <= K ? partOf[t] : 0;
      rgb[t * 3] = slotRgb[s * 3]; rgb[t * 3 + 1] = slotRgb[s * 3 + 1]; rgb[t * 3 + 2] = slotRgb[s * 3 + 2];
    }
    colours.modified(); pd.modified();
    if (lines) lines.getProperty().setOpacity(st.active ? 0.3 : 0.55);
    rw.render();
  }

  /* WHICH PART EACH PIXEL SHOWS: the hardware selector's id pass, captured when the camera settles (and every
     few frames while it turns). Hover, clicks, the tags' visibility and the rings all read it. */
  const sel = apiRW.getSelector ? apiRW.getSelector() : null;
  let ids = null;                 // {w, h, at(x, y) -> slot or -1} in device pixels, y from the top
  let stale = true, busy = false;
  async function capture() {
    if (!sel || busy || canvas.disposed()) return;
    busy = true;
    try {
      const [w, h] = apiRW.getSize();
      sel.setFieldAssociation(1);          // cells
      const got = await sel.getSourceDataAsync(ren, 0, 0, w - 1, h - 1);
      rw.render();                         // the id passes drew into the canvas: put the picture back
      if (!got) { ids = null; return; }
      const out = [0, 0];
      const cache = new Int32Array(w * h).fill(-2);
      ids = { w, h, at: (x, y) => {
        x = Math.round(x); y = Math.round(y);
        if (x < 0 || y < 0 || x >= w || y >= h) return -1;
        const k = y * w + x;
        if (cache[k] !== -2) return cache[k];
        const info = sel.getPixelInformation([x, h - 1 - y], 0, out);
        const t = info && info.prop === actor && info.attributeID !== undefined ? info.attributeID : -1;
        return (cache[k] = t >= 0 && t < nTri ? partOf[t] : -1);
      } };
      stale = false;
      place();
    } catch (e) { console.warn("parts view: no id pass", e && e.message); ids = null; }
    finally { busy = false; }
  }
  let capT = 0, lastCap = 0;
  function soon() {
    stale = true;
    clearTimeout(capT);
    const now = performance.now();
    // while the model turns the tags are re-checked a few times a second; once it stops, at once
    if (now - lastCap > 240) { lastCap = now; capture(); }
    capT = setTimeout(() => { lastCap = performance.now(); capture(); }, 90);
  }
  // only a real move counts (a clipping-range reset on render is not one)
  let camSig = "";
  cam.onModified(() => {
    const sig = [...cam.getPosition(), ...cam.getFocalPoint(), ...cam.getViewUp(), cam.getParallelScale(), cam.getViewAngle()]
      .map((v) => v.toPrecision(7)).join(",");
    if (sig === camSig) return;
    camSig = sig; soon(); tip.hidden = true;
  });

  /* THE TAGS: the coloured parts' numbers, where each is seen; a part behind others has none. A row of many
     parts gets one tag per cluster of them. Rings round areas too small to see. */
  const layer = document.createElement("div"); layer.className = "pv-tags"; host.appendChild(layer);
  const tip = document.createElement("div"); tip.className = "pv-tip"; tip.hidden = true; host.appendChild(tip);
  const DPR = canvas.DPR;
  const centroid = (t) => { const a = tris[t * 3] * 3, b = tris[t * 3 + 1] * 3, c = tris[t * 3 + 2] * 3;
    return [(pts[a] + pts[b] + pts[c]) / 3, (pts[a + 1] + pts[b + 1] + pts[c + 1]) / 3, (pts[a + 2] + pts[b + 2] + pts[c + 2]) / 3]; };
  // a few triangles of each part to anchor its tag on: the one nearest the middle of what is seen is used
  const triOf = new Map();
  for (let t = 0; t < nTri; t++) { const s = partOf[t]; if (!s) continue; let l = triOf.get(s); if (!l) triOf.set(s, l = []); if (l.length < 4000) l.push(t); }
  const spots = (payload.spots || []).map((s) => ({ slot: index.get(s.part) || 0, c: s.c, r: s.r })).filter((s) => s.slot);
  let shown = [];                 // [{el, at: world point, cls}]
  let own = new Int32Array(K + 1).map((_, s) => s);   // the slot whose number a slot's pixels count for
  function project(p, aspect, rect) {
    const nd = ren.worldToNormalizedDisplay(p[0], p[1], p[2], aspect);
    return [nd[0] * rect.width, (1 - nd[1]) * rect.height, nd[2]];
  }
  function seenSamples() {
    // which tagged slots are seen, and where: a coarse scan of the id pass
    const want = new Set(st.tags.map((t) => index.get(t.part)).filter(Boolean));
    // an area no row names counts for the part it is in
    own = new Int32Array(K + 1).map((_, s) => s);
    Object.entries(st.owner || {}).forEach(([f, p]) => { const a = index.get(f), b = index.get(p); if (a && b) own[a] = b; });
    const got = new Map();
    if (!ids) return got;
    const step = Math.max(2, Math.round(3 * DPR));
    for (let y = 0; y < ids.h; y += step) for (let x = 0; x < ids.w; x += step) {
      const s0 = ids.at(x, y); if (s0 <= 0) continue;
      const s = own[s0]; if (!want.has(s)) continue;
      let g = got.get(s); if (!g) got.set(s, g = { n: 0, sx: 0, sy: 0, pts: [] });
      g.n++; g.sx += x; g.sy += y; if (g.pts.length < 400) g.pts.push([x, y]);
    }
    return got;
  }
  function anchorFor(slot, g, aspect, rect) {
    // the seen pixel nearest the middle of what is seen of the part, then the part's triangle drawn nearest it
    const mx = g.sx / g.n, my = g.sy / g.n;
    let best = g.pts[0], bd = Infinity;
    for (const p of g.pts) { const d = (p[0] - mx) ** 2 + (p[1] - my) ** 2; if (d < bd) { bd = d; best = p; } }
    const tx = best[0] / DPR, ty = best[1] / DPR;
    let bt = -1, bdd = Infinity;
    const cands = [];
    for (let s = 1; s <= K; s++) if (own[s] === slot) cands.push(...(triOf.get(s) || []));
    for (const t of cands) {
      const q = project(centroid(t), aspect, rect); if (q[2] <= 0 || q[2] >= 1) continue;
      const d = (q[0] - tx) ** 2 + (q[1] - ty) ** 2; if (d < bdd) { bdd = d; bt = t; }
    }
    return bt >= 0 ? centroid(bt) : null;
  }
  function spotSeen(sp, aspect, rect) {
    const q = project(sp.c, aspect, rect);
    if (!ids || q[2] <= 0 || q[2] >= 1) return null;
    // its size on screen: the radius at the spot's depth
    const right = cam.getViewUp(), dir = cam.getDirectionOfProjection();
    const rx = [right[1] * dir[2] - right[2] * dir[1], right[2] * dir[0] - right[0] * dir[2], right[0] * dir[1] - right[1] * dir[0]];
    const e = project([sp.c[0] + rx[0] * sp.r, sp.c[1] + rx[1] * sp.r, sp.c[2] + rx[2] * sp.r], aspect, rect);
    const rpx = Math.hypot(e[0] - q[0], e[1] - q[1]);
    const R = Math.max(1.5, rpx * 0.9) * DPR;
    for (let a = 0; a < 9; a++) {
      const x = q[0] * DPR + (a ? Math.cos(a * Math.PI / 4) * R : 0), y = q[1] * DPR + (a ? Math.sin(a * Math.PI / 4) * R : 0);
      if (ids.at(x, y) === sp.slot) return { x: q[0], y: q[1], rpx };
    }
    return null;
  }
  function place() {
    layer.textContent = ""; shown = [];
    if (!ids || stale) return;
    const size = apiRW.getSize(), aspect = size[0] / Math.max(size[1], 1), rect = host.getBoundingClientRect();
    const seen = seenSamples();
    const boxes = [];
    const fits = (x, y, w, h) => {
      const b = [x - w / 2 - 2, y - h / 2 - 2, x + w / 2 + 2, y + h / 2 + 2];
      if (b[0] < 0 || b[1] < 0 || b[2] > rect.width || b[3] > rect.height) return false;
      if (boxes.some((o) => !(b[2] <= o[0] || o[2] <= b[0] || b[3] <= o[1] || o[3] <= b[1]))) return false;
      boxes.push(b); return true;
    };
    const add = (label, colour, at, cls, off) => {
      const q = project(at, aspect, rect), w = 10 + 7 * String(label).length, h = 18;
      const y = q[1] - (off || 0);
      if (!fits(q[0], y, w, h)) return;
      const el = document.createElement("span"); el.className = "pv-tag" + (cls ? " " + cls : "");
      el.style.setProperty("--c", colour); el.textContent = label;
      layer.appendChild(el); shown.push({ el, at, off: off || 0 });
    };
    // the rings first: an area too small to see, seen from here
    const ringed = new Map();
    for (const sp of spots) {
      const id = order[sp.slot - 1], tag = st.tags.find((t) => t.part === id);
      if (!tag) continue;
      const got = spotSeen(sp, aspect, rect);
      if (!got || got.rpx * 2 >= SMALL_PX) continue;
      const el = document.createElement("span");
      el.className = "pv-ring" + (st.focusRow != null && tag.row !== st.focusRow ? " dim" : "");
      el.style.setProperty("--c", tag.colour);
      layer.appendChild(el); shown.push({ el, at: sp.c, off: 0, ring: true });
      if (!ringed.has(sp.slot)) ringed.set(sp.slot, sp.c);
    }
    // the row being looked at first, so its tags win a crowded spot
    const rows = new Map();
    for (const t of st.tags) { let l = rows.get(t.row); if (!l) rows.set(t.row, l = []); l.push(t); }
    const rowOrder = [...rows.keys()].sort((a, b) => (b === st.focusRow) - (a === st.focusRow));
    for (const r of rowOrder) {
      const list = rows.get(r).map((t) => {
        const slot = index.get(t.part), g = seen.get(slot);
        if (!g) return ringed.has(slot) ? { t, at: ringed.get(slot), n: 1, ring: true } : null;
        return { t, g, n: g.n };
      }).filter(Boolean);
      const dim = st.focusRow != null && r !== st.focusRow ? "dim" : "";
      if (list.length > MANY) {
        // ONE TAG PER CLUSTER: the row's seen parts grouped where they sit on the screen
        const groups = [];
        for (const it of list) {
          const x = it.g ? it.g.sx / it.g.n / DPR : project(it.at, aspect, rect)[0], y = it.g ? it.g.sy / it.g.n / DPR : project(it.at, aspect, rect)[1];
          const grp = groups.find((q) => Math.hypot(q.x - x, q.y - y) < CLUSTER_PX);
          if (grp) { grp.items.push(it); } else groups.push({ x, y, items: [it] });
        }
        for (const grp of groups) {
          const big = grp.items.reduce((a, b) => (b.n > a.n ? b : a));
          const at = big.g ? anchorFor(index.get(big.t.part), big.g, aspect, rect) : big.at;
          if (at) add(clusterLabel(grp.items.map((it) => it.t.n)), big.t.colour, at, dim);
        }
        continue;
      }
      list.sort((a, b) => b.n - a.n);
      for (const it of list) {
        const at = it.g ? anchorFor(index.get(it.t.part), it.g, aspect, rect) : it.at;
        if (at) add(it.t.n, it.t.colour, at, dim, it.ring ? 16 : 0);
      }
    }
    follow();
  }
  // THE TAGS FOLLOW THE MODEL every frame; which are shown is re-checked as the camera settles
  function follow() {
    if (!shown.length) return;
    const size = apiRW.getSize(), aspect = size[0] / Math.max(size[1], 1), rect = host.getBoundingClientRect();
    for (const s of shown) {
      const q = project(s.at, aspect, rect);
      s.el.style.transform = `translate(${q[0].toFixed(1)}px,${(q[1] - s.off).toFixed(1)}px)`;
    }
  }
  overlay.each(follow);

  /* THE CURSOR: which part is under it (hover), and a click picks it */
  const slotAt = (cx, cy) => {
    if (!ids || stale) return -1;
    const r = host.getBoundingClientRect();
    return ids.at((cx - r.left) * DPR, (cy - r.top) * DPR);
  };
  let hovered = null, down = null, inside = false;
  host.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; }, true);
  host.addEventListener("pointermove", (e) => {
    inside = true;
    if (e.buttons) return;
    const s = slotAt(e.clientX, e.clientY), id = s > 0 ? order[s - 1] : null;
    if (id !== hovered) { hovered = id; if (opts.onHover) opts.onHover(id); }
    host.style.cursor = id && st.picking ? "pointer" : "";
    if (!id || !opts.label) { tip.hidden = true; return; }
    const r = host.getBoundingClientRect();
    tip.textContent = opts.label(id);
    tip.hidden = false;
    const x = e.clientX - r.left + 12, y = e.clientY - r.top + 14;
    tip.style.transform = `translate(${Math.min(x, r.width - tip.offsetWidth - 4)}px,${Math.min(y, r.height - tip.offsetHeight - 4)}px)`;
  });
  host.addEventListener("pointerleave", () => {
    inside = false; tip.hidden = true; host.style.cursor = "";
    if (hovered) { hovered = null; if (opts.onHover) opts.onHover(null); }
  });
  host.addEventListener("pointerup", (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) { down = null; return; }
    down = null;
    const s = slotAt(e.clientX, e.clientY);
    if (s > 0 && opts.onClick) opts.onClick(order[s - 1]);
  }, true);
  // double-click or F: the whole model in view again, from where the user is looking
  host.addEventListener("dblclick", (e) => { e.preventDefault(); canvas.touched = false; fit(); });
  const onKey = (e) => {
    if (!document.body.contains(host)) { document.removeEventListener("keydown", onKey); return; }
    if (!inside || (e.key !== "f" && e.key !== "F") || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target; if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault(); canvas.touched = false; fit();
  };
  document.addEventListener("keydown", onKey);

  canvas.setRenderScale(); iso(); paint(); soon();

  const view = {
    /** What the checklist shows: `colour` {id: hex} for every part a row means, `active` (a Set) the parts lit
     *  while a row is hovered or open, `activeColour` its colour, `tags` [{part, n, colour, row}], `focusRow`,
     *  `hover` (a part lit by the cursor), `picking` (a row is open: a click picks). */
    set(next) {
      const tagsChanged = JSON.stringify([next.tags, next.focusRow]) !== JSON.stringify([st.tags, st.focusRow]);
      st = { ...st, ...next };
      paint();
      if (tagsChanged) place();
    },
    frame: () => { canvas.touched = false; iso(); },
    dispose: () => { document.removeEventListener("keydown", onKey); overlay.stop(); canvas.dispose(); },
    // the browser tier and the measurements read these
    debug: { cam: () => cam, render: () => rw.render(), slotAt, tags: () => shown.filter((s) => !s.ring).map((s) => s.el.textContent),
      rings: () => shown.filter((s) => s.ring).length, triangles: nTri, capture, ready: () => !!ids && !stale,
      screenOf: (id) => { const g = seenSamples().get(index.get(id)); const r = host.getBoundingClientRect();
        return g ? [r.left + g.pts[Math.floor(g.pts.length / 2)][0] / DPR, r.top + g.pts[Math.floor(g.pts.length / 2)][1] / DPR] : null; } },
  };
  return view;
}
