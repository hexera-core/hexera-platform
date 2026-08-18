# Responsibility: Verify an authoring tool accepts only its own engine's knobs and names the redirect for another's.
from __future__ import annotations

from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.base import Diagnostic  # noqa: E402
from meshpipeline.engines.registry import get_spec  # noqa: E402


def _ctx(workspace, *, engine, with_geometry=True):
    from pathlib import Path

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    geometry = None
    if with_geometry:
        from tests._geometry_support import materialized
        geometry = materialized(Path(workspace) / "_geom")
    return BuilderToolContext(workspace=Path(workspace), geometry=geometry, engine=engine)


# the shared Diagnostic protocol
def test_diagnostic_serialises_to_the_shared_shape():
    d = Diagnostic("error", "surface_level[1]", "max must be >= min")
    assert d.as_dict() == {"severity": "error", "path": "surface_level[1]",
                           "message": "max must be >= min"}


# each engine EXPOSES a palette + validator through its spec
def test_flow_engines_expose_an_authoring_tool_named_configure_mesh():
    for name in ("cfmesh", "snappy"):
        tool = get_spec(name).authoring_tool
        assert tool and tool["function"]["name"] == "configure_mesh", name


def test_gmsh_exposes_no_authoring_tool():
    # gmsh authors its spec via write_file (validated JSON scratch), not a palette.
    sp = get_spec("gmsh")
    assert sp.authoring_tool is None
    assert sp.validate_authoring({"anything": 1}) == []


# a VALID strategy passes each engine's own grammar
def test_valid_cfmesh_strategy_has_no_diagnostics():
    strat = {"domain_margin": {"up": 4, "down": 4, "side": 3, "vert": 3},
             "n_layers": 4, "max_cell_factor": 40, "wall_cell": 0.05,
             "thickness_ratio": 1.2,
             "features": [{"name": "wake", "type": "box", "cellSize": 0.01}]}
    assert get_spec("cfmesh").validate_authoring(strat) == []


def test_valid_snappy_strategy_has_no_diagnostics():
    strat = {"domain_margin": {"up": 6, "down": 6, "side": 5, "vert": 5},
             "n_layers": 5, "first_layer_rel": 0.35, "quality": "strict",
             "surface_level": [2, 4], "feature_level": 4, "max_cells": 8_000_000}
    assert get_spec("snappy").validate_authoring(strat) == []


# cross-engine knobs are REJECTED (not silently dropped)
def test_cfmesh_rejects_a_snappy_knob_and_names_the_redirect():
    diags = get_spec("cfmesh").validate_authoring({"surface_level": [2, 4]})
    assert [d for d in diags if d.severity == "error" and d.path == "surface_level"]
    assert any("snappy" in d.message for d in diags)


def test_snappy_rejects_a_cfmesh_knob_and_names_the_redirect():
    diags = get_spec("snappy").validate_authoring({"wall_cell": 0.05})
    assert [d for d in diags if d.severity == "error" and d.path == "wall_cell"]
    assert any("cfMesh" in d.message for d in diags)


def test_unknown_key_is_rejected():
    diags = get_spec("cfmesh").validate_authoring({"maxCellSiez": 0.1})  # typo
    assert any(d.severity == "error" and d.path == "maxCellSiez" for d in diags)


# out-of-range values are REJECTED
def test_out_of_range_values_are_flagged():
    assert any(d.path == "n_layers"
               for d in get_spec("cfmesh").validate_authoring({"n_layers": -1}))
    assert any(d.path == "thickness_ratio"
               for d in get_spec("cfmesh").validate_authoring({"thickness_ratio": 0.5}))
    assert any(d.path == "surface_level[1]"
               for d in get_spec("snappy").validate_authoring({"surface_level": [4, 2]}))
    assert any(d.path == "quality"
               for d in get_spec("snappy").validate_authoring({"quality": "insane"}))


# the builder composes the palette per engine, holds NO vocabulary
def test_active_tools_injects_the_selected_engines_palette():
    from meshpipeline.agents.builder.tools import _active_tools

    def _names(engine):
        return {t["function"]["name"] for t in _active_tools(engine)}

    assert "configure_mesh" in _names("cfmesh")
    assert "configure_mesh" in _names("snappy")
    # gmsh authors via write_file - no configure_mesh palette
    assert "configure_mesh" not in _names("gmsh")


def test_builder_holds_no_engine_block_vocabulary():
    # the palettes moved OUT of the builder into the engine bundles - the builder must
    # not name any cfmesh/snappy-specific knob (that vocabulary belongs to the engine).
    # Word-boundary match so the contract field `min_thickness_ratio` is not a false hit.
    import re
    # configure_mesh lives in the meshing family.
    src = (APP / "agents" / "builder" / "tools" / "meshing.py").read_text()
    for knob in ("surface_level", "wall_cell", "max_cell_factor", "first_layer_rel",
                 "feature_level", "thickness_ratio", "max_cells"):
        assert not re.search(rf"\b{knob}\b", src), f"builder still names the engine knob {knob!r}"


def test_bad_strategy_is_rejected_before_render():
    # end-to-end through the builder tool: a snappy knob sent to cfmesh is rejected with
    # the Diagnostic payload BEFORE any render is attempted.
    import tempfile

    from meshpipeline.agents.builder.tools import _tool_configure_mesh
    ws = Path(tempfile.mkdtemp())
    (ws / "input.stl").write_text("solid x\nendsolid x\n")
    out = _tool_configure_mesh(_ctx(ws, engine="cfmesh"), {"surface_level": [2, 4]})
    assert out["success"] is False
    assert out["diagnostics"] and out["diagnostics"][0]["path"] == "surface_level"
