# Responsibility: Verify the render backend receives resolved handles only, and reads no manifest, path or engine name.
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import ArtifactFormat
from meshpipeline.engines.registry import ENGINE_CATALOG
from meshpipeline.sandbox import render_inputs as RI
from meshpipeline.sandbox.render_inputs import (
    RenderLimits,
    RenderMetadata,
    ResolvedRegion,
    ResolvedRenderInputs,
    resolve_region,
    resolve_regions,
)
from meshpipeline.sandbox.review_session import ResolvedArtifact

BACKEND_SRC = Path(inspect.getsourcefile(RI)).parent / "backend.py"

# Every module the render backend is now made of. split `backend.py` into the scene
#: itself plus three authorities, and the structural bans below are properties of the RENDER PATH,
#: not of one file: scanning only `backend.py` would have let the mesh read - which is what opens a
#: path at all - walk out of range of the checks written to police it. Each check that names a
#: specific construct also asserts it still FINDS that construct somewhere in this set, so a later
#: move cannot make one of them pass by having nothing left to inspect.
RENDER_PATH_SRC: tuple[Path, ...] = tuple(
    BACKEND_SRC.parent / n
    for n in ("backend.py", "mesh_reader.py", "capture.py", "camera_math.py"))

CURRENT_VISUAL = ("snappy", "cfmesh", "snappy_multiregion")
CURRENT_METRIC = ("gmsh", "vmtk")


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text())


def _backend_class() -> ast.ClassDef:
    for node in ast.walk(_tree(BACKEND_SRC)):
        if isinstance(node, ast.ClassDef) and node.name == "MeshRenderBackend":
            return node
    pytest.fail("MeshRenderBackend not found in backend.py")


def _string_constants(node: ast.AST) -> set[str]:
    docstrings: set[int] = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(n, "body", [])
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings}


# 1-2: the typed declarations
@pytest.mark.parametrize("name", CURRENT_VISUAL)
def test_visual_engines_declare_required_surface_and_optional_volume(name):
    arts = {a.artifact_key: a for a in ENGINE_CATALOG[name].render_artifacts}
    assert "mesh_paths.surface" in arts and arts["mesh_paths.surface"].required
    assert "mesh_paths.volume" in arts, f"{name} declares no volume artifact"
    assert arts["mesh_paths.volume"].required is False, (
        f"{name}: a required volume would make every job without a volume export unreviewable")


@pytest.mark.parametrize("name", CURRENT_VISUAL)
def test_the_volume_requirement_is_typed_and_manifest_keyed(name):
    vol = next(a for a in ENGINE_CATALOG[name].render_artifacts
               if a.artifact_key == "mesh_paths.volume")
    assert not vol.artifact_key.startswith("/") and ".." not in vol.artifact_key
    assert set(vol.allowed_formats) == {ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP}
    assert "internal volume inspection and sectional review" in vol.purpose


@pytest.mark.parametrize("name", CURRENT_METRIC)
def test_the_metric_engines_did_not_gain_a_volume_for_symmetry(name):
    keys = {a.artifact_key for a in ENGINE_CATALOG[name].render_artifacts}
    assert "mesh_paths.volume" not in keys
    assert [t.target_id for t in ENGINE_CATALOG[name].inspection_targets if t.required] == []


# 5: the backend accepts only resolved handles
def test_the_backend_constructor_accepts_only_resolved_handles():
    init = next(n for n in _backend_class().body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    params = [a.arg for a in init.args.args]
    assert params == ["self", "inputs", "metadata", "save_dir", "limits", "regions"]
    for forbidden in ("manifest", "vtk_path", "workspace", "msh_path"):
        assert forbidden not in params, f"the backend takes {forbidden!r} - it could resolve"


def test_the_inputs_type_carries_a_required_surface_and_optional_volume():
    surf = ResolvedArtifact("mesh_paths.surface", Path("/ws/mesh.msh"), ArtifactFormat.GMSH_MSH)
    assert ResolvedRenderInputs(surface=surf).volume is None
    assert ResolvedRenderInputs(surface=surf).has_volume is False

    vol = ResolvedArtifact("mesh_paths.volume", Path("/ws/i.vtu"), ArtifactFormat.VTK_VTU)
    assert ResolvedRenderInputs(surface=surf, volume=vol).has_volume is True


def test_the_metadata_type_cannot_carry_a_path():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(RenderMetadata)}
    # Presentation is renderer-owned; the metadata carries engineering facts only.
    assert fields == {"mesh_units", "patch_roles"}

    meta = RenderMetadata.from_manifest({
        "mesh_units": "m",
        "patch_types": {"wall": "wall"},
        "mesh_paths": {"surface": "/etc/passwd", "volume": "/etc/shadow"},
        "inspection_regions": [{"name": "r"}],
    })
    assert meta.mesh_units == "m" and dict(meta.patch_roles) == {"wall": "wall"}
    for value in vars(meta).values():
        assert "etc" not in str(value), "a path survived into the metadata"


# 6-11: what the backend structurally cannot do
@pytest.mark.parametrize("src", RENDER_PATH_SRC, ids=lambda p: p.name)
def test_the_backend_contains_no_manifest_artifact_read(src):
    evaluated = _string_constants(_tree(src))
    for key in ("mesh_paths", "surface", "volume", "inspection_regions"):
        assert key not in evaluated, f"{src.name} evaluates the manifest key {key!r}"


@pytest.mark.parametrize("src", RENDER_PATH_SRC, ids=lambda p: p.name)
def test_the_backend_never_derives_a_workspace_from_an_artifact_path(src):
    for node in ast.walk(_tree(src)):
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            used = ast.unparse(node)
            assert "msh_path" not in used and "surface" not in used and "workspace" not in used, (
                f"{src.name} derives a root from an artifact path: {ast.unparse(node)}")


@pytest.mark.parametrize("src", RENDER_PATH_SRC, ids=lambda p: p.name)
def test_the_backend_performs_no_artifact_path_joining(src):
    for node in ast.walk(_tree(src)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = ast.unparse(node.left)
            for banned in ("_workspace", "manifest", "_volume_path", "root"):
                assert banned not in left, f"{src.name} joins onto {banned}: {ast.unparse(node)}"


@pytest.mark.parametrize("src", RENDER_PATH_SRC, ids=lambda p: p.name)
def test_the_backend_performs_no_fallback_glob(src):
    for node in ast.walk(_tree(src)):
        if isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            assert not fn.endswith(".glob") and not fn.endswith(".rglob"), (
                f"{src.name} globs for artifacts: {ast.unparse(node)}")


@pytest.mark.parametrize("src", RENDER_PATH_SRC, ids=lambda p: p.name)
def test_the_backend_performs_no_engine_name_dispatch(src):
    evaluated = _string_constants(_tree(src))
    for engine in ENGINE_CATALOG:
        assert engine not in evaluated, f"{src.name} evaluates the engine name {engine!r}"


def test_the_backend_never_opens_a_path_it_was_not_given():
    # `msh_path` is the reader's parameter: the caller passes the handle the sandbox proved, and
    # the reader opens that and nothing it derived for itself.
    allowed = ("self.msh_path", "str(vp)", "self._surface.path", "str(self._surface.path)",
               "msh_path")
    seen = 0
    for src in RENDER_PATH_SRC:
        for node in ast.walk(_tree(src)):
            if isinstance(node, ast.Call):
                fn = ast.unparse(node.func)
                if fn in ("gmsh.merge", "pv.read"):
                    seen += 1
                    arg = ast.unparse(node.args[0]) if node.args else ""
                    assert arg in allowed, (
                        f"{src.name}: {fn} receives an uncontrolled argument: {arg}")
    assert seen, ("no mesh-opening call was found anywhere in the render path - the code this "
                  "check exists to police has moved out of RENDER_PATH_SRC and it is now vacuous")


# 12-14: the compatibility wrapper






def test_no_second_resolver_exists_anywhere_in_the_render_path():
    for mod in (RI,):
        src = inspect.getsource(mod)
        assert "relative_to" not in src, f"{mod.__name__} re-implements confinement"
    for path in RENDER_PATH_SRC:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("relative_to", "resolve"):
                src = ast.unparse(node)
                assert "self._" not in src or "path" not in src, (
                    f"{path.name} re-confines a path: {src}")


# 24: region input boundary
def test_the_backend_receives_a_resolved_region_not_a_name():
    fn = next(n for n in _backend_class().body
              if isinstance(n, ast.FunctionDef) and n.name == "inspect_region")
    args = [a.arg for a in fn.args.args]
    assert args == ["self", "region"], f"inspect_region takes {args}"
    ann = fn.args.args[1].annotation
    assert ann is not None and ast.unparse(ann) == "ResolvedRegion"


def test_the_backend_does_not_look_a_region_up_at_command_time():
    fn = next(n for n in _backend_class().body
              if isinstance(n, ast.FunctionDef) and n.name == "inspect_region")
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("get"):
            assert "_regions" not in ast.unparse(node), "the backend resolves a region itself"


def test_arbitrary_region_narration_cannot_become_a_region():
    assert resolve_region({"name": "shroud outlet, near the fillet"}) is not None  # a real name
    for junk in ({}, {"name": ""}, {"name": "   "}, {"name": 42}, {"nope": "x"}):
        assert resolve_region(junk) is None, f"{junk} became a region"


def test_non_finite_geometry_is_refused():
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert resolve_region({"name": "r", "normal": [0, bad, 1]}) is None
        assert resolve_region({"name": "r", "normal": [0, 1, 0], "origin": [bad, 0, 0]}) is None
    assert resolve_region({"name": "r", "normal": [0, 0, 0]}) is None       # no plane
    assert resolve_region({"name": "r", "normal": [0, 1]}) is None          # wrong arity
    assert resolve_region({"name": "r", "normal": "north"}) is None         # not numbers
    assert resolve_region({"name": "r", "normal": [0, 1, 0],
                           "clip_box": [0, 1, 0, 1, 0]}) is None            # malformed box


def test_a_valid_region_survives_intact():
    r = resolve_region({"name": "midspan", "kind": "slice", "normal": [0, 1, 0],
                        "origin": [1.5, 2.0, 3.0], "clip_box": [0, 1, 0, 1, 0, 1]})
    assert r == ResolvedRegion(region_id="midspan", kind="slice", normal=(0.0, 1.0, 0.0),
                               origin=(1.5, 2.0, 3.0), clip_box=(0.0, 1.0, 0.0, 1.0, 0.0, 1.0),
                               requires_volume=True)


def test_regions_are_validated_once_at_construction_not_per_command():
    regions = resolve_regions({"inspection_regions": [
        {"name": "good", "normal": [0, 1, 0]},
        {"name": "bad", "normal": [0, float("nan"), 0]},
        "not even a dict",
    ]})
    assert set(regions) == {"good"}


def test_every_region_declares_that_it_needs_the_volume():
    r = resolve_region({"name": "r", "normal": [0, 1, 0]})
    assert r.requires_volume is True


# 3, 21: optional volume capability
def test_missing_volume_does_not_prevent_a_surface_session():
    surf = ResolvedArtifact("mesh_paths.surface", Path("/ws/mesh.msh"), ArtifactFormat.GMSH_MSH)
    inputs = ResolvedRenderInputs(surface=surf, volume=None)
    assert inputs.surface is surf and inputs.has_volume is False


def test_no_volume_means_no_volume_capability_is_claimable():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ResolvedRenderInputs)}
    assert "has_volume" not in fields, "has_volume is settable - it must stay derived"
    surf = ResolvedArtifact("mesh_paths.surface", Path("/ws/m.msh"), ArtifactFormat.GMSH_MSH)
    assert ResolvedRenderInputs(surface=surf).has_volume is False


# 4, 22: unsafe never reaches the renderer
def test_an_unsafe_volume_never_becomes_render_inputs(tmp_path):
    from meshpipeline.contracts.review_evidence import ReviewRenderError
    from meshpipeline.sandbox.review_session import ArtifactResolver

    ws = tmp_path / "job"; ws.mkdir()
    (tmp_path / "evil.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')

    resolver = ArtifactResolver(ws, {"mesh_paths": {"volume": str(tmp_path / "evil.vtu")}})
    with pytest.raises(ReviewRenderError):
        resolver.resolve_raw("mesh_paths.volume", str(tmp_path / "evil.vtu"),
                             (ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP), required=False)


# 30 marker: representative native rendering is a separate, later evaluation
def test_the_limits_are_the_historic_screenshot_geometry():
    assert (RenderLimits().screenshot_w, RenderLimits().screenshot_h) == (1280, 960)


# 13: cleanup
def test_the_backend_releases_its_render_window_on_close():
    fn = next(n for n in _backend_class().body
              if isinstance(n, ast.FunctionDef) and n.name == "close")
    calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "self._plotter.close" in calls, "close() does not release the render window"
    assert "self._capture.assemble_video" in calls, (
        "close() no longer finalises the session video - the clip is assembled by "
        "the capture authority, which owns the frames and the counter that decides there are any")

    # PRESENCE IS NOT REACHABILITY. A `return` above these leaves both calls lexically intact,
    # so the check above passes over cleanup that never runs. close() returns None and has no
    # business short-circuiting; if an idempotency guard is ever genuinely needed, this
    # assertion must be revisited on purpose rather than silently satisfied.
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Return)], (
        "close() short-circuits - the cleanup below the return never runs")


def test_close_survives_a_plotter_that_refuses_to_close():
    fn = next(n for n in _backend_class().body
              if isinstance(n, ast.FunctionDef) and n.name == "close")
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), "close() can raise"
