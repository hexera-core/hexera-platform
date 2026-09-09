# Responsibility: Verify the thin-feature layer policy - classification to per-region layer
# counts, the authored dict fragments, and the pinned no-thin-features regression.
# Boundaries: the policy and the dict this system authors; the mesher's response is the
# validation campaign's subject (rig-gated).
from __future__ import annotations

import json

import numpy as np
import pytest

import meshpipeline.engines.snappy.settings as scfg
from meshpipeline.cad.thin_features import ThinFeatureField
from meshpipeline.engines.snappy import layer_policy as LP
from meshpipeline.engines.snappy import snappy_runner as R

# a recommendation exactly as recommend_refinement shapes it, with easy numbers:
# base_cell 1/24, level 2 -> intended wall cell 1/96 ~ 0.0104 m
REC = {"base_cell": 1.0 / 24, "surface_level": (2, 2), "afford_level": 3,
       "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
       "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
       "min_feature": 0.001, "surface_area": 1.0, "cells_across_min_feature": 8}
STRATEGY = {"n_layers": 5, "first_layer_rel": 0.35, "max_cells": 2_000_000}


def field_of(thickness, sharp=None):
    t = np.asarray(thickness, dtype=float)
    return ThinFeatureField(
        thickness_m=t,
        sharp=np.asarray(sharp, dtype=bool) if sharp is not None else np.zeros(len(t), bool),
        area_m2=np.ones(len(t)), measured=True)


def mixed_field(n_normal=90, n_thin=6, n_razor=4):
    # 0.03 sits between razor_below (~0.0104) and thin_below (~0.052); 0.005 under razor_below
    return field_of([np.inf] * n_normal + [0.03] * n_thin + [0.005] * n_razor)


# -- the plan-independent scale ---------------------------------------------------------------


def test_intended_surface_cell_mirrors_the_renderers_level_choice_without_deficit():
    assert LP.intended_surface_cell(REC, {}) == pytest.approx((1.0 / 24) / 4)
    # a strategy may move within [floor, afford], never outside
    assert LP.intended_surface_cell(REC, {"surface_level": [3, 3]}) == pytest.approx((1 / 24) / 8)
    assert LP.intended_surface_cell(REC, {"surface_level": [9, 9]}) == pytest.approx((1 / 24) / 8)
    assert LP.intended_surface_cell(REC, {"surface_level": [0, 0]}) == pytest.approx((1 / 24) / 4)
    # tolerant of the scalar spelling some stubs and older plans carry
    assert LP.intended_surface_cell({"base_cell": 1.0, "surface_level": 1}, None) == 0.5


def test_stack_thickness_is_the_geometric_sum_of_the_layer_stack():
    assert LP.stack_thickness_m(0.01, 0, 0.35) == 0.0
    got = LP.stack_thickness_m(0.01, 5, 0.35, expansion=1.2)
    r = 1 / 1.2
    assert got == pytest.approx(0.35 * 0.01 * (1 - r ** 5) / (1 - r))


# -- counts and the ladder --------------------------------------------------------------------


def test_class_layer_counts_per_stage_are_the_documented_table():
    assert LP.class_layer_counts(5, 0) == {"normal": 5, "thin": 3, "razor": 1}
    assert LP.class_layer_counts(5, 1) == {"normal": 5, "thin": 2, "razor": 1}
    assert LP.class_layer_counts(5, 2) == {"normal": 5, "thin": 1, "razor": 0}
    assert LP.class_layer_counts(3, 0) == {"normal": 3, "thin": 2, "razor": 1}
    assert LP.class_layer_counts(0, 0) == {"normal": 0, "thin": 0, "razor": 0}
    # past the terminal stage the terminal counts hold (total function, no KeyError ever)
    assert LP.class_layer_counts(5, 99) == LP.class_layer_counts(5, LP.MAX_ESCALATION_STAGE)


def test_the_ladder_is_finite_and_deterministic():
    assert LP.escalate(0) == 1
    assert LP.escalate(1) == 2
    assert LP.escalate(2) is None, "exhausted - the planner's freeform re-plan takes over"


# -- policy planning --------------------------------------------------------------------------


def test_a_mixed_surface_yields_a_split_policy_with_honest_fractions():
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    assert pol is not None and pol.mode == "split"
    p = pol.policy
    assert p["requested_layers"] == 5 and p["escalation_stage"] == 0
    assert p["classes"]["normal"]["n_layers"] == 5
    assert p["classes"]["thin"] == {"n_layers": 3, "area_frac": 0.06}
    assert p["classes"]["razor"] == {"n_layers": 1, "area_frac": 0.04}
    assert p["region_patches"] == {"body": "normal", "body_thin": "thin", "body_razor": "razor"}
    assert p["min_thickness_rel"] == 0.02, "razor present relaxes minThickness at stage 0"


def test_no_thin_features_means_no_policy_at_all():
    pol = LP.plan_layer_policy(field_of([np.inf] * 100), rec=REC, strategy=STRATEGY,
                               wall_name="body")
    assert pol is None


def test_below_the_area_floor_means_no_policy(monkeypatch):
    monkeypatch.setattr(scfg, "SNAPPY_THIN_AREA_FLOOR", 0.2)
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    assert pol is None


def test_zero_requested_layers_means_no_policy():
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy={"n_layers": 0},
                               wall_name="body")
    assert pol is None


def test_the_master_switch_disables_everything(monkeypatch):
    monkeypatch.setattr(scfg, "SNAPPY_THIN_LAYER_POLICY", False)
    assert LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY,
                                wall_name="body") is None


def test_a_wholly_thin_surface_degrades_to_a_global_policy_not_a_split():
    pol = LP.plan_layer_policy(field_of([0.005] * 100), rec=REC, strategy=STRATEGY,
                               wall_name="body")
    assert pol is not None and pol.mode == "global"
    assert pol.labels is None, "a global policy splits nothing"
    assert pol.policy["region_patches"] == {"body": "razor"}
    counts = LP.layer_counts_for(pol)
    assert counts == {"body": 1}, "the dominant class's count applies to the whole wall"


def test_a_broken_recommendation_costs_no_build(caplog):
    # perception is an improvement, never a prerequisite: garbage in -> None out, loudly
    pol = LP.plan_layer_policy(mixed_field(), rec={"surface_level": 2}, strategy=STRATEGY,
                               wall_name="body")
    assert pol is None


# -- reconcile against what was actually staged -----------------------------------------------


def test_reconcile_keeps_a_split_that_really_staged():
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    out = LP.reconcile_policy(pol, ["body", "body_razor", "body_thin"], "body")
    assert out.mode == "split"
    assert LP.layer_counts_for(out) == {"body": 5, "body_thin": 3, "body_razor": 1}


def test_reconcile_drops_class_regions_that_received_no_triangles():
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    out = LP.reconcile_policy(pol, ["body", "body_razor"], "body")
    assert set(LP.layer_counts_for(out)) == {"body", "body_razor"}


def test_reconcile_degrades_to_uniform_on_real_cad_regions():
    # the user's own named solids are never split; stage 0 keeps the request (knobs only)
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    out = LP.reconcile_policy(pol, ["fluid", "casing"], "body")
    assert out.mode == "uniform"
    assert LP.layer_counts_for(out) == {"body": 5}
    # from stage 1 the ladder reduces uniformly
    pol1 = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body",
                                stage=1)
    out1 = LP.reconcile_policy(pol1, ["fluid", "casing"], "body")
    assert LP.layer_counts_for(out1) == {"body": 2}


# -- the authored artefacts -------------------------------------------------------------------


def _workspace(tmp_path):
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    return tmp_path


ANALYSIS = {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [1.0, 1.0, 1.0],
            "extent": [1.0, 1.0, 1.0], "diag": 1.7320508, "L": 1.0,
            "surface_area": 6.0, "min_feature": 0.05, "curvature_hi": 0.05}


def test_split_surface_and_per_region_layer_counts_reach_the_dict(tmp_path):
    from tests.unit.cad.test_thin_feature_field import wedge

    from meshpipeline.cad.stl_io import write_stl_solids
    from meshpipeline.cad.thin_features import measure_from_triangles

    ws = _workspace(tmp_path)
    tris = wedge()
    write_stl_solids(ws / "input.stl", {"body": tris})
    f = measure_from_triangles(tris)
    pol = LP.plan_layer_policy(f, rec=REC, strategy=STRATEGY, wall_name="body")
    assert pol is not None and pol.mode == "split"
    prep = R.prepare_surface(ws, geometry_file="input.stl", wall_patch="body",
                             domain_min=[-1] * 3, domain_max=[2] * 3,
                             region_labeler=LP.make_region_labeler(pol, "body"))
    assert set(prep["surface_regions"]) == {"body", "body_thin", "body_razor"}
    staged = (ws / "constant" / "triSurface" / "body.stl").read_text()
    for name in ("body", "body_thin", "body_razor"):
        assert f"solid {name}" in staged
    pol = LP.reconcile_policy(pol, prep["surface_regions"], "body")
    summary = R.render_snappy_case(
        ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
        recommendation=REC, domain_min=[-4, -3, -3], domain_max=[7, 4, 4],
        strategy=STRATEGY, surface_regions=prep["surface_regions"],
        layer_counts=LP.layer_counts_for(pol), layer_overrides=LP.overrides_for(pol))
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "body { nSurfaceLayers 5; }" in text
    assert "body_thin { nSurfaceLayers 3; }" in text
    assert "body_razor { nSurfaceLayers 1; }" in text
    assert "minThickness 0.02;" in text, "razor present relaxes minThickness"
    assert text.count("patchInfo { type wall; }") == 3, "every class region is a wall patch"
    assert summary["layer_counts"] == LP.layer_counts_for(pol)


def test_escalated_overrides_reach_the_dict(tmp_path):
    ws = _workspace(tmp_path)
    R.render_snappy_case(
        ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
        recommendation=REC, domain_min=[-4, -3, -3], domain_max=[7, 4, 4],
        strategy=STRATEGY, surface_regions=["body", "body_razor"],
        layer_counts={"body": 5, "body_razor": 0},
        layer_overrides={"min_thickness_rel": 0.01, "max_thickness_to_medial": 0.3})
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "body { nSurfaceLayers 5; }" in text
    assert "body_razor { nSurfaceLayers 0; }" in text
    assert "minThickness 0.01;" in text
    assert "maxThicknessToMedialRatio 0.3;" in text
    assert "addLayers true;" in text, "one region still requests layers"


def test_an_all_zero_count_table_turns_the_layer_stage_off(tmp_path):
    ws = _workspace(tmp_path)
    R.render_snappy_case(
        ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
        recommendation=REC, domain_min=[-4, -3, -3], domain_max=[7, 4, 4],
        strategy=STRATEGY, layer_counts={"body": 0})
    assert "addLayers false;" in (ws / "system" / "snappyHexMeshDict").read_text()


def test_write_layer_policy_records_and_removes(tmp_path):
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    LP.write_layer_policy(tmp_path, pol)
    on_disk = json.loads((tmp_path / LP.LAYER_POLICY_FACT).read_text())
    assert on_disk == pol.policy
    LP.write_layer_policy(tmp_path, None)
    assert not (tmp_path / LP.LAYER_POLICY_FACT).exists(), (
        "a pass without a policy must remove a stale record - the report describes the "
        "delivered mesh, never a previous pass's trade")


def test_the_mirror_doubled_triangle_list_is_labelled_by_tiling():
    pol = LP.plan_layer_policy(mixed_field(), rec=REC, strategy=STRATEGY, wall_name="body")
    label = LP.make_region_labeler(pol, "body")
    base = [((0, 0, 0), (1, 0, 0), (0, 1, 0))] * 100
    once = label(base)
    twice = label(base * 2)
    assert twice == once + once
    # any OTHER mismatch degrades to all-normal rather than mislabel
    assert set(label(base * 3)) == {"body"}


# -- THE REGRESSION PIN: no thin features -> byte-identical authored case ---------------------

GOLDEN_SNAPPY_DICT = (
    'FoamFile{ version 2.0; format ascii; class dictionary; object snappyHexMeshDict; }\n'
    '\ncastellatedMesh true; snap true; addLayers true;\n'
    'geometry { body.stl { type triSurfaceMesh; name body; } }\n'
    'castellatedMeshControls { maxLocalCells 2000000; maxGlobalCells 2000000; minRefinementCells 10;\n'
    '  maxLoadUnbalance 0.10; nCellsBetweenLevels 3; features ( { file "body.eMesh"; level 4; } );\n'
    '  refinementSurfaces { body { level (3 3); } } resolveFeatureAngle 35;\n'
    '  refinementRegions { body { mode distance; levels ((0.0625 3) (0.25 2)); } }\n'
    '  locationInMesh (-3.78 -2.86 -2.86); allowFreeStandingZoneFaces true; }\n'
    'snapControls { nSmoothPatch 3; tolerance 2.0; nSolveIter 50; nRelaxIter 8; nFeatureSnapIter 15;\n'
    '  implicitFeatureSnap false; explicitFeatureSnap true; multiRegionFeatureSnap false; }\n'
    'addLayersControls { relativeSizes true; layers { body { nSurfaceLayers 5; } }\n'
    '  expansionRatio 1.2; finalLayerThickness 0.35; minThickness 0.05; nGrow 0; featureAngle 130;\n'
    '  slipFeatureAngle 30; nRelaxIter 8; nSmoothSurfaceNormals 2; nSmoothNormals 3; nSmoothThickness 10;\n'
    '  maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.5; minMedialAxisAngle 90;\n'
    '  nBufferCellsNoExtrude 0; nLayerIter 50; nRelaxedIter 20; }\n'
    'meshQualityControls { maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80;\n'
    '  minVol 1e-13; minTetQuality -1e30; minArea -1; minTwist 0.02; minDeterminant 0.001;\n'
    '  minFaceWeight 0.02; minVolRatio 0.01; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75;\n'
    '  relaxed { maxNonOrtho 75; maxInternalSkewness 4; } }\n'
    'mergeTolerance 1e-6; debug 0;\n')


def test_no_thin_features_authors_the_byte_identical_historical_dict(tmp_path):
    # Captured from the renderer BEFORE the thin-feature layer policy existed (fixed inputs).
    # A geometry with no thin features - policy None, no counts, no overrides - must author
    # EXACTLY this case: the policy is an addition at thin features, never a drift elsewhere.
    ws = _workspace(tmp_path)
    summary = R.render_snappy_case(
        ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
        recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
        strategy=STRATEGY)
    assert (ws / "system" / "snappyHexMeshDict").read_text() == GOLDEN_SNAPPY_DICT
    assert summary == {"divisions": [82, 83, 83], "surface_level": [3, 3], "feature_level": 4,
                       "location_in_mesh": [-3.78, -2.86, -2.86], "max_cells": 2000000,
                       "n_layers": 5, "domain_min": [-4.0, -3.0, -3.0],
                       "domain_max": [7.0, 4.0, 4.0]}, (
        "the no-policy summary carries exactly the historical keys - no layer_counts leak")
