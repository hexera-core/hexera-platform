# Responsibility: Verify the LOCAL layer repair - a pass whose prism layers folded loses its layers
# only round the spots checkMesh located, never across whole classifier classes - and the layer
# controls every snappy case is authored with.
# Boundaries: the policy, the located evidence and the dict this system authors; the mesher's own
# response is measured in the lab (production image), not here.
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.agents.reviewer.context import layer_policy_line
from meshpipeline.engines.snappy import drivers as D
from meshpipeline.engines.snappy import foam_exec as FE
from meshpipeline.engines.snappy import layer_policy as LP
from meshpipeline.engines.snappy import snappy_runner as R
from meshpipeline.engines.snappy.finalize import folded_class_regions

# base_cell 1/24, level 2 -> intended wall cell 1/96 ~ 0.0104 m
REC = {"base_cell": 1.0 / 24, "surface_level": (2, 2), "afford_level": 3,
       "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
       "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
       "min_feature": 0.001, "surface_area": 1.0, "cells_across_min_feature": 8}
STRATEGY = {"n_layers": 3, "first_layer_rel": 0.35, "max_cells": 2_000_000}
H = (1.0 / 24) / 4
ANALYSIS = {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [1.0, 1.0, 1.0],
            "extent": [1.0, 1.0, 1.0], "diag": 1.7320508, "L": 1.0,
            "surface_area": 6.0, "min_feature": 0.05, "curvature_hi": 0.05}


def plate(n=20, size=1.0, z=0.0):
    """A flat square of 2*n*n triangles in the z plane, as prepare_surface hands them over."""
    tris = []
    s = size / n
    for i in range(n):
        for j in range(n):
            a, b = (i * s, j * s, z), ((i + 1) * s, j * s, z)
            c, d = ((i + 1) * s, (j + 1) * s, z), (i * s, (j + 1) * s, z)
            tris += [[a, b, c], [a, c, d]]
    return tris


# -- every case: the layer controls ----------------------------------------------------------


def test_every_snappy_case_asks_snappy_to_try_every_layer_point(tmp_path):
    (tmp_path / "system").mkdir()
    R.render_snappy_case(tmp_path, surface_name="body", feature_file="body.eMesh",
                         analysis=ANALYSIS, recommendation=REC, domain_min=[-4.0, -3.0, -3.0],
                         domain_max=[7.0, 4.0, 4.0], strategy=STRATEGY)
    d = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    assert f"layerTerminationAngle {R.LAYER_TERMINATION_ANGLE};" in d
    assert R.LAYER_TERMINATION_ANGLE == -180
    assert "nBufferCellsNoExtrude 0;" in d and "featureAngle 130;" in d


def _quality_block(d: str) -> tuple[str, str]:
    q = d[d.index("meshQualityControls"):]
    return q[:q.index("relaxed")], q[q.index("relaxed"):]


def test_balanced_keeps_the_tet_check_for_the_snap_and_relaxes_it_for_layers_only(tmp_path):
    (tmp_path / "system").mkdir()
    R.render_snappy_case(tmp_path, surface_name="body", feature_file="body.eMesh",
                         analysis=ANALYSIS, recommendation=REC, domain_min=[-4.0, -3.0, -3.0],
                         domain_max=[7.0, 4.0, 4.0], strategy=STRATEGY)
    d = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    main, relaxed = _quality_block(d)
    # the snap reads the main set: inverted cells are refused there
    assert "minTetQuality 1e-13;" in main
    # the layer stage reads the relaxed set from its first iteration, tet check off there only
    assert "nRelaxedIter 0;" in d and "minTetQuality -1e30;" in relaxed
    # non-orthogonality stays at the bar the reviewer judges, in both
    assert "maxNonOrtho 65;" in main and "maxNonOrtho 65;" in relaxed


def test_strict_is_unchanged(tmp_path):
    (tmp_path / "system").mkdir()
    R.render_snappy_case(tmp_path, surface_name="body", feature_file="body.eMesh",
                         analysis=ANALYSIS, recommendation=REC, domain_min=[-4.0, -3.0, -3.0],
                         domain_max=[7.0, 4.0, 4.0], strategy={**STRATEGY, "quality": "strict"})
    d = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    main, relaxed = _quality_block(d)
    assert "minTetQuality 1e-13;" in main and "minTetQuality" not in relaxed
    assert "nRelaxedIter 6;" in d and "maxThicknessToMedialRatio 0.3;" in d


# -- where the mesh is invalid ------------------------------------------------------------------


def test_obj_face_centres_reads_writemeshobj_output():
    text = ("v 4.62182 0.775399 0.748599\nv 4.62181 0.775306 0.746923\n"
            "v 4.61535 0.775646 0.749095\nv 4.61591 0.774761 0.742294\nf 1 2 3 4 1\n")
    (c,) = FE._obj_face_centres(text)
    assert c == pytest.approx([4.6187225, 0.775278, 0.74672775])
    # a file of bare vertices (no face records) still places the spot
    assert FE._obj_face_centres("v 1 2 3\n") == [[1.0, 2.0, 3.0]]
    assert FE._obj_face_centres("") == []


def test_fatal_points_turns_checkmesh_sets_into_spots(tmp_path, monkeypatch):
    sets = tmp_path / "constant" / "polyMesh" / "sets"
    sets.mkdir(parents=True)
    (sets / "wrongOrientedFaces").write_text("1\n(\n7\n)\n")
    calls = []

    def fake_run(cmd, cwd, **kw):
        calls.append(cmd[-1])
        (tmp_path / "meshPoints_constant_wrongOrientedFaces.obj").write_text(
            "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4 1\n")

    monkeypatch.setattr(FE, "run_guarded", fake_run)
    pts = FE.fatal_points(tmp_path)
    assert pts == [[0.5, 0.5, 0.0]]
    assert len(calls) == 1 and "writeMeshObj -constant -faceSet wrongOrientedFaces" in calls[0]
    assert not list(tmp_path.glob("*.obj")), "the tool's output is cleaned up"
    # no set written -> nothing located, nothing run
    (sets / "wrongOrientedFaces").unlink()
    assert FE.fatal_points(tmp_path) == [] and len(calls) == 1


def test_fatal_points_are_capped_to_a_sample(tmp_path, monkeypatch):
    sets = tmp_path / "constant" / "polyMesh" / "sets"
    sets.mkdir(parents=True)
    (sets / "zeroVolumeCells").write_text("x")

    def fake_run(cmd, cwd, **kw):
        (tmp_path / "cells.obj").write_text("".join(f"v {i} 0 0\n" for i in range(50)))

    monkeypatch.setattr(FE, "run_guarded", fake_run)
    assert len(FE.fatal_points(tmp_path, limit=10)) == 10


# -- the repair --------------------------------------------------------------------------------


def test_repair_mask_takes_only_the_triangles_round_the_spots():
    tris = plate()
    mask = LP.repair_mask(tris, [[0.5, 0.5, 0.0]], 0.1)
    c = np.asarray(tris).mean(axis=1)
    assert mask.any() and not mask.all()
    # every triangle taken reaches the ball; none taken lies wholly outside it
    T = np.asarray(tris)
    near = np.array([LP._point_triangle_distance(np.array([0.5, 0.5, 0.0]), T[i:i + 1])[0]
                     for i in range(len(T))])
    assert np.array_equal(mask, near <= 0.1)
    assert np.all(np.linalg.norm(c[mask] - [0.5, 0.5, 0.0], axis=1) <= 0.1 + 0.05)
    assert not LP.repair_mask(tris, [], 0.1).any()


def test_a_spot_on_a_long_sliver_takes_that_sliver():
    # a CAD tessellation lays half-metre slivers over flat panels: the spot sits ON the triangle
    # while its centroid is far away - the triangle is still the one the fold sits on
    sliver = [[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.02, 0.0)]]
    spot = [[0.05, 0.001, 0.003]]
    assert LP.repair_mask(sliver, spot, 0.01).tolist() == [True]
    assert LP.points_on_wall(spot, sliver, 0.01) == spot
    assert LP.repair_mask(sliver, [[0.05, 0.001, 0.5]], 0.01).tolist() == [False]


@pytest.mark.parametrize("p, want", [
    ((0.2, 0.2, 1.0), 1.0),                 # above the interior
    ((-1.0, -1.0, 0.0), 2 ** 0.5),          # nearest a vertex
    ((0.5, -2.0, 0.0), 2.0),                # nearest an edge
    ((1.0, 1.0, 0.0), 0.5 ** 0.5),          # beyond the hypotenuse
])
def test_point_triangle_distance_is_exact(p, want):
    T = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]])
    assert LP._point_triangle_distance(np.array(p), T)[0] == pytest.approx(want)


def test_a_spot_off_the_wall_is_no_fold_a_layer_cut_can_fix():
    tris = plate()
    near, far = [0.5, 0.5, 0.01], [0.5, 0.5, 3.0]
    assert LP.points_on_wall([near, far], tris, 0.05) == [near]
    assert LP.points_on_wall([], tris, 0.05) == []


def test_the_repair_grows_over_every_spot_seen_and_is_finite(tmp_path):
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    assert LP.read_repair(ws) is None
    r1 = LP.escalate_repair(ws, [[0.1, 0.1, 0.0]])
    assert r1 == {"stage": 1, "points": [[0.1, 0.1, 0.0]]}
    r2 = LP.escalate_repair(ws, [[0.1, 0.1, 0.0], [0.9, 0.9, 0.0]])
    assert r2["stage"] == 2 and len(r2["points"]) == 2, "a spot already known is not doubled"
    # the class ladder's own stage survives beside the repair in the one fact
    LP.write_escalation(ws, 1)
    assert LP.read_escalation(ws) == 1 and LP.read_repair(ws)["stage"] == 2
    assert LP.escalate_repair(ws, [[0.5, 0.5, 0.0]])["stage"] == LP.MAX_REPAIR_STAGE
    assert LP.escalate_repair(ws, [[0.6, 0.6, 0.0]]) is None, "spent - the planner takes over"
    # a pipeline retry meshes the same geometry: the next attempt starts from what was measured
    ws2 = tmp_path / "attempt_2"
    ws2.mkdir()
    assert LP.read_repair(ws2)["stage"] == LP.MAX_REPAIR_STAGE


def test_repair_radius_grows_with_the_stage_in_wall_cells():
    assert LP.repair_radius_m(H, 1) == pytest.approx(LP.REPAIR_RADIUS_CELLS[1] * H)
    assert LP.repair_radius_m(H, 2) > LP.repair_radius_m(H, 1)
    assert LP.repair_radius_m(H, 99) == LP.repair_radius_m(H, LP.MAX_REPAIR_STAGE)


def test_a_repair_keeps_the_full_request_everywhere_but_round_the_spots(tmp_path):
    rep = {"stage": 1, "points": [[0.5, 0.5, 0.0]]}
    pol = LP.plan_repair_policy(None, rec=REC, strategy=STRATEGY, wall_name="car", repair=rep)
    assert pol.mode == "split" and pol.policy["repair_stage"] == 1
    assert pol.policy["region_patches"] == {"car": "normal", "car_repair": "repair"}
    label = LP.make_region_labeler(pol, "car")
    tris = plate(n=60)             # triangles finer than the 2-wall-cell repair radius
    names = label(tris)
    assert set(names) == {"car", "car_repair"}
    frac = pol.policy["classes"]["repair"]["area_frac"]
    assert 0.0 < frac < 0.1, "the record says how much of the wall the repair took"
    # the counts the renderer gets: full request on the wall, none in the repair
    assert LP.layer_counts_for(pol) == {"car": 3, "car_repair": 0}
    # no repair recorded -> nothing changes
    assert LP.plan_repair_policy(None, rec=REC, strategy=STRATEGY, wall_name="car",
                                 repair=None) is None


def test_a_repair_overrides_the_classifiers_class_cuts():
    from meshpipeline.cad.thin_features import ThinFeatureField
    field = ThinFeatureField(thickness_m=np.array([np.inf] * 90 + [0.03] * 6 + [0.005] * 4),
                             sharp=np.zeros(100, bool), area_m2=np.ones(100), measured=True)
    base = LP.plan_layer_policy(field, rec=REC, strategy=STRATEGY, wall_name="car", stage=3)
    assert base.policy["classes"]["razor"]["n_layers"] == 0
    pol = LP.plan_repair_policy(base, rec=REC, strategy=STRATEGY, wall_name="car",
                                repair={"stage": 2, "points": [[0, 0, 0]]})
    assert {c: v["n_layers"] for c, v in pol.policy["classes"].items()} == {
        "normal": 3, "thin": 3, "razor": 3, "repair": 0}


def test_a_wall_of_the_users_own_parts_is_never_split():
    assert LP.can_split([], "car")
    assert LP.can_split(["car", "car_repair"], "car")
    assert not LP.can_split(["car", "wheels"], "car")
    pol = LP.plan_repair_policy(None, rec=REC, strategy=STRATEGY, wall_name="car",
                                repair={"stage": 1, "points": [[0, 0, 0]]})
    # the split did not stage (the surface carried CAD parts): the record must not claim a repair
    rec = LP.reconcile_policy(pol, ["car", "wheels"], "car")
    assert rec.mode == "uniform" and "repair" not in rec.policy["classes"]


def test_the_repair_region_is_folded_back_into_the_declared_wall(tmp_path):
    (tmp_path / "system").mkdir()
    out = R.render_snappy_case(tmp_path, surface_name="car", feature_file="car.eMesh",
                               analysis=ANALYSIS, recommendation=REC,
                               domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                               strategy=STRATEGY, surface_regions=["car", "car_repair"],
                               layer_counts={"car": 3, "car_repair": 0}, class_regions=True)
    d = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    assert "car { nSurfaceLayers 3; } car_repair { nSurfaceLayers 0; }" in d
    cp = (tmp_path / "system" / "createPatchDict").read_text()
    assert "patches (car car_repair)" in cp
    assert out["merged_regions"] == ["car_repair"]
    poly = tmp_path / "constant" / "polyMesh"
    poly.mkdir(parents=True)
    (poly / "boundary").write_text("2\n(\ncar\n{\n type wall;\n}\nfarfield\n{\n type patch;\n}\n)\n")
    assert folded_class_regions(tmp_path, {"region_patches": {"car": "normal",
                                                              "car_repair": "repair"}}) == {
        "car_repair": "car"}


def test_the_reviewer_is_told_exactly_what_the_repair_left_out():
    line = layer_policy_line({"mode": "split", "requested_layers": 3, "repair_stage": 1,
                              "classes": {"normal": {"n_layers": 3, "area_frac": 0.9},
                                          "repair": {"n_layers": 0, "area_frac": 0.0123,
                                                     "points": 2, "radius_m": 0.05}}})
    assert "2 spot(s)" in line and "50.0 mm" in line and "1.2% of the wall area" in line
    assert "full 3 layers" in line and "real finding" in line


# -- the driver's decision ---------------------------------------------------------------------


class _Surface:
    def __init__(self, path):
        self.path = path


def _staged(tmp_path):
    from meshpipeline.cad.stl_io import write_stl_solids
    p = tmp_path / "input.stl"
    write_stl_solids(p, {"body": plate()})
    return _Surface(p)


FATAL = {"fatal": ["incorrectly oriented faces"], "fatal_points": [[0.5, 0.5, 0.002]]}


def test_a_folded_pass_records_a_local_repair_and_says_so(tmp_path):
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    note = D._repair_after_fold(ws, FATAL, production=False, repairable=True,
                                surface=_staged(tmp_path), rec=REC, strategy=STRATEGY, job_id="j")
    assert note and "1 spot on the body" in note and "full layer count everywhere else" in note
    assert LP.read_repair(ws) == {"stage": 1, "points": [[0.5, 0.5, 0.002]]}
    assert json.loads((ws / LP.ESCALATION_FACT).read_text())["repair"]["stage"] == 1


def test_a_spot_inside_the_repaired_area_is_no_layer_fold(tmp_path):
    # the Camaro's snapped cells folded at the very same spot with no layer within 100 mm:
    # cutting more layers cannot fix that, so the repair stops and the planner takes over
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    surface = _staged(tmp_path)
    assert D._repair_after_fold(ws, FATAL, production=False, repairable=True, surface=surface,
                                rec=REC, strategy=STRATEGY, job_id="j")
    again = {"fatal": ["incorrectly oriented faces"], "fatal_points": [[0.505, 0.5, 0.002]]}
    assert D._repair_after_fold(ws, again, production=False, repairable=True, surface=surface,
                                rec=REC, strategy=STRATEGY, job_id="j") is None
    assert LP.read_repair(ws)["stage"] == 1, "no stage was spent on it"
    # a NEW spot elsewhere on the wall is repaired as usual
    elsewhere = {"fatal": ["negative-volume cells"], "fatal_points": [[0.1, 0.9, 0.0]]}
    assert D._repair_after_fold(ws, elsewhere, production=False, repairable=True,
                                surface=surface, rec=REC, strategy=STRATEGY, job_id="j")
    assert LP.read_repair(ws)["stage"] == 2


def test_a_fold_the_layers_did_not_cause_is_said_so_and_makes_the_next_plan_coarser(tmp_path):
    # the enodise splitter plate: after its one local repair the same plan came back with the same
    # face pointing the wrong way, four passes running, 25-40 minutes each (verify-5fix). The fold
    # is reported as not the layers', the plan is recorded as one this shape cannot take, and the
    # pass after it - this attempt's or the next attempt's - gets half the budget
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    surface = _staged(tmp_path)
    flag: list = []
    assert D._repair_after_fold(ws, FATAL, production=False, repairable=True, surface=surface,
                                rec=REC, strategy=STRATEGY, job_id="j", not_layers=flag)
    assert flag == [], "a first fold is the layers' to repair"
    again = {"fatal": ["incorrectly oriented faces"], "fatal_points": [[0.505, 0.5, 0.002]]}
    assert D._repair_after_fold(ws, again, production=False, repairable=True, surface=surface,
                                rec=REC, strategy=STRATEGY, job_id="j", not_layers=flag) is None
    assert flag == [True]
    D._write_timed_out_plan(ws, {**STRATEGY, "_why": "broken"})
    nxt = tmp_path / "attempt_2"
    nxt.mkdir()
    recorded = D._sibling_timed_out_plan(nxt)
    assert recorded and recorded["max_cells"] == 2_000_000
    plan, cuts = D._coarsen_after_timeout(recorded, dict(STRATEGY), internal=False)
    assert plan["max_cells"] == 1_000_000 and cuts


@pytest.mark.parametrize("q, production, repairable", [
    (FATAL, True, True),                                           # a production-grade pass
    (FATAL, False, False),                                         # the user's own parts
    ({"fatal": ["open cells"], "fatal_points": [[0.5, 0.5, 0]]}, False, True),  # not a fold
    ({"fatal": ["incorrectly oriented faces"]}, False, True),      # not located (old mesh image)
    ({"fatal": ["incorrectly oriented faces"], "fatal_points": [[0.5, 0.5, 5.0]]}, False, True),
])
def test_no_repair_where_a_layer_cut_cannot_help(tmp_path, q, production, repairable):
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    assert D._repair_after_fold(ws, q, production=production, repairable=repairable,
                                surface=_staged(tmp_path), rec=REC, strategy=STRATEGY,
                                job_id="j") is None
    assert LP.read_repair(ws) is None
