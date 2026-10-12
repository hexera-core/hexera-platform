# Responsibility: Prove the snap-grid ECXML mesher's corrections from the hard-case sweep: buried cylinders keep their host (no sealed air) and are snapped to their true shape; cylinders that touch along a line share no face.
from __future__ import annotations

import numpy as np
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


# ------------------------------------------------------------------------------ precision ------
@pytest.mark.parametrize(("text", "noisy"), [
    ("0.300000011920929", True),        # Flotherm: float32 0.3 printed in full
    ("48.263999938964844", True),
    ("0.002600499999999", True),        # a binary float printed in full
    ("5.00100000", False),              # a decimal: exact to its digits, however far out
    ("0.0025", False), ("-0.00000100", False), ("1e-3", False), ("0", False),
    ("0.123456789", False),             # nine digits, but not a float32's
])
def test_a_numbers_precision_is_read_from_its_digits(text, noisy):
    from meshpipeline.cad.ingest.ecxml import number_is_noisy

    assert number_is_noisy(text) is noisy


def _far_sandwich(offset: float = 5.0) -> Ecxml:
    doc = _materials(Ecxml("Far sandwich"))
    doc.domain((offset - 0.002, offset - 0.002, offset - 0.002), (0.03, 0.01, 0.006))
    for k, t in enumerate((1e-6, 2e-6)):
        x = offset + 0.012 * k
        doc.block(f"Bottom {k}", (x, offset, offset), (0.01, 0.006, 0.001), "Si")
        doc.block(f"Layer {k}", (x, offset, offset + 0.001), (0.01, 0.006, t), "Si")
        doc.block(f"Top {k}", (x, offset, offset + 0.001 + t), (0.01, 0.006, 0.001), "Cu", 1.0)
    return doc


def test_micron_layers_far_from_the_origin_written_as_decimals_are_kept(tmp_path):
    result = _mesh(tmp_path, _far_sandwich(), max_cells=100_000)
    rep = result.report
    assert {"Layer_0", "Layer_1"} <= set(rep["solid_regions"])
    assert not rep["flattened"] and rep["file_precision_m"] < 1e-7
    contacts = {tuple(c) for c in rep["solid_contacts"]}
    assert ("Bottom_0", "Top_0") not in contacts and ("Bottom_0", "Layer_0") in contacts
    assert not [ln for ln in rep["build_report"] if ln.startswith("WARNING")]


def test_a_layer_thinner_than_a_float32_can_state_is_flattened_and_said_loudly(tmp_path):
    import re
    import struct

    def as_float32(m: re.Match) -> str:
        v = struct.unpack("<f", struct.pack("<f", float(m.group(2))))[0]
        return f"{m.group(1)}{v:.15g}{m.group(3)}"

    xml = _far_sandwich().xml().decode()
    # the location and size numbers as a float32 exporter writes them
    xml = re.sub(r"(<[xyz]>)([-0-9.e]+)(</[xyz]>)", as_float32, xml)
    src = tmp_path / "model.ecxml"
    tmp_path.mkdir(parents=True, exist_ok=True)
    src.write_text(xml)
    result = mesh_ecxml(src, tmp_path / "case", G.GridPlan(max_cells=100_000), binary=False)
    rep = result.report
    assert rep["file_precision_m"] > 1e-6          # float32 at 5 m: about 1.2 um
    assert any("Layer 0" in f for f in rep["flattened"])
    warn = [ln for ln in rep["build_report"] if ln.startswith("WARNING") and "NOT meshed" in ln]
    assert warn and "Layer 0" in warn[0] and "Bottom_0 and Top_0" in warn[0]
    checked = next(ln for ln in rep["build_report"] if ln.startswith("Checked"))
    assert "not meshed" in checked                 # "none lost" never stands alone


# ------------------------------------------------------------------------------ round-off ------
def test_exporter_round_off_steps_merge_but_a_parts_own_faces_never_do(tmp_path):
    doc = _materials(Ecxml("Round-off"))
    doc.domain((-0.001, -0.001, -0.001), (0.022, 0.008, 0.004))
    doc.block("Base", (0, 0, 0), (0.006, 0.006, 0.001), "Si")
    doc.block("Over", (0, 0, 0.00100003), (0.006, 0.006, 0.001), "Cu")     # a 30 nm step
    doc.block("Film", (0.008, 0, 0), (0.006, 0.006, 5e-8), "Si")          # a 50 nm part
    doc.block("Near", (0.015, 0, 0), (0.002, 0.006, 0.001), "Si")
    doc.block("Far", (0.0170005, 0, 0), (0.002, 0.006, 0.001), "Cu")      # 0.5 um of air
    result = _mesh(tmp_path, doc, max_cells=100_000)
    rep = result.report
    contacts = {tuple(c) for c in rep["solid_contacts"]}
    assert ("Base", "Over") in contacts                      # the 30 nm step is one plane
    assert "Film" in rep["solid_regions"] and not rep["flattened"]   # its own faces stay apart
    line = next(ln for ln in rep["build_report"] if ln.startswith("Exporter round-off"))
    assert "30 nm" in line and "Base" in line and "gap(s) that thin are closed" in line
    assert [r["between"] for r in rep["thin_air"]] == [["Far", "Near"]]
    assert any(ln.startswith("WARNING") and "Far and Near (500 nm)" in ln
               for ln in rep["build_report"])
    assert ("Far", "Near") not in contacts


# ------------------------------------------------------------------------------ budget ---------
def _stack(doc: Ecxml) -> Ecxml:
    doc = _materials(doc)
    doc.domain((0, 0, 0), (0.06, 0.06, 0.02))
    for i in range(4):                       # sixteen stacks of four 30 um layers
        for j in range(4):
            z = 0.005
            for k in range(4):
                doc.block(f"L{i}{j}{k}", (0.004 + 0.014 * i, 0.004 + 0.014 * j, z),
                          (0.01, 0.01, 3e-5), "Si" if k % 2 else "Cu")
                z += 3e-5
    return doc


def test_the_budget_takes_cells_out_of_the_layers_one_at_a_time():
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_place import place
    from meshpipeline.engines.snapgrid import blocks as B

    steps = G.relaxations(G.GridPlan(min_cells_across=4, cylinder_cells=12, growth=1.3))
    # the growth limit relaxes in two steps (1.5, then 2) before a cell comes out of a layer
    assert [p.growth for p in steps][:3] == [1.3, 1.5, 2.0]
    assert [p.min_cells_across for p in steps] == [4, 4, 4, 3, 2, 1, 1, 1, 1]
    assert [p.cylinder_cells for p in steps][-3:] == [10, 8, 6]
    pl = place(read_ecxml(_stack(Ecxml("Stack")).xml()))
    two = G.GridPlan(min_cells_across=2, growth=2.0)
    geo = B.geometry_of(pl, two)
    longest = max(geo.domain.hi[i] - geo.domain.lo[i] for i in range(3))
    needs_two = B.decompose(geo, two, longest).n_cells       # the fewest cells keeping 2 across
    lay = B.build_layout(pl, G.GridPlan(max_cells=needs_two + 10, min_cells_across=4))
    # four through each layer does not fit; two does - not one
    assert lay.plan.min_cells_across == 2 and lay.n_cells <= needs_two + 10
    assert any("2 cell(s) through their thickness (asked 4)" in r for r in lay.relaxed)


# ------------------------------------------------------------------------------ memory ---------
def test_a_budget_the_memory_cannot_hold_is_lowered_before_anything_is_built(tmp_path):
    assert (G.available_memory() or 0) > 0             # read from this machine
    fits = 150_000
    memory = (G.BASE_BYTES + G.BYTES_PER_CELL * fits) / G.MEMORY_SHARE
    src = tmp_path / "model.ecxml"
    src.write_bytes(_stack(Ecxml("Stack")).xml())
    result = mesh_ecxml(src, tmp_path / "case", G.GridPlan(max_cells=2_000_000),
                        dry_run=True, memory_bytes=memory)
    rep = result.report
    assert rep["plan"]["max_cells"] == 2_000_000 and rep["plan_used"]["max_cells"] <= fits
    assert rep["grid"]["cells"] <= fits and rep["memory"]["lowered_from"] == 2_000_000
    assert any("memory this job has" in r for r in rep["relaxed"])


def test_a_model_the_memory_cannot_hold_is_refused_in_one_sentence(tmp_path):
    src = tmp_path / "model.ecxml"
    src.write_bytes(_stack(Ecxml("Stack")).xml())
    memory = (G.BASE_BYTES + G.BYTES_PER_CELL * 2_000) / G.MEMORY_SHARE   # room for 2,000 cells
    with pytest.raises(G.OutOfMemory) as exc:
        mesh_ecxml(src, tmp_path / "case", G.GridPlan(max_cells=2_000_000), memory_bytes=memory)
    said = str(exc.value)
    assert "GB of memory" in said and "refused before anything is built" in said
    assert not (tmp_path / "case").exists()
    assert isinstance(exc.value, G.OverBudget)       # the CLI says it as NOT MESHED, exit 2


# ------------------------------------------------------------------------------ reporting ------
def test_what_the_file_most_likely_got_wrong_is_said_and_left_as_the_file_says(tmp_path):
    doc = _materials(Ecxml("Odd physics"))
    doc.domain((0, 0, 0), (0.1, 0.06, 0.04))
    doc.block("Cooler", (0.005, 0.005, 0.005), (0.01, 0.01, 0.01), "Cu", -2.0)
    doc.block("Half out", (-0.005, 0.03, 0.005), (0.01, 0.01, 0.01), "Cu", 4.0)
    doc.cylinder("Rod out", (0.095, 0.02, 0.01), (0.01, 0.01, 0.02), "+xy", "Cu")
    doc.fan3d("Still", (0.03, 0.005, 0.005), (0.01, 0.02, 0.02), "+yz", 0.008, 0.0)
    doc.fan3d("Backwards", (0.05, 0.005, 0.005), (0.01, 0.02, 0.02), "+yz", 0.008, -0.002)
    doc.fan3d("All hub", (0.07, 0.03, 0.005), (0.01, 0.02, 0.02), "+yz", 0.02, 0.002)
    doc.block("In the fan", (0.032, 0.01, 0.01), (0.004, 0.005, 0.005), "Si")
    doc.fan2d("Dot", (0.05, 0.05, 0.02), (0.0, 0.0, 0.0), "+xy", 0.001)
    result = _mesh(tmp_path, doc, max_cells=150_000)
    rep = result.report
    warn = [ln for ln in rep["build_report"] if ln.startswith("WARNING")]
    text = "\n".join(warn)
    assert "negative power" in text and "Cooler (-2 W)" in text
    assert "domain cuts Half out (50% inside, 4 W)" in text
    assert "Still: a fixed flow of zero" in text and "Backwards: a negative flow" in text
    assert "All hub: its hub (20 mm) is as wide as the fan" in text
    assert "Still: In the fan stand(s) in its swept volume" in text
    assert "Dot: a 2D fan with no area" in text
    # a cylinder the domain cuts is held to the piece of it inside, and says so
    rod = _curved(result, "Rod_out")
    assert rod["clipped_by_domain"] and abs(rod["side_area_error_pct"]) < 5.0
    assert abs(rod["volume_error_pct"]) < 5.0
    assert any(ln.startswith("Curved: Rod_out (cylinder, cut by the solution domain)")
               for ln in rep["build_report"])
    # nothing was changed: the powers are the file's
    side = result.sidecar
    assert side["total_power_W"] == pytest.approx(2.0)


# ------------------------------------------------------------------------------ snapping cost --
def test_each_curve_finds_the_faces_near_it_through_an_index_not_the_whole_mesh():
    from meshpipeline.engines.snapgrid.curved import Curve, _FaceIndex

    rng = np.random.default_rng(7)
    fc = rng.uniform(0.0, 0.1, size=(20_000, 3))
    curves = [Curve(part=k, name=f"c{k}", axis=2, a0=0.01 * k, a1=0.01 * k + 0.004,
                    centre=(0.01 + 0.008 * k, 0.05), radii=(0.002, 0.003)) for k in range(8)]
    index = _FaceIndex(fc, curves)
    for c in curves:
        for reach in (0.0, 0.003, 0.02):
            lo = np.array([c.centre[0] - c.radii[0] - reach, c.centre[1] - c.radii[1] - reach,
                           c.a0 - reach])
            hi = np.array([c.centre[0] + c.radii[0] + reach, c.centre[1] + c.radii[1] + reach,
                           c.a1 + reach])
            brute = np.flatnonzero(np.all((fc >= lo) & (fc <= hi), axis=1))
            assert np.array_equal(index.near(c, reach), brute)


def test_balls_standing_on_a_board_are_each_snapped_onto_their_circle(tmp_path):
    # each ball is snapped in its own window of faces, with the same result as before
    def grid(n):
        doc = _materials(Ecxml(f"Balls {n}"))
        doc.domain((-0.002, -0.002, -0.001), (0.004 + n * 0.001, 0.004 + n * 0.001, 0.004))
        doc.block("Board", (-0.001, -0.001, 0), (0.002 + n * 0.001, 0.002 + n * 0.001, 0.0008),
                  "Si")
        for i in range(n):
            for j in range(n):
                doc.cylinder(f"Ball {i}_{j}", (0.001 * i + 0.0002, 0.001 * j + 0.0002, 0.0008),
                             (0.0006, 0.0006, 0.0004), "+xy", "Cu")
        return doc

    one = _mesh(tmp_path / "a", grid(2), max_cells=150_000, background_cells=20)
    errs = [abs(s["volume_error_pct"]) for s in one.report["staircased"]]
    assert len(errs) == 4 and max(errs) < 3.0
    assert all(abs(s["side_area_error_pct"]) < 3.0 for s in one.report["staircased"])


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
