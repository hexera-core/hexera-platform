# Undeclared openings of an internal-flow shell must be SEALED INTO THE WALL.
#
# The mouth-caps work (55f9491) seals the bore holes of DECLARED port faces. Job
# 3ee48ebe (fda pump housing) showed the gap: an opening that is NOT a declared port -
# an open bore, an undeclared stub, a hole the tessellator itself left - connects the
# carved channel to the exterior void, snappy keeps channel+exterior as one region, and
# the delivered mesh grows a spurious 'outer' patch. For an internal-flow carve the
# fluid may exit ONLY through declared ports: every other opening must be sealed, and
# its seal belongs to the WALL patch, never to a port.
import math

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from pathlib import Path

from meshpipeline.cad.cad_tessellate import seal_open_rims, tessellate_internal
from meshpipeline.cad.stl_io import _box_triangles, read_stl_triangles
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _write_step(shape, path):
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    assert w.Write(str(path)) is not None


def _cyl(base, direction, r, length):
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, length).Shape()


def _hollow_tube():
    """Metal shell: outer r=30 mm, bore r=20 mm, z 0..200 mm; annular mouths at both ends."""
    outer = _cyl((0, 0, 0), (0, 0, 1), 30.0, 200.0)
    bore = _cyl((0, 0, -1.0), (0, 0, 1), 20.0, 202.0)
    return BRepAlgoAPI_Cut(outer, bore).Shape()


def _vessel_with_undeclared_stub(path):
    """Hollow tube plus a hollow side stub nobody declares: stub outer r=10 from the
    bore wall out to x=60, stub bore r=6 drilled from the channel through the stub end.
    Besides the two declared end mouths, the shell is open at the stub - an undeclared
    side bore, the pump-housing-with-rotor-removed class."""
    tube = _hollow_tube()
    stub = _cyl((20, 0, 100), (1, 0, 0), 10.0, 40.0)     # base disc sits inside the metal
    fused = BRepAlgoAPI_Fuse(tube, stub).Shape()
    drill = _cyl((10, 0, 100), (1, 0, 0), 6.0, 51.0)     # channel -> wall -> stub end
    _write_step(BRepAlgoAPI_Cut(fused, drill).Shape(), path)


def _vessel_with_plain_drilled_hole(path, direction=(0, 1, 0)):
    """Hollow tube with a PLAIN round hole drilled through the wall - no stub, no
    planar face at the opening: the gap's rims live on the two curved cylinder faces.
    Default direction +y keeps the hole away from the cylinder faces' parameter seam
    (which OCC puts along +x); pass (1, 0, 0) to straddle the seam deliberately."""
    tube = _hollow_tube()
    drill = _cyl((10 * direction[0], 10 * direction[1], 100), direction, 5.0, 30.0)
    _write_step(BRepAlgoAPI_Cut(tube, drill).Shape(), path)


def _plate_with_protruding_boss(path):
    """A SOLID boss fused over a flat plate: the plate's top face has an inner wire at
    the boss base, but nothing is open there - material fills the hole. Capping such a
    junction would wall off a live passage (the sealed-branch failure), so it must be
    left alone."""
    plate = _cyl((0, 0, 0), (0, 0, 1), 50.0, 10.0)
    boss = _cyl((0, 0, 10), (0, 0, 1), 10.0, 30.0)
    _write_step(BRepAlgoAPI_Fuse(plate, boss).Shape(), path)


def _stl_area(path) -> float:
    total = 0.0
    for a, b, c in read_stl_triangles(Path(path)):
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        cx = u[1] * v[2] - u[2] * v[1]
        cy = u[2] * v[0] - u[0] * v[2]
        cz = u[0] * v[1] - u[1] * v[0]
        total += 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)
    return total


def _segment_crossings(tris, p0, p1) -> int:
    """How many triangles the open segment p0->p1 pierces (Moller-Trumbore)."""
    d = [p1[k] - p0[k] for k in range(3)]
    hits = 0
    for a, b, c in tris:
        e1 = [b[k] - a[k] for k in range(3)]
        e2 = [c[k] - a[k] for k in range(3)]
        pv = [d[1] * e2[2] - d[2] * e2[1], d[2] * e2[0] - d[0] * e2[2],
              d[0] * e2[1] - d[1] * e2[0]]
        det = sum(e1[k] * pv[k] for k in range(3))
        if abs(det) < 1e-18:
            continue
        tv = [p0[k] - a[k] for k in range(3)]
        u = sum(tv[k] * pv[k] for k in range(3)) / det
        if u < -1e-9 or u > 1 + 1e-9:
            continue
        qv = [tv[1] * e1[2] - tv[2] * e1[1], tv[2] * e1[0] - tv[0] * e1[2],
              tv[0] * e1[1] - tv[1] * e1[0]]
        v = sum(d[k] * qv[k] for k in range(3)) / det
        if v < -1e-9 or u + v > 1 + 1e-9:
            continue
        t = sum(e2[k] * qv[k] for k in range(3)) / det
        if 1e-9 < t < 1 - 1e-9:
            hits += 1
    return hits


def test_undeclared_stub_bore_is_sealed_and_the_seal_is_wall(tmp_path):
    step = tmp_path / "vessel_stub.step"
    _vessel_with_undeclared_stub(step)
    out = tmp_path / "stls"

    t = tessellate_internal(step, out, prepared=_prepared(), declared_ports=[
        {"name": "inlet", "area_m2": None, "near_m": (0.0, 0.0, 0.0)},
        {"name": "outlet", "area_m2": None, "near_m": (0.0, 0.0, 0.2)}])

    # the stub is NOT a port: only the declared openings surface as ports
    assert set(t["openings"]) == {"inlet", "outlet"}
    # the stub's end-ring bore hole was sealed with a planar cap. (The drill's rim on
    # the bore wall straddles that cylinder face's parameter seam, so it forms no inner
    # wire and is not seen - see the seam-straddling limitation test below; the end cap
    # alone already closes the passage, leaving the stub bore a blind fluid pocket.)
    sealed = t["sealed"]["undeclared_openings"]
    hole = math.pi * 0.006 ** 2
    assert len(sealed) == 1, sealed
    assert sealed[0]["area"] == pytest.approx(hole, rel=0.05), sealed
    assert sealed[0]["centroid"] == pytest.approx([0.06, 0.0, 0.1], abs=1e-3)
    assert t["sealed"]["open_rims"] == []
    # the seal is WALL: a straight escape from the channel through the stub now
    # crosses wall triangles (before sealing it crossed nothing - that was the leak);
    # nudged off the stub axis so it cannot thread exactly through membrane vertices
    wall_tris = read_stl_triangles(Path(t["stls"]["wall"]))
    assert _segment_crossings(wall_tris,
                              (0.0007, 0.0005, 0.1004), (0.2, 0.0005, 0.1004)) >= 1, (
        "a straight path from the channel through the undeclared stub leaves the part "
        "without crossing the wall - the opening is not sealed")
    # and no port patch was polluted with the stub's seal: the ports still measure as
    # their own full mouths (annular ring + declared-port mouth cap), nothing more
    full_mouth = math.pi * 0.030 ** 2
    assert _stl_area(t["stls"]["inlet"]) == pytest.approx(full_mouth, rel=0.02)
    assert _stl_area(t["stls"]["outlet"]) == pytest.approx(full_mouth, rel=0.02)


def test_plain_drilled_hole_is_sealed_via_rim_membranes(tmp_path):
    # no stub and no planar face at the gap: both rims live on curved faces, so the
    # seal is a rim membrane, not a planar cap - the shape the pump housing class needs
    step = tmp_path / "vessel_drilled.step"
    _vessel_with_plain_drilled_hole(step)
    out = tmp_path / "stls"

    t = tessellate_internal(step, out, prepared=_prepared())

    assert set(t["openings"]) == {"inlet", "outlet"}
    sealed = t["sealed"]["undeclared_openings"]
    hole = math.pi * 0.005 ** 2
    assert len(sealed) == 2, sealed          # outer-surface gap + bore-surface gap
    assert all(s["area"] == pytest.approx(hole, rel=0.3) for s in sealed), sealed
    wall_tris = read_stl_triangles(Path(t["stls"]["wall"]))
    assert _segment_crossings(wall_tris,
                              (0.0005, 0.0007, 0.1004), (0.0005, 0.2, 0.1004)) >= 1, (
        "a straight path from the channel through the drilled hole leaves the part "
        "without crossing the wall - the hole is not sealed")
    # the combined staged surface stays crack-free: every membrane rim reuses the
    # surrounding surface's own vertices
    edges: dict = {}
    for name in t["stls"]:
        for tri in read_stl_triangles(Path(t["stls"][name])):
            keys = [tuple(v) for v in tri]
            for i in range(3):
                e = frozenset((keys[i], keys[(i + 1) % 3]))
                edges[e] = edges.get(e, 0) + 1
    assert not [e for e, n in edges.items() if n == 1]


def test_a_protruding_boss_junction_is_not_mistaken_for_an_opening(tmp_path):
    # the plate top's inner wire is a JUNCTION - the boss fills it. Sealing it would
    # slice a wall disc across live geometry: the exact catastrophe the manifold work
    # documents (a sealed branch that nothing downstream can see). It must stay unsealed.
    step = tmp_path / "plate_boss.step"
    _plate_with_protruding_boss(step)
    out = tmp_path / "stls"

    t = tessellate_internal(step, out, prepared=_prepared(), declared_ports=[
        {"name": "inlet", "area_m2": None, "near_m": (0.0, 0.0, 0.0)},
        {"name": "outlet", "area_m2": None, "near_m": (0.0, 0.0, 0.04)}])

    assert t["sealed"]["undeclared_openings"] == []
    assert t["sealed"]["open_rims"] == []
    # wall area is exactly the plate ring + plate rim + boss flank - no cap disc grew
    expected = (math.pi * (0.050 ** 2 - 0.010 ** 2)     # plate top ring
                + 2 * math.pi * 0.050 * 0.010           # plate rim
                + 2 * math.pi * 0.010 * 0.030)          # boss flank
    assert _stl_area(t["stls"]["wall"]) == pytest.approx(expected, rel=0.02)


# KNOWN LIMITATION, recorded deliberately rather than left to be rediscovered in a
# customer's mesh (the same practice as the flat-junction-ring record in
# test_internal_ports.py).
#
# Opening detection walks the INNER wires of shell faces. A hole that straddles a face
# boundary - here the cylinder faces' parameter seam, which OCC lays along +x - is
# split across the host's outer wire(s) and forms no inner wire, so it is invisible to
# the scan and stays open. The triangle-level rim audit cannot see it either: the B-rep
# stays watertight around the drill wall, so the staged soup has no boundary edges. Such
# a carve still leaks, and the finalize leak audit (internal_unexpected_patches) is the
# net that blocks delivery. The general fix would detect openings by region
# connectivity rather than by wire structure.
def test_a_seam_straddling_hole_is_still_missed(tmp_path):
    step = tmp_path / "vessel_seam_hole.step"
    _vessel_with_plain_drilled_hole(step, direction=(1, 0, 0))   # dead on the seam
    out = tmp_path / "stls"

    t = tessellate_internal(step, out, prepared=_prepared())

    assert t["sealed"]["undeclared_openings"] == []              # not seen
    assert t["sealed"]["open_rims"] == []                        # and no rim to audit
    wall_tris = read_stl_triangles(Path(t["stls"]["wall"]))
    assert _segment_crossings(wall_tris,
                              (0.0007, 0.0005, 0.1004), (0.2, 0.0005, 0.1004)) == 0, (
        "the seam-straddling hole got sealed - detection improved: fold this test into "
        "the sealed cases above")


def test_seal_open_rims_closes_a_gap_and_leaves_closed_soups_alone():
    box = _box_triangles([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])

    membranes, report = seal_open_rims(box)
    assert membranes == [] and report == []      # a closed soup is left untouched

    holed = box[:-1]                             # one triangle removed: an open rim
    membranes, report = seal_open_rims(holed)
    assert len(report) == 1 and report[0]["sealed"] and report[0]["edges"] == 3
    edges: dict = {}
    for tri in holed + membranes:
        keys = [tuple(v) for v in tri]
        for i in range(3):
            e = frozenset((keys[i], keys[(i + 1) % 3]))
            edges[e] = edges.get(e, 0) + 1
    assert not [e for e, n in edges.items() if n == 1], (
        "the membrane did not close the soup")
