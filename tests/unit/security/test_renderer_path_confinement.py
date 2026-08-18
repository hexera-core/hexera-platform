# Responsibility: Verify every render artifact resolves inside the workspace, and a refusal is not provider downtime.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import ArtifactFormat, ReviewRenderError
from meshpipeline.sandbox.review_session import ArtifactResolver

SURFACE_KEY = "mesh_paths.surface"
VOLUME_KEY = "mesh_paths.volume"
SURFACE_FORMATS = (ArtifactFormat.GMSH_MSH,)
VOLUME_FORMATS = (ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP)

# Real signatures. The manifest is untrusted, so content is what gets checked - never the name.
MSH = b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n"
VTU = b'<?xml version="1.0"?>\n<VTKFile type="UnstructuredGrid">\n'


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "workspaces" / "job-1"
    (w / "VTK" / "case_0").mkdir(parents=True)
    (w / "mesh.msh").write_bytes(MSH)
    (w / "VTK" / "case_0" / "internal.vtu").write_bytes(VTU)
    return w


def _surface(ws, raw):
    return ArtifactResolver(ws.resolve(), {}).resolve_raw(
        SURFACE_KEY, raw, SURFACE_FORMATS, required=True)


def _volume(ws, raw):
    return ArtifactResolver(ws.resolve(), {}).resolve_raw(
        VOLUME_KEY, raw, VOLUME_FORMATS, required=False)


# 1-3: the shapes that really occur keep working
def test_a_valid_in_workspace_surface_resolves(ws):
    art = _surface(ws, str(ws / "mesh.msh"))
    assert art.path == (ws / "mesh.msh").resolve()
    assert art.fmt is ArtifactFormat.GMSH_MSH


def test_a_valid_in_workspace_volume_resolves(ws):
    art = _volume(ws, "VTK/case_0/internal.vtu")
    assert art is not None
    assert art.path == (ws / "VTK" / "case_0" / "internal.vtu").resolve()


def test_a_missing_optional_volume_is_nonfatal(ws):
    assert _volume(ws, "VTK/case_0/never_written.vtu") is None


# 4-7: escapes are refused for required and optional alike
def test_an_absolute_surface_outside_the_workspace_is_rejected(ws, tmp_path):
    outside = tmp_path / "outside.msh"
    outside.write_bytes(MSH)
    with pytest.raises(ReviewRenderError, match="outside the job workspace"):
        _surface(ws, str(outside))


def test_an_absolute_volume_outside_the_workspace_is_rejected(ws, tmp_path):
    outside = tmp_path / "outside.vtu"
    outside.write_bytes(VTU)
    with pytest.raises(ReviewRenderError, match="outside the job workspace"):
        _volume(ws, str(outside))


def test_a_dotdot_surface_traversal_is_rejected(ws):
    with pytest.raises(ReviewRenderError):
        _surface(ws, "../../etc/passwd")


def test_a_dotdot_volume_traversal_is_rejected(ws):
    with pytest.raises(ReviewRenderError):
        _volume(ws, "VTK/../../../etc/passwd")


# 8-10: symlinks resolve to the truth
def test_a_surface_symlink_escape_is_rejected(ws, tmp_path):
    (tmp_path / "evil.msh").write_bytes(MSH)
    (ws / "link.msh").symlink_to(tmp_path / "evil.msh")
    with pytest.raises(ReviewRenderError):
        _surface(ws, str(ws / "link.msh"))


def test_a_volume_symlink_escape_is_rejected(ws, tmp_path):
    (tmp_path / "evil.vtu").write_bytes(VTU)
    (ws / "link.vtu").symlink_to(tmp_path / "evil.vtu")
    with pytest.raises(ReviewRenderError):
        _volume(ws, "link.vtu")


def test_a_symlink_that_stays_inside_the_workspace_is_allowed(ws):
    link = ws / "alias.vtu"
    link.symlink_to(ws / "VTK" / "case_0" / "internal.vtu")
    art = _volume(ws, "alias.vtu")
    assert art is not None
    assert art.path == (ws / "VTK" / "case_0" / "internal.vtu").resolve()


# 11-12: content, not extension
def test_invalid_surface_content_is_rejected_before_gmsh(ws):
    (ws / "mesh.msh").write_bytes(b"PK\x03\x04 this is a zip, not a mesh")
    with pytest.raises(ReviewRenderError, match="does not match any declared format"):
        _surface(ws, str(ws / "mesh.msh"))


def test_invalid_volume_content_is_rejected_before_pyvista(ws):
    (ws / "VTK" / "case_0" / "internal.vtu").write_bytes(b"PK\x03\x04 not a vtu")
    with pytest.raises(ReviewRenderError, match="does not match any declared format"):
        _volume(ws, "VTK/case_0/internal.vtu")


def test_a_non_string_volume_is_refused_rather_than_coerced(ws):
    assert _volume(ws, {"nested": "object"}) is None


# 13-14: the loaders only ever see confined paths
def test_the_renderer_never_receives_an_unconfined_volume_path(ws, tmp_path):
    root = ws.resolve()
    (tmp_path / "evil.vtu").write_bytes(VTU)
    (ws / "link.vtu").symlink_to(tmp_path / "evil.vtu")

    for hostile in ("/etc/passwd", str(tmp_path / "evil.vtu"), "../../../etc/passwd",
                    "link.vtu", "VTK/../../../etc/passwd"):
        try:
            volume = _volume(ws, hostile)
        except ReviewRenderError:
            continue
        if volume is not None:
            assert volume.path.resolve().is_relative_to(root), f"{hostile} escaped"


def test_the_renderer_never_receives_an_unconfined_surface_path(ws, tmp_path):
    root = ws.resolve()
    (tmp_path / "evil.msh").write_bytes(MSH)
    (ws / "link.msh").symlink_to(tmp_path / "evil.msh")

    for hostile in ("/etc/passwd", str(tmp_path / "evil.msh"), "../../../etc/passwd",
                    str(ws / "link.msh")):
        try:
            surface = _surface(ws, hostile)
        except ReviewRenderError:
            continue
        assert surface.path.resolve().is_relative_to(root), f"{hostile} escaped"


# 15: diagnostics name the key, never the path
def test_diagnostics_name_the_manifest_key_not_the_absolute_path(ws, tmp_path):
    secret = tmp_path / "very_secret_dir" / "loot.msh"
    secret.parent.mkdir()
    secret.write_bytes(MSH)
    with pytest.raises(ReviewRenderError) as exc:
        _surface(ws, str(secret))

    msg = str(exc.value)
    assert "mesh_paths.surface" in msg
    assert "very_secret_dir" not in msg and "loot.msh" not in msg
    assert str(tmp_path) not in msg and str(ws) not in msg


# 16: the root cannot be chosen by the manifest
def test_a_manifest_cannot_widen_the_root_by_declaring_a_surface_elsewhere(ws, tmp_path):
    elsewhere = tmp_path / "attacker" / "mesh.msh"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(MSH)
    with pytest.raises(ReviewRenderError, match="outside the job workspace"):
        _surface(ws, str(elsewhere))


# The renderer keeps no path logic of its own.
# These used to read `sandbox/sandbox.py`, a MeshSandbox wrapper that nothing constructed. The
# renderer the five engines actually build through is `render_adapter.build_backend`, so the
# contract is asserted against that module - the wrapper's deletion must not quietly retire it.
def _renderer_source():
    import inspect

    from meshpipeline.sandbox import render_adapter

    return Path(inspect.getsourcefile(render_adapter)).read_text()


def test_the_renderer_module_never_reads_manifest_paths():
    tree = ast.parse(_renderer_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in ("mesh_paths",), (
                f"render_adapter.py still addresses the manifest key {node.value!r}")


def test_the_renderer_module_performs_no_workspace_path_joining():
    tree = ast.parse(_renderer_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = ast.unparse(node.left)
            assert "_workspace" not in left, f"render_adapter.py joins onto the workspace: {left}"
            assert "manifest" not in left, (
                f"render_adapter.py joins onto a manifest value: {left}")


def test_the_confinement_fence_has_exactly_one_implementation():
    import inspect

    from meshpipeline.sandbox import backend, capture, mesh_reader, render_adapter, review_session

    homes = []
    for mod in (review_session, render_adapter, backend, mesh_reader, capture):
        tree = ast.parse(Path(inspect.getsourcefile(mod)).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "confine":
                homes.append(mod.__name__)
            elif isinstance(node, ast.Attribute) and node.attr == "relative_to" \
                    and mod is not review_session:
                homes.append(f"{mod.__name__} (relative_to)")
    assert homes == ["meshpipeline.sandbox.review_session"], (
        f"confinement implementations found in {homes}; ArtifactResolver.confine must be the "
        "only one")


# 20-22: the rejection is reported truthfully
def test_a_refused_artifact_is_not_reported_as_provider_downtime():
    from meshpipeline.errors import FailureClass, classify_api_failure

    assert classify_api_failure("reviewer_evidence_missing") is FailureClass.REVIEW_EVIDENCE_MISSING
    assert classify_api_failure("reviewer_render_unavailable") is FailureClass.REVIEW_EVIDENCE_MISSING


def test_the_reviewer_routes_a_refused_artifact_to_the_evidence_marker():
    import inspect as _i

    from meshpipeline.agents.reviewer import visual

    src = _i.getsource(visual.node_reviewer)
    assert "except ReviewRenderError as exc" in src
    assert "_render_failure_result(inputs, exc)" in src

    mapper = _i.getsource(visual._render_failure_result)
    assert 'marker = "reviewer_evidence_missing"' in mapper
    assert 'marker = "reviewer_render_unavailable"' in mapper  # the two truthful categories
    from meshpipeline.errors import FailureClass, classify_api_failure
    for m in ("reviewer_evidence_missing", "reviewer_render_unavailable"):
        assert classify_api_failure(m) is FailureClass.REVIEW_EVIDENCE_MISSING


def test_a_refused_artifact_never_becomes_a_mesh_verdict():
    import inspect as _i

    from meshpipeline.agents.reviewer import visual

    src = _i.getsource(visual.node_reviewer)
    head, _, tail = src.partition('_service_failure = _evidence_failure')
    assert "submit_findings" not in tail.split("return")[0]
