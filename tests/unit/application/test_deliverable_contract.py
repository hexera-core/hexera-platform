# Responsibility: Verify each engine's deliverable comes from its manifest, and a bundle is byte-identical on rebuild.
from __future__ import annotations

import tarfile
import uuid

import pytest
from tests.engine_workspaces import build_workspace, engine_names

import meshpipeline.application.artifact_uploader as au
from meshpipeline.engines.registry import ENGINE_CATALOG

ENGINES = engine_names()


# engine identity (F8)

def test_the_marker_collision_is_real_so_guessing_could_not_have_worked():
    by_marker: dict[str, list[str]] = {}
    for name, spec in ENGINE_CATALOG.items():
        if spec.implemented and spec.deliverable:
            by_marker.setdefault(spec.deliverable.marker, []).append(name)
    shared = {m: e for m, e in by_marker.items() if len(e) > 1}
    assert shared == {"constant/polyMesh/owner": ["cfmesh", "snappy"]}, shared


def test_a_snappy_workspace_is_never_classified_as_cfmesh(tmp_path):
    ws = build_workspace(tmp_path, "snappy")
    assert au.resolve_engine(ws, "snappy").name == "snappy"
    with pytest.raises(au.BundleContractError, match="disagrees"):
        au.resolve_engine(ws, "cfmesh")


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_selects_its_own_deliverable(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    assert au.resolve_engine(ws, engine).name == engine


def test_a_missing_manifest_fails_rather_than_falling_back(tmp_path):
    ws = build_workspace(tmp_path, "gmsh", omit=("mesh_manifest.json",))
    (ws / "mesh_manifest.json").unlink(missing_ok=True)
    with pytest.raises(au.BundleContractError, match="missing"):
        au.resolve_engine(ws, "gmsh")


def test_a_malformed_manifest_fails(tmp_path):
    ws = build_workspace(tmp_path, "gmsh", manifest="malformed")
    with pytest.raises(au.BundleContractError, match="unreadable"):
        au.resolve_engine(ws, "gmsh")


def test_a_manifest_disagreeing_with_the_execution_fails(tmp_path):
    ws = build_workspace(tmp_path, "gmsh", manifest={"mesh_mode": "vmtk"})
    with pytest.raises(au.BundleContractError, match="disagrees"):
        au.resolve_engine(ws, "gmsh")


def test_an_unknown_or_absent_engine_is_a_contract_failure(tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    with pytest.raises(au.BundleContractError, match="unknown engine"):
        au.resolve_engine(ws, "not-an-engine")
    with pytest.raises(au.BundleContractError, match="no engine"):
        au.resolve_engine(ws, "")


def test_the_uploader_no_longer_iterates_the_catalog():
    src = (au.__file__ and open(au.__file__).read()) or ""
    assert "ENGINE_CATALOG" not in src


# bundle completeness (F9)

@pytest.mark.parametrize("engine", ENGINES)
def test_a_complete_workspace_produces_a_valid_bundle(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    plan = au._build_plan(uuid.uuid4(), ws, [], engine)
    bundle = [p for p in plan if p.logical_key == "mesh_bundle"]
    assert len(bundle) == 1
    spec = ENGINE_CATALOG[engine]
    with tarfile.open(bundle[0].local_path) as tf:
        names = set(tf.getnames())
    for rel in spec.deliverable.required:
        assert f"{spec.deliverable.prefix}/{rel}" in names, rel


@pytest.mark.parametrize("engine", ENGINES)
def test_a_missing_required_member_blocks_delivery(tmp_path, engine):
    spec = ENGINE_CATALOG[engine]
    victim = spec.deliverable.required[-1]
    ws = build_workspace(tmp_path, engine, omit=(victim,))
    with pytest.raises(au.BundleContractError, match="contract not satisfied"):
        au._build_plan(uuid.uuid4(), ws, [], engine)


@pytest.mark.parametrize("engine", ENGINES)
def test_missing_optional_members_still_deliver(tmp_path, engine):
    ws = build_workspace(tmp_path, engine, include_optional=False)
    plan = au._build_plan(uuid.uuid4(), ws, [], engine)
    assert any(p.logical_key == "mesh_bundle" for p in plan)


def test_an_empty_required_file_is_not_a_deliverable(tmp_path):
    ws = build_workspace(tmp_path, "gmsh", empty=("gmsh_spec.json",))
    with pytest.raises(au.BundleContractError, match="empty"):
        au._build_plan(uuid.uuid4(), ws, [], "gmsh")


def test_an_empty_required_directory_is_not_a_deliverable(tmp_path):
    ws = build_workspace(tmp_path, "snappy", empty=("constant/triSurface",))
    with pytest.raises(au.BundleContractError, match="empty"):
        au._build_plan(uuid.uuid4(), ws, [], "snappy")


def test_a_symlink_escaping_the_workspace_is_refused(tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    target = ws / "gmsh_spec.json"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(au.BundleContractError, match="outside the workspace"):
        au._build_plan(uuid.uuid4(), ws, [], "gmsh")


def test_a_marker_only_archive_is_rejected(tmp_path):
    ws = build_workspace(tmp_path, "cfmesh", omit=("system/meshDict", "geom.stl"))
    with pytest.raises(au.BundleContractError):
        au._build_plan(uuid.uuid4(), ws, [], "cfmesh")


@pytest.mark.parametrize("engine", ENGINES)
def test_the_bundle_is_byte_identical_across_rebuilds(tmp_path, engine):
    import hashlib
    ws = build_workspace(tmp_path, engine)
    digests = []
    for _ in range(2):
        plan = au._build_plan(uuid.uuid4(), ws, [], engine)
        path = next(p.local_path for p in plan if p.logical_key == "mesh_bundle")
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]
