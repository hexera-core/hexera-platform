# Responsibility: Prove the client-computed cell-size metric in a real browser - that it measures
#                 a known face correctly, that it registers with the mesh's own range, and that it
#                 REFUSES to register on a reduced surface instead of overstating the cell.
# Boundaries: no job and no API; polydata is built by hand. It asserts the module, never pixels.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# WHY. The heatmap answers "are the cells good" and not "are the cells where they should be". On a
# graded mesh that is the first question: a cold plate is 25 um across its channels and 400 um in
# the far field, a 16x span, and whether the fine cells landed on the channels is visible in one
# glance and in no table of maxima. The square root of a delivered face's area is the cell's edge
# length at the wall, and the geometry is already in the browser.

_LOAD = """(async () => {
  const { loadVtk } = await import('/static/js/viewer/scene/vtk_load.js');
  await loadVtk();
  window._cs = await import('/static/js/viewer/scene/cell_size.js');
  // One flat quad of side `s` in the x-y plane, as four points and one polygon.
  window._quad = (s) => {
    const pd = vtk.Common.DataModel.vtkPolyData.newInstance();
    pd.getPoints().setData(Float32Array.from([0,0,0, s,0,0, s,s,0, 0,s,0]), 3);
    pd.getPolys().setData(Uint32Array.from([4, 0, 1, 2, 3]));
    return pd;
  };
  // An entry as the viewer builds them: a patch name, a face count and an actor with a mapper.
  window._entry = (patch, pd) => {
    const mp = vtk.Rendering.Core.vtkMapper.newInstance(); mp.setInputData(pd);
    const ac = vtk.Rendering.Core.vtkActor.newInstance(); ac.setMapper(mp);
    return {patch, nCells: 1, q: {}, actor: ac};
  };
  window._qf = () => ({metrics: {non_ortho: {label: 'Non-orthogonality', limit: 65}},
                       patches: {wall: {}}});
  return true;
})()"""


def _load(live) -> None:
    live.evaluate(_LOAD, timeout=60)


def test_a_known_face_measures_its_own_area(live):
    _load(live)
    got = live.evaluate(
        "(() => {const a = window._cs.faceAreas(window._quad(0.002));"
        " return {n: a.length, area: a[0]}; })()")
    assert got["n"] == 1
    # a 2 mm square is 4e-6 m2, and its square root is the 2 mm side back again
    assert got["area"] == pytest.approx(4e-6, rel=1e-5)


def test_the_metric_registers_over_the_mesh_own_range_in_a_readable_unit(live):
    _load(live)
    got = live.evaluate(
        "(() => {const qf = window._qf();"
        " const es = [window._entry('wall', window._quad(0.0001))];"   # 100 um
        " const why = window._cs.addCellSizeMetric(es, qf, 'm', {});"
        " const m = qf.metrics.cell_size;"
        " return {why, m, q: es[0].q.cell_size ? Array.from(es[0].q.cell_size) : null}; })()")
    assert got["why"] is None, got
    m = got["m"]
    # 100 um of cell, so the unit must be microns and the value about 100
    assert m["unit"] == "µm", m
    assert m["limit"] == pytest.approx(100.0, abs=0.5), m
    assert m["max"] == pytest.approx(100.0, abs=0.5), m
    assert got["q"][0] == pytest.approx(100.0, abs=0.5), got
    # the limit IS the coarsest cell, so that "Only bad cells" means the coarse end rather than
    # greying the whole mesh; it is not a quality bar and nothing is "over" it
    assert m["limit"] == m["max"], m


def test_a_coarse_mesh_reads_in_millimetres_not_a_run_of_zeroes(live):
    _load(live)
    unit = live.evaluate(
        "(() => {const qf = window._qf();"
        " const es = [window._entry('wall', window._quad(0.02))];"    # 20 mm
        " window._cs.addCellSizeMetric(es, qf, 'm', {});"
        " return qf.metrics.cell_size.unit; })()")
    assert unit == "mm"


def test_it_refuses_a_reduced_surface_rather_than_overstating_the_cell(live):
    """A merged face's area is the sum of the faces in it, so the square root would overstate the
    cell by the square root of the merge count. A confidently wrong picture is worse than none."""
    _load(live)
    got = live.evaluate(
        "(() => {const qf = window._qf();"
        " const es = [window._entry('wall', window._quad(0.0001))];"
        " const why = window._cs.addCellSizeMetric(es, qf, 'm', {reduced: true});"
        " return {why, registered: 'cell_size' in qf.metrics}; })()")
    assert got["registered"] is False, "it must not register on a reduced surface"
    assert "reduced" in (got["why"] or ""), got


def test_a_field_that_does_not_match_the_face_count_is_dropped(live):
    """The same rule the backend metrics get: a misaligned array would paint every face with its
    neighbour's number, so it is dropped rather than trusted."""
    _load(live)
    got = live.evaluate(
        "(() => {const qf = window._qf();"
        " const e = window._entry('wall', window._quad(0.0001)); e.nCells = 99;"
        " const why = window._cs.addCellSizeMetric([e], qf, 'm', {});"
        " return {why, has: !!e.q.cell_size}; })()")
    assert got["has"] is False
    assert got["why"] is not None
