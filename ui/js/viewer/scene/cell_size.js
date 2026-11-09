// Responsibility: Offer the heatmap one more thing to colour by - how big the cell at each face
// actually is - computed here from the geometry the browser already has.
// Owns: the per-face size array and the metric descriptor it registers.
// Boundaries: it computes and registers. It draws nothing, owns no ramp, no legend and no
// control, and it refuses to register at all rather than register a figure it cannot stand behind.

/* WHY THIS METRIC.
 *
 * The heatmap answers "are the cells good". It does not answer "are the cells where they should
 * be", and on a graded mesh that is the first question anyone asks: a cold plate is meshed at
 * 25 um across its channels and 400 um out in the far field, a 16x span, and whether the fine
 * cells actually landed on the channels is visible in one glance and in no table of maxima.
 *
 * Nothing needs fetching. A delivered face's area is in the points and polys already downloaded,
 * and the square root of that area is the cell's own edge length at the wall.
 *
 * WARM MEANS COARSE, NOT BAD. The other three metrics have a bar: past it, a cell is wrong.
 * Cell size has no bar, so the ramp is spanned over the mesh's own range and the legend carries
 * real lengths. A reader sees "410 um", not a failure. The limit is set to the coarsest cell
 * precisely so that the panel's "Only bad cells" button keeps a useful meaning here - it shows
 * the coarsest end of the mesh - rather than greying out everything.
 *
 * WHEN IT REFUSES. If the surface was REDUCED on the way here, a delivered face is several real
 * faces merged and its area is their sum, so the square root would overstate the cell by the
 * square root of the merge count. That would be a confidently wrong picture, which is worse than
 * no picture. Until the reducer carries a per-face merge count, any sign of reduction in the
 * payload makes this metric decline to register.
 */

/** Per-face area of a vtkPolyData, by fanning each polygon from its first vertex.
 *  Exact for a planar polygon of any vertex count, which is what a boundary face is. */
export function faceAreas(pd) {
  const pts = pd && pd.getPoints && pd.getPoints().getData();
  const polys = pd && pd.getPolys && pd.getPolys().getData();
  if (!pts || !polys || !polys.length) return null;
  const out = [];
  let i = 0;
  while (i < polys.length) {
    const n = polys[i];
    if (n < 3 || i + n >= polys.length + 1) break;
    const o = polys[i + 1] * 3;
    let ax = 0, ay = 0, az = 0;
    for (let k = 2; k < n; k++) {
      const p = polys[i + k] * 3, q = polys[i + k + 1] * 3;
      const ux = pts[p] - pts[o], uy = pts[p + 1] - pts[o + 1], uz = pts[p + 2] - pts[o + 2];
      const vx = pts[q] - pts[o], vy = pts[q + 1] - pts[o + 1], vz = pts[q + 2] - pts[o + 2];
      ax += uy * vz - uz * vy;
      ay += uz * vx - ux * vz;
      az += ux * vy - uy * vx;
    }
    out.push(0.5 * Math.sqrt(ax * ax + ay * ay + az * az));
    i += n + 1;
  }
  return Float32Array.from(out);
}

/** mesh_units to a display unit and the factor onto it. A plate's cells are microns and a
 *  manifold's are millimetres, and neither should be read in scientific notation. */
function displayUnit(units, maxLen) {
  const perMetre = { m: 1, metre: 1, meters: 1, mm: 1e-3, millimetre: 1e-3, cm: 1e-2,
                     um: 1e-6, micron: 1e-6, in: 0.0254, inch: 0.0254 };
  const f = perMetre[String(units || "").toLowerCase()] || 1;   // unknown: treat as metres
  const metres = maxLen * f;
  if (metres < 2e-3) return { unit: "µm", scale: f / 1e-6 };
  if (metres < 2.0) return { unit: "mm", scale: f / 1e-3 };
  return { unit: "m", scale: f };
}

/** Register `cell_size` on the heatmap, or do nothing and say why.
 *  `entries` are the viewer's per-patch records; each needs `.patch`, `.nCells` and an actor
 *  whose mapper holds the polydata. Returns the reason it declined, or null when it registered. */
export function addCellSizeMetric(entries, qf, units, surf) {
  if (!qf || !qf.metrics || !qf.patches || !Array.isArray(entries) || !entries.length) {
    return "no quality_fields to extend";
  }
  // A reduced surface would make this metric lie. See the note above.
  const reduced = surf && (surf.reduced || surf.decimated ||
    (surf.patches || []).some(p => p && (p.reduced_from || p.merge_count_b64 || p.reduced)));
  if (reduced) return "the surface was reduced; a merged face's area is not a cell's area";

  let lo = Infinity, hi = 0, got = 0;
  const per = [];
  for (const en of entries) {
    const mp = en && en.actor && en.actor.getMapper && en.actor.getMapper();
    const pd = mp && mp.getInputData && mp.getInputData();
    const a = pd ? faceAreas(pd) : null;
    if (!a || (en.nCells && a.length !== en.nCells)) { per.push(null); continue; }
    const s = new Float32Array(a.length);
    for (let i = 0; i < a.length; i++) {
      const v = Math.sqrt(a[i]);
      s[i] = v;
      if (v > 0) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }
    per.push(s);
    got++;
  }
  if (!got || !(hi > 0)) return "no face geometry to measure";

  const { unit, scale } = displayUnit(units, hi);
  entries.forEach((en, i) => {
    const s = per[i];
    if (!s || !en.q) return;
    const out = new Float32Array(s.length);
    for (let k = 0; k < s.length; k++) out[k] = s[k] * scale;
    en.q.cell_size = out;
  });
  qf.metrics.cell_size = {
    label: "Cell size",
    unit,
    floor: Number((lo === Infinity ? 0 : lo * scale).toFixed(4)),
    // no bar exists for cell size; the coarsest cell is used as the limit so that the panel's
    // "Only bad cells" filter means "the coarsest end" rather than greying the whole mesh
    limit: Number((hi * scale).toFixed(4)),
    max: Number((hi * scale).toFixed(4)),
  };
  return null;
}
