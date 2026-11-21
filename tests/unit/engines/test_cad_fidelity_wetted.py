# Responsibility: Verify the CAD fidelity gate holds an external mesh only to the CAD faces the far field wets at the
# mesh's own scale (the sealed-cavities step's reading), so faces hidden inside an assembly are never "CAD the mesh
# missed" - and a single closed body reads exactly as before (rulebook 75).
# Boundaries: engines/cad_fidelity.wetted_skin / both_skins / outer_skin on synthetic surfaces; no mesher runs.
from __future__ import annotations

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")

from meshpipeline.engines import cad_fidelity as F  # noqa: E402


def _surface(mesh) -> F.Surface:
    m = mesh.extract_surface().triangulate()
    return F.Surface(np.asarray(m.points, dtype=float), np.asarray(m.faces).reshape(-1, 4)[:, 1:])


def _shell_with_a_hole_and_a_part_inside():
    """An assembly of two solids pushed into each other, the first with a crack in its skin narrower than a mesh
    cell: the second solid's face that lies inside the first is hidden from the flow."""
    a = pv.Box(bounds=(0, 1, 0, 1, 0, 1), level=9, quads=True)
    cen = np.asarray(a.cell_centers().points)
    crack = (cen[:, 2] > 0.999) & (np.abs(cen[:, 0] - 0.25) < 0.05) & (np.abs(cen[:, 1] - 0.55) < 0.05)
    a = a.extract_cells(np.flatnonzero(~crack)).extract_surface().triangulate()
    b = pv.Box(bounds=(0.8, 1.8, 0.2, 0.8, 0.2, 0.8), level=5).triangulate()
    cad = _surface(a.merge(b))
    bc = np.asarray(b.cell_centers().points)
    hidden = bc[bc[:, 0] < 0.801]                        # b's face inside a
    seen = np.asarray(a.cell_centers().points)
    seen = seen[seen[:, 0] < 0.999]                       # a's faces the flow sees (not the one inside b)
    return cad, hidden, seen


def test_faces_the_far_field_does_not_wet_at_the_mesh_s_scale_are_not_missed_cad():
    """The RM3 wave absorber: two production-grade meshes refused at "14.6% of your CAD not reached", where
    the far field wets 12,303 of its 15,648 faces."""
    cad, inside, outside = _shell_with_a_hole_and_a_part_inside()
    cell = 0.1                                            # the hole is one mesh cell across
    fine = F.outer_skin(cad, cell)
    wet = F.wetted_skin(cad, cell)
    assert fine is not None and wet is not None
    assert np.asarray(fine(inside)).mean() > 0.5, "precondition: the fine flood gets in through the hole"
    assert np.asarray(wet(inside)).mean() < 0.05, "the far field does not wet the part inside"
    assert np.asarray(wet(outside)).mean() > 0.9
    skin = F.both_skins(fine, wet)
    assert np.asarray(skin(inside)).mean() < 0.05 and np.asarray(skin(outside)).mean() > 0.9


def test_a_single_closed_body_is_read_as_before():
    cad = _surface(pv.Box(bounds=(0, 1, 0, 0.4, 0, 0.4), level=6))
    assert F.wetted_skin(cad, 0.05) is None, "every face wetted: no second reading, outer_skin's alone"
    fine = F.outer_skin(cad, 0.05)
    assert F.both_skins(fine, None) is fine and F.both_skins(None, None) is None
