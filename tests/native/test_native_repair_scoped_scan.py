# Responsibility: Verify against the real kernel that a region-scoped re-inspection agrees with a
#                 whole-shape scan about its own region, and that omitting the scope changes
#                 nothing at all.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair.localize import (
    SCOPE_GROW_FRACTION,
    _diagonal,
    localize_scan,
)
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


#: A 100 mm box beside a 0.05 mm box, 200 mm away. The tiny box's faces are slivers against the
#: part's total area, so every one of them is a FACE defect, and they are all in one place.
BIG = 100.0
SLIVER = 0.05
SLIVER_AT = (300.0, 0.0, 0.0)


def _box_with_a_sliver_body():
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    x, y, z = SLIVER_AT
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(BIG, BIG, BIG).Shape())
    builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(x, y, z),
                                              gp_Pnt(x + SLIVER, y + SLIVER,
                                                     z + SLIVER)).Shape())
    return compound


def test_a_scope_whose_face_indices_name_nothing_still_finds_a_face_defect_in_its_box():
    """A face is admitted by index OR by box, and the OR is the safety property.

    THE BLIND SPOT THIS PINS. A region clustered from edge defects alone names no face, so a
    scope that admitted faces by index only would measure none - and a face defect that appeared
    inside the region AFTER the region was built would be reported absent by a scan that never
    looked at it. A package that damaged the region it was proving would pass its own
    re-inspection, which is the one failure the region loop exists to prevent.
    """
    from meshpipeline.cad.repair.regions import Region

    shape = _box_with_a_sliver_body()
    everything, _complete = localize_scan(shape)
    slivers = {d.fingerprint for d in everything if d.entity_type == "face"}
    assert slivers, "the fixture carries no face defect, so it proves nothing"

    x, y, z = SLIVER_AT
    scope = Region(index=0, fingerprints=frozenset(),
                   bbox_min=(x, y, z), bbox_max=(x + SLIVER, y + SLIVER, z + SLIVER),
                   face_indices=frozenset())           # names NOTHING: the box is the only test
    scoped, complete = localize_scan(shape, within=scope)

    assert complete
    assert {d.fingerprint for d in scoped if d.entity_type == "face"} == slivers
    # ...and the scope is still a scope: the 100 mm box's own geometry stays out of it.
    assert all(d.location["bbox_min"][0] >= x - BIG * SCOPE_GROW_FRACTION
               for d in scoped if d.location)


def test_the_scan_grows_the_region_box_from_the_caller_s_own_diagonal():
    """ONE GROWN BOX FOR BOTH SIDES OF A SCOPE, which is what `scoped_to`'s contract claims.

    The scan grew the region's box by the CURRENT shape's diagonal while `session.try_op` filtered
    its own records with the UPLOAD'S. Measured drift on the real part was zero, so this was
    latent rather than live - but a repair that shrinks the part leaves the filter admitting
    records the scan would not have reported, which inflates the `previous` side of the progress
    rule and excuses a warning the operation introduced into the region it is being proved
    against. That is the hazard Task 9 exists to close, in smaller form.
    """
    from meshpipeline.cad.repair.localize import scoped_to

    shape = _two_holed_boxes()
    everything, _complete = localize_scan(shape)
    regions = decompose(everything, diagonal_mm=_diagonal(shape))
    assert len(regions) == 2
    region = regions[0]

    # A DIAGONAL THE SHAPE DOES NOT HAVE, and large enough that the grown box reaches the far
    # box as well - so the number really is what decided what was measured.
    wide = _diagonal(shape) * 1000.0
    scoped, complete = localize_scan(shape, within=region, scope_diagonal_mm=wide)
    assert complete
    assert {d.fingerprint for d in scoped} == {d.fingerprint for d in everything}
    # AND THE RECORD FILTER AGREES WITH IT, given the same diagonal: that agreement is the point.
    assert ({d.fingerprint for d in scoped_to(everything, region, diagonal_mm=wide)}
            == {d.fingerprint for d in scoped})

    # The default is the shape's own diagonal, which is the whole-shape scan's answer and is what
    # the narrow scope above measured - so the widening was not a no-op.
    assert (localize_scan(shape, within=region)
            == localize_scan(shape, within=region, scope_diagonal_mm=_diagonal(shape)))
    narrow = {d.fingerprint for d in localize_scan(shape, within=region)[0]}
    assert narrow < {d.fingerprint for d in scoped}
