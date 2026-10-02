// Responsibility: Find a part's open ends from its triangles alone, and say which one a click on
// the stage means - so "Add an opening" lands on the mouth the user pointed at, with its real
// centre, size and direction, even where the mouth is a hole with no surface to click.
// Owns: the open-end index of one skin (welded vertices, edges, open loops, flat rings) and the
// choice of the one a ray through the clicked pixel means.
// Boundaries: pure geometry over arrays: no DOM, no vtk, no storage, and nothing about what the
// part is. An open end is a loop of edges that belong to one triangle only (a thin wall's open
// end) or the hole of a flat ring (a thick wall's end face); nothing else is guessed.

/* WHAT AN OPEN END IS, in triangles:
 *  - a RIM: the edges used by one triangle only, chained into a closed loop - where a thin wall
 *    simply stops;
 *  - a RING: an edge-connected set of triangles in one plane, with their normals agreeing, bounded
 *    by two loops - the end face of a thick wall. Its HOLE, the inner loop, is the opening (the
 *    measuring step reads rings the same way).
 * Each is placed at its loop's area centroid, sized by its area (the equivalent diameter, and the
 * width and height of a loop that is not round), and faces out of the part: away from the wall the
 * loop is the edge of. */

const PAIR_COS = Math.cos(3 * Math.PI / 180);   // two neighbouring triangles of one flat face
const FLAT_COS = Math.cos(10 * Math.PI / 180);  // every triangle of a flat ring against the ring
const FLAT_TOL = 2e-3;                           // a flat ring's thickness, in part sizes
const MIN_BORE = 0.1;                            // a ring's hole against its outer area (the scout's)
const MIN_LOOP = 2e-3;                           // a loop narrower than this, in part sizes, is a flaw
const NEAR = 0.05;                               // how far from an open end a click still means it

const f32 = new Float32Array(3), u32 = new Uint32Array(f32.buffer);
function nextPow2(n) { let s = 1; while (s < n) s *= 2; return s; }
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
function unit(a) { const L = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / L, a[1] / L, a[2] / L]; }

/** The open ends of a skin. `patches` are the stage's skin patches - `pts` (x, y, z, ...) and
 *  `polys` in VTK's cell form ([n, i0, ..., n-1], ...) - in any units; everything returned is in
 *  the same units. Vertices the skin split to keep its shading crisp are welded back by position. */
export function openEnds(patches) {
  const { vx, tri } = weld(patches || []);
  const nv = vx.length / 3, nf = tri.length / 3;
  let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < nv; i++) for (let k = 0; k < 3; k++) {
    const v = vx[i * 3 + k]; if (v < lo[k]) lo[k] = v; if (v > hi[k]) hi[k] = v; }
  const diag = nv ? Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) || 1 : 1;
  const centre = nv ? [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2] : [0, 0, 0];

  // the triangles' unit normals (from their winding), areas and centres
  const fn = new Float64Array(nf * 3), fa = new Float64Array(nf), fc = new Float64Array(nf * 3);
  for (let f = 0; f < nf; f++) {
    const a = tri[f * 3] * 3, b = tri[f * 3 + 1] * 3, c = tri[f * 3 + 2] * 3;
    const ux = vx[b] - vx[a], uy = vx[b + 1] - vx[a + 1], uz = vx[b + 2] - vx[a + 2];
    const wx = vx[c] - vx[a], wy = vx[c + 1] - vx[a + 1], wz = vx[c + 2] - vx[a + 2];
    const nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
    const L = Math.hypot(nx, ny, nz);
    fa[f] = L / 2;
    if (L > 0) { fn[f * 3] = nx / L; fn[f * 3 + 1] = ny / L; fn[f * 3 + 2] = nz / L; }
    for (let k = 0; k < 3; k++) fc[f * 3 + k] = (vx[a + k] + vx[b + k] + vx[c + k]) / 3;
  }
  const E = edges(tri, nv);
  const ends = { vx, tri, fn, fa, fc, diag, centre, openings: [] };
  const minD = MIN_LOOP * diag;

  // RIMS: every closed loop of edges that belong to one triangle only
  const open = [];
  for (let e = 0; e < E.count; e++) if (E.cnt[e] === 1) open.push(e);
  for (const loop of loops(open, E)) {
    const m = measure(loop.verts, vx);
    if (!m || m.d < minD) continue;
    // out of the part: away from the wall the loop is the edge of
    const s = sideOf(loop.edges.map((e) => E.f0[e]), m, fa, fc);
    if (s > 0 || (s === 0 && dot(m.n, sub(m.c, centre)) < 0)) flip(m);
    ends.openings.push({ kind: "rim", ...m, outer2: m.poly2, outer3: m.pts3 });
  }

  // RINGS: flat faces bounded by two loops, the hole being the opening
  for (const ring of flatRings(ends, E)) ends.openings.push(ring);
  return ends;
}

/* ------------------------------------------------------------------------------ the click ---- */

/** Which open end a ray through the clicked pixel means. `ray` is {origin, dir} in the skin's
 *  units. Returns:
 *   - `hit`: the first surface the ray meets ({t, point, normal, face}), or null;
 *   - `through`: the open end whose plane the ray crosses inside its loop - into the hole, or on
 *     a ring's own face - before it meets anything else; the first such one along the ray;
 *   - `near`: else the open end closest to the hit point, within a twentieth of the part's size,
 *     whose mouth does not face away from the viewer. */
export function snapAt(ends, ray) {
  const o = ray.origin, d = unit(ray.dir);
  const hit = firstHit(ends, o, d);
  const tHit = hit ? hit.t : Infinity;
  let through = null, bestT = Infinity;
  for (const e of ends.openings) {
    const den = dot(e.n, d);
    if (Math.abs(den) < 1e-6) continue;
    const t = dot(e.n, sub(e.c, o)) / den;
    // the end's plane is crossed in front of the camera and no later than the clicked surface:
    // a click on a ring's own face crosses its plane at the hit, a click into the hole before it
    if (!(t > 0) || t > tHit + (FLAT_TOL + 1e-4) * ends.diag / Math.max(Math.abs(den), 0.2)) continue;
    const p = sub([o[0] + t * d[0], o[1] + t * d[1], o[2] + t * d[2]], e.c);
    const x = dot(p, e.u), y = dot(p, e.v);
    // inside the loop - a ring's outer one, so its own face counts - or just on its edge
    if (!inside(e.outer2, x, y) && edgeDist2(e.outer2, x, y) > (0.05 * e.d) ** 2) continue;
    if (t < bestT - 1e-9 * ends.diag || (Math.abs(t - bestT) <= 1e-9 * ends.diag && e.area < through.area)) {
      through = e; bestT = t;
    }
  }
  let near = null;
  if (hit && !through) {
    let best = NEAR * ends.diag;
    for (const e of ends.openings) {
      if (dot(e.n, d) > 0.5) continue;             // a mouth facing away is not the one in view
      const dd = Math.min(loopDist(e.pts3, hit.point), e.outer3 === e.pts3 ? Infinity : loopDist(e.outer3, hit.point));
      if (dd < best) { best = dd; near = e; }
    }
  }
  return { hit, through, near };
}

/** The first triangle the ray meets (Moller-Trumbore, either side), as {t, point, normal, face}. */
function firstHit(ends, o, d) {
  const { vx, tri, fn } = ends;
  let bestT = Infinity, bestF = -1;
  const nf = tri.length / 3, eps = 1e-12;
  for (let f = 0; f < nf; f++) {
    const a = tri[f * 3] * 3, b = tri[f * 3 + 1] * 3, c = tri[f * 3 + 2] * 3;
    const e1x = vx[b] - vx[a], e1y = vx[b + 1] - vx[a + 1], e1z = vx[b + 2] - vx[a + 2];
    const e2x = vx[c] - vx[a], e2y = vx[c + 1] - vx[a + 1], e2z = vx[c + 2] - vx[a + 2];
    const px = d[1] * e2z - d[2] * e2y, py = d[2] * e2x - d[0] * e2z, pz = d[0] * e2y - d[1] * e2x;
    const det = e1x * px + e1y * py + e1z * pz;
    if (det > -eps && det < eps) continue;
    const inv = 1 / det;
    const sx = o[0] - vx[a], sy = o[1] - vx[a + 1], sz = o[2] - vx[a + 2];
    const u = (sx * px + sy * py + sz * pz) * inv;
    if (u < 0 || u > 1) continue;
    const qx = sy * e1z - sz * e1y, qy = sz * e1x - sx * e1z, qz = sx * e1y - sy * e1x;
    const v = (d[0] * qx + d[1] * qy + d[2] * qz) * inv;
    if (v < 0 || u + v > 1) continue;
    const t = (e2x * qx + e2y * qy + e2z * qz) * inv;
    if (t > 0 && t < bestT) { bestT = t; bestF = f; }
  }
  if (bestF < 0) return null;
  return { t: bestT, face: bestF, point: [o[0] + bestT * d[0], o[1] + bestT * d[1], o[2] + bestT * d[2]],
           normal: [fn[bestF * 3], fn[bestF * 3 + 1], fn[bestF * 3 + 2]] };
}

/* --------------------------------------------------------------------------- the topology ---- */

/** The patches' points welded by position (the skin splits a vertex where shading breaks; the
 *  surface is still joined there) and their polygons fanned into triangles, degenerate ones dropped. */
function weld(patches) {
  let total = 0;
  for (const p of patches) total += Math.floor((p.pts || []).length / 3);
  const size = nextPow2(Math.max(16, total * 2)), mask = size - 1;
  const slot = new Int32Array(size).fill(-1);
  const bits = new Uint32Array(total * 3), coords = new Float64Array(total * 3);
  let nv = 0;
  const tris = [];
  for (const p of patches) {
    const pts = p.pts, n = Math.floor(pts.length / 3), map = new Int32Array(n);
    for (let i = 0; i < n; i++) {
      f32[0] = pts[i * 3]; f32[1] = pts[i * 3 + 1]; f32[2] = pts[i * 3 + 2];
      const b0 = u32[0], b1 = u32[1], b2 = u32[2];
      let h = (Math.imul(b0, 0x8da6b343) ^ Math.imul(b1, 0xd8163841) ^ Math.imul(b2, 0xcb1ab31f)) >>> 0;
      h = (h ^ (h >>> 15)) & mask;
      for (;;) {
        const s = slot[h];
        if (s < 0) {
          slot[h] = nv; bits[nv * 3] = b0; bits[nv * 3 + 1] = b1; bits[nv * 3 + 2] = b2;
          coords[nv * 3] = f32[0]; coords[nv * 3 + 1] = f32[1]; coords[nv * 3 + 2] = f32[2];
          map[i] = nv++; break;
        }
        if (bits[s * 3] === b0 && bits[s * 3 + 1] === b1 && bits[s * 3 + 2] === b2) { map[i] = s; break; }
        h = (h + 1) & mask;
      }
    }
    const polys = p.polys || [];
    for (let q = 0; q < polys.length;) {
      const k = polys[q];
      for (let j = 1; j + 1 < k; j++) {
        const a = map[polys[q + 1]], b = map[polys[q + 1 + j]], c = map[polys[q + 2 + j]];
        if (a !== b && b !== c && a !== c) tris.push(a, b, c);
      }
      q += k + 1;
    }
  }
  return { vx: coords.slice(0, nv * 3), tri: Int32Array.from(tris) };
}

/** Every edge once: its two ends, the first two triangles on it, and how many use it. */
function edges(tri, nv) {
  const nf = tri.length / 3, cap = nf * 3;
  const size = nextPow2(Math.max(16, cap * 2)), mask = size - 1;
  const slot = new Int32Array(size).fill(-1);
  const ea = new Int32Array(cap), eb = new Int32Array(cap), f0 = new Int32Array(cap).fill(-1),
    f1 = new Int32Array(cap).fill(-1), cnt = new Uint16Array(cap);
  let count = 0;
  for (let f = 0; f < nf; f++) for (let k = 0; k < 3; k++) {
    let a = tri[f * 3 + k], b = tri[f * 3 + (k + 1) % 3];
    if (a > b) { const t = a; a = b; b = t; }
    let h = ((Math.imul(a, 0x9e3779b1) ^ Math.imul(b + nv, 0x85ebca6b)) >>> 0);
    h = (h ^ (h >>> 13)) & mask;
    for (;;) {
      const s = slot[h];
      if (s < 0) { slot[h] = count; ea[count] = a; eb[count] = b; f0[count] = f; cnt[count] = 1; count++; break; }
      if (ea[s] === a && eb[s] === b) { if (cnt[s] === 1) f1[s] = f; if (cnt[s] < 65535) cnt[s]++; break; }
      h = (h + 1) & mask;
    }
  }
  return { count, ea, eb, f0, f1, cnt };
}

/** The given edges chained into closed loops, as {verts, edges}; a vertex met by more than two of
 *  them is left through whichever unused edge comes first, as the measuring step walks them. */
function loops(edgeIds, E) {
  const at = new Map();
  for (const e of edgeIds) {
    for (const v of [E.ea[e], E.eb[e]]) { let l = at.get(v); if (!l) at.set(v, l = []); l.push(e); }
  }
  const used = new Set(), out = [];
  for (const start of edgeIds) {
    if (used.has(start)) continue;
    used.add(start);
    const v0 = E.ea[start];
    const verts = [v0], es = [start];
    let cur = E.eb[start], guard = 0;
    while (cur !== v0 && guard++ < 1e6) {
      verts.push(cur);
      const next = (at.get(cur) || []).find((e) => !used.has(e));
      if (next === undefined) break;
      used.add(next); es.push(next);
      cur = E.ea[next] === cur ? E.eb[next] : E.ea[next];
    }
    if (cur === v0 && verts.length >= 3) out.push({ verts, edges: es });
  }
  return out;
}

/* ------------------------------------------------------------------------------ the rings ---- */

/** The flat rings: triangles joined across shared edges where they lie in one plane, kept when
 *  every triangle agrees with the whole and the whole is flat, and bounded by two loops or more.
 *  The hole - the largest loop after the outer one - is the opening, as the measuring step reads
 *  a ring face. */
function flatRings(ends, E) {
  const { vx, fn, fa, fc, diag, centre } = ends;
  const nf = fa.length;
  const parent = new Int32Array(nf);
  for (let f = 0; f < nf; f++) parent[f] = f;
  const find = (x) => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
  const tol = FLAT_TOL * diag;
  for (let e = 0; e < E.count; e++) {
    if (E.cnt[e] !== 2) continue;
    const a = E.f0[e], b = E.f1[e];
    const c = fn[a * 3] * fn[b * 3] + fn[a * 3 + 1] * fn[b * 3 + 1] + fn[a * 3 + 2] * fn[b * 3 + 2];
    if (Math.abs(c) < PAIR_COS) continue;
    const dx = fc[b * 3] - fc[a * 3], dy = fc[b * 3 + 1] - fc[a * 3 + 1], dz = fc[b * 3 + 2] - fc[a * 3 + 2];
    if (Math.abs(fn[a * 3] * dx + fn[a * 3 + 1] * dy + fn[a * 3 + 2] * dz) > tol) continue;
    const ra = find(a), rb = find(b);
    if (ra !== rb) parent[rb] = ra;
  }
  // the regions big enough to hold a hole (an annulus takes six triangles at the least)
  const size = new Int32Array(nf);
  for (let f = 0; f < nf; f++) size[find(f)]++;
  const bounds = new Map();
  const edgeOf = (r, e) => { if (size[r] < 6) return; let l = bounds.get(r); if (!l) bounds.set(r, l = []); l.push(e); };
  for (let e = 0; e < E.count; e++) {
    const ra = find(E.f0[e]), rb = E.f1[e] >= 0 ? find(E.f1[e]) : -1;
    if (E.cnt[e] !== 2 || ra !== rb) { edgeOf(ra, e); if (rb >= 0 && rb !== ra) edgeOf(rb, e); }
  }
  const members = new Map();
  for (const r of bounds.keys()) members.set(r, []);
  for (let f = 0; f < nf; f++) { const l = members.get(find(f)); if (l) l.push(f); }

  const out = [];
  for (const [r, es] of bounds) {
    const faces = members.get(r);
    // the ring's own normal: area-weighted, each triangle turned to agree with the first
    const n0 = [fn[faces[0] * 3], fn[faces[0] * 3 + 1], fn[faces[0] * 3 + 2]];
    let N = [0, 0, 0], area = 0;
    for (const f of faces) {
      const s = (fn[f * 3] * n0[0] + fn[f * 3 + 1] * n0[1] + fn[f * 3 + 2] * n0[2]) < 0 ? -fa[f] : fa[f];
      N[0] += s * fn[f * 3]; N[1] += s * fn[f * 3 + 1]; N[2] += s * fn[f * 3 + 2]; area += fa[f];
    }
    if (!(area > 0)) continue;
    N = unit(N);
    // flat: every triangle agrees with the whole, every corner within the tolerance of its plane
    if (faces.some((f) => Math.abs(fn[f * 3] * N[0] + fn[f * 3 + 1] * N[1] + fn[f * 3 + 2] * N[2]) < FLAT_COS)) continue;
    const ls = loops(es, E);
    if (ls.length < 2) continue;
    const polys = ls.map((l) => ({ l, m: measure(l.verts, vx, N) })).filter((x) => x.m).sort((a, b) => b.m.area - a.m.area);
    if (polys.length < 2) continue;
    const outer = polys[0], hole = polys[1];
    if (hole.m.area < MIN_BORE * outer.m.area || hole.m.d < MIN_LOOP * diag) continue;
    const off = dot(N, hole.m.c);
    let bent = false;
    for (const f of faces) for (let k = 0; k < 3 && !bent; k++) {
      const v = ends.tri[f * 3 + k] * 3;
      if (Math.abs(N[0] * vx[v] + N[1] * vx[v + 1] + N[2] * vx[v + 2] - off) > tol) bent = true;
    }
    if (bent) continue;
    // out of the part: away from the wall the hole's edge leads into (the triangles on the
    // hole's loop that are not the ring's own)
    const walls = [];
    for (const e of hole.l.edges) {
      for (const f of [E.f0[e], E.f1[e]]) if (f >= 0 && find(f) !== r) walls.push(f);
    }
    const m = hole.m;
    const s = sideOf(walls, m, fa, fc);
    if (s > 0 || (s === 0 && dot(m.n, sub(m.c, centre)) < 0)) flip(m);
    // the outer loop in the hole's frame, for a click on the ring's own face
    const outer2 = project(outer.m.pts3, m.c, m.u, m.v);
    out.push({ kind: "ring", ...m, outer2, outer3: outer.m.pts3, ringArea: area });
  }
  return out;
}

/* ---------------------------------------------------------------------------- the loops ---- */

/** A closed loop measured in its own plane: area centroid `c`, unit normal `n` (Newell, or the
 *  given one), in-plane axes `u` and `v`, area, equivalent diameter `d`, the smallest box around it
 *  (`wh`) and the `shape` that box and the area make - the measuring step's rule - with the loop's
 *  points in 3D (`pts3`) and in the plane about `c` (`poly2`). Null for a loop with no area. */
function measure(verts, vx, given) {
  const n = verts.length;
  if (n < 3) return null;
  const P = verts.map((i) => [vx[i * 3], vx[i * 3 + 1], vx[i * 3 + 2]]);
  let nn = [0, 0, 0];
  const mean = [0, 0, 0];
  for (let i = 0; i < n; i++) {
    const p = P[i], q = P[(i + 1) % n];
    nn[0] += (p[1] - q[1]) * (p[2] + q[2]); nn[1] += (p[2] - q[2]) * (p[0] + q[0]); nn[2] += (p[0] - q[0]) * (p[1] + q[1]);
    mean[0] += p[0] / n; mean[1] += p[1] / n; mean[2] += p[2] / n;
  }
  if (!(Math.hypot(nn[0], nn[1], nn[2]) > 0)) return null;
  nn = unit(nn);
  if (given) nn = dot(given, nn) < 0 ? [-given[0], -given[1], -given[2]] : given.slice();
  const u = unit(cross(nn, Math.abs(nn[2]) < 0.9 ? [0, 0, 1] : [0, 1, 0])), v = cross(nn, u);
  const xy = P.map((p) => { const r = sub(p, mean); return [dot(r, u), dot(r, v)]; });
  let a2 = 0, cx = 0, cy = 0;
  for (let i = 0; i < n; i++) {
    const [x0, y0] = xy[i], [x1, y1] = xy[(i + 1) % n], cr = x0 * y1 - x1 * y0;
    a2 += cr; cx += (x0 + x1) * cr; cy += (y0 + y1) * cr;
  }
  if (!(Math.abs(a2) > 0)) return null;
  cx /= 3 * a2; cy /= 3 * a2;
  const area = Math.abs(a2) / 2;
  const c = [mean[0] + cx * u[0] + cy * v[0], mean[1] + cx * u[1] + cy * v[1], mean[2] + cx * u[2] + cy * v[2]];
  const poly2 = new Float64Array(n * 2);
  for (let i = 0; i < n; i++) { poly2[i * 2] = xy[i][0] - cx; poly2[i * 2 + 1] = xy[i][1] - cy; }
  const wh = boxOf(poly2);
  const [w, h] = wh;
  const shape = !(w > 0 && h > 0) ? "other"
    : Math.abs(w - h) <= 0.08 * Math.max(w, h) && Math.abs(area - Math.PI * w * h / 4) <= 0.12 * area ? "circle"
    : Math.abs(area - w * h) <= 0.12 * area ? "rectangle" : "other";
  return { c, n: nn, u, v, area, d: 2 * Math.sqrt(area / Math.PI), wh, shape, pts3: P, poly2 };
}

/** The smallest box around a plane loop, over the directions of its own edges (a rectangle's
 *  box is its sides; a circle's is its diameter whichever way): [long side, short side]. */
function boxOf(poly2) {
  const n = poly2.length / 2, step = Math.max(1, Math.floor(n / 90));
  let best = null;
  for (let i = 0; i < n; i += step) {
    const j = (i + 1) % n;
    let ax = poly2[j * 2] - poly2[i * 2], ay = poly2[j * 2 + 1] - poly2[i * 2 + 1];
    const L = Math.hypot(ax, ay); if (!(L > 0)) continue;
    ax /= L; ay /= L;
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    for (let k = 0; k < n; k++) {
      const x = poly2[k * 2], y = poly2[k * 2 + 1], s = x * ax + y * ay, t = -x * ay + y * ax;
      if (s < a0) a0 = s; if (s > a1) a1 = s; if (t < b0) b0 = t; if (t > b1) b1 = t;
    }
    const w = a1 - a0, h = b1 - b0;
    if (!best || w * h < best[0] * best[1]) best = [w, h];
  }
  if (!best) return [0, 0];
  return best[0] >= best[1] ? best : [best[1], best[0]];
}

/** Which side of a loop's plane the given triangles lie on, area-weighted: positive along `n`. */
function sideOf(faces, m, fa, fc) {
  let s = 0;
  for (const f of faces) s += fa[f] * ((fc[f * 3] - m.c[0]) * m.n[0] + (fc[f * 3 + 1] - m.c[1]) * m.n[1] + (fc[f * 3 + 2] - m.c[2]) * m.n[2]);
  return Math.abs(s) > 1e-18 ? s : 0;
}

/** Turn a measured loop to face the other way, keeping its frame right-handed. */
function flip(m) {
  m.n = [-m.n[0], -m.n[1], -m.n[2]]; m.v = [-m.v[0], -m.v[1], -m.v[2]];
  for (let i = 1; i < m.poly2.length; i += 2) m.poly2[i] = -m.poly2[i];
}

function project(pts3, c, u, v) {
  const out = new Float64Array(pts3.length * 2);
  pts3.forEach((p, i) => { const r = sub(p, c); out[i * 2] = dot(r, u); out[i * 2 + 1] = dot(r, v); });
  return out;
}

/** Whether (x, y) is inside a plane loop (even-odd). */
function inside(poly2, x, y) {
  const n = poly2.length / 2;
  let inn = false;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = poly2[i * 2], yi = poly2[i * 2 + 1], xj = poly2[j * 2], yj = poly2[j * 2 + 1];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inn = !inn;
  }
  return inn;
}

/** The squared distance from (x, y) to a plane loop's edge. */
function edgeDist2(poly2, x, y) {
  const n = poly2.length / 2;
  let best = Infinity;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const ax = poly2[j * 2], ay = poly2[j * 2 + 1], bx = poly2[i * 2], by = poly2[i * 2 + 1];
    const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
    const t = L2 > 0 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / L2)) : 0;
    const ex = ax + t * dx - x, ey = ay + t * dy - y, d2 = ex * ex + ey * ey;
    if (d2 < best) best = d2;
  }
  return best;
}

/** The distance from a point to a closed loop of 3D points. */
function loopDist(pts3, p) {
  let best = Infinity;
  for (let i = 0, j = pts3.length - 1; i < pts3.length; j = i++) {
    const a = pts3[j], b = pts3[i], ab = sub(b, a), L2 = dot(ab, ab);
    const t = L2 > 0 ? Math.max(0, Math.min(1, dot(sub(p, a), ab) / L2)) : 0;
    const q = [a[0] + t * ab[0] - p[0], a[1] + t * ab[1] - p[1], a[2] + t * ab[2] - p[2]];
    const d2 = dot(q, q);
    if (d2 < best) best = d2;
  }
  return Math.sqrt(best);
}
