# Responsibility: Verify every name the cfMesh bundle declares resolves within it, with no cross-engine import.
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from tests._scan import scanned

from meshpipeline.engines.runtime import get_engine

ENGINES = Path(inspect.getsourcefile(get_engine)).parent
CFMESH = ENGINES / "cfmesh"

#: Every name resolved off the cfMesh engine adapter at run time, and who resolves it.
#: `pipeline/executor.py` drives the first group directly; `cfmesh/finalize.py` resolves the second
#: off the engine it is handed. Both routes end at `cfmesh_runner`, whether the name is DEFINED
#: there or re-exported into it.
DECLARED_RUNTIME_SURFACE: dict[str, str] = {
    "finalize":                    "pipeline/executor.py - the engine's finalization entry",
    "check_domain_extents":        "pipeline/executor.py - the A1 domain-extent gate",
    "check_solvability":           "pipeline/executor.py - the FV solvability gate",
    "run_cartesian_mesh":          "builder tools + the uniform engine seam in engines/base.py",
    "configure_mesh":              "builder tools - authoring entry",
    "tessellate_to_stl":           "cad/staging.py - the engine's CAD surface",
    "inspect_stl":                 "builder geometry tools + finalize (body bbox)",
    "check_mesh":                  "cfmesh/finalize.py - the quality facts",
    "write_manifest":              "cfmesh/finalize.py - publishes the measured facts",
    "read_stl_solids":             "cfmesh/finalize.py - review geometry",
    "build_review_msh":            "cfmesh/finalize.py - the reviewer's surface mesh",
    "export_volume_vtk":           "cfmesh/finalize.py - the sliceable volume",
    "review_surface_is_body_only": "cfmesh/finalize.py - declares the manifest completion rule",
    "name":                        "builder tools + manifest mesh_mode",
}

#: Capability BY DECLARATION: `finalize.py` asks `hasattr`/`getattr` and changes behaviour on the
#: answer. cfMesh must NOT declare these - it grows no prism layers, stages one geom.stl, and
#: body-fits no reference surface. Declaring one silently switches on a code path that has no
#: cfMesh meaning; the absence is the signal, so it is asserted as firmly as the presences above.
MUST_NOT_DECLARE: dict[str, str] = {
    "parse_layer_coverage":      "cfMesh grows no prism layers - there is no layer log to parse",
    "review_geometry_stls":      "cfMesh assembles exactly one geom.stl; the default is correct",
    "surface_capture_reference": "cfMesh does not body-fit a snapped wall to a reference CAD",
}


@pytest.mark.parametrize("name", sorted(DECLARED_RUNTIME_SURFACE))
def test_every_declared_runtime_name_resolves_off_the_engine(name):
    resolved = getattr(get_engine("cfmesh"), name, None)
    assert resolved is not None, (
        f"get_engine('cfmesh').{name} no longer resolves. {DECLARED_RUNTIME_SURFACE[name]} reads "
        "it by name at run time, so nothing imports it statically and nothing else will fail.")


@pytest.mark.parametrize("name", sorted(MUST_NOT_DECLARE))
def test_cfmesh_declares_no_capability_it_does_not_have(name):
    assert not hasattr(get_engine("cfmesh"), name), (
        f"cfMesh now declares {name}: {MUST_NOT_DECLARE[name]}")


def test_the_declared_surface_covers_every_name_the_code_actually_resolves():
    repo = ENGINES.parents[2]
    seen: set[str] = set()
    for root in ("src", "tests"):
        for py in sorted((repo / root).rglob("*.py")):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and "get_engine(" in ast.unparse(node.value):
                    seen.add(node.attr)
                if isinstance(node, ast.Call) and ast.unparse(node.func) in ("getattr", "hasattr"):
                    if (node.args and "get_engine(" in ast.unparse(node.args[0])
                            and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
                        seen.add(node.args[1].value)
    assert seen, "found no adapter-resolved names at all - this check has stopped checking"
    known = set(DECLARED_RUNTIME_SURFACE) | set(MUST_NOT_DECLARE)
    # `run_policy` is read off the engine SPEC, not the adapter; `connect` is the API server.
    unknown = seen - known - {"run_policy", "connect", "lower"}
    assert not unknown, (
        f"these names are resolved off an engine adapter but are not declared here: "
        f"{sorted(unknown)}. Add them to DECLARED_RUNTIME_SURFACE (or MUST_NOT_DECLARE) so the "
        "next deletion fails loudly.")


# module topology
#: symbol -> the module inside the cfMesh BUNDLE that owns it. Scoped to the bundle on purpose:
#: each OpenFOAM-family bundle owns its own stack by design ("deliberate duplication"), so snappy
#: having its own `configure_mesh` is the architecture, not a duplicate. What must not happen is
#: two definitions inside cfMesh.
OWNERS = {
    "run_cartesian_mesh":       "cfmesh/native.py",
    "_run_cartesian_mesh_local": "cfmesh/native.py",
    "polymesh_problems":        "cfmesh/deliverable.py",
    "reconcile_boundary_types": "cfmesh/deliverable.py",
    "boundary_foam_types":      "cfmesh/deliverable.py",
    "boundary_patch_names":     "cfmesh/deliverable.py",
    "configure_mesh":           "cfmesh/cfmesh_runner.py",
    "render_cfmesh_case":       "cfmesh/cfmesh_runner.py",
    "prepare_surface":          "cfmesh/cfmesh_runner.py",
}


@pytest.mark.parametrize("symbol,owner", sorted(OWNERS.items()))
def test_each_moved_symbol_has_exactly_one_definition_in_the_bundle(symbol, owner):
    homes = []
    for py in scanned(sorted(CFMESH.rglob("*.py")), "the cfMesh bundle modules"):
        for node in ast.parse(py.read_text(encoding="utf-8", errors="replace")).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == symbol:
                homes.append("cfmesh/" + py.relative_to(CFMESH).as_posix())
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == symbol:
                        homes.append("cfmesh/" + py.relative_to(CFMESH).as_posix())
    assert homes == [owner], f"{symbol} is defined in {homes}, expected only {owner}"


@pytest.mark.parametrize("symbol", ["FATAL_TOPOLOGY", "MAX_NON_ORTHO"])
def test_the_shared_openfoam_criteria_have_one_definition_repo_wide(symbol):
    homes = []
    for py in scanned(sorted(ENGINES.rglob("*.py")), "the engine bundle modules"):
        for node in ast.parse(py.read_text(encoding="utf-8", errors="replace")).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
                homes.append(py.relative_to(ENGINES).as_posix())
    assert homes == ["openfoam_criteria.py"], f"{symbol} is defined in {homes}"


def _spawning_functions(module_name: str) -> list[str]:
    tree = ast.parse((CFMESH / module_name).read_text())
    out = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(c, ast.Call) and ast.unparse(c.func).endswith("run_guarded")
                for c in ast.walk(fn)):
            out.append(ast.unparse(fn))
    return out


def test_only_the_native_module_launches_the_cfmesh_mesher():
    MESHERS = ("cartesianMesh", "cartesian2DMesh", "createPatch")
    authoring = _spawning_functions("cfmesh_runner.py")
    assert authoring, "the authoring module spawns nothing at all - this check stopped checking"
    for fn_src in authoring:
        for binary in MESHERS:
            assert binary not in fn_src, f"cfmesh_runner still launches {binary}"
    # what it DOES still spawn is surface staging, which belongs with authoring
    assert any("surfaceFeatureEdges" in f for f in authoring), (
        "surface feature extraction left the authoring module unexpectedly")

    import meshpipeline.engines.cfmesh.native as native_mod

    native = _spawning_functions("native.py")
    assert native, "cfmesh/native.py spawns nothing - the native phase did not move"
    joined = " ".join(native)
    # the binaries are named once, as constants, and the spawning function uses those names
    assert native_mod.CARTESIAN_MESH == "cartesianMesh"
    assert native_mod.CARTESIAN_2D_MESH == "cartesian2DMesh"
    for ref in ("CARTESIAN_MESH", "CARTESIAN_2D_MESH", "createPatch"):
        assert ref in joined, f"cfmesh/native.py does not launch {ref}"


def test_cfmesh_imports_no_other_engines_internals():
    others = {"snappy", "snappy_multiregion", "gmsh", "vmtk"}
    offenders = []
    for py in scanned(sorted(CFMESH.rglob("*.py")), "the cfMesh bundle modules"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8", errors="replace"))):
            mod = ""
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            for other in others:
                if mod.startswith(f"meshpipeline.engines.{other}."):
                    offenders.append(f"{py.name}:{node.lineno} -> {mod}")
    assert not offenders, f"cfMesh reaches into another engine: {offenders}"


def test_dispatch_reaches_the_cfmesh_executor_through_its_owning_module():
    import meshpipeline.engines.cfmesh.native as native
    from meshpipeline.engines.dispatch import engine_runners

    runners = engine_runners()
    assert set(runners) == {"cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk"}
    assert runners["cfmesh"] is native._run_cartesian_mesh_local, (
        "dispatch no longer resolves cfMesh's local executor from the module that owns it")


def test_no_compatibility_re_export_survives_in_the_cfmesh_bundle():
    import meshpipeline.engines.cfmesh.finalize as fin

    assert not hasattr(fin, "reconcile_boundary_types"), (
        "cfmesh/finalize.py re-exports reconcile_boundary_types - deliverable.py owns it, and two "
        "paths to one gate is how they drift apart")
    for gone in ("_polymesh_patch_names", "_polymesh_boundary_foam_types",
                 "_ROLE_REQUIRED_FOAM_TYPE"):
        assert not hasattr(fin, gone), f"cfmesh/finalize.py still carries {gone}"
