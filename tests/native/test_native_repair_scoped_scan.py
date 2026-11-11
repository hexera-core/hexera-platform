# Responsibility: Verify against the real kernel that a region-scoped re-inspection agrees with a
#                 whole-shape scan about its own region, and that omitting the scope changes
#                 nothing at all.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair.localize import _diagonal, localize_scan
from meshpipeline.cad.repair.regions import decompose

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")

#: Two 10 mm boxes 200 mm apart, each missing one face. BUILT HERE RATHER THAN READ: the point of
#: the shape is that its defects are in two places far enough apart that clustering cannot merge
#: them, which is a property of the construction, not of a file. One missing face per box leaves a
#: sewn shell declared a solid, which is the one defect class `ShapeAnalysis_FreeBounds` localises
#: exactly (see `cad_fixtures.write_open_shell_step`).
SIDE = 10.0
GAP = 200.0


def _holed_box(origin):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    x, y, z = origin
    box = BRepPrimAPI_MakeBox(gp_Pnt(x, y, z),
                              gp_Pnt(x + SIDE, y + SIDE, z + SIDE)).Shape()
    faces = []
    explorer = TopExp_Explorer(box, TopAbs_FACE)
    while explorer.More():
        faces.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()

    sewing = BRepBuilderAPI_Sewing(1e-6)
    for face in faces[:-1]:          # every face but one
        sewing.Add(face)
    sewing.Perform()
    maker = BRepBuilderAPI_MakeSolid()
    maker.Add(TopoDS.Shell_s(sewing.SewedShape()))
    return maker.Solid()


def _two_holed_boxes():
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for origin in ((0.0, 0.0, 0.0), (GAP, 0.0, 0.0)):
        builder.Add(compound, _holed_box(origin))
    return compound


def test_scoped_scan_finds_the_same_defects_in_its_region_as_a_global_scan():
    shape = _two_holed_boxes()
    everything, complete = localize_scan(shape)
    assert complete
    regions = decompose(everything, diagonal_mm=_diagonal(shape))
    # The fixture is only evidence if its defects really did fall into two separated, located
    # regions - one scope that swallowed everything would pass the comparison below for nothing.
    assert len(regions) == 2, [r.fingerprints for r in regions]
    assert all(region.located for region in regions)

    for region in regions:
        scoped, scoped_complete = localize_scan(shape, within=region)
        assert scoped_complete, f"region {region.index} reported an incomplete scan"
        expected = {d.fingerprint for d in everything if d.fingerprint in region.fingerprints}
        assert expected, f"region {region.index} named no defect the global scan found"
        assert {d.fingerprint for d in scoped} == expected


def test_omitting_within_is_unchanged():
    shape = _two_holed_boxes()
    assert [d.fingerprint for d in localize_scan(shape)[0]] == \
           [d.fingerprint for d in localize_scan(shape, within=None)[0]]
    assert localize_scan(shape) == localize_scan(shape, within=None)
