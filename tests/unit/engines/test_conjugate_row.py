"""The engine row for a conjugate case: what finalize publishes and what the gates refuse.

MEASURED on a real two-region unit_cell at 4 cells across, which is where every number below
comes from:

    fluid    166,400 cells   2.44473 mm3   non-ortho  1.2128   skew 0.0321727
    solid    495,376 cells   6.00527 mm3   non-ortho 27.9932   skew 0.546826
    interface 93,600 faces on BOTH sides
    ports    coolant_in -> coolant_out, on ymin and ymax

AND THE ONE THING THE ROOT MESH TEACHES. splitMeshRegions leaves the pre-split mesh in place at
constant/polyMesh, carrying every patch with the two regions' faces ADDED TOGETHER: ymin has 532
faces there, which is the fluid's 128 plus the solid's 404. Anything that reads the root sees
every patch populated on a case where the coolant never reached one of them, so every read here
is per region.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import gates as G
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION
from meshpipeline.engines.cfmesh_conjugate.slices import flow_axis, review_slices

# the measured run, as native._as_dict writes it
REPORT = {
    "ok": True, "problems": [], "notes": [],
    "regions": {
        FLUID_REGION: {"cells": 166400, "volume_m3": 2.44473e-9, "pieces": 1, "mesh_ok": True,
                       "fatal": [], "patches": {"coolant_in": 128, "coolant_out": 128,
                                                "wall": 20000, "fluid_to_solid": 93600},
                       "max_non_ortho": 1.2128, "max_skewness": 0.0321727},
        SOLID_REGION: {"cells": 495376, "volume_m3": 6.00527e-9, "pieces": 1, "mesh_ok": True,
                       "fatal": [], "patches": {"wall": 40000, "solid_to_fluid": 93600},
                       "max_non_ortho": 27.9932, "max_skewness": 0.546826},
    },
    "interface_faces_fluid": 93600, "interface_faces_solid": 93600,
    "ports": {"inlet": "ymin", "outlet": "ymax", "reason": "the passage's own extent"},
    "patch_names": {"ymin": "coolant_in", "ymax": "coolant_out"},
    "seconds": {}, "peak_rss_bytes": 0,
    "plan": {"background_cell_m": 2e-4, "levels": 3, "cells_across": 4, "volume_tol": 0.1989,
             "narrowest_m": 1e-4, "expect_fluid_volume_m3": 2.44e-9, "expect_fluid_pieces": 1},
}


def _boundary(path: Path, patches: dict) -> None:
    """A polyMesh/boundary file, in the shape read_boundary_patches reads."""
    path.mkdir(parents=True, exist_ok=True)
    body = ["FoamFile\n{\n    version 2.0;\n}\n", f"{len(patches)}\n(\n"]
    start = 0
    for name, (of_type, n) in patches.items():
        body.append(f"    {name}\n    {{\n        type {of_type};\n"
                    f"        nFaces {n};\n        startFace {start};\n    }}\n")
        start += n
    body.append(")\n")
    (path / "boundary").write_text("".join(body))
    (path / "owner").write_text("0\n")


@pytest.fixture
def case(tmp_path: Path) -> Path:
    """A delivered two-region case: both regions' boundaries, regionProperties, and the report."""
    _boundary(tmp_path / "constant" / FLUID_REGION / "polyMesh",
              {"coolant_in": ("patch", 128), "coolant_out": ("patch", 128),
               "wall": ("wall", 20000), "fluid_to_solid": ("mappedWall", 93600)})
    _boundary(tmp_path / "constant" / SOLID_REGION / "polyMesh",
              {"wall": ("wall", 40000), "solid_to_fluid": ("mappedWall", 93600)})
    # THE STALE ROOT, exactly as splitMeshRegions leaves it: ymin = 128 + 404.
    _boundary(tmp_path / "constant" / "polyMesh",
              {"ymin": ("patch", 532), "ymax": ("patch", 532), "wall": ("wall", 60000)})
    (tmp_path / "constant" / "regionProperties").write_text(
        f"regions\n(\n    fluid       ({FLUID_REGION})\n    solid       ({SOLID_REGION})\n);\n")
    (tmp_path / "conjugate_report.json").write_text(json.dumps(REPORT))
    return tmp_path


# #
# the quality dict finalize publishes
# #
def test_both_regions_are_reported_with_what_was_measured(case):
    q = F.quality_of(case, REPORT)
    assert q["cells"] == 166400 + 495376
    assert [r["name"] for r in q["regions"]] == [FLUID_REGION, SOLID_REGION]
    assert q["regions"][0]["type"] == "fluid" and q["regions"][1]["type"] == "solid"
    assert q["max_non_ortho"] == pytest.approx(27.9932), "the WORSE of the two regions"
    assert q["max_skewness"] == pytest.approx(0.546826)
    assert q["mesh_ok"] is True


def test_an_unmeasured_quality_number_is_absent_rather_than_zero(case):
    """0.0 for "not measured" reads as a flawless mesh to every bar that compares it."""
    report = json.loads(json.dumps(REPORT))
    for r in report["regions"].values():
        r["max_non_ortho"] = None
        r["max_skewness"] = None
    q = F.quality_of(case, report)
    assert "max_non_ortho" not in q and "max_skewness" not in q


def test_the_coolant_is_compared_against_the_cad(case):
    q = F.quality_of(case, REPORT)
    assert q["coolant_expected_m3"] == pytest.approx(2.44e-9)
    assert q["coolant_error_pct"] == pytest.approx(abs(2.44473 - 2.44) / 2.44 * 100, rel=1e-6)
    assert q["coolant_tol_pct"] == pytest.approx(19.89)


def test_the_piece_count_disagreement_is_a_list_not_a_flag(case):
    """The declared criterion is op="empty", so an absent key must read as unmeasured and a
    False would read as a pass."""
    assert F.quality_of(case, REPORT)["coolant_pieces_short"] == []
    cut = json.loads(json.dumps(REPORT))
    cut["regions"][FLUID_REGION]["pieces"] = 2
    short = F.quality_of(case, cut)["coolant_pieces_short"]
    assert len(short) == 1 and "2 piece" in short[0]


def test_the_resolution_shortfall_is_stated_not_hidden(case):
    short = F.quality_of(case, REPORT)["resolution_short"]
    assert len(short) == 1
    assert "4 cells across" in short[0] and "13" in short[0]


def test_at_the_flow_rule_there_is_no_shortfall_to_state(case):
    from meshpipeline.engines.passage import PASSAGE_CELLS_ACROSS
    full = json.loads(json.dumps(REPORT))
    full["plan"]["cells_across"] = PASSAGE_CELLS_ACROSS
    assert F.quality_of(case, full)["resolution_short"] == []


# #
# every boundary read per region, never off the root
# #
def test_a_patch_with_no_faces_is_found_in_the_region_that_owns_it(case):
    assert F.empty_patches(case) == []
    _boundary(case / "constant" / FLUID_REGION / "polyMesh",
              {"coolant_in": ("patch", 0), "wall": ("wall", 20000),
               "fluid_to_solid": ("mappedWall", 93600)})
    assert F.empty_patches(case) == [f"{FLUID_REGION}:coolant_in"]


def test_the_stale_root_mesh_is_never_the_source(case):
    """The root's ymin has 532 faces - the two regions added together - so a reader pointed there
    sees patches that no region actually owns."""
    delivered = F.delivered_boundary_types(case, [])
    assert "ymin" not in delivered and "ymax" not in delivered, delivered
    assert set(delivered) == {"coolant_in", "coolant_out", "wall"}, delivered


def test_the_interface_is_no_part_of_the_users_contract(case):
    delivered = F.delivered_boundary_types(case, [])
    assert not any("_to_" in n for n in delivered), delivered


def test_the_mesh_says_wall_and_the_declaration_says_which_end(case):
    """A wall is written as `type wall` and the mesh witnesses it. An inlet and an outlet are
    both written as a plain `patch`, so only the declaration knows which is which."""
    declared = [{"name": "coolant_in", "type": "inlet"},
                {"name": "coolant_out", "type": "outlet"},
                {"name": "wall", "type": "inlet"}]
    delivered = F.delivered_boundary_types(case, declared)
    assert delivered["coolant_in"] == "inlet"
    assert delivered["coolant_out"] == "outlet"
    assert delivered["wall"] == "wall", "the mesh overrules a declaration it can witness"


# #
# the manifest
# #
def test_the_manifest_carries_the_block_the_plan_chose(case):
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    PL.write_plan(case, PL.ConjugatePlan(
        background_cell_m=2e-4, levels=3, expect_fluid_volume_m3=2.44e-9, expect_fluid_pieces=1,
        volume_tol=0.1989, narrowest_m=1e-4, inlet="ymin", outlet="ymax",
        block_lo=(0.0, 0.0, 0.0), block_hi=(0.0002, 0.0325, 0.0013)))
    lo, hi = F._block(case, REPORT)
    assert hi == [0.0002, 0.0325, 0.0013]
    assert lo == [0.0, 0.0, 0.0]


def test_with_no_plan_the_block_is_not_a_point(case):
    lo, hi = F._block(case, {})
    assert [h - l for l, h in zip(lo, hi)] == [1.0, 1.0, 1.0], (
        "a bbox of zeros publishes a mesh with no extent")


def test_the_users_port_name_resolves_back_to_a_block_face(case):
    """The slice across the flow needs the AXIS, and `coolant_in` carries none."""
    q = F.quality_of(case, REPORT)
    assert F._port(q, "inlet") == "ymin"
    assert F._port(q, "outlet") == "ymax"


# #
# the gates
# #
class _Ctx:
    def __init__(self, workspace, manifest, intake_patches=()):
        self.workspace = str(workspace)
        self._m = manifest
        self.intake_patches = list(intake_patches)

    def manifest_or_load(self):
        return self._m


def _manifest(case, **over):
    q = F.quality_of(case, REPORT)
    q.update(over)
    return {"schema_version": "2.1", "geometry": {}, "patches": [], "validation": {},
            "quality": q, "cell_count": q["cells"], "patch_types": {}}


def _gate(key):
    return next(g for g in G.CONJUGATE_GATES if g.key == key)


def test_a_delivered_case_passes_every_gate(case):
    man = _manifest(case)
    (case / "mesh_manifest.json").write_text(json.dumps(man))
    for gate in G.CONJUGATE_GATES:
        if gate.key == "quality_floor":
            continue                 # needs the engine registered; its own test covers it
        ok, said = gate.check(_Ctx(case, man))
        assert ok, f"{gate.key}: {said}"


def test_the_blocks_own_faces_are_no_part_of_the_users_contract(case):
    """MEASURED on the manifold plate, where this refused a mesh that had done what was asked.

    The intake contract was coolant_in and coolant_out. A conjugate mesh is cut out of the part's
    bounding box, so the metal comes back with its six outer faces too, and the delivered
    patch_types were coolant_in, coolant_out, xmin, xmax, zmin, zmax - four of which the user
    never named, and all four of which are the outside of their plate.

    A face the user named carries THEIR name (renameBoundary puts it there before the split). A
    face still called xmin is one the engine named, and it is engine-owned like the interface.
    """
    man = _manifest(case)
    man["patch_types"] = {"coolant_in": "inlet", "coolant_out": "outlet", "xmin": "wall",
                          "xmax": "wall", "zmin": "wall", "zmax": "wall"}
    ok, said = _gate("patch_contract").check(
        _Ctx(case, man, [{"name": "coolant_in", "type": "inlet"},
                         {"name": "coolant_out", "type": "outlet"}]))
    assert ok, said


def test_a_port_the_user_named_that_is_not_delivered_is_still_refused(case):
    """The exclusion is of the BLOCK's names, not of the contract."""
    man = _manifest(case)
    man["patch_types"] = {"coolant_in": "inlet", "xmin": "wall"}
    ok, said = _gate("patch_contract").check(
        _Ctx(case, man, [{"name": "coolant_in", "type": "inlet"},
                         {"name": "coolant_out", "type": "outlet"}]))
    assert not ok and "coolant_out" in said, said


def test_a_patch_nobody_asked_for_under_its_own_name_is_refused(case):
    man = _manifest(case)
    man["patch_types"] = {"coolant_in": "inlet", "coolant_out": "outlet", "mystery": "wall"}
    ok, said = _gate("patch_contract").check(
        _Ctx(case, man, [{"name": "coolant_in", "type": "inlet"},
                         {"name": "coolant_out", "type": "outlet"}]))
    assert not ok and "mystery" in said, said


def test_a_mesh_of_the_wrong_cells_is_refused_by_volume(case):
    """MEASURED: the unit cell meshed from a coolant surface wound inside-out came out 95.16%
    wrong by volume with checkMesh reporting "Mesh OK"."""
    man = _manifest(case, coolant_error_pct=95.16)
    ok, said = _gate("coolant_volume").check(_Ctx(case, man))
    assert not ok and "95.16%" in said and "19.89%" in said


# #
# one quantity, one bar
# #
def _gating_bar(key: str):
    """The declared gating criterion for `key`, read straight off the bundle.

    Not through the registry: the engine is only registered once a job has delivered it, and this
    invariant has to hold before that.
    """
    from meshpipeline.engines.cfmesh_conjugate.criteria import CRITERIA_ROWS
    rows = [c for c in CRITERIA_ROWS if c.key == key]
    assert len(rows) == 1, f"{key} is declared {len(rows)} times"
    return rows[0]


def test_the_binding_bar_is_the_ratio_and_not_the_percentage():
    """MEASURED, by the native-to-terminal tier on a two-channel plate: the coolant came out 9.95%
    from the CAD at 5 cells across, which the resolution allows 19.89% of. The gate passed it and
    the declared criterion refused it, because the criterion carried a fixed 5% - one quantity
    with two readers that disagree, which is the exact failure engines/staircase_error warns about
    for the sizing rule."""
    ratio = _gating_bar("coolant_error_over_tol")
    assert ratio.op == "<=" and ratio.threshold == 1.0 and ratio.gating is True
    pct = _gating_bar("coolant_error_pct")
    assert pct.gating is False, (
        "a second gating bar on the same quantity is how the two came to disagree")


@pytest.mark.parametrize(("err", "tol", "want"), [
    (9.9477, 19.89, True),      # the tier's own run, which the two bars disagreed about
    (0.538, 19.89, True),       # the manifold plate at 5 across
    (13.26, 19.89, True),       # the unit cell at 5 across, the worst measured
    (19.89, 19.89, True),       # exactly at the bar
    (19.90, 19.89, False),
    (95.16, 19.89, False),      # the inside-out surface, with checkMesh saying "Mesh OK"
])
def test_the_gate_and_the_criterion_agree_on_the_same_mesh(case, err, tol, want):
    man = _manifest(case, coolant_error_pct=err, coolant_tol_pct=tol,
                    coolant_error_over_tol=err / tol)
    gate_ok, _said = _gate("coolant_volume").check(_Ctx(case, man))
    bar = _gating_bar("coolant_error_over_tol")
    criterion_ok = (err / tol) <= bar.threshold
    assert gate_ok == want, f"the gate said {gate_ok}"
    assert criterion_ok == want, f"the criterion said {criterion_ok}"


def test_the_ratio_is_absent_when_there_is_no_tolerance_to_divide_by(case):
    """Absent reads as not-measured and refuses; a ratio of error over zero would be a number."""
    report = json.loads(json.dumps(REPORT))
    report["plan"]["volume_tol"] = 0.0
    q = F.quality_of(case, report)
    assert "coolant_error_over_tol" not in q


def test_the_ratio_is_the_error_over_the_tolerance(case):
    q = F.quality_of(case, REPORT)
    assert q["coolant_error_over_tol"] == pytest.approx(
        q["coolant_error_pct"] / q["coolant_tol_pct"])


def test_a_volume_inside_the_resolutions_own_bar_passes(case):
    man = _manifest(case, coolant_error_pct=13.26)   # the unit cell at 5 across, measured
    ok, _ = _gate("coolant_volume").check(_Ctx(case, man))
    assert ok, "the staircase error IS the expected disagreement at this resolution"


def test_no_comparison_at_all_is_a_refusal_not_a_pass(case):
    man = _manifest(case)
    man["quality"].pop("coolant_error_pct")
    ok, said = _gate("coolant_volume").check(_Ctx(case, man))
    assert not ok and "not measured" in said


def test_the_right_volume_in_the_wrong_number_of_places_is_refused(case):
    man = _manifest(case, coolant_pieces=2, coolant_pieces_expected=1)
    ok, said = _gate("coolant_pieces").check(_Ctx(case, man))
    assert not ok and "cut apart" in said


def test_a_coupling_that_does_not_match_face_for_face_is_refused(case):
    man = _manifest(case, interface_faces_solid=93599, interface_ok=False)
    ok, said = _gate("interface").check(_Ctx(case, man))
    assert not ok and "93,599" in said


def test_two_regions_that_never_touch_are_refused(case):
    man = _manifest(case, interface_faces_fluid=0, interface_faces_solid=0, interface_ok=False)
    ok, said = _gate("interface").check(_Ctx(case, man))
    assert not ok and "no surface for heat to cross" in said


def test_a_name_with_nothing_behind_it_is_refused(case):
    man = _manifest(case, empty_patches=[f"{FLUID_REGION}:coolant_in"])
    ok, said = _gate("named_patches").check(_Ctx(case, man))
    assert not ok and "coolant_in" in said


def test_a_third_region_is_refused(case):
    man = _manifest(case, regions_undeclared=["domain0"])
    ok, said = _gate("regions_split").check(_Ctx(case, man))
    assert not ok and "domain0" in said


def test_the_declaration_must_match_the_delivered_regions(case):
    (case / "constant" / "regionProperties").write_text(
        "regions\n(\n    fluid       (fluid)\n    solid       (solid)\n    solid  (lid)\n);\n")
    man = _manifest(case)
    (case / "mesh_manifest.json").write_text(json.dumps(man))
    ok, said = _gate("manifest_valid").check(_Ctx(case, man))
    assert not ok and "does not match" in said


def test_a_region_directory_with_no_mesh_in_it_is_not_a_region(case):
    """splitMeshRegions makes the directory before it writes into it."""
    (case / "constant" / "lid" / "polyMesh").mkdir(parents=True)
    assert G.meshed_regions(case) == [FLUID_REGION, SOLID_REGION]


# #
# how fine, and what it costs
# #
def test_the_resolution_comes_down_to_fit_the_budget():
    lo, hi = [0.0, 0.0, 0.0], [0.0033, 0.004, 0.00105]      # manifold_two_level, measured
    n, est, said = B.cells_across_for(fidelity="standard", block_lo=lo, block_hi=hi,
                                      narrowest_m=1e-4, limit=8_000_000)
    assert n == 5 and said == "", "its own fidelity fits"
    n, est, said = B.cells_across_for(fidelity="standard", block_lo=lo, block_hi=hi,
                                      narrowest_m=1e-4, limit=1_500_000)
    assert n == 4 and "rather than the 5" in said, said


def test_it_never_goes_below_the_floor():
    n, est, said = B.cells_across_for(fidelity="max", block_lo=[0, 0, 0], block_hi=[1, 1, 1],
                                      narrowest_m=1e-4, limit=1000)
    assert n == B.FLOOR_CELLS_ACROSS
    assert "the fewest this engine will use" in said
    assert "decides" in said, "it still meshes - the delivered count is the real limit"


def test_the_estimate_is_the_block_at_the_finest_cell():
    # one background cell of 200 um, refined three levels -> 8^3 cells, plus the margin
    est = B.estimate_cells(block_lo=[0, 0, 0], block_hi=[2e-4, 2e-4, 2e-4],
                           narrowest_m=1e-4, cells_across=4, levels=3)
    assert est == int(512 * B.ESTIMATE_MARGIN)


def test_a_resolution_nobody_can_mesh_at_is_refused_outright():
    with pytest.raises(ValueError):
        B.estimate_cells(block_lo=[0, 0, 0], block_hi=[1, 1, 1], narrowest_m=0.0,
                         cells_across=4)


# #
# the cuts a cold plate is looked at through
# #
@pytest.mark.parametrize(("inlet", "outlet", "want"), [
    ("ymin", "ymax", 1), ("xmin", "xmax", 0), ("zmin", "zmax", 2),
    ("", "ymax", 1), ("", "", None), ("coolant_in", "", None),
])
def test_the_flow_axis_comes_off_the_block_face_names(inlet, outlet, want):
    assert flow_axis(inlet, outlet) == want


def test_the_three_cuts_are_across_along_and_through():
    cuts = review_slices([0, 0, 0], [0.0033, 0.004, 0.00105], inlet="ymin", outlet="ymax")
    by_name = {c["name"]: c for c in cuts}
    assert set(by_name) == {"across_the_flow", "through_the_plate", "along_the_flow"}
    assert by_name["across_the_flow"]["normal"] == [0.0, 1.0, 0.0], "square across the flow"
    assert by_name["through_the_plate"]["normal"] == [0.0, 0.0, 1.0], "the thinnest axis"
    assert by_name["along_the_flow"]["normal"] == [1.0, 0.0, 0.0]
    assert all(c["why"] for c in cuts), "a cut nobody can say the purpose of is a cut skipped"


def test_a_cut_is_never_exactly_down_the_middle():
    """A block meshed with an even number of cells has a grid plane there, and a cut along a face
    of cells shows no cells at all."""
    cut = review_slices([0, 0, 0], [1, 1, 1], inlet="ymin", outlet="ymax")[0]
    assert cut["origin"][1] != 0.5 and abs(cut["origin"][1] - 0.5) < 1e-2


def test_a_sealed_part_is_still_cut_three_ways():
    cuts = review_slices([0, 0, 0], [0.0033, 0.004, 0.00105])
    assert len(cuts) == 3, "no ports is a legal plan - the branching part has none"


def test_a_degenerate_block_promises_nothing():
    assert review_slices([0, 0, 0], [0.0, 1.0, 1.0]) == []


# #
# the driver's own two decisions
# #
def test_the_coolant_is_cut_out_of_the_brep_not_the_skin(tmp_path):
    from meshpipeline.engines.cfmesh_conjugate.driver import find_source
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")
    assert find_source(tmp_path) is None, "a triangle file has no faces to ask"
    (tmp_path / "geometry.step").write_text("ISO-10303-21;\n")
    assert find_source(tmp_path).name == "geometry.step"


def test_the_geometry_picks_the_face_and_the_declaration_picks_the_name(tmp_path):
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    from meshpipeline.engines.cfmesh_conjugate.driver import declared_port_names
    plan = PL.ConjugatePlan(background_cell_m=2e-4, levels=3, expect_fluid_volume_m3=1e-9,
                            expect_fluid_pieces=1, volume_tol=0.2, narrowest_m=1e-4,
                            inlet="ymin", outlet="ymax", port_walls=("xmin", "xmax"))
    (tmp_path / "port_declaration.json").write_text(json.dumps([
        {"name": "coolant_in", "type": "inlet"}, {"name": "coolant_out", "type": "outlet"},
        {"name": "plate_wall", "type": "wall"}]))
    names, said = declared_port_names(tmp_path, plan)
    assert names["ymin"] == ("coolant_in", "inlet")
    assert names["ymax"] == ("coolant_out", "outlet")
    assert names["xmin"] == ("plate_wall", "wall") and names["xmax"] == ("plate_wall", "wall")
    assert said == ""


def test_two_declared_inlets_are_not_guessed_between(tmp_path):
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    from meshpipeline.engines.cfmesh_conjugate.driver import declared_port_names
    plan = PL.ConjugatePlan(background_cell_m=2e-4, levels=3, expect_fluid_volume_m3=1e-9,
                            expect_fluid_pieces=1, volume_tol=0.2, narrowest_m=1e-4,
                            inlet="ymin", outlet="ymax")
    (tmp_path / "port_declaration.json").write_text(json.dumps([
        {"name": "in_a", "type": "inlet"}, {"name": "in_b", "type": "inlet"},
        {"name": "coolant_out", "type": "outlet"}]))
    names, said = declared_port_names(tmp_path, plan)
    assert (names or {}).get("ymin") is None, "alphabetical order must not pick a customer's port"
    assert names["ymax"] == ("coolant_out", "outlet")
    assert "more than one inlet" in said and "ymin" in said


def test_no_declaration_at_all_leaves_the_block_its_own_names(tmp_path):
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    from meshpipeline.engines.cfmesh_conjugate.driver import declared_port_names
    plan = PL.ConjugatePlan(background_cell_m=2e-4, levels=3, expect_fluid_volume_m3=1e-9,
                            expect_fluid_pieces=1, volume_tol=0.2, narrowest_m=1e-4,
                            inlet="ymin", outlet="ymax")
    assert declared_port_names(tmp_path, plan) == (None, "")
