# Responsibility: Hold the FEA measurement to the rule that two surfaces lying against each other - two parts
# pressed together, or two bodies meeting along an edge - are a CONTACT, never a sharp inside corner, and that
# a file of loose surfaces has its bodies counted.
# Boundaries: synthetic triangles for the edge rule; small OpenCASCADE solids for the body count. No store.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.cad import fea_features as ff

# THE DEFECT THIS PINS. Shared dev, 1 Nov: an IGES landing gear (11 x 27.2 x 15 in: bolts in holes, links on
# lugs) reported "570 sharp inside corners - 0 degrees, refinement capped" and "Bodies: not counted". 412 of
# the corners were under 10 degrees, every one where two of its parts touch: their faces, back to back, share
# a welded edge and read as a valley turning by nearly 180 degrees. No part has such a corner.


def _surface(tris, *, body=None, labels=None):
    pts = np.asarray([p for t in tris for p in t], dtype=float)
    idx = np.arange(len(pts)).reshape(-1, 3)
    from meshpipeline.cad.fea_measure import _weld

    xyz, t = _weld(pts, idx)
    lab = np.asarray(labels if labels is not None else range(len(tris)), dtype=np.int64)
    return ff.Surface(xyz=xyz, tris=t, label=lab, names=[f"Face_{k + 1}" for k in range(int(lab.max()) + 1)],
                      body=None if body is None else np.asarray(body, dtype=np.int64))


FLOOR = ((0, 0, 0), (1, 0, 0), (0, 1, 0))          # normal +z
WALL = ((0, 0, 0), (0, 1, 0), (0, 0, 1))           # normal +x: a square inside corner with the floor
LYING_ON = ((0, 0, 0), (0, 1, 0), (1, 0, 0.02))    # normal about -z: lying back to back on the floor


def test_a_square_inside_corner_is_still_a_corner():
    s = _surface([FLOOR, WALL])
    corners = ff.sharp_corners(s, ff.edge_pairs(s))
    assert len(corners) == 1 and corners[0]["angle_deg"] == pytest.approx(90.0, abs=0.5)
    assert ff.contacts(s, ff.edge_pairs(s)) == []


def test_two_faces_lying_back_to_back_are_a_contact_not_a_0_degree_corner():
    s = _surface([FLOOR, LYING_ON])
    pairs = ff.edge_pairs(s)
    assert pairs.concave[0] and pairs.turn_deg[0] > 170.0          # what read as a "1 degree" corner
    assert ff.sharp_corners(s, pairs) == []
    (c,) = ff.contacts(s, pairs)
    assert c["labels"] == [0, 1] and c["length_mm"] == pytest.approx(1.0)


def test_a_corner_between_two_bodies_is_a_contact():
    # the same square valley, but its two faces are two parts' (a bolt's head on a lug)
    s = _surface([FLOOR, WALL], body=[0, 1])
    pairs = ff.edge_pairs(s)
    assert ff.sharp_corners(s, pairs) == []
    (c,) = ff.contacts(s, pairs)
    assert c["bodies"] == [0, 1]


def test_the_contact_angle_is_well_clear_of_any_real_corner():
    # the landing gear's real corners began at 54 degrees; a V-notch is 30 to 90
    assert ff.CONTACT_DEG <= 10.0 < 30.0 <= ff.SHARP_TURN_DEG + 0.0


# ------------------------------------------------------------------- the bodies of loose faces ----
pytest.importorskip("OCP.BRepPrimAPI")


def _loose_faces(*boxes):
    """A compound of the boxes' FACES only - no shell, no solid: what an IGES of trimmed surfaces reads as."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for at, size in boxes:
        ex = TopExp_Explorer(BRepPrimAPI_MakeBox(gp_Pnt(*at), *size).Shape(), TopAbs_FACE)
        while ex.More():
            b.Add(comp, ex.Current())
            ex.Next()
    return comp


def test_a_file_of_loose_faces_has_its_bodies_counted():
    from meshpipeline.cad.fea_measure import measure_cad

    rec = measure_cad(_loose_faces(((0, 0, 0), (10, 10, 10)), ((30, 0, 0), (5, 5, 5))), mm_per_unit=1.0)
    # no solid to fuse or stage: the solids' count stays as it was, and the bodies are said beside it
    assert rec["bodies"] == 0
    assert rec["bodies_sewn"] == 2


def test_solids_are_counted_as_solids_and_not_sewn():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from meshpipeline.cad.fea_measure import measure_cad

    rec = measure_cad(BRepPrimAPI_MakeBox(10, 10, 10).Shape(), mm_per_unit=1.0)
    assert rec["bodies"] == 1 and "bodies_sewn" not in rec
