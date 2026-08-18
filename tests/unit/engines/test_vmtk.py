# Responsibility: Verify the VMTK bundle's authoring, centerline-driven sizing, gates and delivered lumen mesh.
from __future__ import annotations

from meshpipeline.engines import registry as ec  # noqa: E402
from meshpipeline.engines.vmtk import authoring, vmtk_runner  # noqa: E402


def _ctx(workspace, *, engine, with_geometry=True):
    from pathlib import Path

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    geometry = None
    if with_geometry:
        from tests._geometry_support import materialized
        geometry = materialized(Path(workspace) / "_geom")
    return BuilderToolContext(workspace=Path(workspace), geometry=geometry, engine=engine)

# spec / registry / capability-first framing

def test_vmtk_is_an_implemented_engine():
    assert "vmtk" in ec.engine_names()
    sp = ec.get_spec("vmtk")
    assert sp.implemented
    assert [(c.input_kind, c.output_kind) for c in sp.capabilities] == [("body-surface", "fluid-volume")]


def test_engine_is_the_tool_the_domain_is_the_purpose():
    sp = ec.get_spec("vmtk")
    assert sp.name == "vmtk"                       # the tool, not 'vmtk_vascular'
    desc = sp.descriptor.lower()
    assert "centerline" in desc and "tetrahedral" in desc   # capability leads
    assert not desc.startswith("biomedical") and "vascular meshing" not in desc
    # it serves the ordinary CFD purposes - no bespoke 'vascular' purpose was invented
    from meshpipeline.engines.purposes import PURPOSES, compatible_purposes
    assert "vascular" not in PURPOSES
    assert "internal_cfd" in compatible_purposes(sp)


def test_configure_mesh_palette_keys_are_taught_in_the_prompt():
    sp = ec.get_spec("vmtk")
    props = sp.authoring_tool["function"]["parameters"]["properties"]
    missing = [k for k in props if k not in sp.system_prompt]
    assert not missing, f"palette keys not taught: {missing}"


# strategy resolution

def test_resolve_strategy_fills_defaults_and_drops_unknowns():
    s = vmtk_runner.resolve_strategy({"edge_length_factor": 0.15, "bogus": 1})
    assert s["edge_length_factor"] == 0.15
    assert s["boundary_layers"] == 3 and s["cap_openings"] is True
    assert "bogus" not in s


# the pype builder (the pure core)

_SEED = {"source_ids": [0], "target_ids": [1]}


def test_pype_is_radius_adaptive_by_construction():
    argv = vmtk_runner.build_pype({"edge_length_factor": 0.25, **_SEED})
    joined = " ".join(argv)
    assert "vmtkcenterlines" in joined
    assert "vmtkdistancetocenterlines" in joined and "-useradius 1" in joined
    assert "-elementsizemode edgelengtharray" in joined
    assert "-edgelengtharray DistanceToCenterlines" in joined
    assert "-edgelengthfactor 0.25" in joined
    assert "-edgelength " not in joined          # never an absolute cell size
    assert argv[-2:] == ["-ofile", "mesh.vtu"]


def test_pype_never_uses_an_interactive_seed_selector():
    for strat in ({"source_ids": [0], "target_ids": [1]},
                  {"source_points": [0, 0, 0], "target_points": [1, 1, 1]}):
        joined = " ".join(vmtk_runner.build_pype(strat))
        assert "openprofiles" not in joined and "pickpoint" not in joined
        assert "carotidprofiles" not in joined


def test_pype_seeding_modes_and_missing_seeds():
    ids = " ".join(vmtk_runner.build_pype({"source_ids": [2], "target_ids": [5, 7]}))
    assert "-seedselector profileidlist" in ids
    assert "-sourceids 2" in ids and "-targetids 5 7" in ids

    pts = " ".join(vmtk_runner.build_pype(
        {"source_points": [1.5, -2.0, 3.0], "target_points": [4.0, 5.0, 6.0]}))
    assert "-seedselector pointlist" in pts
    # Seeds are serialised round-trip-safe rather than at a fixed number of DECIMAL PLACES.
    # `%.6f` was an absolute 1e-6 grid, so a sub-micrometre lumen in metres had every endpoint
    # written as 0.000000 - see tests/unit/engines/test_vmtk_seed_precision.py.
    assert "-sourcepoints 1.5 -2.0 3.0" in pts
    assert "-targetpoints 4.0 5.0 6.0" in pts

    # explicit points win (they also work for a CLOSED lumen, which has no open profiles)
    both = " ".join(vmtk_runner.build_pype({**_SEED, "source_points": [0, 0, 0],
                                            "target_points": [1, 1, 1]}))
    assert "-seedselector pointlist" in both

    import pytest
    with pytest.raises(ValueError, match="seeding requires"):
        vmtk_runner.build_pype({"edge_length_factor": 0.3})   # no seeds at all


def test_pype_adds_boundary_layers_only_when_requested():
    with_layers = " ".join(vmtk_runner.build_pype({"boundary_layers": 4, **_SEED,
                                                   "boundary_layer_thickness_factor": 0.15}))
    assert "-boundarylayer 1" in with_layers
    assert "-sublayers 4" in with_layers and "-thicknessfactor 0.15" in with_layers
    assert "-boundarylayeroncaps 0" in with_layers   # layers on the wall, not the caps
    assert "-numberoflayers" not in with_layers      # not a real vmtk option

    no_layers = " ".join(vmtk_runner.build_pype({"boundary_layers": 0, **_SEED}))
    assert "-boundarylayer 0" in no_layers
    assert "-sublayers" not in no_layers


def test_pype_maps_cap_and_remesh_to_the_inverse_skip_flags():
    on = " ".join(vmtk_runner.build_pype({"cap_openings": True, "remesh_surface": True, **_SEED}))
    off = " ".join(vmtk_runner.build_pype({"cap_openings": False, "remesh_surface": False, **_SEED}))
    assert "-skipcapping 0" in on and "-skipremeshing 0" in on
    assert "-skipcapping 1" in off and "-skipremeshing 1" in off


def test_run_mesh_without_a_spec_fails_cleanly(tmp_path):
    # _run_vmtk_local is the EXECUTION (runs in the cloud container); run_cartesian_mesh is
    # now the cloud-dispatch seam, so the execution behaviour is tested on the local fn.
    res = vmtk_runner._run_vmtk_local(tmp_path, timeout=5)
    assert res["rc"] != 0 and not res["timed_out"]
    assert "vmtk_spec.json" in res["log_tail"]


# authoring validator

def test_authoring_accepts_a_valid_strategy():
    assert authoring.validate({"edge_length_factor": 0.3, "boundary_layers": 3,
                               "cap_openings": True, "remesh_surface": True,
                               "source_ids": [0], "target_ids": [1]}) == []


def test_authoring_rejects_out_of_range_edge_length_factor():
    for bad in (0.0, 1.5, "0.3"):
        d = authoring.validate({"edge_length_factor": bad})
        assert any(x.path == "edge_length_factor" for x in d), bad


def test_authoring_redirects_openfoam_knobs():
    d = authoring.validate({"surface_level": [2, 2], "n_layers": 3})
    knobs = [x for x in d if "OpenFOAM-mesher knob" in x.message]
    assert {x.path for x in knobs} == {"surface_level", "n_layers"}
    assert all("centerline radius" in x.message for x in knobs)


def test_authoring_requires_non_interactive_centerline_seeding():
    d = authoring.validate({"edge_length_factor": 0.3})
    assert any("seeding is required" in x.message for x in d)
    half = authoring.validate({"source_ids": [0]})
    assert any("BOTH source_ids and target_ids" in x.message for x in half)
    bad = authoring.validate({"source_points": [1.0, 2.0], "target_points": [3.0, 4.0]})
    assert any("(x,y,z) triples" in x.message for x in bad)


def test_authoring_rejects_bad_layer_counts_and_booleans():
    assert any(x.path == "boundary_layers" for x in authoring.validate({"boundary_layers": 99}))
    assert any(x.path == "cap_openings" for x in authoring.validate({"cap_openings": "yes"}))


def test_recommend_never_branches_on_a_key_its_input_does_not_carry():
    import inspect as _inspect

    from meshpipeline.cad.analysis import analyze_surface  # the real producer of this dict
    src = _inspect.getsource(analyze_surface)
    assert '"closed"' not in src and "'closed'" not in src   # it genuinely has no such key

    scales = {"diag": 1.0, "min_feature": 0.01, "thin_gap": 0.02, "extent": [1, 1, 1],
              "n_triangles": 100, "surface_area": 3.0}
    r = authoring.recommend(scales)
    assert r["boundary_layers"] == 3 and r["cap_openings"] is True   # open-lumen defaults
    # …and it delegates the closed-lumen call to the builder, naming where the flag comes from
    assert "geometry_report" in r["note"] and "closed" in r["note"]


# a diagnostic must be distinguishable from a bug in the tool

def test_boolean_diagnostic_echoes_what_it_received():
    from meshpipeline.engines.vmtk.authoring import validate
    d = validate({"cap_openings": "false"})
    msgs = [x.message for x in d if x.path == "cap_openings"]
    assert msgs, "a string boolean must still be rejected"
    assert "'false'" in msgs[0] and "str" in msgs[0], f"diagnostic hides the input: {msgs[0]}"


def test_real_booleans_are_accepted_both_ways():
    from meshpipeline.engines.vmtk.authoring import validate
    for v in (True, False):
        d = validate({"cap_openings": v, "remesh_surface": v})
        assert not [x for x in d if x.path in ("cap_openings", "remesh_surface")], \
            f"the JSON boolean {v!r} must be accepted"


# self-intersecting geometry is rejected up front (live model.vtp defect)

def test_vmtk_contract_requires_a_non_self_intersecting_surface():
    ic = ec.get_spec("vmtk").input_contract
    assert ic is not None and ic.require_no_self_intersection is True


def test_wrap_then_fill_engines_do_not_require_it():
    for e in ("snappy", "cfmesh"):
        ic = ec.get_spec(e).input_contract
        if ic is not None:
            assert ic.require_no_self_intersection is False, f"{e} should not require it"


def test_geometry_unsuitable_rejects_a_self_intersecting_surface():
    msg = ec.get_spec("vmtk").geometry_unsuitable({"self_intersecting": True})
    assert msg.startswith("[GEOMETRY_UNSUITABLE]")
    assert "self-intersect" in msg
    assert "no" in msg.lower() and "parameter" in msg.lower()   # says no strategy fixes it


def test_geometry_unsuitable_passes_a_clean_surface():
    assert ec.get_spec("vmtk").geometry_unsuitable({"self_intersecting": False}) == ""
    assert ec.get_spec("vmtk").geometry_unsuitable({}) == ""


def test_a_flagged_surface_is_ignored_by_an_engine_that_does_not_require_it():
    msg = ec.get_spec("snappy").geometry_unsuitable({"self_intersecting": True})
    assert "self-intersect" not in msg


# run_mesh guidance is defect-aware: Invalid PLC => STOP, not "coarsen"

def _enrich(fatal, success=False):
    out = {"success": success, "fatal_defects": fatal}
    vmtk_runner.run_enricher(None, None, {}, {}, out)
    return out["guidance"]


def test_invalid_plc_guidance_says_stop_not_retry():
    g = _enrich(["TetGen rejected the boundary as self-intersecting (Invalid PLC) - x"])
    assert "STOP" in g and "do not reconfigure" in g.lower()
    assert "coarser" not in g.lower() and "reduce" not in g.lower()


# run_mesh enforces the input contract as a HARD, deterministic gate
# The spec method geometry_unsuitable is covered above, but the LIVE model.vtp bug was that
# nothing MADE the builder consult it: that build went read_file → configure_mesh → run_mesh
# WITHOUT ever calling measure_scales (the only tool that computes self_intersecting), so the
# self-intersection was never surfaced and it ground on an unmeshable lumen for many rounds.
# The rejection must therefore fire from run_mesh ITSELF, independent of which inspection
# tools the model happened to call.

_SCALES = {"diag": 1.0, "extent": [1.0, 1.0, 1.0], "min_feature": 0.01,
           "thin_gap": 0.1, "n_triangles": 10, "surface_area": 3.0}


def _stage_runnable_workspace(tmp_path):
    for f in ec.get_spec("vmtk").run_policy.required_files:
        (tmp_path / f).write_text("{}")
    (tmp_path / "input.stl").write_text("")
    return tmp_path


def test_run_mesh_rejects_a_self_intersecting_input_before_building(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import tools
    from meshpipeline.engines.vmtk import vmtk_runner
    _stage_runnable_workspace(tmp_path)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: True)
    # the expensive off-box mesh dispatch must NEVER be reached for an unmeshable input
    def _boom(*_a, **_k):
        raise AssertionError("run_cartesian_mesh reached despite an unmeshable input")
    monkeypatch.setattr(vmtk_runner, "run_cartesian_mesh", _boom)

    out = tools.meshing.run_mesh(_ctx(tmp_path, engine="vmtk"))
    assert out["success"] is False
    assert out.get("geometry_unsuitable") is True
    assert out["error"].startswith("[GEOMETRY_UNSUITABLE]") and "self-intersect" in out["error"]
    assert "do NOT retry" in out["guidance"]   # tells the builder not to burn rounds reconfiguring


def test_run_mesh_lets_a_clean_input_reach_the_mesher(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import tools
    from meshpipeline.engines.vmtk import vmtk_runner
    _stage_runnable_workspace(tmp_path)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: False)
    reached = {"v": False}
    def _reached(workspace, *, timeout, context=None):
        reached["v"] = True
        return {"timed_out": False, "rc": 0, "log_tail": ""}
    monkeypatch.setattr(vmtk_runner, "run_cartesian_mesh", _reached)
    monkeypatch.setattr(vmtk_runner, "check_mesh",
                        lambda ws: {"cells": 1, "fatal": [], "mesh_ok": True})

    out = tools.meshing.run_mesh(_ctx(tmp_path, engine="vmtk"))
    assert reached["v"] is True, "a clean input must clear the gate and reach the mesher"
    assert "geometry_unsuitable" not in out


def test_run_mesh_gate_is_inert_for_wrap_then_fill_engines(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import tools
    called = {"v": False}
    def _tripwire(*_a, **_k):
        called["v"] = True
        return True
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", _tripwire)
    (tmp_path / "input.stl").write_text("")
    assert tools.input_contract_rejection(ec.get_spec("snappy"), tmp_path) == ""
    assert called["v"] is False


def test_ordinary_failure_still_gets_the_normal_coaching():
    g = _enrich(["max skewness above the floor"])
    assert "STOP" not in g
    assert "Not valid" in g


def test_success_guidance_points_at_submit():
    g = _enrich([], success=True)
    assert "submit" in g.lower()


def test_pack_guidance_does_not_prescribe_reducing_layers_first_for_plc():
    from meshpipeline.engines.vmtk import pack
    txt = pack.VMTK_SYSTEM
    # the Invalid-PLC line must lead with the geometry defect, not "reduce boundary_layers first"
    i = txt.find("Invalid PLC")
    seg = txt[i:i + 500]
    assert "geometry_report flags" in seg or "unmeshable" in seg


def test_check_mesh_rejects_a_truncated_vtu_without_invoking_the_reader(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("the VTK reader must not be invoked on a truncated file")
    monkeypatch.setattr(vmtk_runner, "_read_surface", _boom, raising=False)
    (tmp_path / "mesh.vtu").write_text('<?xml version="1.0"?>\n<VTKFile type="Unstructu')
    q = vmtk_runner.check_mesh(tmp_path)
    assert q["mesh_ok"] is False
    assert any("truncated" in f for f in q["fatal"])
