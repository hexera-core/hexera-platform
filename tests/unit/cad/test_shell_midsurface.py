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
    rec = ms.extract_file(path, tmp_path / "out", mm_per_unit=1.0)
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

