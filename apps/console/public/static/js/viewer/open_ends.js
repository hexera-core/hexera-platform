// Responsibility: Say which hole a click on the stage means, and where a click lands on the part.
// The holes are the check's own (cad/open_ends on the server: the one definition the upload's
// stickers use too); this module only reads them against a ray through the clicked pixel.
// Owns: the ray cast over the skin's triangles, the choice of hole a ray means, and the circle
// three clicked rim points make when no hole was found.
// Boundaries: pure geometry over arrays: no DOM, no vtk, no storage, and nothing about what the
// part is. It finds no holes of its own.

const NEAR = 0.05;                               // how far from a hole a click still means it, in part sizes

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
function unit(a) { const L = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / L, a[1] / L, a[2] / L]; }
function frame(n) {
  const u = unit(cross(n, Math.abs(n[2]) < 0.9 ? [0, 0, 1] : [0, 1, 0]));
  return [u, cross(n, u)];
}

/** What the clicks are read against: the skin's triangles (for the ray cast) and the check's
 *  holes, each in its own plane. `patches` are the stage's skin patches (`pts`, `polys` in VTK's
 *  cell form); `holes` are the proposal's (`centroid_m`, `normal`, `loop_m`, `outer_m`, ...). */
export function clickIndex(patches, holes) {
  const tris = [];
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const p of patches || []) {
    const pts = p.pts, polys = p.polys || [];
    for (let i = 0; i < pts.length; i++) { const k = i % 3; if (pts[i] < lo[k]) lo[k] = pts[i]; if (pts[i] > hi[k]) hi[k] = pts[i]; }
    for (let q = 0; q < polys.length;) {
      const n = polys[q];
      for (let j = 1; j + 1 < n; j++) {
        for (const v of [polys[q + 1], polys[q + 1 + j], polys[q + 2 + j]]) tris.push(pts[v * 3], pts[v * 3 + 1], pts[v * 3 + 2]);
      }
      q += n + 1;
    }
  }
  const diag = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) || 1;
  return { tris: Float64Array.from(tris), diag, holes: (holes || []).map(prepare).filter(Boolean) };
}

function prepare(h) {
  const c = h.centroid_m, n = h.normal, loop = h.loop_m;
  if (!c || !n || !loop || loop.length < 3) return null;
  const [u, v] = frame(n);
  const flat = (pts) => pts.map((p) => { const r = sub(p, c); return [dot(r, u), dot(r, v)]; });
  const d = (h.diameter_mm || 0) / 1000;
  return { h, c, n, u, v, d, loop, outer: h.outer_m || null, loop2: flat(loop), outer2: h.outer_m ? flat(h.outer_m) : null };
}

/** Which hole a ray through the clicked pixel means. `ray` is {origin, dir}. Returns:
 *   - `hit`: the first surface the ray meets ({t, point, normal}), or null;
 *   - `through`: the hole whose plane the ray crosses inside its loop - into the hole, or on a
 *     thick wall's end face around it - before it meets anything else; the first along the ray;
 *   - `near`: else the hole closest to the hit point, within a twentieth of the part's size,
 *     whose mouth does not face away from the viewer.
 *  Both holes are the check's own objects, as the proposal served them. */
export function snapAt(index, ray) {
  const o = ray.origin, d = unit(ray.dir);
  const hit = firstHit(index, o, d);
  const tHit = hit ? hit.t : Infinity;
  let through = null, bestT = Infinity;
  for (const e of index.holes) {
    const den = dot(e.n, d);
    if (Math.abs(den) < 1e-6) continue;
    const t = dot(e.n, sub(e.c, o)) / den;
    // crossed in front of the camera and no later than the clicked surface: a click on the end
    // face crosses its plane at the hit, a click into the hole before it
    if (!(t > 0) || t > tHit + 2.1e-3 * index.diag / Math.max(Math.abs(den), 0.2)) continue;
    const p = sub([o[0] + t * d[0], o[1] + t * d[1], o[2] + t * d[2]], e.c);
    const x = dot(p, e.u), y = dot(p, e.v), poly = e.outer2 || e.loop2;
    if (!inside(poly, x, y) && edgeDist(poly, x, y) > 0.05 * e.d) continue;
    if (t < bestT) { through = e; bestT = t; }
  }
  let near = null;
  if (hit && !through) {
    let best = NEAR * index.diag;
    for (const e of index.holes) {
      if (dot(e.n, d) > 0.5) continue;               // a mouth facing away is not the one in view
      const dd = Math.min(loopDist(e.loop, hit.point), e.outer ? loopDist(e.outer, hit.point) : Infinity);
      if (dd < best) { best = dd; near = e; }
    }
  }
  return { hit, through: through && through.h, near: near && near.h };
}

/** The first triangle the ray meets (either side), as {t, point, normal}. */
export function firstHit(index, o, d) {
  const T = index.tris, n = T.length / 9;
  let bestT = Infinity, best = -1;
  for (let f = 0; f < n; f++) {
    const a = f * 9;
    const e1x = T[a + 3] - T[a], e1y = T[a + 4] - T[a + 1], e1z = T[a + 5] - T[a + 2];
    const e2x = T[a + 6] - T[a], e2y = T[a + 7] - T[a + 1], e2z = T[a + 8] - T[a + 2];
    const px = d[1] * e2z - d[2] * e2y, py = d[2] * e2x - d[0] * e2z, pz = d[0] * e2y - d[1] * e2x;
    const det = e1x * px + e1y * py + e1z * pz;
    if (det > -1e-18 && det < 1e-18) continue;
    const inv = 1 / det, sx = o[0] - T[a], sy = o[1] - T[a + 1], sz = o[2] - T[a + 2];
    const u = (sx * px + sy * py + sz * pz) * inv;
    if (u < 0 || u > 1) continue;
    const qx = sy * e1z - sz * e1y, qy = sz * e1x - sx * e1z, qz = sx * e1y - sy * e1x;
    const v = (d[0] * qx + d[1] * qy + d[2] * qz) * inv;
    if (v < 0 || u + v > 1) continue;
    const t = (e2x * qx + e2y * qy + e2z * qz) * inv;
    if (t > 1e-12 && t < bestT) { bestT = t; best = f; }
  }
  if (best < 0) return null;
  const a = best * 9;
  const nrm = unit(cross([T[a + 3] - T[a], T[a + 4] - T[a + 1], T[a + 5] - T[a + 2]], [T[a + 6] - T[a], T[a + 7] - T[a + 1], T[a + 8] - T[a + 2]]));
  return { t: bestT, point: [o[0] + bestT * d[0], o[1] + bestT * d[1], o[2] + bestT * d[2]], normal: nrm };
}

/** The circle through three points on a hole's rim: its centre, diameter and the normal of the
 *  three points' plane, turned toward `viewer` (the side the user clicked from: out of the part).
 *  Null when the three points lie on one line. */
export function circleThrough(p1, p2, p3, viewer) {
  const a = sub(p2, p1), b = sub(p3, p1), n = cross(a, b), n2 = dot(n, n);
  if (!(n2 > 1e-12 * Math.max(dot(a, a), dot(b, b)) ** 2)) return null;
  // the circumcentre: p1 + (|a|^2 (b x n) + |b|^2 (n x a)) / (2 |n|^2)
  const t1 = cross(b, n), t2 = cross(n, a), aa = dot(a, a), bb = dot(b, b);
  const c = [0, 1, 2].map((k) => p1[k] + (aa * t1[k] + bb * t2[k]) / (2 * n2));
  let nn = unit(n);
  if (viewer && dot(nn, sub(viewer, c)) < 0) nn = [-nn[0], -nn[1], -nn[2]];
  return { centre: c, diameter: 2 * Math.hypot(...sub(p1, c)), normal: nn };
}

function inside(poly, x, y) {
  let inn = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inn = !inn;
  }
  return inn;
}

function edgeDist(poly, x, y) {
  let best = Infinity;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [ax, ay] = poly[j], [bx, by] = poly[i], dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
    const t = L2 > 0 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / L2)) : 0;
    best = Math.min(best, Math.hypot(ax + t * dx - x, ay + t * dy - y));
  }
  return best;
}

/** The distance from a point to a closed loop of 3D points. */
export function loopDist(pts, p) {
  let best = Infinity;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const a = pts[j], ab = sub(pts[i], a), L2 = dot(ab, ab);
    const t = L2 > 0 ? Math.max(0, Math.min(1, dot(sub(p, a), ab) / L2)) : 0;
    best = Math.min(best, Math.hypot(a[0] + t * ab[0] - p[0], a[1] + t * ab[1] - p[1], a[2] + t * ab[2] - p[2]));
  }
  return best;
}
