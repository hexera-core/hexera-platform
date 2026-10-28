# Responsibility: Hold the shell mid-surface extraction (cad/shell_pairs.py, cad/midsurface.py) to thin-walled
# parts of KNOWN geometry: which bodies are shells, the walls found and their thicknesses, the mid-faces'
# places and areas, the junctions met (T, L, closed section, bend, stiffened panel), the open edges named,
# every face given a role - and the same answer twice.
# Boundaries: parts are built in the test (tests/shell_fixtures.py); OpenCASCADE only. No network, no store,
# no model, no mesher.
from __future__ import annotations

import json
import math

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import fea_features as ff  # noqa: E402
from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad import shell_pairs as sp  # noqa: E402

#: mid-face areas and wall thicknesses are exact on these analytic parts; the record keeps six figures
REL = 1e-5


def _shell_pairs(rec: dict) -> list[dict]:
    return [p for p in rec["pairs"] if p["shell"]]


def _shell_mids(rec: dict) -> list[dict]:
    return [m for m in rec["mids"] if m["shell"]]


def _sewn(rec: dict, body: str = "Part_1") -> dict:
    return next(s for s in rec["sewn"] if s["body"] == body)


def _all_checks(rec: dict) -> None:
    assert rec["checks"] == {"fidelity": True, "connected": True, "faces_accounted": True, "volume": True}, rec["checks"]


def test_a_flat_plate_is_one_shell_wall_at_half_its_thickness():
    rec, surface = ms.extract(sf.flat_plate(5.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    (pair,) = _shell_pairs(rec)
    assert pair["thickness_mm"] == pytest.approx(5.0, rel=REL)
    (mid,) = _shell_mids(rec)
    assert mid["area_mm2"] == pytest.approx(20_000.0, rel=REL)
    assert mid["fidelity"]["max_error_mm"] <= 1e-9 and mid["fidelity"]["points"] >= 9
    # the mid-face lies at z = 2.5
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(surface, box)
    z0, z1 = box.Get()[2], box.Get()[5]
    assert z0 == pytest.approx(2.5, abs=1e-6) and z1 == pytest.approx(2.5, abs=1e-6)
    roles = {f["role"] for f in rec["faces"]}
    assert roles == {sp.PAIRED, sp.RIM}
    _all_checks(rec)


def test_a_hole_is_kept_and_its_bore_is_a_rim():
    rec, _ = ms.extract(sf.plate_with_hole(3.0, 10.0), mm_per_unit=1.0)
    (mid,) = _shell_mids(rec)
    assert mid["area_mm2"] == pytest.approx(20_000.0 - math.pi * 100.0, rel=REL)
    bore = [f for f in rec["faces"] if f["kind"] == "cylinder"]
    assert len(bore) == 1 and bore[0]["role"] == sp.RIM
    assert _sewn(rec)["boundary_edges"] == 5          # four plate edges and the hole
    _all_checks(rec)


def _sewn_area(surface) -> float:
    return ms._area(surface)


def test_a_t_joint_is_sewn_on_one_line():
    tp, ts = 5.0, 4.0
    rec, surface = ms.extract(sf.t_joint(tp, ts), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert sorted(p["thickness_mm"] for p in _shell_pairs(rec)) == pytest.approx([ts, tp, tp])
    # the plate's two pieces each run half the stiffener across, the stiffener runs half the plate down: the
    # sewn surface is the whole 200 x 100 mid-plane and the web from z = tp/2 to its top
    assert _sewn_area(surface) == pytest.approx(200 * 100 + (60 + tp / 2) * 100, rel=REL)
    ext = sorted(e["area_mm2"] for m in _shell_mids(rec) for e in m["extended"])
    assert ext == pytest.approx([ts / 2 * 100, ts / 2 * 100, tp / 2 * 100], rel=REL)
    sewn = _sewn(rec)
    assert sewn["junction_edges"] >= 1 and sewn["open_edges"] == 0
    _all_checks(rec)


def test_an_l_corner_meets_at_the_crossing_of_the_mid_planes():
    rec, surface = ms.extract(sf.l_angle(5.0, 5.0), mm_per_unit=1.0)
    # each leg runs on to the other leg's mid-plane: two legs of 97.5 mm, 80 deep
    assert _sewn_area(surface) == pytest.approx(2 * 97.5 * 80, rel=REL)
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_an_l_corner_of_two_walls_meets_where_each_reaches_the_other():
    rec, surface = ms.extract(sf.l_angle(4.0, 8.0), mm_per_unit=1.0)
    ext = {m["thickness_mm"]: sum(e["area_mm2"] for e in m["extended"]) for m in _shell_mids(rec)}
    # the 4 mm leg runs half the 8 mm upright's wall (4 mm), the upright half the leg's (2 mm), 80 deep
    assert ext == pytest.approx({4.0: 4.0 * 80, 8.0: 2.0 * 80}, rel=REL)
    assert _sewn_area(surface) == pytest.approx((100 - 4) * 80 + (100 - 2) * 80, rel=REL)
    assert _sewn(rec)["open_edges"] == 0


def test_a_closed_box_section_is_one_ring_of_four_walls():
    rec, surface = ms.extract(sf.box_section(100, 60, 4, 300), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert len(_shell_mids(rec)) == 4
    # the mid-perimeter (100 - 4) x 2 + (60 - 4) x 2, 300 long
    assert _sewn_area(surface) == pytest.approx((96 * 2 + 56 * 2) * 300, rel=REL)
    assert _sewn(rec)["open_edges"] == 0
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.06)
    _all_checks(rec)


# ------------------------------------------------------------------------ the junction classes ----
def test_a_tube_welded_on_a_plate_meets_it_on_a_circle():
    tp, r_out, t, h = 5.0, 20.0, 2.0, 100.0
    rec, surface = ms.extract(sf.tube_on_plate(tp, r_out, t, h), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    # the whole plate mid-plane (the ring under the tube wall closed) and the mid-cylinder down to it
    want = 200 * 200 + 2 * math.pi * (r_out - t / 2) * (h + tp / 2)
    assert _sewn_area(surface) == pytest.approx(want, rel=1e-4)
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["junction_edges"] >= 1
    _all_checks(rec)


def test_a_cylinder_running_into_a_cone_is_one_closed_shell():
    rec, _ = ms.extract(sf.cone_cylinder(), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    assert {m["kind"] for m in _shell_mids(rec)} == {"cylinder", "cone"}
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_a_flange_cut_short_by_a_relief_still_meets_its_sheet():
    rec, _ = ms.extract(sf.relief_flange(2.0), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["junction_edges"] >= 1
    _all_checks(rec)


def test_a_mitred_tube_corner_closes_inside_and_out():
    rec, surface = ms.extract(sf.mitred_tube_corner(40.0, 3.0, 200.0), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_crossing_girders_on_a_deck_are_sewn_at_every_cross():
    rec, _ = ms.extract(sf.grillage(), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["junction_edges"] >= 8
    _all_checks(rec)


def test_a_filleted_t_is_bridged_over_its_fillets():
    rec, surface = ms.extract(sf.filleted_t(5.0, 4.0, 4.0), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert {f["role"] for f in rec["faces"]} <= {"paired", "rim", "blend"}
    assert _sewn_area(surface) == pytest.approx(200 * 100 + (60 + 2.5) * 100, rel=1e-4)
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_a_stiffened_panel_has_two_sheets_and_a_junction_per_stiffener():
    rec, _ = ms.extract(sf.stiffened_panel(tp=6.0, ts=8.0, h=50.0, n=3), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    sheets = {}
    for p in _shell_pairs(rec):
        sheets.setdefault(p["sheet"], set()).add(p["thickness_mm"])
    # the deck plate's four pieces are one 6 mm sheet; each 8 mm web is its own sheet
    assert sorted(min(v) for v in sheets.values()) == [6.0, 8.0, 8.0, 8.0]
    sewn = _sewn(rec)
    assert sewn["junction_edges"] == 3 and sewn["open_edges"] == 0
    _all_checks(rec)


def test_a_tube_is_its_mid_cylinder():
    rec, _ = ms.extract(sf.tube(25.0, 2.0, 200.0), mm_per_unit=1.0)
    (mid,) = _shell_mids(rec)
    assert mid["kind"] == "cylinder"
    assert mid["area_mm2"] == pytest.approx(2 * math.pi * 24.0 * 200.0, rel=REL)
    assert _sewn(rec)["open_edges"] == 0                # the seam is no open edge
    _all_checks(rec)


def test_a_bent_sheet_is_one_sheet_whose_bend_meets_its_flanges_without_extension():
    t, r = 2.0, 3.0
    rec, _ = ms.extract(sf.bent_bracket(t=t, r=r), mm_per_unit=1.0)
    mids = _shell_mids(rec)
    assert {m["kind"] for m in mids} == {"plane", "cylinder"}
    assert len({p["sheet"] for p in _shell_pairs(rec)}) == 1
    bend = next(m for m in mids if m["kind"] == "cylinder")
    assert bend["area_mm2"] == pytest.approx(0.5 * math.pi * (r + t / 2) * 50.0, rel=REL)
    assert all(not m["extended"] for m in mids)
    sewn = _sewn(rec)
    assert sewn["shared_edges"] == 2 and sewn["open_edges"] == 0
    _all_checks(rec)


def test_a_chunky_block_is_solid_and_has_no_mid_surface():
    rec, surface = ms.extract(sf.chunky_block(), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["solid"]
    assert not _shell_mids(rec) and rec["surface_faces"] == [] and rec["sewn"] == []


def test_a_plate_with_a_thick_boss_is_mixed_and_its_links_to_the_boss_are_named():
    rec, _ = ms.extract(sf.plate_with_boss(4.0), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "mixed"
    assert len(body["solid_regions"]) == 1
    sewn = _sewn(rec)
    assert sewn["solid_link_edges"] == 4 and sewn["open_edges"] == 0


def test_an_assembly_names_its_faces_by_part_and_shells_each_body():
    rec, _ = ms.extract(sf.two_plates_assembly(3.0), mm_per_unit=1.0)
    assert [(b["body"], b["verdict"]) for b in rec["bodies"]] == [("Part_1", "shell"), ("Part_2", "shell")]
    assert all(f["face"].startswith(("Part_1_Face_", "Part_2_Face_")) for f in rec["faces"])
    assert sorted(m["area_mm2"] for m in _shell_mids(rec)) == pytest.approx([5_000.0, 10_000.0])


def test_the_unit_is_honoured():
    rec, _ = ms.extract(sf.flat_plate(0.005), mm_per_unit=1000.0)   # a part drawn in metres
    (pair,) = _shell_pairs(rec)
    assert pair["thickness_mm"] == pytest.approx(5.0, rel=REL)


def test_the_record_and_surface_are_the_same_twice(tmp_path):
    path = sf.write_step(sf.stiffened_panel(), tmp_path / "panel.step")
    out = []
    for k in range(2):
        rec = ms.extract_file(path, tmp_path / f"run{k}", mm_per_unit=1.0)
        rec.pop("seconds")
        out.append((json.dumps(rec, sort_keys=True), (tmp_path / f"run{k}" / ms.SURFACE_FILE).read_bytes()))
        saved = json.loads((tmp_path / f"run{k}" / ms.RECORD_FILE).read_text())
        assert saved["checks"] == rec["checks"]
    assert out[0] == out[1]


def test_a_variable_wall_is_named_and_not_shelled():
    from tests.fea_fixtures import beveled_plate

    # a plate whose side is beveled: the bevel and the bottom are not an even wall
    rec, _ = ms.extract(beveled_plate(75.0), mm_per_unit=1.0)
    uneven = [p for p in rec["pairs"] if not p["even"]]
    assert all(p["why"] for p in uneven)


def test_the_exit_face_reading_changes_no_wall():
    from meshpipeline.cad import fea_measure as fm

    surface, _f, _k = fm._brep_surface(sf.t_joint(), 1.0)
    P, D, L = sp._sample_rays(surface)
    eps = 1e-6 * surface.diag
    plain = ff.wall_reach(surface, P + D * eps, D, L, eps=eps)
    hit = np.full(len(P), -2, dtype=np.int64)
    marked = ff.wall_reach(surface, P + D * eps, D, L, eps=eps, exit_label=hit)
    assert np.array_equal(np.isnan(plain), np.isnan(marked))
    assert np.allclose(plain[~np.isnan(plain)], marked[~np.isnan(marked)], rtol=0, atol=0)
    assert np.array_equal(hit >= 0, ~np.isnan(marked))


def test_a_brp_file_is_read_as_brep(tmp_path):
    from OCP.BRepTools import BRepTools

    path = tmp_path / "plate.brp"
    BRepTools.Write_s(sf.flat_plate(5.0), str(path))
    rec = ms.extract_file(path, mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]


def test_a_hem_folded_back_on_itself_is_one_sheet_with_no_open_edge():
    rec, _ = ms.extract(sf.hemmed_sheet(), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert {m["kind"] for m in _shell_mids(rec)} == {"plane", "cylinder"}
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_a_duct_elbow_closes_across_its_seam():
    R, r_out, t = 300.0, 100.0, 2.0
    rec, surface = ms.extract(sf.duct_elbow(R, r_out, t, 90.0), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    # a quarter of the torus at the mid radius: (2 pi R) (2 pi r) / 4
    assert _sewn_area(surface) == pytest.approx(math.pi ** 2 * R * (r_out - t / 2), rel=1e-4)
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)
