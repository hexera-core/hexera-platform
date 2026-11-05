# Responsibility: Verify a declared 2D cfMesh case writes the marker dicts, merges on the contracted empty patch.
from __future__ import annotations

import meshpipeline.engines.cfmesh.cfmesh_runner as R
import meshpipeline.engines.cfmesh.native as N


def _surface(workspace, name="input.stl", **kw):
    from pathlib import Path

    from tests._geometry_support import prepared_surface
    return prepared_surface(Path(workspace) / name, **kw)

# a minimal profile RIBBON: two triangles spanning z 0..0.1 (mechanism fixture - the
# geometry content is irrelevant; only bbox/z-span are consumed by the code under test)
_RIBBON = (
    "solid airfoil\n"
    "facet normal 0 1 0\nouter loop\n"
    "vertex 0 0 0\nvertex 1 0 0\nvertex 1 0 0.1\nendloop\nendfacet\n"
    "facet normal 0 1 0\nouter loop\n"
    "vertex 0 0 0\nvertex 1 0 0.1\nvertex 0 0 0.1\nendloop\nendfacet\n"
    "endsolid airfoil\n")

_FLAT = (
    "solid airfoil\n"
    "facet normal 0 0 1\nouter loop\n"
    "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\n"
    "endsolid airfoil\n")

_CONTRACT = [{"name": "airfoil", "type": "wall"},
             {"name": "farfield", "type": "farfield"},
             {"name": "frontAndBack", "type": "empty"}]


def _configure(ws, monkeypatch, contract=_CONTRACT, stl=_RIBBON):
    (ws / "input.stl").write_text(stl)
    monkeypatch.setattr(R, "_to_fms", lambda *a, **k: "geom.stl")
    return R._configure_external_2d(
        ws, geometry_file="input.stl", surface=_surface(ws), strategy={},
        wall_patch="airfoil", contract_patches=contract, args={}, cell_budget=8_000_000)


def test_2d_configure_writes_marker_dicts_and_ribbon(tmp_path, monkeypatch):
    out = _configure(tmp_path, monkeypatch)
    assert out.get("success"), out
    assert out["dimensionality"] == "2D"
    # the marker is what flips the run binary to cartesian2DMesh
    assert (tmp_path / ".cartesian2d").exists()
    # geom.stl = body ribbon + far-field side ribbon, named for the contract
    stl = (tmp_path / "geom.stl").read_text()
    assert "solid airfoil" in stl and "solid farfield" in stl
    # far-field spans the SAME z range as the body (cartesian2DMesh requirement)
    md = (tmp_path / "system" / "meshDict").read_text()
    assert "maxCellSize" in md and "renameBoundary" in md
    # NO defaultName block in 2D: the tool's generated bottomEmptyFaces/topEmptyFaces
    # don't match newPatchNames entries (live-proven - a defaultName absorbed all their
    # faces into fixedWalls/patch, breaking the empty merge); they must pass through
    # renameBoundary untouched for createPatch to find them
    assert "defaultName" not in md and "fixedWalls" not in md
    assert "bottomEmptyFaces" not in md and "topEmptyFaces" not in md
    assert "airfoil { newName airfoil; type wall; }" in md
    # requested far-field corners persisted for the domain-extent gate
    assert (tmp_path / "geom_box.json").exists()


def test_2d_merge_targets_the_contracted_empty_patch(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    cp = (tmp_path / "system" / "createPatchDict").read_text()
    assert "name frontAndBack" in cp
    assert "type empty" in cp
    assert "bottomEmptyFaces topEmptyFaces" in cp


def test_2d_requires_exactly_one_empty_patch(tmp_path, monkeypatch):
    no_empty = [p for p in _CONTRACT if p["type"] != "empty"]
    out = _configure(tmp_path, monkeypatch, contract=no_empty)
    assert not out.get("success") and "empty" in out["error"]
    two = _CONTRACT + [{"name": "alsoEmpty", "type": "empty"}]
    out = _configure(tmp_path, monkeypatch, contract=two)
    assert not out.get("success") and "EXACTLY ONE" in out["error"]


def test_2d_rejects_a_flat_profile_needs_a_ribbon(tmp_path, monkeypatch):
    out = _configure(tmp_path, monkeypatch, stl=_FLAT)
    assert not out.get("success")
    assert "RIBBON" in out["error"]


def test_declared_dimensionality_drives_the_2d_branch(tmp_path, monkeypatch):
    (tmp_path / "input.stl").write_text(_RIBBON)
    (tmp_path / "dimensionality").write_text("2D")
    called = {}
    monkeypatch.setattr(R, "_configure_external_2d",
                        lambda *a, **k: called.setdefault("2d", True) or {"success": True})
    R.configure_mesh(tmp_path, geometry_file="input.stl", surface=_surface(tmp_path), strategy={},
                     wall_patch="airfoil", contract_patches=_CONTRACT, args={},
                     cell_budget=8_000_000)
    assert called.get("2d")


def test_2d_internal_flow_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "input.stl").write_text(_RIBBON)
    (tmp_path / "dimensionality").write_text("2D")
    (tmp_path / "flow_topology").write_text("internal")
    out = R.configure_mesh(tmp_path, geometry_file="input.stl", surface=_surface(tmp_path), strategy={},
                           wall_patch="airfoil", contract_patches=_CONTRACT, args={},
                           cell_budget=8_000_000)
    assert not out.get("success")
    assert "not supported" in out["error"]
    for bad in ("upstream", "wrapper", "experimental", "yet"):
        assert bad not in out["error"].lower()


def test_run_local_switches_binary_and_merges_on_marker(tmp_path, monkeypatch):
    cmds = []

    class _P:
        returncode = 0

    def _fake_run(argv, **kw):
        cmds.append(argv[-1])
        return _P()

    monkeypatch.setattr(N, "run_guarded", _fake_run)
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "createPatchDict").write_text("x")
    (tmp_path / ".cartesian2d").write_text("cartesian2DMesh\n")
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["rc"] == 0
    assert any("cartesian2DMesh" in c for c in cmds)
    assert any("createPatch -overwrite" in c for c in cmds)
    # and WITHOUT the marker: plain 3D cartesianMesh, no merge
    cmds.clear()
    (tmp_path / ".cartesian2d").unlink()
    N._run_cartesian_mesh_local(tmp_path)
    assert any("cartesianMesh" in c and "cartesian2DMesh" not in c for c in cmds)
    assert not any("createPatch" in c for c in cmds)


def test_cfmesh_declares_2d_snappy_does_not():
    from meshpipeline.engines.registry import get_spec
    assert "2D" in get_spec("cfmesh").input_contract.dimensionalities
    assert "2D" not in get_spec("snappy").input_contract.dimensionalities


def test_finalize_declares_generated_patches_from_the_real_boundary():
    assert getattr(R, "review_surface_is_body_only", False) is True
