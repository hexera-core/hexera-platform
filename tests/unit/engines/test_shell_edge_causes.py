# Responsibility: Hold the edge-line record to its rule (SHMESH-51o64b): an element under the user's shortest edge is
# forced when any of its edges lies on a hole too small for 8 such edges round it (or on a shorter CAD edge, or across a
# narrower gap); on a mapped face it is structure; else it is ours - measured on built plates.
# Boundaries: gmsh builds and meshes small plates in a temporary folder; no solver.
from __future__ import annotations

import numpy as np
import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines.gmsh import shell_edge_causes as EC  # noqa: E402


def _plate(tmp_path, hole_d: float | None, size: float, mapped: bool = False):
    """A 20 x 10 mm plate (one face) with a round hole of `hole_d` mm at its middle, saved as a brep and meshed with
    quads at `size` (a transfinite grid when `mapped`): (brep, xyz, elements)."""
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("p")
        r = gmsh.model.occ.addRectangle(0, 0, 0, 20, 10)
        if hole_d:
            d = gmsh.model.occ.addDisk(10, 5, 0, hole_d / 2, hole_d / 2)
            gmsh.model.occ.cut([(2, r)], [(2, d)])
        gmsh.model.occ.synchronize()
        brep = tmp_path / "plate.brep"
        gmsh.write(str(brep))
        gmsh.option.setNumber("Mesh.MeshSizeMin", size)
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.RecombineAll", 1)
        if mapped:
            # 40 x 5 along the long sides: 0.5 mm along them, 2 mm across - every element under a 1 mm shortest edge
            for _d, c in gmsh.model.getEntities(1):
                b = gmsh.model.getBoundingBox(1, c)
                gmsh.model.mesh.setTransfiniteCurve(c, 41 if b[3] - b[0] > 1 else 6)
            for _d, s in gmsh.model.getEntities(2):
                gmsh.model.mesh.setTransfiniteSurface(s)
        gmsh.model.mesh.generate(2)
        tags, coords, _p = gmsh.model.mesh.getNodes()
        xyz = np.zeros((int(tags.max()) + 1, 3))
        xyz[tags.astype(int)] = coords.reshape(-1, 3)
        els = []
        for typ, n in ((3, 4), (2, 3)):
            _t, nodes = gmsh.model.mesh.getElementsByType(typ)
            els += [list(row) for row in nodes.reshape(-1, n).astype(int)]
    finally:
        gmsh.finalize()
    return brep, xyz, els


def test_a_hole_too_small_for_eight_shortest_edges_forces_them(tmp_path):
    # a 3 mm hole is 9.4 mm round: 8 edges of 2 mm do not fit, so its rim's elements cannot meet a 2 mm shortest edge
    brep, xyz, els = _plate(tmp_path, 3.0, 2.0)
    got = EC.classify(brep, 1.0, xyz, els, ["Mid_1"] * len(els), 2.0, None)["short"]
    assert got["by_cause"].get("small_hole", 0) >= 8 and got["cannot_meet"] is True
    assert got["forced"] == sum(got["by_cause"].get(k, 0) for k in EC.FORCED)
    assert got["forced"] + got["structure"] + got["ours"] == got["under"]
    assert got["forced_by"]["holes"] == 1 and abs(got["forced_by"]["smallest_hole_mm"] - 3.0) < 0.01


def test_a_hole_with_room_for_eight_forces_nothing(tmp_path):
    # a 5.6 mm hole is 17.6 mm round: 8 edges of 2 mm fit, and it stands 2.2 mm from the long sides: nothing forced
    brep, xyz, els = _plate(tmp_path, 5.6, 2.0)
    got = EC.classify(brep, 1.0, xyz, els, ["Mid_1"] * len(els), 2.0, None)["short"]
    assert got.get("forced", 0) == 0 and not got.get("cannot_meet")


def test_a_mapped_face_is_structure_and_the_longest_edge_is_read_too(tmp_path):
    brep, xyz, els = _plate(tmp_path, None, 1.0, mapped=True)
    got = EC.classify(brep, 1.0, xyz, els, ["Mid_1"] * len(els), 1.0, 1.5)
    assert got["short"]["under"] == len(els) == got["short"]["by_cause"]["mapped"] == got["short"]["structure"]
    assert got["short"]["forced"] == 0 and got["short"]["cannot_meet"] is False
    assert got["long"]["over"] == len(els) and got["long"]["by_cause"] == {"mapped": len(els)}


def test_nothing_outside_the_lines_records_nothing(tmp_path):
    brep, xyz, els = _plate(tmp_path, None, 2.0)
    assert EC.classify(brep, 1.0, xyz, els, ["Mid_1"] * len(els), 0.5, 10.0) == {"short": {}, "long": {}}


def test_the_row_says_what_cannot_be_met_and_only_that():
    from meshpipeline.engines import mesh_requirements as MR

    rec = {"forced": 64, "structure": 518, "ours": 42, "cannot_meet": True, "shortest_mm": 5.0,
           "forced_by": {"holes": 8, "holes_under_mm": 12.268, "short_cad_edges": 0, "narrow_gaps": 0}}
    assert MR.forced_words(rec, 5.0) == ("64 of these cannot be longer on this part at this size: 8 holes under "
                                         "12.3 mm across.")
    assert MR.forced_words({**rec, "forced": 0}, 5.0) == ""
    applied = {"requirements": {"rows": [{"field": "min_edge", "delivered": "a target - 1.2% of elements (624) have an "
                                          "edge shorter than 5 mm (the shortest is 3 mm)"}, {"field": "max_edge"}]}}
    EC._rows(applied, {"short": rec, "long": {}})
    row = applied["requirements"]["rows"][0]
    assert row["delivered"].endswith("mm). 64 of these cannot be longer on this part at this size: 8 holes under "
                                     "12.3 mm across.")
    assert (row["forced"], row["structure"], row["ours"], row["cannot_meet"]) == (64, 518, 42, True)
    assert "forced" not in applied["requirements"]["rows"][1]
