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
    # the 4 mm leg runs half the 8 mm upright's wall (4 mm), the upright half the leg's (2 mm), 80 deep
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
    assert _sewn(rec)["open_edges"] == 0
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


def test_a_thick_bracket_is_a_shell_when_asked_and_says_it_is_short():
    # SHELL-23: an 8 mm angle with 40 mm legs is about 6 walls across, not 10. The user asked for a shell, so it is
    # one - both legs, met at the corner - and one line says the walls are short for their thickness
    rec, _ = ms.extract(sf.short_angle(8.0, 40.0, 30.0), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "shell" and "closest_wall" not in body
    assert sorted(m["thickness_mm"] for m in _shell_mids(rec)) == [8.0, 8.0]
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)
    (row,) = rec["short_walls"]
    assert row["thickness_mm"] == 8.0 and row["span_walls"] < 10
    assert rec["advice"].startswith(f"Part_1: 8 mm thick over {ms._num(row['span_mm'])} mm, {row['span_walls']:g} times "
                                    "its thickness. Shell theory is less exact under 10 times (about ")


#: what the advice says a free plate 5.5 walls or more across costs (cad/shell_words: measured)
_FREE_PLATE = " (about 2% off on a free plate this short, more where it bends from a joint)."


def test_a_block_says_it_is_no_plate_in_numbers():
    # a 50 x 40 x 30 block: its most plate-like "wall" is 30 mm thick and 40 mm wide, 1.3 walls - a block
    rec, surface = ms.extract(sf.chunky_block(), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "solid" and rec["surface_faces"] == [] and rec["advice"] == ""
    assert body["why"] == ("no wall of it is a plate: its most plate-like wall is 30 mm thick but only 40 mm wide, "
                           "1.3 times its thickness; under 2 times, it is a bar or a block, not a plate")
    assert body["closest_wall"] == {"thickness_mm": 30.0, "span_mm": pytest.approx(44.7214, rel=1e-4), "width_mm": 40.0,
                                    "span_walls": 1.5, "width_walls": 1.3, "why": "narrow"}


def test_a_plate_with_a_thick_boss_is_mixed_and_its_links_to_the_boss_are_named():
    rec, _ = ms.extract(sf.plate_with_boss(4.0), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "mixed"
    assert len(body["solid_regions"]) == 1
    sewn = _sewn(rec)
    assert sewn["solid_link_edges"] == 4 and sewn["open_edges"] == 0


def test_a_long_rod_under_a_plate_is_a_solid_region_not_a_blend():
    # round with radius 8 under a 5 mm plate (under two walls), but 250 mm long: no fillet strip
    rec, _ = ms.extract(sf.plate_with_rod(), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "mixed"
    rod = next(f for f in rec["faces"] if f["kind"] == "cylinder")
    assert rod["role"] in (sp.SOLID, sp.THICK)
    sewn = _sewn(rec)
    assert sewn["open_edges"] == 0 and sewn["solid_link_edges"] >= 1


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


def test_plates_of_two_thicknesses_butted_in_line_become_one_flat_shell_with_an_offset():
    rec, surface = ms.extract(sf.butt_step(6.0, 3.0), mm_per_unit=1.0)
    assert _sewn_area(surface) == pytest.approx(200 * 50, rel=REL)
    offsets = {m["thickness_mm"]: m.get("offset_mm", 0.0) for m in _shell_mids(rec)}
    # the 3 mm plate lies on the 6 mm plate's mid-plane (z = 3); its own mid-plane is 1.5 mm below it
    assert offsets == pytest.approx({3.0: -1.5, 6.0: 0.0})
    # ... along the 6 mm plate's normal (the mesh writes it as ZOFFS / OFFSET)
    (moved,) = [m for m in _shell_mids(rec) if m.get("offset_mm")]
    assert np.abs(moved["offset_normal"]) == pytest.approx([0.0, 0.0, 1.0])
    assert _sewn(rec)["open_edges"] == 0 and not rec["fallback_regions"]
    _all_checks(rec)


def test_where_mid_faces_do_not_meet_the_region_is_meshed_solid_and_said_so(monkeypatch):
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)   # the join itself under test, no SHELL-13 strips
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)     # ... and no SHELL-22 strips
    monkeypatch.setattr(ms, "snap_steps", lambda *_a, **_k: None)   # leave the butt step open
    rec, _ = ms.extract(sf.butt_step(6.0, 3.0), mm_per_unit=1.0)
    regions = rec["fallback_regions"]
    assert regions and all(r["faces"] and r["at_mm"] and r["why"] for r in regions)
    assert any("meshed solid" in n for n in rec["notes"])


def test_a_part_the_extraction_cannot_read_is_meshed_solid_whole(tmp_path, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("boom")

    path = sf.write_step(sf.flat_plate(), tmp_path / "plate.step")
    monkeypatch.setattr(ms, "extract", broken)
    # in this process (deadline 0): the bounded child runs the same fallback, where a patch cannot reach
    rec = ms.extract_file(path, tmp_path / "out", mm_per_unit=1.0, deadline_s=0.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["solid"] and rec["complete"] is False
    assert (tmp_path / "out" / ms.RECORD_FILE).exists()


@pytest.mark.parametrize("name", ["tube_on_plate", "cone_cylinder", "relief_flange", "mitred_tube_corner"])
def test_the_general_join_closes_what_the_straight_one_cannot(name):
    rec, _ = ms.extract(getattr(sf, name)(), mm_per_unit=1.0)
    (row,) = rec["sewn"]
    assert row["open_edges"] == 0
    assert row["join"] in ("straight", "general")


def test_each_join_alone_is_held_to_the_same_parts(monkeypatch):
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)   # the join itself under test, no SHELL-13 strips
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)     # ... and no SHELL-22 strips
    # the straight join alone leaves the tube on its plate open; the general join closes it
    monkeypatch.setattr(ms, "_join_general", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert rec["sewn"][0]["open_edges"] > 0 and rec["sewn"][0]["join"] == "straight"


def test_the_general_join_in_its_own_process_gives_the_same_record(monkeypatch):
    forked, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    monkeypatch.setattr(ms, "JOIN_DEADLINE_S", 0.0)          # 0: run here, unbounded
    here, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert forked["sewn"][0]["join"] == "general"
    for r in (forked, here):
        r.pop("seconds", None)
    assert json.dumps(forked, sort_keys=True) == json.dumps(here, sort_keys=True)


def test_a_general_join_past_its_deadline_is_stopped_and_the_straight_join_stands(monkeypatch):
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)   # the join itself under test, no SHELL-13 strips
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)     # ... and no SHELL-22 strips
    import time as _time

    def slow(*_a, **_k):
        _time.sleep(30.0)

    monkeypatch.setattr(ms, "_join_general", slow)
    monkeypatch.setattr(ms, "JOIN_DEADLINE_S", 0.5)
    t0 = _time.time()
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert _time.time() - t0 < 20.0
    assert rec["sewn"][0]["join"] == "straight" and rec["sewn"][0]["open_edges"] > 0
    assert any("SHELL-11" in n for n in rec["notes"])


def test_a_web_ending_inside_a_thicker_plate_leaves_no_open_edge():
    rec, _ = ms.extract(sf.thin_web_on_thick_plate(), mm_per_unit=1.0)
    row = _sewn(rec)
    assert row["open_edges"] == 0 and row["ends_in_wall_edges"] >= 2
    _all_checks(rec)


def test_a_junction_stopped_short_is_still_open(monkeypatch):
    # with neither join allowed to extend, the L legs stop short of each other: that gap must stay OPEN, never
    # be taken for an edge ending inside the other wall
    monkeypatch.setattr(ms, "extend", lambda *a, **k: None)
    monkeypatch.setattr(ms, "_join_general", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)     # ... and no SHELL-22 strips
    rec, _ = ms.extract(sf.l_angle(5.0, 5.0), mm_per_unit=1.0)
    assert _sewn(rec)["open_edges"] >= 1


def test_a_junction_stopped_short_is_bridged_to_the_edge_beside_it(monkeypatch):
    # SHELL-13: the same L legs, stopped short of each other, are joined by the strip between their two edges
    monkeypatch.setattr(ms, "extend", lambda *a, **k: None)
    monkeypatch.setattr(ms, "_join_general", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    rec, surface = ms.extract(sf.l_angle(5.0, 5.0), mm_per_unit=1.0)
    assert _sewn(rec)["open_edges"] == 0
    assert any("SHELL-13" in n for n in rec["notes"])
    # the two legs (each 95 mm by 80, stopped at the other leg's inside face) and the strip across the corner (2.5 x sqrt 2 by 80)
    assert _sewn_area(surface) == pytest.approx(80 * (95.0 + 95.0 + 2.5 * math.sqrt(2.0)), rel=1e-3)


def test_a_pad_rounded_into_its_plate_is_linked_across_its_fillets():
    # SHELL-6b: the plate's edges stop where the fillets start, 2 mm short of the pad's sides; the fillets round
    # into the pad, so the edges are the pad's solid links, not open
    rec, _ = ms.extract(sf.plate_with_filleted_pad(), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "mixed"
    sewn = _sewn(rec)
    assert sewn["open_edges"] == 0 and sewn["solid_link_edges"] == 4


def test_a_spool_closes_at_both_plates():
    rec, _ = ms.extract(sf.spool(), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_a_sliver_the_coarse_mesher_cannot_triangulate_still_gives_an_inner_point(monkeypatch):
    # a thin curved sliver of a grown mid-face (a hopper wall cut slantwise) once came back with no triangles at
    # the coarse deflection, so it had no inner point, no owner, and its wall was left open: it is meshed finer
    import OCP.BRepMesh as bm

    real = bm.BRepMesh_IncrementalMesh
    face = ms._faces(sf.tube())[0]
    lo, hi = ms._box(face)
    coarse = ms.INNER_MESH_SHARES[0] * float(np.linalg.norm(hi - lo))
    calls = []

    def fussy(shape, deflection, *a):
        calls.append(deflection)
        return None if deflection >= 0.99 * coarse else real(shape, deflection, *a)

    monkeypatch.setattr(bm, "BRepMesh_IncrementalMesh", fussy)
    p = ms._inner_point(face)
    assert p is not None and len(calls) == 2
    assert ms._dist(ms._vertex(p), face) <= 1e-6


@pytest.mark.parametrize("tilt", [20.0, 50.0])
def test_a_wall_cut_slantwise_runs_to_its_cut_face(tilt):
    # the two wall faces end at different heights; the mid-face is carried on to the cut face, no open edge
    rec, _ = ms.extract(sf.oblique_half_pipe(tilt_deg=tilt), mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "shell"
    _all_checks(rec)


@pytest.mark.parametrize("w", [120.0, 130.0])
def test_a_spool_closes_whatever_its_plate_size(w):
    rec, _ = ms.extract(sf.spool(w=w), mm_per_unit=1.0)
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["join"] == "general"
    _all_checks(rec)


@pytest.mark.parametrize("normal_z", [1.0, -1.0])
def test_a_hole_offset_inward_shrinks_whichever_way_its_plane_faces(normal_z):
    # OCC's 2D offset takes its sign from how the wire runs against the plane's normal: a 20 mm hole asked to
    # shrink by 10 once grew to 30. The region is checked, and offset the other way when it went wrong.
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.Geom import Geom_Plane
    from OCP.GeomAbs import GeomAbs_Arc
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    surf = Geom_Plane(gp_Pnt(-65, -65, 2), gp_Dir(0, 0, normal_z))
    for z_dir in (1.0, -1.0):
        circle = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(0, 0, 2), gp_Dir(0, 0, z_dir)), 20.0)).Edge()
        wire = BRepBuilderAPI_MakeWire(circle).Wire()
        (shrunk,) = ms._offset_region(surf, wire, -10.0, GeomAbs_Arc)
        assert ms._area(shrunk) == pytest.approx(math.pi * 100.0, rel=1e-6)
        (grown,) = ms._offset_region(surf, wire, 10.0, GeomAbs_Arc)
        assert ms._area(grown) == pytest.approx(math.pi * 900.0, rel=1e-6)
        assert ms._offset_region(surf, wire, -25.0, GeomAbs_Arc) == []


def test_a_wall_stepping_where_a_plate_crosses_it_becomes_one_flat_shell():
    # the two wall pieces never touch (the plate parts them by its wall), yet they are one stepped wall: the
    # thinner piece is snapped onto the thicker piece's mid-plane with its offset kept (SHELL-7)
    rec, _ = ms.extract(sf.stepped_wall_through_plate(), mm_per_unit=1.0)
    offsets = [abs(m["offset_mm"]) for m in rec["mids"] if "offset_mm" in m]
    assert offsets == [pytest.approx(1.0, abs=1e-6)]
    _all_checks(rec)


def test_two_plates_laid_one_on_the_other_are_one_shell_through_their_lap():
    # the lap is 6 mm thick and 30 mm wide: too narrow to be a shell on its own, yet it is the two 3 mm plates
    # stacked, so it is shelled with them (SHELL-8) and both plates snap onto its mid-plane with their offsets
    # (SHELL-7): one flat shell, x 0..370, the bolt holes kept
    rec, surface = ms.extract(sf.bolted_lap(3.0, 30.0, 3.0), mm_per_unit=1.0)
    laps = [p for p in rec["pairs"] if p["why"].startswith("a lap")]
    assert [p["thickness_mm"] for p in laps] == [pytest.approx(6.0)]
    assert _sewn_area(surface) == pytest.approx(370 * 100 - 2 * math.pi * 9.0, rel=1e-4)
    offsets = sorted(m.get("offset_mm", 0.0) for m in _shell_mids(rec))
    assert offsets == pytest.approx([-1.5, 0.0, 1.5])
    assert _sewn(rec)["open_edges"] == 0 and not rec["fallback_regions"]
    _all_checks(rec)


def test_a_thick_pad_on_one_side_of_a_plate_is_no_lap():
    # only one face of the pad's pair is a side of a shell plate: a pad, not two plates stacked
    rec, _ = ms.extract(sf.plate_with_boss(), mm_per_unit=1.0)
    assert not [p for p in rec["pairs"] if p["why"].startswith("a lap")]


@pytest.mark.parametrize("make", [lambda: sf.l_angle(5.0, 5.0), lambda: sf.short_angle(3.0, 40.0)])
def test_walls_written_as_b_splines_meet_like_plain_ones(make):
    # every face a B-spline, so every mid-face is an offset surface: OCC will not carry an offset surface past its
    # own ends, so each is re-made as a B-spline first; and the face classifier's tolerance is read in each
    # surface's own parameters (0..1 over the whole wall), so a point well inside a grown band is still inside
    rec, surface = ms.extract(sf.nurbs(make()), mm_per_unit=1.0)
    assert {m["kind"] for m in _shell_mids(rec)} == {"offset"}
    plain, plain_surface = ms.extract(make(), mm_per_unit=1.0)
    assert _sewn_area(surface) == pytest.approx(_sewn_area(plain_surface), rel=1e-4)
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_a_gusset_with_a_weld_relief_ends_inside_the_plates_it_stands_on():
    # the gusset's slanted edges, carried into the base and the upright, end on their mid-faces inside their
    # walls (and one runs across their corner): ends in the junction, not open
    rec, _ = ms.extract(sf.gusseted_angle(), mm_per_unit=1.0)
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["ends_in_wall_edges"] >= 1
    _all_checks(rec)


def test_a_short_legged_angle_holds_its_volume_with_its_corner():
    # the volume check reads the sewn surface (the mid-faces as extended to their junction) times the walls:
    # the mid-faces alone hold only 90% of a 16 mm angle of 3 mm plate
    rec, _ = ms.extract(sf.short_angle(3.0, 16.0), mm_per_unit=1.0)
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.02)
    _all_checks(rec)


def test_a_round_stand_off_is_a_solid_region_not_a_fillet():
    # a cylinder that closes round its axis is never a blend: the boss stays solid, and where the plate's
    # mid-face meets it is a solid link, not an open edge
    rec, _ = ms.extract(sf.standoff_plate(), mm_per_unit=1.0)
    boss = [f for f in rec["faces"] if f["kind"] == "cylinder" and f["role"] == "blend"]
    assert not boss
    assert rec["bodies"][0]["verdict"] == "mixed"
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["solid_link_edges"] >= 1


def test_a_slit_narrower_than_the_open_step_is_a_real_cut():
    # past a 0.5 mm slit in a 3 mm plate the metal starts again 0.9 mm on; the air right past the edge says cut
    rec, _ = ms.extract(sf.slit_plate(3.0, 0.5), mm_per_unit=1.0)
    assert _sewn(rec)["open_edges"] == 0
    _all_checks(rec)


def test_a_general_join_that_loses_mid_faces_is_not_used(monkeypatch):
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)   # the join itself under test, no SHELL-13 strips
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)     # ... and no SHELL-22 strips
    # a join that comes back without its mid-faces has no free edge left to count: it must not win on that
    real = ms.join_body

    def lossy(mine, body_faces, solid, **k):
        real(mine, body_faces, solid, **k)
        return ms._compound([]), [], [], [[] for _ in mine]

    monkeypatch.setattr(ms, "join_body", lossy)
    monkeypatch.setattr(ms, "extend", lambda *a, **k: None)
    rec, _ = ms.extract(sf.t_joint(), mm_per_unit=1.0)
    assert _sewn(rec)["join"] == "straight" and _sewn(rec)["open_edges"] > 0
    assert any("general join lost" in n for n in rec["notes"])


def test_a_tube_running_into_a_plug_is_linked_to_it():
    # the plug's end face inside the tube is narrow, yet it faces the plug's other end across the metal: it is a
    # side of a thick region, so where the tube's mid-face stops on it is a solid link, not an open edge
    rec, _ = ms.extract(sf.plugged_tube(), mm_per_unit=1.0)
    assert not [f for f in rec["faces"] if f["kind"] == "plane" and f["role"] == "blend"]
    assert _sewn(rec)["open_edges"] == 0 and _sewn(rec)["solid_link_edges"] == 2


# ------------------------------------------------------------------------------------------ speed ----
def test_faces_that_cannot_touch_are_sewn_in_separate_groups():
    boxes = [(np.zeros(3), np.ones(3)), (np.array([1.005, 0, 0]), np.array([2.0, 1, 1])),
             (np.array([5.0, 5, 5]), np.array([6.0, 6, 6]))]
    assert ms.touching_groups(boxes, 0.01) == [[0, 1], [2]]
    assert ms.touching_groups(boxes, 0.001) == [[0], [1], [2]]
    assert ms.touching_groups(boxes[::-1], 0.01) == [[0], [1, 2]]
    # two T-joints far apart, sewn in one call: each junction still splits its plate, and nothing is lost
    rec_one, _ = ms.extract(sf.t_joint(), mm_per_unit=1.0)
    two = ms._compound([sf.t_joint(), sf.t_joint().Moved(sf._shift(0, 1000, 0))])
    rec_two, _ = ms.extract(two, mm_per_unit=1.0)
    for row in rec_two["sewn"]:
        assert row["open_edges"] == 0 and row["junction_edges"] == _sewn(rec_one)["junction_edges"]


def test_the_quick_inside_test_agrees_with_the_exact_classifier():
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN, TopAbs_SOLID
    from OCP.TopoDS import TopoDS

    for shape, wall in ((sf.box_section(), 4.0), (sf.nurbs(sf.tube()), 2.0), (sf.bent_bracket(), 2.0)):
        sol = TopoDS.Solid_s(ms._map(shape, TopAbs_SOLID).FindKey(1))
        quick = ms._QuickInside(sol, ms.QUICK_DEFLECTION * wall, 1e-3 * wall)
        assert quick.ok
        exact = BRepClass3d_SolidClassifier(sol)
        lo, hi = ms._box(sol)
        pts = lo + np.random.default_rng(0).random((400, 3)) * (hi - lo)
        told = 0
        for p in pts:
            st = quick.state(p)
            if st is None:
                continue
            told += 1
            exact.Perform(gp_Pnt(*map(float, p)), 1e-3 * wall)
            assert st == (exact.State() == TopAbs_IN), p
        assert told >= 0.8 * len(pts)


def test_an_offset_mid_face_is_remade_as_a_bspline_where_it_lies():
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_BSplineSurface, Geom_OffsetSurface

    top = max(ms._faces(sf.nurbs(sf.flat_plate())), key=ms._area)
    off = ms._faces(ms.half_offset(top, 2.5))[0]
    assert isinstance(BRep_Tool.Surface_s(off), Geom_OffsetSurface)
    g = ms.remade_offset(off, 1e-4)
    assert g is not None and isinstance(BRep_Tool.Surface_s(g), Geom_BSplineSurface)
    assert ms._area(g) == pytest.approx(ms._area(off), rel=1e-6)
    assert ms.remade_offset(ms._faces(sf.flat_plate())[0], 1e-4) is None
    # a closed offset that the re-making cannot hold to the same area (a tube) stays an offset, and shells as before
    outer = max(ms._faces(sf.nurbs(sf.tube())), key=ms._area)
    assert ms.remade_offset(ms._faces(ms.half_offset(outer, 1.0))[0], 1e-4) is None
    rec, _ = ms.extract(sf.nurbs(sf.tube()), mm_per_unit=1.0)
    _all_checks(rec)
    assert [m["kind"] for m in _shell_mids(rec)] == ["offset"]


def test_an_extruded_free_form_wall_is_projected_on_within_its_own_reach():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GeomAPI import GeomAPI_PointsToBSpline
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TColgp import TColgp_Array1OfPnt

    pts = TColgp_Array1OfPnt(1, 5)
    for i, (x, y) in enumerate(((0, 0), (25, 8), (50, 0), (75, -8), (100, 0)), start=1):
        pts.SetValue(i, gp_Pnt(x, y, 0))
    curve = GeomAPI_PointsToBSpline(pts).Curve()
    face = ms._faces(BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeEdge(curve).Edge(), gp_Vec(0, 0, 40)).Shape())[0]
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

    proj = sp._projector(face)
    from OCP.Precision import Precision

    assert any(Precision.IsInfinite_s(x) for x in BRep_Tool.Surface_s(face).Bounds())
    for p in (gp_Pnt(50, 3, 20), gp_Pnt(10, -4, 1), gp_Pnt(90, 6, 39)):
        proj.Perform(p)
        whole = GeomAPI_ProjectPointOnSurf(p, BRep_Tool.Surface_s(face))
        assert proj.NbPoints() >= 1 and proj.LowerDistance() == pytest.approx(whole.LowerDistance(), abs=1e-9)


# ---------------------------------------------------------------------- variable walls (SHELL-9) ----
def _tapered(rec: dict) -> list[dict]:
    return [p for p in rec["pairs"] if p.get("tapered")]


@pytest.mark.parametrize(("t0", "t1", "length"), [(4.0, 8.0, 200.0), (3.0, 12.0, 150.0)])
def test_a_tapered_plate_is_one_shell_carrying_its_thickness_range(t0, t1, length):
    rec, _ = ms.extract(sf.wedge_plate(t0, t1, length), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    (pair,) = _tapered(rec)
    lo, hi = pair["thickness_range_mm"]
    # the wall measured square to the mid-surface: t cos(turn), the turn atan((t1 - t0) / length)
    c = math.cos(math.atan((t1 - t0) / length))
    assert lo == pytest.approx(t0 * c, rel=2e-3) and hi == pytest.approx(t1 * c, rel=2e-3)
    assert pair["shell"] and pair["thickness_mm"] == pytest.approx(hi)
    (mid,) = _shell_mids(rec)
    assert mid["fitted"] and mid["thickness_range_mm"] == pytest.approx([lo, hi], rel=1e-3)
    # the mid-face spans the plate (on the slant midway, trimmed where both faces lie across it: its ends a little
    # short), and holds the plate's metal within 1%
    assert mid["area_mm2"] == pytest.approx(100.0 * math.hypot(length, 0.5 * (t1 - t0)), rel=5e-3)
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.01)


def test_a_tapered_wall_carries_readings_of_its_wall_over_its_mid_face():
    # SHELL-9b: the mesh gives every node its own wall from these; on a wedge each reading is the wall there
    t0, t1, length = 4.0, 8.0, 200.0
    rec, _ = ms.extract(sf.wedge_plate(t0, t1, length), mm_per_unit=1.0)
    (mid,) = _shell_mids(rec)
    rows = np.asarray(mid["wall_samples_mm"])
    assert len(rows) >= 50
    c = math.cos(math.atan((t1 - t0) / length))
    # the wall square to the mid-surface at x: (t0 + (t1 - t0) x / length) cos(turn), x read at the foot below
    want = (t0 + (t1 - t0) * rows[:, 0] / length) * c
    assert rows[:, 3] == pytest.approx(want, abs=0.02)
    # an even wall carries none
    flat, _ = ms.extract(sf.flat_plate(), mm_per_unit=1.0)
    assert not any("wall_samples_mm" in m for m in flat["mids"])


def test_a_wall_turned_past_the_taper_limit_stays_a_variable_wall():
    # 3 to 40 mm over 100 mm: the faces turn 20 degrees - two sides of a solid wedge, not one wall
    rec, _ = ms.extract(sf.wedge_plate(3.0, 40.0, 100.0), mm_per_unit=1.0)
    assert not _tapered(rec) and not _shell_mids(rec)
    assert rec["bodies"][0]["verdict"] == "solid"


def test_a_drafted_tube_is_one_closed_shell_round_its_seam():
    rec, _ = ms.extract(sf.drafted_tube(), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    (pair,) = _tapered(rec)
    assert pair["thickness_range_mm"] == pytest.approx([2.5, 4.0], rel=2e-3)
    assert _sewn(rec)["open_edges"] == 0


def test_a_drafted_cyclone_cylinder_runs_into_its_uneven_cone():
    rec, _ = ms.extract(sf.drafted_cyclone(), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert len(_tapered(rec)) == 2 and all(m["fitted"] for m in _shell_mids(rec))
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("flats", [0.0, 30.0])
def test_a_twisted_strap_is_one_shell_through_its_twist(flats):
    rec, _ = ms.extract(sf.twisted_strap(flats=flats), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert any(m.get("fitted") for m in _shell_mids(rec))
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.01)


def test_a_fitted_mid_face_lies_midway_and_reads_the_wall_on_both_sides():
    shape = sf.wedge_plate(4.0, 8.0)
    faces = ms._faces(shape)
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    (pair,) = _tapered(rec)
    a, b = (int(n.split("_")[1]) - 1 for n in pair["faces"])
    got, lo, hi, mean, why = ms.fitted_mid(faces[a], faces[b], 8.0, 8e-4)
    assert got and not why
    _worst, rel, n, flo, fhi = ms.fitted_fidelity(got[0], faces[a], faces[b])
    assert n >= 9 and rel <= 1e-3 and lo <= flo <= fhi <= hi
    assert mean == pytest.approx(6.0, rel=0.01)


def test_variable_walls_read_the_same_twice():
    shape = sf.drafted_cyclone()
    a, _ = ms.extract(shape, mm_per_unit=1.0)
    b, _ = ms.extract(shape, mm_per_unit=1.0)
    a.pop("seconds")
    b.pop("seconds")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# -------------------------------------------------------- bends not concentric (SHELL-10, bridged) ----
@pytest.mark.parametrize("make", [lambda: sf.rounded_tube(), lambda: sf.bent_bracket(t=1.0, r=5.0, outer_r=5.0),
                                  lambda: sf.rounded_tube(t=1.5, r=10.0)])
def test_a_bend_whose_outside_is_not_concentric_sews_to_its_flanges(make, monkeypatch):
    # the bend's faces are not an even pair: its fitted mid-face ends where they stop facing each other, short of
    # the flanges' mid-planes running on almost tangent to it; a ruled strip carries it to them. The general join
    # sometimes closes these too (not on a fan housing's 1 mm duct, sh0021): the straight join must on its own
    def no_general(*_a, **_k):
        raise RuntimeError("the general join is off in this test")

    monkeypatch.setattr(ms, "_join_general", no_general)
    rec, _ = ms.extract(make(), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert any(m.get("fitted") for m in _shell_mids(rec))
    assert _sewn(rec)["open_edges"] == 0
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.02)


def _sewn_faces(surface) -> list:
    return ms._faces(surface)


@pytest.mark.parametrize(("ratio", "angle"), [(1.0, 90.0), (0.5, 45.0)])
def test_a_pipe_branch_is_joined_on_its_saddle(ratio, angle):
    # SHELL-14: an equal tee and a 45 degree lateral: both mid-cylinders joined on their saddle, every check met
    rec, surface = ms.extract(sf.pipe_branch(ratio=ratio, angle_deg=angle), mm_per_unit=1.0)
    _all_checks(rec)
    assert rec["bodies"][0]["verdict"] == "shell"
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.01)
    # each mid-cylinder's grown patch is one face: no band edge left a wall's width from the junction
    assert len(_sewn_faces(surface)) <= 12


def test_grown_along_its_open_direction_a_tube_is_one_face():
    # SHELL-14: a tube grown past its ends is one face, not its own face and two bands
    shape = sf.tube(25.0, 2.0, 100.0)
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    mid = rec["mids"][0]
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    face = ms._faces(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 24.0, 100.0).Shape())[0]
    grown = ms.grow(face, 5.0)
    assert len(ms._faces(grown)) == 1
    assert ms._area(grown) == pytest.approx(2 * math.pi * 24.0 * 110.0, rel=1e-6)
    assert mid["kind"] == "cylinder"


@pytest.mark.parametrize("part", ["l_angle", "tube", "t_joint"])
def test_an_iges_copy_reads_as_its_step(part, tmp_path):
    # SHELL-15: an IGES file of trimmed surfaces (no topology) is sewn at a measured tolerance into the same solid:
    # the same verdicts, walls and open edges as the STEP
    shape = getattr(sf, part)()
    step = ms.extract_file(sf.write_step(shape, tmp_path / "p.step"), tmp_path / "s", mm_per_unit=1.0)
    iges = ms.extract_file(sf.write_iges(shape, tmp_path / "p.igs"), tmp_path / "i", mm_per_unit=1.0)
    assert [b["verdict"] for b in iges["bodies"]] == [b["verdict"] for b in step["bodies"]]
    walls = sorted({round(p["thickness_mm"], 4) for p in step["pairs"] if p["shell"]})
    assert sorted({round(p["thickness_mm"], 4) for p in iges["pairs"] if p["shell"]}) == walls
    assert iges["checks"] == step["checks"]
    assert any("SHELL-15" in n for n in iges["notes"])


def test_iges_sewing_never_reaches_across_a_wall(tmp_path):
    # SHELL-15: the tolerance is capped at a tenth of the narrowest face (a 2 mm plate's rim): its two sides stay apart
    shape = sf.flat_plate(2.0)
    rec = ms.extract_file(sf.write_iges(shape, tmp_path / "p.igs"), tmp_path / "i", mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    (note,) = [n for n in rec["notes"] if "SHELL-15" in n]
    tol = float(note.split("measured ")[1].split(" mm")[0])
    assert tol <= 0.2


def test_touching_iges_bodies_stay_apart(tmp_path):
    # SHELL-15b: two plates fused face to face in a STEP are one body; two separate touching boxes written as IGES
    # come back as two solids, not one shell sewn across them
    from tests.fea_fixtures import _box

    a, b = _box(0, 0, 0, 100, 60, 3), _box(100, 0, 0, 100, 60, 3)
    comp = ms._compound([a, b])
    shape, notes = ms.solidify(ms._compound(ms._faces(comp)), 1.0)
    from OCP.TopAbs import TopAbs_SOLID

    assert ms._map(shape, TopAbs_SOLID).Extent() == 2
    assert sorted(round(ms._volume(s), 3) for s in (ms._map(shape, TopAbs_SOLID).FindKey(i) for i in (1, 2))) == \
        [18000.0, 18000.0]


def test_a_wall_split_into_strips_is_as_wide_as_the_wall():
    # SHELL-2b: faces that share an edge are one region: the shared edge is not their outline. A 120 x 60 x 2 box's
    # top and front faces (sharing a 120 mm edge) read 60 wide as one region; added up apart, 49
    from tests.fea_fixtures import _box

    box = _box(0, 0, 0, 120, 60, 2)
    top = [f for f in ms._faces(box) if ms._box(f)[0][2] > 1.999]
    front = [f for f in ms._faces(box) if ms._box(f)[1][1] < 1e-3]
    assert len(top) == 1 and len(front) == 1
    assert ms.union_width(top + front) == pytest.approx(60.0, rel=1e-6)
    assert ms.width_of(ms._compound(top + front)) < 50.0


def test_touching_iges_bodies_each_own_their_faces(tmp_path):
    # SHELL-15d/15e: a 2 mm plate with a 10 mm layer on it (a duct wall and its insulation), two bodies touching face
    # to face. Read from IGES, the volume maker makes their shared side one face of both; each body must own its own,
    # as in the STEP, or the plate loses a side and its wall (and the vote on which volumes are material)
    from tests.fea_fixtures import _box

    comp = ms._compound([_box(0, 0, 0, 100, 60, 2), _box(0, 0, 2, 100, 60, 10)])
    step = ms.extract_file(sf.write_step(comp, tmp_path / "p.step"), tmp_path / "s", mm_per_unit=1.0)
    iges = ms.extract_file(sf.write_iges(comp, tmp_path / "p.igs"), tmp_path / "i", mm_per_unit=1.0)
    assert sorted(b["verdict"] for b in iges["bodies"]) == sorted(b["verdict"] for b in step["bodies"])
    assert len(iges["faces"]) == len(step["faces"]) == 12
    walls = sorted({round(p["thickness_mm"], 3) for p in step["pairs"] if p["shell"]})
    # (SHELL-23: the 10 mm layer is a plate too, 6 walls wide)
    assert sorted({round(p["thickness_mm"], 3) for p in iges["pairs"] if p["shell"]}) == walls == [2.0, 10.0]


def test_an_extraction_past_its_work_budget_fails_fast_in_words(tmp_path):
    # SHELL-18: the stages check the clock; a part out of budget is meshed solid, told why in numbers
    path = sf.write_step(sf.tube_on_plate(), tmp_path / "p.step")
    rec = ms.extract_file(path, tmp_path / "o", mm_per_unit=1.0, budget_s=1e-6)
    assert [b["verdict"] for b in rec["bodies"]] == ["solid"]
    assert "work budget" in rec["bodies"][0]["why"] and rec["surface_faces"] == []
    assert json.loads((tmp_path / "o" / ms.RECORD_FILE).read_text())["bodies"][0]["why"] == rec["bodies"][0]["why"]


def test_a_general_join_gets_only_what_is_left_of_the_budget(monkeypatch):
    # SHELL-18: with under JOIN_MIN_S left after the reserve, no general join starts; the straight join stands
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)     # ... and no SHELL-22 strips
    monkeypatch.setattr(ms, "JOIN_RESERVE_S", 1e9)
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert rec["sewn"][0]["join"] == "straight"
    assert any("SHELL-18" in n for n in rec["notes"])


def test_an_extraction_past_its_deadline_is_stopped_and_the_part_meshed_solid(monkeypatch, tmp_path):
    # SHELL-18: one OpenCASCADE call cannot be stopped from inside: the whole extraction runs in a child process,
    # stopped at the deadline; the record says so and the solid mesh is delivered
    import time as _time

    seen = {}

    def stopped(entry, kwargs, deadline_s):
        seen.update(entry=entry, deadline=deadline_s, **kwargs)
        return "timeout", None

    real = ms.run_bounded
    monkeypatch.setattr(ms, "run_bounded", stopped)
    path = sf.write_step(sf.flat_plate(2.0), tmp_path / "p.step")
    rec = ms.extract_file(path, tmp_path / "o", mm_per_unit=1.0, deadline_s=1.0)
    assert seen["entry"] == ms.EXTRACT_ENTRY and seen["deadline"] == 1.0 and seen["path"] == str(path)
    assert rec["bodies"][0]["verdict"] == "solid" and "ran past 1 s" in rec["bodies"][0]["why"]
    assert (tmp_path / "o" / ms.RECORD_FILE).exists() and (tmp_path / "o" / ms.SURFACE_FILE).exists()
    t0 = _time.time()
    assert real("tests.shell_fixtures:sleep_for", {"seconds": 60.0}, 1.0) == ("timeout", None)
    assert _time.time() - t0 < 30.0
    status, why = real("tests.shell_fixtures:sleep_for", {"seconds": -1.0}, 60.0)
    assert status == "error" and why.startswith("ValueError")


def test_a_stopped_extraction_takes_its_general_join_with_it(tmp_path):
    # SHELL-18b: the extraction stopped at its deadline while a general join (its own fork) runs: the join is
    # stopped too, not left running orphaned
    import time as _time

    pid_file = tmp_path / "join.pid"
    assert ms.run_bounded("tests.shell_fixtures:fork_and_sleep", {"pid_file": str(pid_file)}, 5.0) == ("timeout", None)
    pid = int(pid_file.read_text())
    gone = False
    for _ in range(50):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                gone = fh.read().split(")")[-1].split()[0] == "Z"
        except OSError:
            gone = True
        if gone:
            break
        _time.sleep(0.1)
    assert gone


def test_the_bounded_extraction_runs_after_a_parallel_mesh_in_the_same_process(tmp_path):
    # SHELL-18d: a fork of a process whose OpenCASCADE thread pool had started (a parallel mesh) hung a 2 s extraction
    # until its deadline: the bounded extraction runs in a new process, not a fork
    import time as _time

    from meshpipeline.cad.occ_box import incremental_mesh

    incremental_mesh(sf.tube_on_plate(), 0.5, 0.3, relative=False, parallel=True)
    path = sf.write_step(sf.flat_plate(2.0), tmp_path / "p.step")
    t0 = _time.time()
    rec = ms.extract_file(path, tmp_path / "o", mm_per_unit=1.0, deadline_s=120.0)
    assert _time.time() - t0 < 60.0
    assert rec["bodies"][0]["verdict"] == "shell"


def test_the_bounded_extraction_writes_what_the_unbounded_one_does(tmp_path):
    # the child runs the general join in a child of its own (it must not be a daemon, which may have none)
    path = sf.write_step(sf.tube_on_plate(), tmp_path / "p.step")
    a = ms.extract_file(path, tmp_path / "a", mm_per_unit=1.0)
    b = ms.extract_file(path, tmp_path / "b", mm_per_unit=1.0, deadline_s=0.0)
    assert a["sewn"][0]["join"] == "general"
    for r in (a, b):
        r.pop("seconds", None)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert (tmp_path / "a" / ms.SURFACE_FILE).read_bytes() == (tmp_path / "b" / ms.SURFACE_FILE).read_bytes()


def test_an_iges_box_with_a_sealed_cavity_is_one_body(tmp_path):
    # SHELL-15h: two closed shells, one inside the other, are one solid with a void (as in the STEP), not a box and a
    # solid block the size of the cavity
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from tests.fea_fixtures import _box

    part = BRepAlgoAPI_Cut(_box(0, 0, 0, 100, 60, 40), _box(2, 2, 2, 96, 56, 36)).Shape()
    step = ms.extract_file(sf.write_step(part, tmp_path / "p.step"), tmp_path / "s", mm_per_unit=1.0)
    iges = ms.extract_file(sf.write_iges(part, tmp_path / "p.igs"), tmp_path / "i", mm_per_unit=1.0)
    assert len(iges["bodies"]) == len(step["bodies"]) == 1
    assert iges["bodies"][0]["volume_mm3"] == pytest.approx(step["bodies"][0]["volume_mm3"], rel=1e-6)
    assert iges["bodies"][0]["verdict"] == step["bodies"][0]["verdict"] == "shell"


def test_an_iges_dome_reads_as_its_step(tmp_path):
    # SHELL-15g: a sphere written as a surface of revolution is re-made as a sphere the right way round (or kept as
    # written when the re-made face would not mesh): the same verdict and wall as the STEP
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere
    from OCP.gp import gp_Pnt

    ball = BRepAlgoAPI_Cut(BRepPrimAPI_MakeSphere(60.0).Shape(), BRepPrimAPI_MakeSphere(58.0).Shape()).Shape()
    dome = BRepAlgoAPI_Cut(ball, BRepPrimAPI_MakeBox(gp_Pnt(-100, -100, -100), 200, 200, 100).Shape()).Shape()
    step = ms.extract_file(sf.write_step(dome, tmp_path / "p.step"), tmp_path / "s", mm_per_unit=1.0)
    iges = ms.extract_file(sf.write_iges(dome, tmp_path / "p.igs"), tmp_path / "i", mm_per_unit=1.0)
    assert [b["verdict"] for b in iges["bodies"]] == [b["verdict"] for b in step["bodies"]] == ["shell"]
    assert sorted({round(p["thickness_mm"], 3) for p in iges["pairs"] if p["shell"]}) == [2.0]


def test_a_seam_written_as_two_edges_is_split_and_sewn():
    # SHELL-15f: a closed face whose seam is two separate edges of its wire: sewing leaves both free; the face split
    # across its closed direction sews shut
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 20.0, 50.0).Shape()
    side = [f for f in ms._faces(cyl) if "Cylindrical" in type(BRep_Tool.Surface_s(f)).__name__][0]
    wire = BRepBuilderAPI_MakeWire()
    ex = TopExp_Explorer(ms._outer_wire(side), TopAbs_EDGE)
    while ex.More():                                       # every edge made anew from its curve: the seam twice
        e = TopoDS.Edge_s(ex.Current())
        ex.Next()
        ad = BRepAdaptor_Curve(e)
        ne = BRepBuilderAPI_MakeEdge(ad.Curve().Curve(), ad.FirstParameter(), ad.LastParameter()).Edge()
        wire.Add(TopoDS.Edge_s(ne.Oriented(e.Orientation())))
    from OCP.ShapeFix import ShapeFix_Face

    fix = ShapeFix_Face(BRepBuilderAPI_MakeFace(BRep_Tool.Surface_s(side), wire.Wire(), True).Face())
    fix.Perform()                                          # its curves on the surface, as an IGES reader gives them
    face = fix.Face()
    em = ms._map(face, TopAbs_EDGE)
    seam = [e for e in (TopoDS.Edge_s(em.FindKey(i)) for i in range(1, em.Extent() + 1)) if abs(float(np.ptp([ms._ends(e)[0][2], ms._ends(e)[1][2]])) - 50.0) < 1e-6]
    assert len(seam) == 2                                  # the seam, twice: each a free edge of the one face
    split = ms._split_self_seams(ms._compound([face]), seam, 1e-4)
    assert split is not None and len(split) == 2
    assert sum(ms._area(f) for f in split) == pytest.approx(ms._area(face), rel=1e-9)
    caps = [f for f in ms._faces(cyl) if f is not side and "Plane" in type(BRep_Tool.Surface_s(f)).__name__]
    assert not ms._free_edge_list(ms._sew(split + caps, 1e-4))


def test_iges_bodies_lying_on_each_other_along_edges_are_told_apart(tmp_path):
    # SHELL-15i: a frame whose short tube lies on a longer one over its length (both from x = 0): sewing pairs their
    # edges as the faces come (in one order one valid solid made of both tubes' faces). Told apart by the angles
    # round each edge, the two tubes come back as two solids of their own volumes in any face order, as in the STEP
    from OCP.TopAbs import TopAbs_SOLID

    long_, short = sf.box_section(60, 40, 3, 300), sf.box_section(60, 40, 3, 100)
    comp = ms._compound([long_, short])
    want = sorted(round(ms._volume(s), 1) for s in (long_, short))
    faces = ms._faces(comp)
    for order in (faces, faces[::-1], faces[0::2] + faces[1::2]):
        shape, notes = ms.solidify(ms._compound(order), 1.0)
        sols = ms._map(shape, TopAbs_SOLID)
        assert sols.Extent() == 2 and any("SHELL-15i" in n for n in notes)
        assert sorted(round(ms._volume(sols.FindKey(i)), 1) for i in (1, 2)) == want
    step = ms.extract_file(sf.write_step(comp, tmp_path / "p.step"), tmp_path / "s", mm_per_unit=1.0)
    iges = ms.extract_file(sf.write_iges(comp, tmp_path / "p.igs"), tmp_path / "i", mm_per_unit=1.0)
    assert sorted(b["verdict"] for b in iges["bodies"]) == sorted(b["verdict"] for b in step["bodies"])
    assert sorted(round(b["volume_mm3"]) for b in iges["bodies"]) == sorted(round(b["volume_mm3"]) for b in step["bodies"])


def test_bodies_touching_face_to_face_are_not_read_by_angle():
    # SHELL-15i reads only edges where each face leaves at its own angle, or two pairs lie on each other; boxes side by
    # side (a shared face) are left to the volume maker (SHELL-15b)
    from tests.fea_fixtures import _box

    loose = ms._faces(ms._compound([_box(0, 0, 0, 100, 60, 3), _box(100, 0, 0, 100, 60, 3)]))
    assert ms._solids_by_angle(loose, 1e-4, 0.1) is None


# ---- SHELL-21, SHELL-22: what lies past an edge, and junctions the joins left open -------------------------------
def _no_general(monkeypatch):
    monkeypatch.setattr(ms, "_join_general", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))


@pytest.mark.parametrize("t2", [1.98, 1.7, 1.2])
def test_a_tube_whose_wall_steps_is_one_shell_across_the_step(t2, monkeypatch):
    # SHELL-22: two mid-cylinders in line, half the step apart - from far inside a hundredth of a wall to a fifth
    rec, _ = ms.extract(sf.stepped_tube(20.0, 2.0, t2, 200.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    assert _sewn(rec)["open_edges"] == 0 and not rec["fallback_regions"]
    assert any("SHELL-22" in n for n in rec["notes"])
    _all_checks(rec)
    monkeypatch.setattr(ms, "CLOSE_GAPS", False)
    before, _ = ms.extract(sf.stepped_tube(20.0, 2.0, t2, 200.0), mm_per_unit=1.0)
    assert _sewn(before)["open_edges"] == 2


def test_a_t_joint_no_join_reached_is_closed_edge_by_edge(monkeypatch):
    # SHELL-22: with neither join allowed to extend and no SHELL-13 strip, the web is carried down to the plate's
    # mid-plane and the plate's two pieces across to the web's - the surface the straight extension makes
    monkeypatch.setattr(ms, "extend", lambda *a, **k: None)
    _no_general(monkeypatch)
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)
    tp, ts = 5.0, 4.0
    rec, surface = ms.extract(sf.t_joint(tp, ts), mm_per_unit=1.0)
    row = _sewn(rec)
    assert row["open_edges"] == 0 and row["junction_edges"] >= 1
    assert any("SHELL-22" in n for n in rec["notes"])
    # the whole 200 x 100 mid-plane and the web from z = tp / 2 to its top, as test_a_t_joint_is_sewn_on_one_line
    assert _sewn_area(surface) == pytest.approx(200 * 100 + (60 + tp / 2) * 100, rel=REL)


@pytest.mark.parametrize("name", ["tube_on_plate", "l_angle"])
def test_junctions_of_other_kinds_no_join_reached_are_closed_edge_by_edge(name, monkeypatch):
    # SHELL-22: a cylinder's end circle carried along its axis to the plate; an L's two legs each to the other's plane
    monkeypatch.setattr(ms, "extend", lambda *a, **k: None)
    _no_general(monkeypatch)
    monkeypatch.setattr(ms, "CLOSE_STRIPS", False)
    rec, _ = ms.extract(getattr(sf, name)(), mm_per_unit=1.0)
    assert _sewn(rec)["open_edges"] == 0
    assert any("SHELL-22" in n for n in rec["notes"])


@pytest.mark.parametrize("name", ["knife_plate", "round_ended_plate"])
def test_a_plate_bevelled_or_rounded_at_its_end_has_a_free_edge_there(name, monkeypatch):
    # SHELL-21: the plate's two faces stop facing each other before the plate ends; with no join to run the mid-face
    # on to the tip, the metal past its edge is the plate's own end - a real free edge, not a gap
    _no_general(monkeypatch)
    rec, _ = ms.extract(getattr(sf, name)(), mm_per_unit=1.0)
    row = _sewn(rec)
    assert row["open_edges"] == 0 and row["rim_edges"] == 1 and row["boundary_edges"] == 4
    assert not rec["fallback_regions"]
    monkeypatch.setattr(ms, "PAST_EDGE", False)
    before, _ = ms.extract(getattr(sf, name)(), mm_per_unit=1.0)
    assert _sewn(before)["open_edges"] == 1


def _quad(*pts):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    poly = BRepBuilderAPI_MakePolygon()
    for p in pts:
        poly.Add(gp_Pnt(*map(float, p)))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire()).Face()


def test_an_edge_running_into_thicker_metal_is_a_solid_link_in_a_mixed_body_only():
    # SHELL-21, read on the part itself: a plate's mid-face ending where a block starts, with none of the block's
    # faces handed to the census (as when its faces beside the edge are chamfers named rim or blend)
    t = 2.0
    body = sf.plate_into_block(t)
    mid = _quad((0, 0, t / 2), (170, 0, t / 2), (170, 100, t / 2), (0, 100, t / 2))
    solid = ms._Solid(body, 1e-3 * t, wall=t)
    mixed = ms.edge_census(ms._compound([mid]), {1: t}, solid, scale=1.0, mixed=True)
    assert mixed["open_edges"] == 0 and mixed["solid_link_edges"] == 1 and mixed["bulk_link_edges"] == 1
    assert mixed["boundary_edges"] == 3
    # a body with no thick region has nothing to link to: the same edge stays open
    shell = ms.edge_census(ms._compound([mid]), {1: t}, solid, scale=1.0, mixed=False)
    assert shell["open_edges"] == 1 and shell["solid_link_edges"] == 0
    # ... and the join is still judged by it (a join that meets the walls themselves is preferred)
    assert ms._unjoined(mixed) == 1 and ms._unjoined(shell) == 1


def test_an_edge_with_a_shell_wall_ahead_stays_open_whatever_the_body():
    # SHELL-21 never takes a junction the join did not close for a link or a free edge: a web stopped at its
    # plate's face, the plate's mid-face half a plate further on
    tp, ts = 5.0, 4.0
    body = sf.t_joint(tp, ts)
    plate = _quad((0, 0, tp / 2), (200, 0, tp / 2), (200, 100, tp / 2), (0, 100, tp / 2))
    web = _quad((100, 0, tp), (100, 100, tp), (100, 100, tp + 60), (100, 0, tp + 60))
    solid = ms._Solid(body, 1e-3 * ts, wall=ts)
    for mixed in (True, False):
        row = ms.edge_census(ms._compound([plate, web]), {1: tp, 2: ts}, solid, scale=1.0, mixed=mixed)
        assert row["open_edges"] == 1 and row["solid_link_edges"] == 0 and row["rim_edges"] == 0


def test_the_closed_junctions_are_the_same_twice(tmp_path):
    for k, shape in enumerate((sf.stepped_tube(20.0, 2.0, 1.7, 200.0), sf.round_ended_plate())):
        path = sf.write_step(shape, tmp_path / f"part{k}.step")
        out = []
        for run in range(2):
            rec = ms.extract_file(path, tmp_path / f"p{k}r{run}", mm_per_unit=1.0)
            rec.pop("seconds")
            out.append((json.dumps(rec, sort_keys=True), (tmp_path / f"p{k}r{run}" / ms.SURFACE_FILE).read_bytes()))
        assert out[0] == out[1]
        assert sum(s["open_edges"] for s in json.loads(out[0][0])["sewn"]) == 0


# ------------------------------------------------------- every plate is a shell (SHELL-23), flat flanges (SHELL-24) ----
def _box_of(surface) -> tuple[list[float], list[float]]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(surface, box, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return [x0, y0, z0], [x1, y1, z1]


def test_a_gusset_plate_under_ten_walls_across_is_a_shell_with_one_line_of_advice():
    # SHELL-23: a 12.7 mm gusset whose face spans 96.5 mm is 7.6 walls across. The user asked for a shell: it is one,
    # at its own thickness, and one plain line says shell theory is less exact that short
    rec, surface = ms.extract(sf.gusset(7.6, 12.7), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "shell" and body["mid_volume_ratio"] == pytest.approx(1.0, abs=1e-4)
    (mid,) = _shell_mids(rec)
    assert mid["thickness_mm"] == pytest.approx(12.7, rel=REL)
    assert mid["area_mm2"] == pytest.approx((7.6 * 12.7) ** 2, rel=REL)
    assert {f["role"] for f in rec["faces"]} == {sp.PAIRED, sp.RIM}
    _all_checks(rec)
    assert rec["short_walls"] == [{"body": "Part_1", "sheet": "S1", "thickness_mm": 12.7,
                                   "span_mm": pytest.approx(96.52, rel=REL), "width_mm": pytest.approx(79.96, rel=1e-3),
                                   "span_walls": 7.6, "width_walls": 6.3}]
    assert rec["advice"] == ("Part_1: 12.7 mm thick over 96.5 mm, 7.6 times its thickness. Shell theory is less exact "
                             "under 10 times" + _FREE_PLATE)


@pytest.mark.parametrize("ratio", [3.0, 5.0, 9.0, 12.0])
def test_a_plate_of_any_ratio_is_a_shell(ratio):
    rec, _ = ms.extract(sf.gusset(ratio, 10.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    assert [m["thickness_mm"] for m in _shell_mids(rec)] == [pytest.approx(10.0, rel=REL)]
    # only a plate under SHELL-2's ten walls gets the advice
    assert bool(rec["advice"]) == (ratio < 10.0) and len(rec["short_walls"]) == (1 if ratio < 10.0 else 0)
    _all_checks(rec)


def test_a_plate_stops_being_one_under_two_walls_wide():
    # the measured line: twice as wide as thick. A 12.7 mm flat bar 25.4 mm wide is a shell; 19 mm wide it is a bar
    wide, _ = ms.extract(sf.flat_bar(2.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in wide["bodies"]] == ["shell"] and len(_shell_mids(wide)) == 1
    bar, surface = ms.extract(sf.flat_bar(1.5), mm_per_unit=1.0)
    (body,) = bar["bodies"]
    assert body["verdict"] == "solid" and bar["surface_faces"] == [] and bar["advice"] == ""
    assert body["why"].endswith("is 12.7 mm thick but only 19.1 mm wide, 1.5 times its thickness; under 2 times, it is "
                                "a bar or a block, not a plate")


def test_a_narrow_strip_is_told_by_its_width():
    # 5 mm thick, 15 mm wide, 400 long: 15 walls across by its area, 3 walls wide - the advice names the width
    rec, _ = ms.extract(sf.flat_bar(3.0, 5.0, 400.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    assert rec["advice"] == ("Part_1: 5 mm thick and 15 mm wide, 3 times its thickness. Shell theory is less exact "
                             "under 4 times (about 2% off on a free strip this narrow, more where it bends from a joint).")


def test_a_thick_bush_is_no_shell():
    # a wall 18 mm thick round a 21 mm radius: wide enough by its numbers, but a thick ring no shell stands for
    rec, _ = ms.extract(sf.bush(length=60.0), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "solid" and body["closest_wall"]["why"] == "ring"
    assert "18 mm thick round a tight bore, a thick ring" in body["why"]
    # ... while a tube whose radius is over twice its wall is one, however short
    tube, _ = ms.extract(sf.tube(25.0, 8.0, 40.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in tube["bodies"]] == ["shell"]


def test_two_faces_across_a_gap_are_no_wall():
    # a shallow channel's two flanges look at each other across its open side: 40 mm apart and 150 mm wide, but
    # only the web's 30 mm is metal between them
    rec, _ = ms.extract(sf.wide_hat(), mm_per_unit=1.0)
    across = [p for p in rec["pairs"] if p["thickness_mm"] == pytest.approx(40.0)]
    assert across and not any(p["shell"] for p in across)
    assert all(p["why"] == "the two faces look at each other across a gap, not across metal" for p in across if p["mids"])
    assert sorted({round(p["thickness_mm"], 3) for p in _shell_pairs(rec)}) == [12.0]       # its two flanges


def test_a_pad_on_a_plate_stays_solid():
    # a 60 x 60 pad 10 mm high on a 4 mm plate is 4 walls wide, but it stands on the plate: the plate is the shell,
    # the pad a thick region of it, as before
    rec, _ = ms.extract(sf.plate_with_low_pad(), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "mixed" and len(body["solid_regions"]) == 1
    assert sorted({round(p["thickness_mm"], 3) for p in _shell_pairs(rec)}) == [4.0]
    (pad,) = [p for p in rec["pairs"] if p["thickness_mm"] == pytest.approx(14.0)]
    assert not pad["shell"] and pad["why"].startswith("a pad on a thinner wall")
    assert rec["advice"] == ""


def test_a_rolled_channels_tapered_flange_is_one_flat_plate_at_its_table_thickness():
    # SHELL-24: a C15 x 50 (flanges 16.5 mm by the table, 10.1 at the toe to 22.9 at the web, a 12.7 mm root fillet
    # and a 6.1 mm toe round). Each flange is ONE flat plate, 16.5 mm thick, half of that inside the flange's flat
    # outside, from the web's mid-plane to the toe; the web between them; all three sewn
    d, bf, tw, tf, length = 381.0, 94.5, 18.2, 16.5, 600.0
    rec, surface = ms.extract(sf.rolled_channel(), mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "shell" and body["mid_volume_ratio"] == pytest.approx(1.0, abs=0.01)
    mids = _shell_mids(rec)
    flanges = [m for m in mids if m.get("flat")]
    assert len(mids) == 3 and len(flanges) == 2
    half = (bf - tw) / 2.0 / 6.0
    for m in flanges:
        assert m["thickness_mm"] == pytest.approx(tf, rel=1e-6)                 # within 2% of the table: exact
        assert m["taper_mm"] == pytest.approx([tf - half, tf + half], rel=1e-4)
        assert "thickness_range_mm" not in m and "wall_samples_mm" not in m      # meshed at one wall
        assert m["fidelity"]["max_error_mm"] <= 1e-9 and m["kind"] == "plane"
    (web,) = [m for m in mids if not m.get("flat")]
    assert web["thickness_mm"] == pytest.approx(tw, rel=REL)
    sewn = _sewn(rec)
    assert sewn["open_edges"] == 0 and sewn["shared_edges"] == 2 and sewn["faces"] == 3
    lo, hi = _box_of(surface)
    assert lo == pytest.approx([tw / 2, tf / 2, 0.0], abs=1e-6)                  # the flanges' planes, the web's
    assert hi == pytest.approx([bf, d - tf / 2, length], abs=1e-6)              # ... and out to the toe
    assert _sewn_area(surface) == pytest.approx(2 * (bf - tw / 2) * length + (d - tf) * length, rel=1e-6)
    assert {f["role"] for f in rec["faces"]} == {sp.PAIRED, sp.RIM, sp.BLEND}
    _all_checks(rec)
    assert rec["advice"] == ""                                                  # its sheet is thin: nothing to say


def test_a_rolled_beams_four_tapered_half_flanges_meet_its_web():
    # an S12 x 50: each flange is two tapered halves over one flat outside face; one flat plate each side of the
    # web, 16.7 mm, and the web meets them on one line (three faces an edge)
    d, bf, tw, tf, length = 304.8, 139.2, 17.4, 16.7, 600.0
    rec, surface = ms.extract(sf.rolled_i(), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    mids = _shell_mids(rec)
    assert sorted(round(m["thickness_mm"], 4) for m in mids) == [16.7, 16.7, 16.7, 16.7, 17.4]
    assert sum(1 for m in mids if m.get("flat")) == 4
    sewn = _sewn(rec)
    assert sewn["open_edges"] == 0 and sewn["junction_edges"] == 2
    assert _sewn_area(surface) == pytest.approx(2 * bf * length + (d - tf) * length, rel=1e-6)
    _all_checks(rec)


def test_a_long_wedge_keeps_its_true_wall_and_a_short_one_is_flat():
    # SHELL-24 is for walls short along their taper (a flange). 4 to 8 mm over 200 mm is 33 walls long: it bends
    # along its taper, and keeps SHELL-9's fitted mid-surface with its wall at every point
    long_rec, _ = ms.extract(sf.wedge_plate(4.0, 8.0, 200.0), mm_per_unit=1.0)
    (mid,) = _shell_mids(long_rec)
    assert mid.get("fitted") and not mid.get("flat") and mid["thickness_range_mm"] == pytest.approx([4.0, 8.0], rel=1e-3)
    # 4 to 6 mm over 40 mm is 8 walls long: one flat plate at its mean
    short_rec, _ = ms.extract(sf.wedge_plate(4.0, 6.0, 40.0), mm_per_unit=1.0)
    (mid,) = _shell_mids(short_rec)
    assert mid.get("flat") and not mid.get("fitted")
    assert mid["thickness_mm"] == pytest.approx(5.0, rel=5e-3) and mid["taper_mm"] == pytest.approx([4.0, 6.0], rel=5e-3)
    _all_checks(short_rec)


def test_a_welded_plate_pedestal_is_all_shell():
    # a base plate, a pipe column, four gussets (7.6 walls across), a top plate and a rolled channel, as separate
    # bodies: every one a shell, none left solid, no open edge inside a body, the plates at their own thickness
    rec, _ = ms.extract(sf.welded_pedestal(), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"] * 8
    assert sum(s["open_edges"] for s in rec["sewn"]) == 0
    walls = {b: sorted({round(m["thickness_mm"], 3) for m in _shell_mids(rec) if m["body"] == b})
             for b in (r["body"] for r in rec["bodies"])}
    assert walls == {"Part_1": [25.4], "Part_2": [9.5], "Part_3": [12.7], "Part_4": [12.7], "Part_5": [12.7],
                     "Part_6": [12.7], "Part_7": [19.05], "Part_8": [16.5, 18.2]}
    _all_checks(rec)
    assert rec["advice"] == ("4 walls are short for their thickness (the shortest, Part_3: 12.7 mm thick over 96.5 mm, "
                             "7.6 times its thickness). Shell theory is less exact under 10 times" + _FREE_PLATE)


def test_the_advice_is_one_plain_line():
    assert ms.short_wall_words([]) == ""
    one = {"body": "Part_6", "sheet": "S3", "thickness_mm": 12.7, "span_mm": 96.52, "width_mm": 80.0, "span_walls": 7.6,
           "width_walls": 6.3}
    assert ms.short_wall_words([one]) == ("Part_6: 12.7 mm thick over 96.5 mm, 7.6 times its thickness. Shell theory is "
                                          "less exact under 10 times" + _FREE_PLATE)
    shorter = dict(one, body="Part_9", thickness_mm=31.75, span_mm=125.6, span_walls=4.0, width_walls=3.3)
    assert ms.short_wall_words([one, shorter]) == (
        "2 walls are short for their thickness (the shortest, Part_9: 31.8 mm thick over 126 mm, 4 times its thickness). "
        "Shell theory is less exact under 10 times (about 5% off on a free plate this short, more where it bends from a "
        "joint).")
    # the two thin-rule figures the advice names are SHELL-2's own
    from meshpipeline.cad import shell_words as sw

    assert sw.SPAN_WALLS == 1.0 / sp.SHELL_RATIO and sw.WIDTH_WALLS == 1.0 / sp.SHELL_WIDTH_RATIO


def test_plates_and_flat_flanges_read_the_same_twice(tmp_path):
    for k, shape in enumerate((sf.gusset(7.6), sf.rolled_channel(length=300.0))):
        path = sf.write_step(shape, tmp_path / f"part{k}.step")
        out = []
        for run in range(2):
            rec = ms.extract_file(path, tmp_path / f"p{k}r{run}", mm_per_unit=1.0)
            rec.pop("seconds")
            out.append((json.dumps(rec, sort_keys=True), (tmp_path / f"p{k}r{run}" / ms.SURFACE_FILE).read_bytes()))
        assert out[0] == out[1]


def test_a_flat_flange_can_carry_its_true_wall_at_every_node(monkeypatch):
    # SHELL-24, FLAT_TAPER_WALL "true": the flat plate keeps its place and its mean, and the record carries the wall
    # read between the two planes at points over it (SHELL-9b's readings), so the mesh gives every node its own
    tf, half = 16.5, (94.5 - 18.2) / 2.0 / 6.0
    monkeypatch.setattr(ms, "FLAT_TAPER_WALL", "true")
    rec, _ = ms.extract(sf.rolled_channel(length=300.0), mm_per_unit=1.0)
    flanges = [m for m in _shell_mids(rec) if m.get("flat")]
    assert len(flanges) == 2
    for m in flanges:
        assert m["thickness_mm"] == pytest.approx(tf, rel=1e-6)
        assert m["thickness_range_mm"] == pytest.approx([tf - half, tf + half], rel=1e-4)
        walls = np.array(m["wall_samples_mm"])[:, 3]
        assert len(walls) >= 40 and walls.min() >= tf - half - 1e-3 and walls.max() <= tf + half + 1e-3
        assert walls.min() < tf - 0.8 * half and walls.max() > tf + 0.8 * half      # read from toe to web
    _all_checks(rec)
    # the default carries one wall: no readings, no range
    monkeypatch.setattr(ms, "FLAT_TAPER_WALL", "mean")
    rec, _ = ms.extract(sf.rolled_channel(length=300.0), mm_per_unit=1.0)
    assert all("wall_samples_mm" not in m and "thickness_range_mm" not in m for m in _shell_mids(rec))


def test_a_ring_is_as_wide_as_its_metal():
    # SHELL-23's width is read at the widest place of the mid-face, holes counted: a flange ring 9 mm thick whose
    # metal is 17 mm across (1.9 walls) is no plate, though its outline is 80 mm; a washer 4 walls across is one
    ring = sf._cut(sf._cyl(0, 0, 0, 40.0, 9.0), sf._cyl(0, 0, -1, 23.0, 11.0))
    rec, _ = ms.extract(ring, mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "solid" and body["closest_wall"]["why"] == "narrow"
    assert body["closest_wall"]["width_mm"] == pytest.approx(17.0, rel=0.02)
    washer = sf._cut(sf._cyl(0, 0, 0, 40.0, 4.0), sf._cyl(0, 0, -1, 23.0, 6.0))
    rec, _ = ms.extract(washer, mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
    # ... and the width itself, on plain faces: a square its side, a strip its width
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln

    assert ms.inscribed_width(BRepBuilderAPI_MakeFace(gp_Pln(), 0.0, 50.0, 0.0, 50.0).Face()) == pytest.approx(50.0, rel=1e-3)
    assert ms.inscribed_width(BRepBuilderAPI_MakeFace(gp_Pln(), 0.0, 300.0, 0.0, 20.0).Face()) == pytest.approx(20.0, rel=1e-3)


def test_a_lump_with_a_tab_is_a_shell_tab_and_a_solid_region():
    # SHELL-23b: a 60 x 60 x 40 block with a 6 mm tab 30 x 20 on its side. The tab is a plate (3.3 walls wide) and holds
    # 2% of the part: it is a shell, the block a solid region, as a thin tab on a block always was (SHELL-2). The block
    # alone has no mid-surface and stays solid
    lump = sf._fuse(sf._box(0, 0, 0, 60, 60, 40), sf._box(60, 20, 0, 30, 20, 6))
    rec, _ = ms.extract(lump, mm_per_unit=1.0)
    (body,) = rec["bodies"]
    assert body["verdict"] == "mixed" and len(body["solid_regions"]) == 1
    assert [round(m["thickness_mm"], 3) for m in _shell_mids(rec)] == [6.0]
    assert body["mid_volume_ratio"] == pytest.approx(0.024, abs=0.003)
    block, _ = ms.extract(sf.chunky_block(), mm_per_unit=1.0)
    assert [b["verdict"] for b in block["bodies"]] == ["solid"]


def test_a_step_up_to_twice_the_wall_is_a_shell_and_a_taller_pad_is_not():
    # SHELL-23c: a 4 mm plate with a 60 x 60 land 2 mm high (6 mm there, flush underneath) is one shell, the plate laid
    # onto the land's mid-plane with its offset (SHELL-7); 4 mm high (8 mm, twice the plate) too; 10 mm high (14 mm,
    # 3.5 times) its mid-plane lies outside the plate's metal: a pad, solid, as before
    for h, off in ((2.0, -1.0), (4.0, -2.0)):
        rec, surface = ms.extract(sf.plate_with_low_pad(4.0, h), mm_per_unit=1.0)
        assert [b["verdict"] for b in rec["bodies"]] == ["shell"]
        walls = sorted((m["thickness_mm"], m.get("offset_mm", 0.0)) for m in _shell_mids(rec))
        assert walls == [(4.0, pytest.approx(off)), (4.0 + h, 0.0)]
        assert _sewn(rec)["open_edges"] == 0 and _sewn_area(surface) == pytest.approx(200 * 100, rel=1e-6)
        _all_checks(rec)
    rec, _ = ms.extract(sf.plate_with_low_pad(4.0, 10.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in rec["bodies"]] == ["mixed"]
    (pad,) = [p for p in rec["pairs"] if p["thickness_mm"] == pytest.approx(14.0)]
    assert not pad["shell"] and pad["why"].startswith("a pad on a thinner wall")


def test_a_part_can_be_read_without_its_short_plates():
    # SHELL-26: extract(plates=False) reads the whole part as before SHELL-23 and SHELL-24 - the shell the run falls
    # back to when a mesh of the short plates cannot be made
    rec, _ = ms.extract(sf.gusset(7.6, 12.7), mm_per_unit=1.0)
    assert ms.has_plates(rec) and [b["verdict"] for b in rec["bodies"]] == ["shell"]
    old, _ = ms.extract(sf.gusset(7.6, 12.7), mm_per_unit=1.0, plates=False)
    assert not ms.has_plates(old) and [b["verdict"] for b in old["bodies"]] == ["solid"] and old["advice"] == ""
    flange, _ = ms.extract(sf.rolled_channel(length=300.0), mm_per_unit=1.0)
    assert ms.has_plates(flange)
    assert not ms.has_plates(ms.extract(sf.rolled_channel(length=300.0), mm_per_unit=1.0, plates=False)[0])
    thin, _ = ms.extract(sf.flat_plate(2.0), mm_per_unit=1.0)
    assert not ms.has_plates(thin)


def test_plates_that_would_hang_loose_stay_solid(monkeypatch):
    # SHELL-23's last guard: a body whose short plates leave edges open is read again without them, and the reading
    # with fewer open edges is kept, said in a note; one that is no better without them keeps its plates
    calls = []

    def reading(opened_with: int, opened_without: int):
        def fake(shape, *, mm_per_unit, names, clock, no_plate):
            calls.append(set(no_plate))
            out = "Part_2" in no_plate
            rec = {"sewn": [{"body": "Part_1", "open_edges": 0},
                            {"body": "Part_2", "open_edges": opened_without if out else opened_with}],
                   "short_walls": [] if out else [{"body": "Part_2"}], "mids": [], "notes": []}
            return rec, ("without" if out else "with")
        return fake

    monkeypatch.setattr(ms, "_extract", reading(3, 0))
    rec, surface = ms.extract(None, mm_per_unit=1.0)
    assert surface == "without" and calls == [set(), {"Part_2"}]
    assert rec["notes"] == ["Part_2: its short plates would leave 3 edge(s) open where they meet its other walls (0 "
                            "without them), so they stay solid, as before (SHELL-23)."]
    calls.clear()
    monkeypatch.setattr(ms, "_extract", reading(3, 3))
    rec, surface = ms.extract(None, mm_per_unit=1.0)
    assert surface == "with" and calls == [set(), {"Part_2"}] and rec["notes"] == []
    # a body with no open edge is never read twice
    calls.clear()
    monkeypatch.setattr(ms, "_extract", reading(0, 0))
    assert ms.extract(None, mm_per_unit=1.0)[1] == "with" and calls == [set()]


def test_a_body_whose_plates_hold_under_half_of_it_once_joined_is_left_as_it_was(monkeypatch):
    # SHELL-23, the body share, judged on the sewn shell (MIXED-4's measure) - off since SHELL-23b; its switch still
    # works: at a half, 30% is left as it was
    monkeypatch.setattr(ms, "PLATE_BODY_SHARE", 0.5)
    calls = []

    def fake(shape, *, mm_per_unit, names, clock, no_plate):
        calls.append(dict(no_plate))
        out = "Part_1" in no_plate
        rec = {"bodies": [{"body": "Part_1", "verdict": "solid" if out else "mixed",
                           **({} if out else {"mid_volume_ratio": 0.3})}],
               "sewn": [] if out else [{"body": "Part_1", "open_edges": 0}],
               "short_walls": [] if out else [{"body": "Part_1"}], "mids": [], "notes": []}
        return rec, ("without" if out else "with")

    monkeypatch.setattr(ms, "_extract", fake)
    rec, surface = ms.extract(None, mm_per_unit=1.0)
    assert surface == "without" and calls == [{}, {"Part_1": "lump"}]
    assert rec["notes"] == ["Part_1: its plates would hold only 30% of its metal as a shell: it is mostly thick, so it "
                            "is left as it was (SHELL-23)."]


def test_a_narrow_long_wedge_is_fitted_once_it_is_two_walls_wide(monkeypatch):
    # SHELL-25: a wedge 4 to 8 mm over 200 mm (33 walls long: SHELL-9's fitted wall, its true thickness at every point)
    # 25 mm wide is 3.1 walls at its thickest. The old line (four walls) left it solid while an even plate as wide was
    # a shell; the plate's own line (two) fits it. 12 mm wide (1.5 walls) is a bar, solid either way
    rec, _ = ms.extract(sf.wedge_plate(4.0, 8.0, 200.0, 25.0), mm_per_unit=1.0)
    (mid,) = _shell_mids(rec)
    assert [b["verdict"] for b in rec["bodies"]] == ["shell"] and mid["kind"] == "bspline"
    assert mid["thickness_range_mm"] == pytest.approx([4.0, 8.0], rel=1e-3)
    assert mid["area_mm2"] == pytest.approx(200.0 * 25.0, rel=1e-3)
    _all_checks(rec)
    bar, _ = ms.extract(sf.wedge_plate(4.0, 8.0, 200.0, 12.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in bar["bodies"]] == ["solid"]
    monkeypatch.setattr(ms, "FIT_PLATE_WIDTH", False)
    old, _ = ms.extract(sf.wedge_plate(4.0, 8.0, 200.0, 25.0), mm_per_unit=1.0)
    assert [b["verdict"] for b in old["bodies"]] == ["solid"]
