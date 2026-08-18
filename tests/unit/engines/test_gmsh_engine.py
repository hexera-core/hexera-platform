# Responsibility: Verify the Gmsh driver produces its deck, gates it on SICN, and ships the FEA bundle.
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

gmsh = pytest.importorskip("gmsh", reason="gmsh SDK not installed")

REAL_STEP = Path(__file__).parents[3] / "tests" / "fixtures" / "external" / "geometry" / "elbow90_fluid.step"
# external_fixture: the whole module needs tests/fixtures/external/geometry/elbow90_fluid.step (licensed,
# non-redistributable). Excluded from the fast tier; run via `make test-external-fixtures`.
pytestmark = [
    pytest.mark.external_fixture,
    pytest.mark.skipif(not REAL_STEP.exists(), reason="real STEP fixture missing"),
]


@pytest.fixture(scope="module")
def meshed_ws(tmp_path_factory) -> Path:
    from meshpipeline.engines.gmsh import gmsh_runner
    ws = tmp_path_factory.mktemp("gmsh_ws")
    gmsh_runner.tessellate_to_stl(REAL_STEP, ws / "input.stl")
    info = gmsh_runner.inspect_stl(ws)
    assert info["volumes"] >= 1 and info["surfaces"]
    # contract the largest face as 'fixed_base' - a real face, found by area
    largest = max(info["surfaces"], key=lambda s: s["area"])["tag"]
    (ws / "gmsh_spec.json").write_text(json.dumps({
        "element_order": 2,
        "size": {"mode": "factor", "value": 0.08},
        "groups": [{"name": "fixed_base", "role": "fixed",
                    "surface_tags": [largest]}],
        "default_group": "free",
        "optimize": True,
    }))
    # _run_gmsh_local is the EXECUTION (runs in the cloud container); run_cartesian_mesh is
    # now the cloud-dispatch seam. This test validates the real driver subprocess, so it
    # calls the execution fn directly - the same one do_mesh invokes on Cloud Run.
    res = gmsh_runner._run_gmsh_local(ws, timeout=300)
    assert res["rc"] == 0 and not res["timed_out"], res["log_tail"]
    out = gmsh_runner.finalize(str(ws), [{"name": "fixed_base", "type": "fixed"}],
                               "gmsh", domain="elbow bracket FEA",
                               engine_params={"element_order": "2"})
    assert out["success"], out["output"]
    return ws


def test_driver_produces_deck_msh_and_quality(meshed_ws):
    from meshpipeline.engines.gmsh import gmsh_runner
    for f in ("mesh.inp", "mesh.msh", "quality.json"):
        assert (meshed_ws / f).exists() and (meshed_ws / f).stat().st_size > 0
    q = gmsh_runner.check_mesh(meshed_ws)
    assert q["cells"] > 0 and q["nodes"] > q["cells"] / 4
    assert q["element_order"] == 2
    assert q["min_sicn"] > 0.0 and not q["fatal"]
    # the contracted group is a named ELSET (+NSET) in the Abaqus deck
    deck = (meshed_ws / "mesh.inp").read_text()
    assert "ELSET=fixed_base" in deck


def test_finalize_writes_the_uniform_manifest(meshed_ws):
    m = json.loads((meshed_ws / "mesh_manifest.json").read_text())
    assert m["mesh_mode"] == "gmsh"
    assert m["engine_params"] == {"element_order": "2"}
    assert m["patch_types"]["fixed_base"] == "fixed"
    assert m["cell_count"] > 0
    # the criteria report evaluated the gmsh rows against real measurements
    rows = {r["key"]: r for r in m["quality_criteria"]["criteria"]}
    assert rows["min_sicn"]["passed"] is not None
    assert (m["mesh_paths"]["volume"] or "").endswith("mesh.inp")


def test_declared_gates_pass_on_the_real_mesh(meshed_ws):
    from meshpipeline.engines.gates import GateCtx, run_gates
    from meshpipeline.engines.registry import get_spec
    ok, key, fb = run_gates(get_spec("gmsh").gates, GateCtx(
        workspace=meshed_ws, engine="gmsh",
        intake_patches=[{"name": "fixed_base", "type": "fixed"}]))
    assert ok, f"{key}: {fb}"


def test_sicn_gate_rejects_below_floor(meshed_ws, tmp_path):
    ws = tmp_path / "bad"
    shutil.copytree(meshed_ws, ws)
    m = json.loads((ws / "mesh_manifest.json").read_text())
    m["quality"]["min_sicn"] = 0.01
    (ws / "mesh_manifest.json").write_text(json.dumps(m))
    from meshpipeline.engines.gates import GateCtx, run_gates
    from meshpipeline.engines.registry import get_spec
    ok, key, fb = run_gates(get_spec("gmsh").gates,
                            GateCtx(workspace=ws, engine="gmsh"))
    assert not ok and key == "sicn_floor"
    assert "gmsh_spec.json" in fb and "size.value" in fb


def test_missing_deck_rejected(tmp_path):
    from meshpipeline.engines.gates import GateCtx, run_gates
    from meshpipeline.engines.registry import get_spec
    (tmp_path / "mesh_manifest.json").write_text(json.dumps(
        {"schema_version": "2.1", "geometry": {}, "patches": {},
         "validation": {}, "quality": {}}))
    ok, key, fb = run_gates(get_spec("gmsh").gates,
                            GateCtx(workspace=tmp_path, engine="gmsh"))
    assert not ok and key == "manifest_valid" and "mesh.inp" in fb


def test_review_wiring_is_unified_post_c2():
    from meshpipeline.engines.assurance import derive_assurance_plan
    from meshpipeline.engines.registry import get_spec
    for e in ("gmsh", "cfmesh"):
        sp = get_spec(e)
        assert not hasattr(sp, "visual_review")
        assert sp.renders_for_review is True
        plan = derive_assurance_plan(sp, "")
        assert plan.requires_render
    rev = (APP / "agents" / "reviewer" / "visual.py").read_text()
    assert "run_unified_review" in rev


# last-mile wiring (the audit's five catches - each was a real bug)

def test_submit_mesh_gates_on_the_deck_not_polymesh(tmp_path):
    from meshpipeline.agents.builder.tools import _tool_submit_mesh
    out = _tool_submit_mesh(tmp_path, "gmsh")
    assert not out["success"] and "gmsh_spec.json" in out["error"]
    (tmp_path / "mesh.inp").write_text("*Heading")
    assert _tool_submit_mesh(tmp_path, "gmsh") == {"success": True, "deck": True}
    # flow engines keep the polyMesh gate verbatim
    out = _tool_submit_mesh(tmp_path, "cfmesh")
    assert not out["success"] and "meshDict" in out["error"]


def test_uploader_ships_the_fea_bundle():
    from meshpipeline.engines.registry import get_spec
    d = get_spec("gmsh").deliverable
    assert d.bundle == "gmsh_case.tar.gz" and d.marker == "mesh.inp"
    for f in ("mesh.inp", "gmsh_spec.json", "geometry.step"):
        assert f in d.files


def test_builder_first_message_speaks_the_engine_language():
    from meshpipeline.agents.builder.messages import _build_initial_messages
    msgs = _build_initial_messages(
        "/x/bracket.step", Path("/tmp/ws"), domain="bracket FEA",
        intake_patches=[{"name": "fixed_base", "type": "fixed"}],
        engine="gmsh", engine_params={"element_order": "1"})
    user = msgs[1]["content"]
    assert "gmsh_spec.json" in user and "geometry.step" in user
    assert "prepare_surface" not in user and "meshDict" not in user
    assert "NAMED GROUP" in user
    assert '"element_order": "1"' in user          # declared param HONORED
    # cfmesh is now declarative too (configure_mesh, not hand-written meshDict)
    flow = _build_initial_messages("/x/wing.step", Path("/tmp/ws"),
                                   engine="cfmesh")[1]["content"]
    assert "configure_mesh" in flow and "input.stl" in flow
    assert "prepare_surface" not in flow           # no more manual surface prep
    assert "do NOT hand-write" in flow             # the meshDict is rendered


def test_retry_carries_the_spec_and_solid():
    # Retry carry + context recovery reach the authored spec via the ENGINE's
    # declared run_policy.required_files - never a hardcoded filename in shared
    # builder code (2026-07-07 audit; the old hardcode left snappy with no carry
    # and no recovery spec). gmsh declares its re-runnable spec there;
    # geometry.step stays a generic staple (the staged CAD solid).
    from meshpipeline.agents.builder.tools import get_spec_run_files
    assert get_spec_run_files("gmsh") == ("gmsh_spec.json",)
    node_src = (APP / "agents" / "builder" / "agent.py").read_text()
    assert "get_spec_run_files" in node_src and '"geometry.step"' in node_src
    loop_src = (APP / "agents" / "builder" / "loop.py").read_text()
    assert "get_spec_run_files" in loop_src


def test_viewer_surface_hook_serves_named_groups(meshed_ws):
    from meshpipeline.engines.registry import get_spec
    hook = get_spec("gmsh").viewer_surface
    assert hook is not None
    # the unified viewer_surface seam returns the finished kind=stl response
    resp = hook(meshed_ws, roles={}, units="m", skip_names=())
    assert resp["kind"] == "stl"
    names = {p["name"]: p for p in resp["patches"]}
    assert "fixed_base" in names and "free" in names
    assert names["fixed_base"]["tri_count"] > 10    # real triangles
    # flow engines now own their render too (polyMesh boundary polygons)
    assert get_spec("cfmesh").viewer_surface is not None


# group roles are PURPOSE-derived, not a structural-only hardcode (live failure:
# the first fluid-domain CFD run had wall/inlet/outlet groups REJECTED by the old
# {fixed, load, contact, free} set; the builder shoehorned them into FEA roles and
# the patch contract then failed)

def test_group_roles_cover_every_purpose_the_engine_can_serve():
    from meshpipeline.engines.gmsh.driver import _allowed_roles
    roles = _allowed_roles()
    # structural vocabulary
    assert {"fixed", "load", "contact", "free"} <= roles
    # flow vocabulary - gmsh declares fluid-domain → fluid-volume, so a supplied
    # fluid domain's contract roles must be authorable
    assert {"wall", "inlet", "outlet", "farfield"} <= roles


def test_spec_validator_accepts_flow_roles_for_a_fluid_domain_deck():
    from meshpipeline.engines.gmsh.driver import _validate_spec
    errs = _validate_spec({
        "element_order": 2,
        "groups": [{"name": "inlet", "role": "inlet", "surface_tags": [1]},
                   {"name": "outlet", "role": "outlet", "surface_tags": [2]},
                   {"name": "wall", "role": "wall", "surface_tags": [3, 4]}],
    })
    assert errs == [], errs


def test_spec_validator_still_rejects_unknown_roles():
    from meshpipeline.engines.gmsh.driver import _validate_spec
    errs = _validate_spec({"groups": [{"name": "x", "role": "bogus", "surface_tags": [1]}]})
    assert any("role must be one of" in e for e in errs)


def test_finalize_does_not_fabricate_the_default_group(tmp_path):
    import json as _json

    from meshpipeline.engines.gmsh import gmsh_runner
    ws = tmp_path
    (ws / "mesh.inp").write_text("*HEADING\n")
    (ws / "mesh.msh").write_text("$MeshFormat\n")
    base = {"cells": 10, "nodes": 20, "element_order": 2, "min_sicn": 0.5,
            "sicn_low_fraction": 0.0, "fatal": [], "size_h": 0.1,
            "bounds": [0, 0, 0, 1, 1, 1],
            "groups": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
            "default_group": "free"}
    # all surfaces assigned → default group NOT created → not in the manifest
    (ws / "quality.json").write_text(_json.dumps({**base, "default_group_used": False}))
    out = gmsh_runner.finalize(str(ws), [], "gmsh")
    m = _json.loads((ws / "mesh_manifest.json").read_text())
    assert out["success"] is True
    assert "free" not in m["patch_types"], "fabricated default group reached the manifest"
    # leftover surfaces existed → the group is real → keep it
    (ws / "quality.json").write_text(_json.dumps({**base, "default_group_used": True}))
    gmsh_runner.finalize(str(ws), [], "gmsh")
    m = _json.loads((ws / "mesh_manifest.json").read_text())
    assert m["patch_types"].get("free") == "free"


def test_pack_teaches_contract_derived_roles_not_a_structural_menu():
    from meshpipeline.engines.gmsh.pack import GMSH_SYSTEM
    assert "fixed|load|contact|free" not in GMSH_SYSTEM
    assert "CONTRACTED type" in GMSH_SYSTEM
    # and it names the CFD vocabulary as an example so the model knows it is allowed
    assert "wall/inlet/outlet" in GMSH_SYSTEM
