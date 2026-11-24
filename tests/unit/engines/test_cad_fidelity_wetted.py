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


def test_a_wetted_reading_that_keeps_almost_none_of_the_outer_skin_falls_back_to_it():
    """A failed wetting reading is not an assembly: the mesh is held to the outer skin, and the record says so."""
    cad, _inside, outside = _shell_with_a_hole_and_a_part_inside()
    fine = F.outer_skin(cad, 0.1)
    nothing = lambda pts: np.zeros(len(pts), dtype=bool)  # noqa: E731
    skin, note = F.sane_skin(cad, fine, nothing)
    assert skin is fine and "held to the whole outer skin" in note
    # the real reading of this assembly keeps most of the outer skin and is used as it is
    skin, note = F.sane_skin(cad, fine, F.wetted_skin(cad, 0.1))
    assert note == "" and np.asarray(skin(outside)).mean() > 0.9


def test_a_body_patch_not_read_is_not_measured_rather_than_all_missed(tmp_path, monkeypatch):
    """RM5 (c2_rm5): a production-grade mesh, 285,321 faces on 'body', measured with only the far field and the
    ground read - "100.0% of your CAD surface is not reached" and the mesh thrown away."""
    box = _surface(pv.Box(bounds=(-10, 10, -10, 10, 0, 10), level=2))
    body = _surface(pv.Box(bounds=(-1, 1, -1, 1, 0, 1), level=4))
    box.cell_size = np.full(box.n, 2.0)
    monkeypatch.setattr(F, "mesh_patches", lambda ws: {"farfield": box})
    monkeypatch.setattr(F, "cad_reference", lambda ws, flow, ip, **kw: (body, "the uploaded geometry", True))
    rec = F.measure(tmp_path, intake_patches=[{"name": "body", "type": "wall"}],
                    manifest={"flow_topology": "external", "patch_types": {"body": "wall", "farfield": "farfield"},
                              "patch_face_counts": {"body": 285321, "farfield": 17178}})
    assert rec["verdict"] == F.UNMEASURED and "'body'" not in rec["why"] and "body was not read" in rec["why"]


@pytest.mark.parametrize("half, verdicts", [(False, ("pass",)), (True, ("rebuild",))])
def test_an_assembly_mesh_on_its_outer_skin_passes_and_one_missing_half_of_it_fails(tmp_path, monkeypatch, half,
                                                                                   verdicts):
    """Two overlapping solids: the mesh that follows the outer skin passes; a mesh missing half the skin fails."""
    cad, _inside, _outside = _shell_with_a_hole_and_a_part_inside()
    box = _surface(pv.Box(bounds=(-3, 5, -3, 4, -3, 4), level=2))
    box.cell_size = np.full(box.n, 1.0)
    skin = F.outer_skin(cad, 0.05)
    keep = np.asarray(skin(cad.centroids()), dtype=bool)
    if half:
        keep &= cad.centroids()[:, 0] < 0.9
    used = np.unique(cad.tris[keep])
    remap = -np.ones(len(cad.points), dtype=np.int64)
    remap[used] = np.arange(len(used))
    body = F.Surface(cad.points[used], remap[cad.tris[keep]])
    body.cell_size = np.full(body.n, 0.05)
    monkeypatch.setattr(F, "mesh_patches", lambda ws: {"farfield": box, "body": body})
    monkeypatch.setattr(F, "cad_reference", lambda ws, flow, ip, **kw: (cad, "the uploaded geometry", True))
    rec = F.measure(tmp_path, intake_patches=[{"name": "body", "type": "wall"}],
                    manifest={"flow_topology": "external", "patch_types": {"body": "wall", "farfield": "farfield"},
                              "patch_face_counts": {"body": body.n, "farfield": box.n}})
    assert rec["patches_compared"] == ["body"]
    assert rec["verdicts"]["cad_to_mesh"] in verdicts, rec["surface"]
