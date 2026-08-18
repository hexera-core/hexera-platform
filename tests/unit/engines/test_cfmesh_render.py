# Responsibility: Verify the rendered meshDict is valid, budget-clamped, and cannot be injected through a feature.
from __future__ import annotations

import tempfile
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.cfmesh import cfmesh_runner as R  # noqa: E402


def _render(**strategy) -> str:
    ws = Path(tempfile.mkdtemp())
    body_bbox = ([-0.5, -0.1, -0.1], [0.5, 0.1, 0.1])
    dmin, dmax = R.domain_from_strategy(body_bbox, L=1.0, strategy=strategy)
    R.render_cfmesh_case(
        ws, surface_file="geom.fms", wall_patch="airfoil",
        patches=[{"name": "airfoil", "type": "wall"},
                 {"name": "farfield", "type": "farfield"}],
        body_bbox=body_bbox, L=1.0, domain_min=dmin, domain_max=dmax,
        strategy=strategy, cell_budget=8_000_000)
    return (ws / "system" / "meshDict").read_text()


def test_render_is_valid_and_complete():
    md = _render(max_cell_factor=40, wall_cell=0.05, n_layers=4,
                 first_layer_thickness=0.001)
    assert md.count("{") == md.count("}"), "unbalanced braces"
    for block in ("surfaceFile", "maxCellSize", "localRefinement",
                  "boundaryLayers", "renameBoundary"):
        assert block in md
    assert 'surfaceFile "geom.fms";' in md
    assert "airfoil { cellSize 0.05; }" in md
    assert "nLayers 4;" in md and "maxFirstLayerThickness 0.001;" in md


def test_no_layers_omits_the_block():
    md = _render(n_layers=0)
    assert "boundaryLayers" not in md
    assert "localRefinement" in md          # base blocks still present


def test_rename_boundary_maps_roles_to_openfoam_types():
    md = _render()
    assert "airfoil { newName airfoil; type wall; }" in md
    assert "farfield { newName farfield; type patch; }" in md


def test_budget_clamp_coarsens_a_too_fine_background():
    ws = Path(tempfile.mkdtemp())
    body_bbox = ([-0.5, -0.5, -0.5], [0.5, 0.5, 0.5])
    dmin, dmax = R.domain_from_strategy(body_bbox, L=1.0)
    s = R.render_cfmesh_case(
        ws, surface_file="geom.fms", wall_patch="body",
        patches=[{"name": "body", "type": "wall"}],
        body_bbox=body_bbox, L=1.0, domain_min=dmin, domain_max=dmax,
        strategy={"max_cell_factor": 100000}, cell_budget=1_000_000)
    assert s["est_background_cells"] <= 1_000_000, s


def test_object_refinements_render_valid_types_and_skip_junk():
    md = _render(features=[
        {"name": "wake", "type": "box", "cellSize": 0.1,
         "centre": [3, 0, 0], "lengthX": 6, "lengthY": 1, "lengthZ": 1},
        {"name": "nose", "type": "sphere", "cellSize": 0.05,
         "centre": [0, 0, 0], "radius": 0.3},
        {"name": "bad", "type": "box", "cellSize": 0.1},          # missing geometry → skipped
        {"name": "unknown", "type": "torus", "cellSize": 0.1},    # bad type → skipped
    ])
    assert "wake { type box;" in md and "nose { type sphere;" in md
    assert "bad {" not in md and "torus" not in md
    assert md.count("{") == md.count("}")


def test_features_cannot_inject_raw_text():
    md = _render(features=[
        {"name": "evil", "type": "box", "cellSize": 0.1, "centre": [0, 0, 0],
         "lengthX": 1, "lengthY": 1, "lengthZ": 1}])
    assert md.count("{") == md.count("}")


def test_cfmesh_pack_is_strategy_not_syntax():
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.engines.cfmesh.pack import CFMESH_SYSTEM, CFMESH_TOOL_NAMES

    # the LLM no longer hand-writes the dict or looks up syntax
    assert "write_file" not in CFMESH_TOOL_NAMES
    assert "query_syntax" not in CFMESH_TOOL_NAMES
    assert "prepare_surface" not in CFMESH_TOOL_NAMES
    assert "configure_mesh" in CFMESH_TOOL_NAMES
    # "you do not hand-write" is a Builder-wide rule and now lives in the packaged role prompt;
    # what the BUNDLE must still say is how cfMesh in particular is configured. Assert the rule
    # against the message the builder is actually given - the composition, not either half.
    assert "hand-write" in polcfg.prompts.builder_system
    assert "configure_mesh(strategy)" in CFMESH_SYSTEM


def test_configure_mesh_is_delegated_to_the_engine_bundle():
    # configure_mesh lives in the meshing family since.
    src = (APP / "agents" / "builder" / "tools" / "meshing.py").read_text()
    assert 'if ename == "cfmesh":' not in src        # the leak is gone
    assert "R.configure_mesh(" in src                # uniform delegation
    # the render body now lives in the cfMesh bundle library
    from meshpipeline.engines.cfmesh import cfmesh_runner
    assert callable(cfmesh_runner.configure_mesh)
    from meshpipeline.engines.runtime import get_engine
    assert hasattr(get_engine("cfmesh"), "configure_mesh")
    assert not hasattr(get_engine("gmsh"), "configure_mesh")


def test_cfmesh_run_policy_requires_the_rendered_dict():
    from meshpipeline.engines.registry import get_spec
    assert get_spec("cfmesh").run_policy.required_files == ("system/meshDict",)
