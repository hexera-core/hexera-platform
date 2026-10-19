# Responsibility: Verify cfMesh's wall layers are asked for, rendered and MEASURED the way snappy's
# are: the meshDict names every declared wall with cfMesh's own keywords, a strategy without a
# layer count takes the one the request states, and the delivered mesh's quality record counts
# the layers against what the user asked for - or says why it could not.
# Boundaries: the meshDict renderer, the strategy hook and the finalize step's census, on small
# synthetic workspaces; no mesher runs.
from __future__ import annotations

from pathlib import Path

from test_layer_census import _write_column_mesh

from meshpipeline.engines.cfmesh.cfmesh_runner import render_cfmesh_case, with_requested_layers
from meshpipeline.engines.cfmesh.finalize import census_quality


def _render(ws: Path, strategy: dict, patches) -> str:
    render_cfmesh_case(ws, surface_file="geom.fms", wall_patch="body", patches=patches,
                       body_bbox=([0, 0, 0], [1, 1, 1]), L=1.0, domain_min=[-5, -5, -5],
                       domain_max=[5, 5, 5], strategy=strategy)
    return (ws / "system" / "meshDict").read_text()


def test_the_meshdict_names_every_wall_with_cfmeshs_own_keywords(tmp_path):
    text = _render(tmp_path, {"n_layers": 5, "thickness_ratio": 1.2},
                   [{"name": "body", "type": "wall"}, {"name": "wheels", "type": "wall"},
                    {"name": "ground", "type": "wall"}, {"name": "farfield", "type": "farfield"}])
    assert "allowedDiscontinuity 0;" in text and "allowDiscontinuity" not in text
    assert "optimiseLayer 1;" in text and "untangleLayers 1;" in text
    block = text[text.index("patchBoundaryLayers"):text.index("optimiseLayer")]
    assert "body\n" in block and "wheels\n" in block          # every declared wall, not only the first
    assert "ground" not in block and "farfield" not in block   # the floor and the far field carry none
    assert block.count("nLayers 5;") == 2


def test_no_layers_asked_renders_no_boundary_layers(tmp_path):
    assert "boundaryLayers" not in _render(tmp_path, {}, [{"name": "body", "type": "wall"}])


def test_a_strategy_without_a_count_takes_the_requests(tmp_path):
    (tmp_path / "request.txt").write_text("Internal CFD mesh ... Resolve the near-wall flow with 5 prism "
                                          "layers (expansion ratio 1.2).")
    s, note = with_requested_layers(tmp_path, {})
    assert s["n_layers"] == 5 and note["layers_requested"] == 5
    s, note = with_requested_layers(tmp_path, {"n_layers": 2})
    assert s["n_layers"] == 2 and "measured against 5" in note["layers_note"]   # the builder is told
    s, note = with_requested_layers(tmp_path, {"n_layers": 6})
    assert s["n_layers"] == 6 and "layers_note" not in note
    (tmp_path / "request.txt").write_text("mesh it")
    assert with_requested_layers(tmp_path, {}) == ({}, {})


def _workspace(tmp_path: Path, heights, request: str, dict_layers: int) -> Path:
    ws = tmp_path / "ws"
    _write_column_mesh(ws / "constant" / "polyMesh", heights)
    (ws / "system").mkdir(parents=True)
    (ws / "system" / "meshDict").write_text(
        f"boundaryLayers {{ patchBoundaryLayers {{ wall {{ nLayers {dict_layers}; }} }} }}")
    (ws / "request.txt").write_text(request)
    return ws


def test_two_layers_where_five_were_asked_read_as_two_of_five(tmp_path):
    ws = _workspace(tmp_path, [0.05, 0.06, 1.0, 1.0], "5 prism layers (expansion ratio 1.2)", 2)
    q = census_quality(ws, [{"name": "wall", "type": "wall"}, {"name": "side", "type": "outlet"}])
    assert q["layer_coverage_source"] == "census"
    assert q["layers_requested"] == 5 and q["layers_in_meshdict"] == 2 and q["layers_stated"] == 5
    assert q["layer_coverage_pct"] == 40.0 and q["layers_mean"] == 2.0
    assert q["layer_area_full_pct"] == 0.0 and q["layer_area_any_pct"] == 100.0
    assert q["per_patch_layers"]["wall"]["target"] == 5
    # what the meshDict asked, when the request states no count
    ws2 = _workspace(tmp_path / "b", [0.05, 0.06, 1.0, 1.0], "mesh the duct", 2)
    q2 = census_quality(ws2, [{"name": "wall", "type": "wall"}])
    assert q2["layers_requested"] == 2 and q2["layer_coverage_pct"] == 100.0


def test_a_mesh_that_cannot_be_measured_says_why(tmp_path, monkeypatch):
    from meshpipeline.engines import layer_census as lc
    ws = _workspace(tmp_path, [0.05, 0.06, 1.0, 1.0], "5 prism layers", 2)
    monkeypatch.setattr(lc, "CENSUS_MAX_CELLS", 3)
    q = census_quality(ws, [{"name": "wall", "type": "wall"}])
    assert q["layer_coverage_source"] == "unmeasured" and "census limit" in q["layer_census_note"]
    assert "layer_coverage_pct" not in q and q["layers_requested"] == 5
    # nothing asked anywhere: nothing claimed
    ws3 = tmp_path / "none"
    (ws3 / "system").mkdir(parents=True)
    (ws3 / "system" / "meshDict").write_text("maxCellSize 1;")
    assert census_quality(ws3, [{"name": "wall", "type": "wall"}]) == {}


def _review_context(quality: dict) -> str:
    from meshpipeline.agents.reviewer.context import build_review_prompt
    manifest = {"quality": quality, "patches": {"wall": [1]}, "quality_criteria": {"criteria": []}}
    _sys, ctx = build_review_prompt(
        manifest=manifest, nav_context={}, workspace=Path("/tmp"), step_basename="g.stl",
        patch_names=["wall"], patch_colour_legend="", mesh_units="m", request="r", review_brief="b",
        job_id="j", engine="cfmesh", purpose="internal_cfd")
    return ctx


def test_the_reviewer_reads_the_counted_layers_against_the_request(tmp_path):
    ws = _workspace(tmp_path, [0.05, 0.06, 1.0, 1.0], "5 prism layers (expansion ratio 1.2)", 2)
    ctx = _review_context(census_quality(ws, [{"name": "wall", "type": "wall"}]))
    assert "40.0%" in ctx and "wall: 40.0% (2.0/5 layers)" in ctx
    assert "COUNTED on the mesh itself" in ctx and "the mesher was told 2" in ctx
    assert "fewer layers than requested is a shortfall" in ctx
    assert "The request is MET when all 5 cover at least 95% of the wall area" in ctx


def test_the_reviewer_is_told_when_the_layers_could_not_be_counted():
    ctx = _review_context({"layers_requested": 5, "layer_coverage_source": "unmeasured",
                           "layer_census_note": "13,000,000 cells, above the census limit of 12,000,000"})
    assert "5 were requested, but the delivered layers could NOT be measured" in ctx
    assert "above the census limit" in ctx


def test_cfmesh_declares_the_layer_row_snappy_declares():
    from meshpipeline.engines.quality_criteria import criteria_for
    rows = {c.key: c for c in criteria_for("cfmesh")}
    snappy = {c.key: c for c in criteria_for("snappy")}
    assert rows["layer_coverage_pct"].op == snappy["layer_coverage_pct"].op == ">"
    assert rows["layer_coverage_pct"].gating is False and rows["layer_coverage_pct"].threshold == 0.0


def test_a_strategy_with_no_layers_takes_the_requests_count(tmp_path):
    # the builder ran a layerless "coarse base first" and submitted it: the attempt was spent
    (tmp_path / "request.txt").write_text("Inflate 5 prism boundary layers on the body.")
    s, note = with_requested_layers(tmp_path, {"n_layers": 0, "wall_cell": 0.2})
    assert s["n_layers"] == 5 and s["wall_cell"] == 0.2
    assert note["layers_requested"] == 5 and "asked for none" in note["layers_note"]
    (tmp_path / "request.txt").write_text("mesh it")
    assert with_requested_layers(tmp_path, {"n_layers": 0}) == ({"n_layers": 0}, {})


def test_the_builder_is_told_to_configure_the_layers_from_the_first_run():
    from meshpipeline.engines.cfmesh.pack import CFMESH_SYSTEM
    assert "coarse base FIRST" not in CFMESH_SYSTEM
    assert "first configure_mesh" in CFMESH_SYSTEM
