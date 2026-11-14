# Responsibility: Verify the user's mesh requirements on a shell mesh (SHMESH-24..26): with no requirement the limits in force are today's constants and the mesh, its plan and its record are the ones made before; a global size and edge limits set the plan's sizes; the user's quality limits join the limits the tidy-up steps aim at, by the common pre-processor definitions; every requirement is told asked against delivered, never as met when it was not reached; the same notes twice give the same mesh.
# Boundaries: pure numpy on made-up elements, and the real shell run on small built parts (a few seconds each). No model, no solver.
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_quality as SQ
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines.shell_mesh import ShellMesh


def criteria(scale: float = 1.0) -> dict:
    """The typical criteria list as the checked set, at a global size of 10 mm x `scale`."""
    notes = CRITERIA.replace("= 10 mm", f"= {10 * scale:g} mm")
    answer = criteria_answer()
    answer["sizes"][0].update(value=10 * scale, quote=notes.split("\n")[0])
    req, dropped, _na = MR.check(json.dumps(answer), notes)
    assert dropped == []
    return req


# ------------------------------------------------------------------ nothing said: today's constants --------
def test_with_no_requirement_the_limits_in_force_are_todays_constants():
    for nothing in (None, {}, MR.empty()):
        lim = SR.limits_in_force(nothing)
        assert lim is SQ.IN_FORCE
        assert SR.kind_of(nothing) is None
    assert SQ.IN_FORCE.hard is SQ.NASTRAN_LIMITS and SQ.IN_FORCE.aims == () and SQ.IN_FORCE.extra is False
    # the solver's own limits, to the number (a change here changes every shell mesh)
    assert SQ.NASTRAN_LIMITS == (
        ("quad", "skew_deg", ">=", 30.0, "Q4_SKEW"), ("quad", "taper", "<=", 0.5, "Q4_TAPER"),
        ("quad", "warp_coef", "<=", 0.05, "Q4_WARP"), ("quad", "min_angle_quad", ">=", 30.0, "Q4_IAMIN"),
        ("quad", "max_angle_quad", "<=", 150.0, "Q4_IAMAX"), ("tria", "skew_deg", ">=", 10.0, "T3_SKEW"),
        ("tria", "max_angle_tria", "<=", 160.0, "T3_IAMAX"))


def test_with_no_requirement_the_plan_is_untouched():
    rows = [{"face": 1, "mid": "Mid_1", "wall_mm": 2.0, "size_mm": 12.5, "rule": "SHMESH-1", "floored": False,
             "curve_mm": 1.2, "curve_rule": "SHMESH-3"}]
    before = json.dumps(rows, sort_keys=True)
    for nothing in (None, {}, MR.empty()):
        assert SR.size_plan(rows, nothing, [1000.0], planned_max=1.5e6) is None
        assert SR.report(_one_quad(), nothing) is None
    assert json.dumps(rows, sort_keys=True) == before


def test_the_measures_every_step_reads_are_the_same_with_and_without_the_extra_figures():
    rng = np.random.default_rng(7)
    xyz = rng.normal(size=(40, 3))
    quads = rng.integers(0, 40, size=(30, 4))
    trias = rng.integers(0, 40, size=(30, 3))
    for plain, extra in ((SQ.quad_measures(xyz, quads), SQ.quad_measures(xyz, quads, extra=True)),
                         (SQ.tria_measures(xyz, trias), SQ.tria_measures(xyz, trias, extra=True))):
        for k, v in plain.items():
            assert np.array_equal(v, extra[k], equal_nan=True), k
        assert {"jacobian", "min_edge", "max_edge", "skew_pre"} <= set(extra) - set(plain)


# ------------------------------------------------------------------ the definitions --------------------------
def _one_quad(pts=((0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0))) -> ShellMesh:
    return ShellMesh(xyz=np.array(pts, dtype=float), quads=np.array([[0, 1, 2, 3]]), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.array([2.0]), mid=["Mid_1"], part=["Part_1"])


def test_the_jacobian_is_the_gauss_point_ratio_in_the_mean_plane():
    square = SQ.quad_measures(np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float),
                              np.array([[0, 1, 2, 3]]), extra=True)
    assert square["jacobian"][0] == pytest.approx(1.0)
    # a parallelogram has one Jacobian everywhere
    para = SQ.quad_measures(np.array([[0, 0, 0], [2, 0, 0], [3, 1, 0], [1, 1, 0]], dtype=float),
                            np.array([[0, 1, 2, 3]]), extra=True)
    assert para["jacobian"][0] == pytest.approx(1.0)
    # a trapezoid, bottom 2 and top 1: det J is linear across it, 0.5 * (1.5 -+ 0.5 / sqrt(3)) at the Gauss rows
    g = 1.0 / math.sqrt(3.0)
    trap = SQ.quad_measures(np.array([[0, 0, 0], [2, 0, 0], [1.5, 1, 0], [0.5, 1, 0]], dtype=float),
                            np.array([[0, 1, 2, 3]]), extra=True)
    assert trap["jacobian"][0] == pytest.approx((1.5 - 0.5 * g) / (1.5 + 0.5 * g), rel=1e-12)
    # the corner-sine ratio the shell limits already report reads 1 for the same trapezoid: a different figure
    assert trap["jacobian_ratio"][0] == pytest.approx(1.0)
    # a triangle has one Jacobian
    assert SQ.tria_measures(np.array([[0, 0, 0], [3, 0, 0], [0, 1, 0]], dtype=float), np.array([[0, 1, 2]]),
                            extra=True)["jacobian"][0] == 1.0


def test_warpage_aspect_and_angles_are_the_pre_processor_definitions():
    # warpage: the quad split along each diagonal, the angle between the two triangles' normals, the larger
    h = 1.0
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, h], [0, 10, 0]], dtype=float)
    q = SQ.quad_measures(xyz, np.array([[0, 1, 2, 3]]), extra=True)
    n1 = np.cross(xyz[1] - xyz[0], xyz[2] - xyz[0])
    n2 = np.cross(xyz[2] - xyz[0], xyz[3] - xyz[0])
    n3 = np.cross(xyz[1] - xyz[0], xyz[3] - xyz[0])
    n4 = np.cross(xyz[2] - xyz[1], xyz[3] - xyz[1])

    def ang(a, b):
        return math.degrees(math.acos(float(a @ b) / float(np.linalg.norm(a) * np.linalg.norm(b))))

    assert q["warp_deg"][0] == pytest.approx(max(ang(n1, n2), ang(n3, n4)), rel=1e-9)
    # aspect: longest edge over shortest; angles: the interior corner angles; edges in the mesh's mm
    r = SQ.quad_measures(np.array([[0, 0, 0], [40, 0, 0], [40, 10, 0], [0, 10, 0]], dtype=float),
                         np.array([[0, 1, 2, 3]]), extra=True)
    assert r["aspect"][0] == pytest.approx(4.0) and (r["min_edge"][0], r["max_edge"][0]) == (10.0, 40.0)
    assert (r["min_angle_quad"][0], r["max_angle_quad"][0]) == pytest.approx((90.0, 90.0))
    assert r["skew_pre"][0] == pytest.approx(0.0, abs=1e-9)
    assert set(SR.FIGURES) == {("aspect_ratio", "<="), ("warpage", "<="), ("jacobian", ">="), ("skew", "<="),
                               ("skew", ">="), ("taper", "<="), ("min_angle", ">="), ("max_angle", "<=")}


# ------------------------------------------------------------------ SHMESH-25: the limits in force -----------
def test_the_users_limits_become_aims_beside_the_solvers_own():
    lim = SR.limits_in_force(criteria())
    assert lim.hard is SQ.NASTRAN_LIMITS                      # the solver's own never move
    assert set(lim.aims) == {
        ("quad", "warp_deg", "<=", 15.0, 30.0, 0.01), ("quad", "aspect", "<=", 4.0, 6.0, 0.01),
        ("tria", "aspect", "<=", 4.0, 6.0, 0.01), ("quad", "jacobian", ">=", 0.6, 0.4, 0.01),
        ("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01), ("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),
        ("tria", "max_angle_tria", "<=", 120.0, 140.0, 0.01), ("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01)}
    # a limit that is no measure of shell elements is no aim (it is told as not applied)
    cfd = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "element_kind": [], "other": [], "limits": [
        {"measure": "non_orthogonality", "shape": "any", "direction": "max", "target": 65, "allowed_percent": 0,
         "outer": 0, "has_outer": False, "quote": "non-orthogonality under 65"}]}), "non-orthogonality under 65")[0]
    assert SR.limits_in_force(cfd) is SQ.IN_FORCE


def test_a_score_is_zero_inside_the_target_one_in_the_band_and_heavy_past_the_outer_limit():
    lim = SR.limits_in_force(criteria())
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],          # square
                    [0, 0, 0], [50, 0, 0], [50, 10, 0], [0, 10, 0],          # aspect 5: in the band 4..6
                    [0, 0, 0], [80, 0, 0], [80, 10, 0], [0, 10, 0]], dtype=float)  # aspect 8: past 6
    quads = np.array([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])
    assert SQ._quad_aim(xyz, quads, lim).tolist() == [0, SQ.AIM_BAND, SQ.AIM_OUTER]
    assert SQ._quad_aim(xyz, quads, SQ.IN_FORCE).tolist() == [0, 0, 0]


def test_a_quad_past_a_user_limit_is_cut_only_when_the_cut_mends_it_and_only_past_the_allowance():
    lim = SR.limits_in_force(criteria())
    # quad 0: a square. quad 1: a kite with a 138-degree corner (past 135, inside Nastran's 150): the cut through
    # that corner gives two triangles inside every triangle limit. quad 2: aspect 5 - a cut cannot mend aspect
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],
                    [20, 0, 0], [30, 0, 0], [31, 9, 0], [25, 7.5, 0],
                    [40, 0, 0], [90, 0, 0], [90, 10, 0], [40, 10, 0]], dtype=float)
    quads = np.array([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])
    m = SQ.quad_measures(xyz, quads, extra=True)
    assert 135.0 < m["max_angle_quad"][1] < 150.0 and not SQ._quad_past(m)[1]
    keep, tri, came = SQ.split_aimed_quads(xyz, quads, n_elements=3, limits=lim)       # 1% of 3: no allowance
    assert keep.tolist() == [0, 2] and came.tolist() == [1, 1] and len(tri) == 2
    t = SQ.tria_measures(xyz, tri, extra=True)
    assert not SQ._tria_past(t).any() and not SQ.aim_score(t, "tria", lim, 2).any()
    # inside the share the user allows, the quad stays a quad
    keep, tri, _came = SQ.split_aimed_quads(xyz, quads, n_elements=1000, limits=lim)
    assert keep.tolist() == [0, 1, 2] and len(tri) == 0
    # and with no user limit nothing is ever cut here
    keep, tri, _came = SQ.split_aimed_quads(xyz, quads, n_elements=3, limits=SQ.IN_FORCE)
    assert keep.tolist() == [0, 1, 2] and len(tri) == 0


# ------------------------------------------------------------------ SHMESH-24: the sizes ----------------------
def _rows():
    return [{"face": 1, "mid": "Mid_1", "wall_mm": 2.0, "size_mm": 14.0, "rule": "SHMESH-1", "floored": False},
            {"face": 2, "mid": "Mid_1", "wall_mm": 2.0, "size_mm": 3.0, "rule": "SHMESH-15", "floored": False,
             "curve_mm": 1.3, "curve_rule": "SHMESH-3"}]


def test_a_global_size_sets_every_faces_size():
    rows = _rows()
    sizing = SR.size_plan(rows, criteria(), [20000.0, 5000.0], planned_max=1.5e6)
    assert [r["size_mm"] for r in rows] == [10.0, 10.0]                # the user's size, coarser or finer than ours
    assert all(r["rule"].endswith("+SHMESH-24") for r in rows)
    assert (sizing["global_mm"], sizing["low_mm"], sizing["high_mm"]) == (10.0, 5.0, 15.0)
    assert sizing["planned"] == 250 and sizing["rules_planned"] == round(20000 / 14 ** 2 + 5000 / 9)
    assert sizing["coarser"] is True and sizing["held"] is False
    # a curve is refined no finer than the shortest edge allows (with its margin)
    assert rows[1]["curve_mm"] == pytest.approx(SR.MIN_MARGIN * 5.0)
    # the stretch of a mapped strip is held under the user's aspect ratio
    assert sizing["stretch"] == pytest.approx(SR.ASPECT_SHARE * 4.0)


def test_a_global_size_alone_means_about_that_size():
    only = MR.check(json.dumps({"stated_unit": "none", "element_kind": [], "limits": [], "other": [], "sizes": [
        {"what": "global", "value": 8, "unit": "mm", "quote": "elements around 8 mm"}]}), "elements around 8 mm")[0]
    sizing = SR.size_plan(_rows(), only, [20000.0, 5000.0], planned_max=1.5e6)
    assert (sizing["low_mm"], sizing["high_mm"]) == (SR.BAND[0] * 8.0, SR.BAND[1] * 8.0) and sizing["stretch"] is None


def test_a_longest_or_shortest_edge_alone_holds_the_rules_own_sizes():
    def one(what, value):
        text = f"{what} {value} mm"
        return MR.check(json.dumps({"stated_unit": "none", "element_kind": [], "limits": [], "other": [], "sizes": [
            {"what": what, "value": value, "unit": "mm", "quote": text}]}), text)[0]

    rows = _rows()
    SR.size_plan(rows, one("max_edge", 9.1), [20000.0, 5000.0], planned_max=1.5e6)
    assert rows[0]["size_mm"] == pytest.approx(9.1 / SR.MAX_MARGIN) and rows[1]["size_mm"] == 3.0
    rows = _rows()
    SR.size_plan(rows, one("min_edge", 4.0), [20000.0, 5000.0], planned_max=1.5e6)
    assert rows[0]["size_mm"] == 14.0 and rows[1]["size_mm"] == pytest.approx(SR.MIN_MARGIN * 4.0)


def test_a_size_that_cannot_be_built_steps_to_the_nearest_that_can_and_says_so():
    tiny = criteria(0.001)                                    # 0.01 mm on 25,000 mm2: 250 million elements
    rows = _rows()
    sizing = SR.size_plan(rows, tiny, [20000.0, 5000.0], planned_max=1.5e6)
    assert sizing["held"] is True and sizing["planned"] <= 1.5e6
    assert rows[0]["size_mm"] == pytest.approx(0.01 * sizing["scale"], rel=1e-5) and rows[0]["size_mm"] > 0.01
    mesh = _one_quad()
    rep = SR.report(mesh, tiny, sizing)
    first = rep["rows"][0]
    assert first["met"] is False and "the nearest size that builds was used" in first["delivered"]


def test_a_hole_keeps_its_shape_and_a_bend_has_one_element_across():
    # a 3 mm hole under a 10 mm size with a 5 mm shortest edge: never fewer than SHAPE_PER_TURN round it
    assert SR.curved_size(3.0, 0.0, floor=6.5, rule=10.0) == pytest.approx(2 * math.pi * 3.0 / SR.SHAPE_PER_TURN)
    # a large radius takes the size asked for
    assert SR.curved_size(500.0, 0.0, floor=6.5, rule=10.0) == 10.0
    # a bend that turns under 100 degrees is not refined: narrower than the size, it gets one element across
    assert SR.curved_size(3.0, 4.7, floor=6.5, rule=10.0) == 10.0


def test_the_element_kind():
    def kind(what, value=0, text="x"):
        return MR.check(json.dumps({"stated_unit": "none", "sizes": [], "limits": [], "other": [], "element_kind": [
            {"what": what, "value": value, "quote": text}]}), text)[0]

    assert SR.kind_of(kind("triangles_only", text="triangles only")) == "triangles"
    assert SR.kind_of(kind("quads_only", text="no triangles")) == "quads"
    assert SR.kind_of(kind("max_triangle_percent", 5, "max 5% trias")) == "quads"


# ------------------------------------------------------------------ SHMESH-26: asked against delivered --------
def _mesh(quads_xyz: list, trias_xyz: list = ()) -> ShellMesh:
    pts, quads, trias = [], [], []
    for q in quads_xyz:
        quads.append(list(range(len(pts), len(pts) + 4)))
        pts += list(q)
    for t in trias_xyz:
        trias.append(list(range(len(pts), len(pts) + 3)))
        pts += list(t)
    n = len(quads) + len(trias)
    return ShellMesh(xyz=np.array(pts, dtype=float), quads=np.array(quads, dtype=np.int64).reshape(-1, 4),
                     trias=np.array(trias, dtype=np.int64).reshape(-1, 3), wall=np.full(n, 2.0), mid=["Mid_1"] * n,
                     part=["Part_1"] * n)


def _rect(w, h, x=0.0):
    return [(x, 0, 0), (x + w, 0, 0), (x + w, h, 0), (x, h, 0)]


def test_every_requirement_is_told_asked_against_delivered():
    # 197 squares of 10, two quads of aspect 5 (in the 4..6 band: 1% of 200 allows 2), one of aspect 8 (past 6)
    mesh = _mesh([_rect(10, 10)] * 197 + [_rect(50, 10)] * 2 + [_rect(80, 10)])
    rep = SR.report(mesh, criteria())
    by = {r["field"] + ":" + str(i): r for i, r in enumerate(rep["rows"])}
    rows = rep["rows"]
    assert [r["field"] for r in rows] == ["global", "min_edge", "max_edge", "limit:warpage", "limit:aspect_ratio",
                                          "limit:jacobian", "limit:max_angle", "limit:min_angle", "limit:max_angle",
                                          "limit:min_angle"]
    assert by
    aspect = rows[4]
    assert (aspect["outside"], aspect["beyond"], aspect["met"], aspect["excess"]) == (3, 1, False, 1)
    assert aspect["outside_pct"] == 1.5 and aspect["worst"] == 8.0
    assert MR.line(aspect) == ("Aspect ratio under 4 (up to 1% of elements between 4 and 6): not fully met - 1.5% of "
                               "elements (3) are over 4, 1 of them over 6; the worst is 8.")
    # an edge length is a target: reported with the percent outside, never a missed limit
    longest = rows[2]
    assert (longest["outside"], longest["met"], longest["worst"]) == (3, None, 80.0)
    assert MR.line(longest) == ("Longest element edge under 15 mm (1.5 x the global size): a target - 1.5% of "
                                "elements (3) have an edge longer than 15 mm (the longest is 80 mm).")
    assert rows[0]["met"] is True and MR.line(rows[0]) == "Global element size 10 mm: met - elements are about 10 mm."
    assert MR.line(rows[3]) == "Warpage under 15° (up to 1% of elements between 15° and 30°): met."
    assert (rep["met"], rep["missed"]) == (8, 1) and rep["definitions"] == SR.DEFINITIONS
    # the closing message's one line, and the verdict's rows: a miss is a note with its own plain words
    said = SR.summary(rep)
    assert said == ("Of the 10 things you asked for: 8 met, 1 not fully met: aspect ratio under 4 (up to 1% of "
                    "elements between 4 and 6) (1.5% of elements (3) are over 4, 1 of them over 6; the worst is 8); 1 "
                    "edge-length target: 1.5% of elements have an edge longer than you asked.")
    assert len(said) <= 400
    asked = MR.asked_rows({"requirements": rep})
    assert [r["key"] for r in asked] == [f"asked_r{k}" for k in (1, 2, 4, 5, 6, 7, 8, 9, 10)]
    assert [r["measured"] for r in asked] == [0, 0, 0, 1, 0, 0, 0, 0, 0]
    assert all(r["op"] == "==" and r["limit"] == 0 and r["said"] for r in asked)


def test_two_elements_in_the_band_are_inside_the_one_percent_the_user_allows():
    mesh = _mesh([_rect(10, 10)] * 198 + [_rect(50, 10)] * 2)
    aspect = SR.report(mesh, criteria())["rows"][4]
    assert (aspect["outside"], aspect["beyond"], aspect["met"], aspect["excess"]) == (2, 0, True, 0)
    assert MR.line(aspect) == ("Aspect ratio under 4 (up to 1% of elements between 4 and 6): met - 1% of elements (2) "
                               "are over 4, none over 6; the worst is 5.")


def test_what_a_shell_cannot_take_is_told_as_not_applied_never_as_met():
    notes = "thickness 2 mm. second order elements. no triangles. non-orthogonality under 65"
    req = MR.check(json.dumps({"stated_unit": "none", "other": [],
                               "sizes": [{"what": "thickness", "value": 2, "unit": "mm", "quote": "thickness 2 mm"}],
                               "element_kind": [{"what": "quadratic", "value": 0, "quote": "second order elements"},
                                                {"what": "quads_only", "value": 0, "quote": "no triangles"}],
                               "limits": [{"measure": "non_orthogonality", "shape": "any", "direction": "max",
                                           "target": 65, "allowed_percent": 0, "outer": 0, "has_outer": False,
                                           "quote": "non-orthogonality under 65"}]}), notes)[0]
    mesh = _mesh([_rect(10, 10)] * 9, [[(0, 0, 0), (10, 0, 0), (0, 10, 0)]])
    rows = {r["field"]: r for r in SR.report(mesh, req)["rows"]}
    assert rows["thickness"]["met"] is None and "measured from your CAD (2 mm)" in rows["thickness"]["delivered"]
    assert rows["quadratic"]["met"] is None and rows["quadratic"]["applied"] is False
    assert rows["limit:non_orthogonality"]["met"] is None and "not a measure of shell elements" in rows[
        "limit:non_orthogonality"]["delivered"]
    assert rows["quads_only"]["met"] is False and rows["quads_only"]["outside"] == 1
    assert "10% of elements (1) are triangles" in rows["quads_only"]["delivered"]
    assert not any("met" == MR.line(r).split(": ")[-1].rstrip(".") for r in rows.values() if r["met"] is not True)


# ------------------------------------------------------------------ the real shell run -------------------------
pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.engines import shell_delivery as SD  # noqa: E402
from meshpipeline.engines import shell_run  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = sf.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


def _sha(ws: Path, names=("shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb", "shell.msh")) -> dict:
    return {n: hashlib.sha256((ws / n).read_bytes()).hexdigest() for n in names}


def _run(ws: Path, req: dict | None = None) -> dict:
    if req is not None:
        MR.write(ws, MR.section("used", requirements=req))
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged"]) == 0
    return shell_run.read(ws)


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    return _stage(sf.plate_with_hole(), tmp_path_factory.mktemp("plate"))


def _copy(src: Path, ws: Path) -> Path:
    ws.mkdir(parents=True)
    (ws / "geometry.step").write_bytes((src / "geometry.step").read_bytes())
    return ws


def test_no_requirement_leaves_no_trace_in_the_record(tmp_path, plate):
    rec = _run(_copy(plate, tmp_path / "a"))
    assert rec["made"] and not ({"requirements", "sizing", "count_set_aside"} & set(rec))
    applied = json.loads((tmp_path / "a" / "shell_quality.json").read_text())
    assert not ({"requirements", "sizing"} & set(applied)) and "aimed_split" not in applied["quality"]
    assert all("SHMESH-24" not in r["rule"] for r in applied["faces"])
    # an empty reading in the workspace is no requirement either: the same files, byte for byte
    b = _copy(plate, tmp_path / "b")
    (b / MR.FILE).write_text(json.dumps(MR.section("used", requirements=MR.empty())))
    rec_b = _run(b)
    assert _sha(tmp_path / "a") == _sha(b)
    def untimed(d):
        # (timings at any depth - "seconds", "*_s" - differ from run to run)
        if isinstance(d, dict):
            return {k: untimed(v) for k, v in d.items() if not k.endswith("_s") and k != "seconds"}
        return d

    assert untimed(rec) == untimed(rec_b)


def test_the_global_size_sets_the_shells_size_field(tmp_path, plate):
    rules = _run(_copy(plate, tmp_path / "rules"))
    fine = _run(_copy(plate, tmp_path / "fine"), criteria(0.4))          # 4 mm
    coarse = _run(_copy(plate, tmp_path / "coarse"), criteria(2.0))      # 20 mm
    assert fine["made"] and coarse["made"]
    # the count follows the size asked for (a 200 x 100 plate: about 20,000 / size^2), not our rules' plan
    assert 0.6 * 20000 / 16 <= fine["elements"] <= 1.6 * 20000 / 16
    assert 0.5 * 20000 / 400 <= coarse["elements"] <= 2.5 * 20000 / 400
    assert coarse["elements"] < rules["elements"] < fine["elements"]
    for rec, size in ((fine, 4.0), (coarse, 20.0)):
        row = rec["requirements"]["rows"][0]
        assert row["field"] == "global" and row["met"] is True and 0.7 * size <= row["measured"] <= 1.3 * size
        assert rec["sizing"]["global_mm"] == size
    # coarser than our accuracy rules: delivered, and said (never refused)
    assert "coarser than our accuracy rules" in coarse["requirements"]["rows"][0]["delivered"]


def test_the_criteria_list_on_a_plate_is_delivered_and_told_row_by_row(tmp_path, plate):
    ws = _copy(plate, tmp_path / "ws")
    rec = _run(ws, criteria(0.5))                                        # 5 mm
    rows = rec["requirements"]["rows"]
    assert len(rows) == 10 and all(r["met"] in (True, False) for r in rows[3:])
    assert all(r["met"] for r in rows[3:]), [MR.line(r) for r in rows if not r["met"]]   # every quality line met
    assert rows[1]["outside_pct"] <= 5.0 and rows[2]["outside_pct"] <= 5.0                # the edge lengths: a few percent
    assert rec["quality"]["valid"] is True and rec["quality"]["within_nastran"] is True
    # the result: one row per requirement, in plain words; a miss is a note, never a failure
    assert SD.deliver(ws, rec)
    from meshpipeline.engines.fea_results import results

    block = results(ws)
    # one row per measured requirement; the user's Jacobian line is the header's worst-quality row (SHMESH-26)
    asked = [r for r in block["check"]["rows"] if r["key"].startswith("asked_") or r.get("asked")]
    assert len(asked) == sum(1 for r in rows if r["met"] is not None) >= 8 and all(not r["gating"] for r in asked)
    head = next(r for r in asked if r["key"] == SD.WORST_KEY)
    assert head["words"].startswith("Jacobian over 0.6") and head["limit"] == 0.4 and head["ok"] is True
    warp = next(r for r in asked if r["key"] == "asked_r4")
    assert warp["words"] == "Warpage under 15° (up to 1% of elements between 15° and 30°): met."
    assert block["check"]["outcome"] in ("pass", "concerns")
    missed = [r for r in rows if r["met"] is False]
    if missed:
        assert block["check"]["outcome"] == "concerns"
        assert block["check"]["words"].startswith(f"Delivered, with notes: the shell is valid; {len(missed)} of the "
                                                  f"{len(asked)} limits your notes set")
    assert block["requirements"]["missed"] == len(missed)
    assert [w for w in block["plan_words"] if w.startswith("Global element size 5 mm:")]
    # the verdict reads the same rows the screen shows
    from meshpipeline.engines.verdict_limits import fea_limits, words

    limits = fea_limits(json.loads((ws / "quality.json").read_text()))
    jac = next(r["id"] for r in rows if r["field"] == "limit:jacobian")
    assert sorted(r["key"] for r in limits if r["key"].startswith("asked_")) == sorted(
        f"asked_{jac}" if r["key"] == SD.WORST_KEY else r["key"] for r in asked)
    # judged by the user's limits alone: none of our usual targets is a row
    assert not any(r["key"] in ("shell_skew_deg", "shell_aspect", "shell_min_angle_quad") for r in limits)
    assert words(next(r for r in limits if r["key"] == "asked_r4")) == warp["words"]


def test_the_same_notes_twice_give_the_same_mesh(tmp_path, plate):
    a = _run(_copy(plate, tmp_path / "a"), criteria(0.5))
    b = _run(_copy(plate, tmp_path / "b"), criteria(0.5))
    assert _sha(tmp_path / "a") == _sha(tmp_path / "b")
    assert a["requirements"] == b["requirements"] and a["elements"] == b["elements"]


def test_triangles_only_makes_no_quad(tmp_path, plate):
    req = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "limits": [], "other": [], "element_kind": [
        {"what": "triangles_only", "value": 0, "quote": "triangles only"}]}), "triangles only")[0]
    rec = _run(_copy(plate, tmp_path / "ws"), req)
    assert rec["made"] and rec["quads"] == 0 and rec["trias"] == rec["elements"] > 0
    assert rec["requirements"]["rows"][0]["met"] is True and rec["quality"]["valid"] is True


def test_a_count_beside_a_global_size_is_set_aside_and_said(tmp_path, plate):
    ws = _copy(plate, tmp_path / "ws")
    MR.write(ws, MR.section("used", requirements=criteria(0.5)))
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged",
                           json.dumps({"elements": 20000, "mode": "about"})]) == 0
    rec = shell_run.read(ws)
    assert rec["count_set_aside"] == 20000 and "target" not in rec and rec["elements"] < 3000
    assert "Your element count (20,000) was not used: the global size you gave decides the sizes." in rec["words"]


def test_what_was_not_applied_stays_with_the_result_and_changes_no_mesh(tmp_path, plate):
    """NOTE-8: notes that ask for something no setting matches give the rules' own shell, byte for byte, and the
    result still says what was not applied."""
    plain = _copy(plate, tmp_path / "plain")
    _run(plain)
    ws = _copy(plate, tmp_path / "ws")
    sec = MR.section("used", requirements=MR.empty(), not_applied=["3 elements through the thickness"])
    assert MR.write(ws, sec) is True and shell_run.requirements_in(ws) is None
    rec = _run(ws)
    assert _sha(ws) == _sha(plain) and "requirements" not in rec
    assert SD.deliver(ws, rec)
    from meshpipeline.engines.fea_results import results

    line = 'Not applied: "3 elements through the thickness" - no mesh setting of ours matches it yet.'
    assert line in results(ws)["plan_words"]
    assert SR.summary(None, 1) == "1 thing your notes say about the mesh could not be applied (listed with the result)."
    assert SR.summary(None, 0) == ""


def test_a_mixed_run_applies_the_notes_to_its_shell_and_its_solid(tmp_path, monkeypatch):
    """MIXED + NOTE-7: the shell part takes the user's sizes and limits and reports them; the solid region takes the
    size; the two still join. With no notes the mixed run is the run it always was (test_mixed_stage_run.py)."""
    import asyncio

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import mixed_run

    class _Run:
        deadline_s = None

    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    plain = _stage(sf.plate_with_boss(), tmp_path / "plain")
    base = asyncio.run(FD.shell_pass(plain, {"fea_mesh_as": "mixed"}, job_id="j", run=_Run()))
    assert base["made"] is True and "requirements" not in base
    ws = _stage(sf.plate_with_boss(), tmp_path / "ws")
    MR.write(ws, MR.section("used", requirements=criteria(0.6)))                 # 6 mm
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "mixed"}, job_id="j", run=_Run()))
    assert rec["made"] is True, rec.get("why")
    for f in mixed_run.FILES:
        assert (ws / f).stat().st_size > 0, f
    rows = rec["requirements"]["rows"]
    assert len(rows) == 10 and rows[0]["field"] == "global" and rows[0]["met"] is True
    assert 0.7 * 6.0 <= rows[0]["measured"] <= 1.3 * 6.0
    assert rec["counts"]["links"] > 0 and rec["counts"]["shell_elements"] != base["counts"]["shell_elements"]
    # the solid region was planned at the size asked for, too
    plan = json.loads((ws / mixed_run.FOLDER / "solid" / "fea_plan.json").read_text())
    assert plan["base_size_m"] == pytest.approx(0.006) and plan["user_sizes"]["Part_1"]["whole"] is True


def test_a_mesh_past_a_limit_is_set_against_435s_steps_and_the_record_tells_both(tmp_path, plate, monkeypatch):
    """SHMESH-36: both candidate meshes - this PR's steps and #435's - and the one kept, with the rule that chose it,
    are written into the record."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    real = SA._standing

    def standing(applied, past_n, other=None, requirements=None):
        got = real(applied, past_n, other, requirements)
        return got if other is not None else (got[0], 0, max(got[2], 1), got[3])      # always "past a limit"

    monkeypatch.setattr(SA, "_standing", standing)
    ws = _copy(plate, tmp_path / "ws")
    _run(ws, criteria(0.5))
    kept = json.loads((ws / "shell_quality.json").read_text())["kept"]
    assert kept["rule"] == "SHMESH-36" and len(kept["candidates"]) == 2
    names = [c["mesh"] for c in kept["candidates"]]
    assert names == ["this PR's steps (SHMESH-27..35)", "#435's steps"] and kept["kept"] in names
    for c in kept["candidates"]:
        assert {"elements", "trias", "past_solver", "nastran_past", "lines"} <= set(c) and len(c["lines"]) == 7
        assert all({"id", "asked", "outside", "beyond", "worst", "met"} <= set(r) for r in c["lines"])
    assert kept["why"] and set(kept["standing"]) == set(names)
