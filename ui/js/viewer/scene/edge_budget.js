// Responsibility: Decide whether a surface may draw its cell edges, from the polygon count of
// the surface itself.
// Owns: the one budget, and the reason for it.
// Boundaries: it sets edge visibility and nothing else. It does not own colour, representation,
// opacity or the camera, and it never decides whether an actor is visible.

/* WHY A BUDGET EXISTS AT ALL.
 *
 * `setEdgeVisibility(true)` is not a free switch. It makes the mapper build a second, line
 * primitive for every polygon, and on a delivered mesh that is the point at which the browser
 * gives up: measured 2026-11-07 on Intel UHD via ANGLE/D3D11, a surface of 1.9M faces LOST THE
 * WEBGL CONTEXT with edges on. The canvas goes black and does not come back without a reload,
 * and the user is given no reason for it.
 *
 * The same measurement is the reason the cap is not lower. Without edges, 1,904,636 faces /
 * 3,810,902 triangles drew in 0.30 ms of CPU per frame at 55.2 fps. Drawing is not the problem.
 * Edges are.
 *
 * 750,000 is below the observed failure with room to spare, and above every surface the reducer
 * delivers for a normal part, so nothing that works today stops working.
 *
 * WHAT IT DOES NOT DO. It does not warn, and it does not switch representation. A mesh too large
 * for edges is still drawn, as a solid, which is the honest outcome: the shape is right and the
 * cells are simply too small to be worth a line each. Anyone who needs to see individual cells
 * at that size wants the detail window, not a wireframe of the whole part.
 */

/** The cap, in polygons on ONE surface. See the note above before changing it. */
export const EDGE_MAX_POLYS = 750000;

/** How many polygons a vtkPolyData actually carries. vtkCellArray knows, but the shape of its
 *  answer has changed between vtk.js releases, so fall back to reading the packed array: it is
 *  [n, i0..in-1, n, i0..], so walking the counts is exact for mixed cell sizes. */
export function polyCount(pd) {
  try {
    const polys = pd && pd.getPolys && pd.getPolys();
    if (!polys) return 0;
    if (typeof polys.getNumberOfCells === "function") {
      const n = polys.getNumberOfCells();
      if (Number.isFinite(n) && n >= 0) return n;
    }
    const a = typeof polys.getData === "function" ? polys.getData() : null;
    if (!a || !a.length) return 0;
    let i = 0, n = 0;
    while (i < a.length) { n++; i += a[i] + 1; }
    return n;
  } catch (_) {
    return 0;
  }
}

/** Set edge visibility on a property, refusing it above the budget.
 *  `want` is what the UI asked for; the return value is what was actually applied, so a caller
 *  that wants to explain itself to the user can. */
export function setEdges(pr, pd, want) {
  if (!pr || !pr.setEdgeVisibility) return false;
  const on = !!want && polyCount(pd) <= EDGE_MAX_POLYS;
  pr.setEdgeVisibility(on);
  return on;
}

/** True when this surface is over the budget, for a caller that wants to say so in the UI
 *  (for example, to disable a Solid + edges control rather than let it do nothing). */
export function overEdgeBudget(pd) {
  return polyCount(pd) > EDGE_MAX_POLYS;
}
