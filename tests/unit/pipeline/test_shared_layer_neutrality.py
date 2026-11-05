# Responsibility: Verify the shared layers read what the engine declares rather than knowing any engine themselves.
from __future__ import annotations

from pathlib import Path

import meshpipeline.settings.runtime as rtcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402

# Engine ARTIFACT/KNOB vocabulary that must never appear in shared agent/pipeline
# code - each token is owned by an engine bundle (its pack/authoring/runner/gates).
_FORBIDDEN_TOKENS = (
    "meshdict",            # cfmesh's authored artifact (also matches snappyhexmeshdict/blockmeshdict)
    "gmsh_spec",           # gmsh's authored artifact
    "renameboundary",      # cfmesh dict block
    "boundarylayers",      # cfmesh dict block (the OpenFOAM keyword, not the English phrase)
    "localrefinement",     # cfmesh dict block
    "maxcellsize",         # cfmesh knob
    "prepare_surface",     # flow runner plumbing
    "cartesianmesh",       # cfmesh mesher binary
    "snappyhexmesh",       # snappy mesher binary
    "min_sicn",            # gmsh quality key
    "surface_level", "feature_level", "wall_cell", "max_cell_factor",  # palette knobs
    "first_layer_rel",
)

# The shared layers where decision #2 ("builder holds no engine vocabulary") and the
# reviewer neutrality decisions apply.
_SHARED_FILES = (
    "agents/builder/agent.py",
    "agents/builder/executor.py",
    "agents/builder/tools/__init__.py",
    "agents/builder/tools/meshing.py",
    "agents/builder/tools/geometry.py",
    "agents/builder/tools/workspace.py",
    "agents/builder/tools/research.py",
    "agents/builder/messages.py",
    "agents/builder/context.py",
    "agents/builder/workspace.py",
    "agents/reviewer/visual.py",
    "agents/reviewer/context.py",
    "agents/reviewer/tools.py",
    "agents/reviewer/persist.py",
    "pipeline/classifier.py",
)


# criteria load from the ENGINE BUNDLE via the spec (no pipeline-level dict)
def test_every_engine_exposes_criteria_through_its_spec():
    # The hand-edited pipeline-level CRITERIA dict is GONE: criteria_for() asks
    # get_spec(engine).criteria, so an engine physically cannot ship criteria
    # outside its bundle. This guard makes the remaining failure mode (an engine
    # declaring NO rows) loud, and pins criteria_for to the spec path.
    from meshpipeline.engines.quality_criteria import criteria_for
    for e in engine_names():
        rows = get_spec(e).criteria
        assert rows, f"{e} declares no criteria rows (spec._load_criteria)"
        assert tuple(criteria_for(e)) == tuple(rows), f"{e}: criteria_for bypasses the spec"
    import meshpipeline.engines.quality_criteria as qc
    assert not hasattr(qc, "CRITERIA"), "the hand-edited CRITERIA dict is back"


# workspace scaffolding is engine-owned
def test_workspace_scaffold_is_engine_owned(tmp_path, monkeypatch):
    # Shared workspace setup owns only generic mechanics: the OpenFOAM case
    # skeleton (controlDict/fvSchemes/fvSolution) is declared by the FLOW engines'
    # scaffold hook, and a gmsh workspace receives NO OpenFOAM files (it used to
    # get solver dicts it never uses).
    from meshpipeline.agents.builder.workspace import _setup_workspace
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)

    for e in ("cfmesh", "snappy"):
        ws = _setup_workspace(f"job-{e}", 1, engine=e)
        for f in ("controlDict", "fvSchemes", "fvSolution"):
            assert (ws / "system" / f).exists(), f"{e} workspace missing system/{f}"

    ws = _setup_workspace("job-gmsh", 1, engine="gmsh")
    assert not (ws / "system").exists(), \
        "gmsh workspace received an OpenFOAM system/ skeleton it never uses"
    assert ws.is_dir()   # generic mechanics still happened


# prompt <-> authoring-schema drift (the palette must be taught)
def test_flow_palette_keys_are_taught_in_the_engine_prompt():
    # Every configure_mesh palette key must appear in that engine's OWN system
    # prompt - a key added to the schema but never taught is unusable; a key
    # taught but removed from the schema makes the model author rejected fields.
    for e in engine_names():
        sp = get_spec(e)
        tool = sp.authoring_tool
        if not tool:
            continue
        props = (tool.get("function", {}).get("parameters", {}).get("properties") or {})
        prompt = sp.system_prompt
        missing = [k for k in props if k not in prompt]
        assert not missing, f"{e}: palette keys {missing} not taught in the engine prompt"


# the reviewer brief carries EVERY engine's authored recipe
def test_reviewer_mesh_script_uses_the_engines_declared_files(tmp_path):
    # The audit's concrete failure: a hardcoded system/meshDict left snappy visual
    # reviews with NO mesh script. The block must read run_policy.required_files.
    from meshpipeline.agents.reviewer.context import build_review_prompt
    manifest = {"domain": "x", "quality_criteria": {"criteria": []}, "patches": {}}

    def _brief(engine: str) -> str:
        _, ctx = build_review_prompt(
            manifest=manifest, nav_context={}, workspace=tmp_path,
            step_basename="x.stl", patch_names=[], patch_colour_legend="",
            mesh_units="m", request="r", review_brief="b", job_id="t",
            engine=engine, purpose="external_cfd")
        return ctx

    # snappy: BOTH declared dicts appear once present
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "blockMeshDict").write_text("SENTINEL_BLOCK")
    (tmp_path / "system" / "snappyHexMeshDict").write_text("SENTINEL_SNAPPY")
    ctx = _brief("snappy")
    assert "SENTINEL_BLOCK" in ctx and "SENTINEL_SNAPPY" in ctx
    assert "UNTRUSTED ARTIFACT" in ctx #: the builder config is framed as untrusted evidence

    # cfmesh: its dict appears; snappy's files are not read for it
    (tmp_path / "system" / "meshDict").write_text("SENTINEL_CF")
    ctx = _brief("cfmesh")
    assert "SENTINEL_CF" in ctx and "SENTINEL_SNAPPY" not in ctx


# deleted concepts stay deleted in the public docs
