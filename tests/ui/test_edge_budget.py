# Responsibility: Prove the edge budget in a real browser - that it counts a surface's polygons
#                 correctly, that it lets a small surface draw its edges, and that it REFUSES a
#                 surface over the cap instead of handing the request to vtk.
# Boundaries: no job, no payload and no API; it builds polydata by hand. It asserts the module's
#             own behaviour, never pixels, and it never tries to reproduce the context loss.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# WHY THIS TEST EXISTS. `setEdgeVisibility(true)` makes the mapper build a line primitive per
# polygon, and measured 2026-11-07 on Intel UHD via ANGLE/D3D11 a surface of 1.9M faces LOST THE
# WEBGL CONTEXT with edges on: the canvas goes black and does not come back without a reload.
# Without edges the same 1,904,636 faces drew at 55.2 fps, so the fault is edges and not drawing.
# A cap with no test is a cap the next person deletes, so the refusal is pinned here.

_LOAD = """(async () => {
  const { loadVtk } = await import('/static/js/viewer/scene/vtk_load.js');
  await loadVtk();
  window._eb = await import('/static/js/viewer/scene/edge_budget.js');
  // A polydata whose polys array claims `n` triangles. The connectivity does not have to be
  // valid to be counted: the budget reads the packed [count, i, i, i, ...] stream, which is what
  // vtk itself walks.
  window._pd = (n) => {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from([0, 0, 0, 1, 0, 0, 0, 1, 0]), 3);
    const a = new Uint32Array(n * 4);
    for (let i = 0; i < n; i++) { a[4*i] = 3; a[4*i+1] = 0; a[4*i+2] = 1; a[4*i+3] = 2; }
    pd.getPolys().setData(a);
    return pd;
  };
  // A bare property stand-in: it records what it was told rather than owning a scene.
  window._prop = () => {
    const o = {calls: [], setEdgeVisibility(v) { o.calls.push(v); o.on = v; }};
    return o;
  };
  return true;
})()"""


def _load(live) -> None:
    live.evaluate(_LOAD, timeout=60)


def test_the_budget_counts_the_polygons_a_surface_actually_carries(live):
    _load(live)
    got = live.evaluate(
        "(() => ({zero: window._eb.polyCount(window._pd(0)),"
        " one: window._eb.polyCount(window._pd(1)),"
        " many: window._eb.polyCount(window._pd(1000)),"
        " nothing: window._eb.polyCount(null)}))()")
    assert got["zero"] == 0
    assert got["one"] == 1
    assert got["many"] == 1000
    # a missing surface is 0, not an exception: the budget must never be the thing that breaks a
    # draw it was only asked to advise on
    assert got["nothing"] == 0


def test_a_small_surface_is_allowed_its_edges(live):
    _load(live)
    got = live.evaluate(
        "(() => {const p = window._prop();"
        " const applied = window._eb.setEdges(p, window._pd(5000), true);"
        " return {applied, on: p.on, calls: p.calls.length}; })()")
    assert got["applied"] is True
    assert got["on"] is True
    assert got["calls"] == 1, "the property must be set exactly once"


def test_a_surface_over_the_cap_is_refused_rather_than_passed_to_vtk(live):
    """The whole point. Over the cap, vtk is told FALSE - it is not told true and left to die."""
    _load(live)
    got = live.evaluate(
        "(() => {const cap = window._eb.EDGE_MAX_POLYS;"
        " const p = window._prop();"
        " const applied = window._eb.setEdges(p, window._pd(cap + 1), true);"
        " return {cap, applied, on: p.on,"
        "         over: window._eb.overEdgeBudget(window._pd(cap + 1)),"
        "         under: window._eb.overEdgeBudget(window._pd(cap))}; })()")
    assert got["cap"] == 750000, "the cap moved; the measurement behind it is in edge_budget.js"
    assert got["applied"] is False, "over the cap, the request must not be honoured"
    assert got["on"] is False, "vtk must be told false, not left with edges on"
    assert got["over"] is True
    assert got["under"] is False, "the cap itself is allowed; it is a ceiling, not an exclusion"


def test_asking_for_no_edges_is_always_honoured(live):
    """A budget that only ever says no is a bug: `want=false` must reach the property too."""
    _load(live)
    got = live.evaluate(
        "(() => {const p = window._prop();"
        " const applied = window._eb.setEdges(p, window._pd(10), false);"
        " return {applied, on: p.on}; })()")
    assert got["applied"] is False
    assert got["on"] is False
