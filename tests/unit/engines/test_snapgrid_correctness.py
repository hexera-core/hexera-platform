# Responsibility: Prove the snap-grid ECXML mesher's corrections from the hard-case sweep: buried cylinders keep their host (no sealed air) and are snapped to their true shape; cylinders that touch along a line share no face.
from __future__ import annotations

import pytest
from tests.unit.cad.ecxml_models import Ecxml

from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml


def _mesh(tmp_path, doc: Ecxml, **plan):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "model.ecxml"
    src.write_bytes(doc.xml())
    return mesh_ecxml(src, tmp_path / "case", G.GridPlan(**plan), binary=False)


def _curved(result, name):
    return next(s for s in result.report["staircased"] if s["part"] == name)


def _materials(doc: Ecxml) -> Ecxml:
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150))
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390))
    return doc


# ------------------------------------------------------------------------------ buried ---------
def test_cylinders_buried_in_a_block_keep_the_block_around_them_and_their_true_shape(tmp_path):
    doc = _materials(Ecxml("Buried cylinders"))
    doc.domain((-0.002, -0.002, -0.002), (0.014, 0.014, 0.005))
    doc.block("Wafer", (0, 0, 0), (0.01, 0.01, 0.001), "Si")
    doc.cylinder("Via", (0.002, 0.002, 0), (0.001, 0.001, 0.001), "+xy", "Cu")
    doc.cylinder("Pin", (0.005, 0.002, 0.0005), (0.001, 0.001, 0.002), "+xy", "Cu")
    doc.cylinder("Wire", (0.001, 0.007, 0.0003), (0.008, 0.0004, 0.0004), "+yz", "Cu")
    doc.cylinder("Edge via", (0, 0.004, 0), (0.001, 0.001, 0.001), "+xy", "Cu")
    result = _mesh(tmp_path, doc, max_cells=200_000)
    rep = result.report
    # the block holds them: no sealed air pocket beside any of them
    assert rep["fluid_regions"] == ["air"], rep["fluid_regions"]
    # only the via tangent to the wafer's own side gives cells back - to the wafer, never air
    rows = rep["line_contacts"]
    assert [r["part"] for r in rows] and all(r["given_to"] == [0] for r in rows)
    assert {r["part"] for r in rows} == {4}           # Edge via (Wafer is part 0)
    # snapped onto the true circle (a staircase left in place loses ~10%)
    for name in ("Via", "Pin", "Wire", "Edge_via"):
        row = _curved(result, name)
        assert abs(row["volume_error_pct"]) < 3.0, (name, row["volume_error_pct"])
        assert abs(row["side_area_error_pct"]) < 3.0, (name, row["side_area_error_pct"])
    # the wafer's own faces stay exactly the file's: its volume is the box less the cylinders'
    # cells, and every cell of the domain is somebody's
    total = sum(r["volume_m3"] for r in rep["regions"])
    assert total == pytest.approx(0.014 * 0.014 * 0.005, rel=1e-9)


# ------------------------------------------------------------------------------ line contacts --
def test_cylinders_side_by_side_or_stacked_meet_along_a_line_and_share_no_face(tmp_path):
    doc = _materials(Ecxml("Rods"))
    doc.domain((-0.002, -0.002, -0.002), (0.024, 0.016, 0.016))
    for k in range(3):                                    # side by side, tangent
        doc.cylinder(f"Rod {k}", (0.004 * k, 0, 0), (0.004, 0.004, 0.01), "+xy", "Cu")
    doc.block("Floor", (0, 0.006, 0), (0.02, 0.006, 0.002), "Si")
    doc.cylinder("Low", (0, 0.007, 0.002), (0.02, 0.004, 0.004), "+yz", "Cu")    # on the floor
    doc.cylinder("High", (0, 0.007, 0.006), (0.02, 0.004, 0.004), "+yz", "Cu")   # on Low
    result = _mesh(tmp_path, doc, max_cells=200_000)
    contacts = {tuple(c) for c in result.report["solid_contacts"]}
    assert not contacts & {("Rod_0", "Rod_1"), ("Rod_1", "Rod_2"), ("High", "Low"),
                           ("Floor", "Low")}, contacts
    pairs = {(r["part"], tuple(r["others"])) for r in result.report["line_contacts"]}
    assert len(pairs) == 4                       # rod0-rod1, rod1-rod2, low-floor, low-high
    for name in ("Rod_0", "Rod_1", "Low", "High"):
        row = _curved(result, name)
        assert abs(row["volume_error_pct"]) < 5.0 and abs(row["side_area_error_pct"]) < 3.0
