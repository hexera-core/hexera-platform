# Responsibility: Verify every engine produces a serialisable viewer payload that outlives its workspace.
from __future__ import annotations

import json
import shutil
import uuid

import pytest
from tests.engine_workspaces import build_workspace, engine_names

import meshpipeline.application.artifact_uploader as au
from meshpipeline.application.viewer_payload import (
    VIEWER_LOGICAL_KEY,
    ViewerPayloadError,
    build_viewer_payload,
)

ENGINES = engine_names()


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_produces_a_serialisable_viewer_payload(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    payload = build_viewer_payload(ws)

    assert payload["engine"] == engine
    assert payload["mesh_available"] is True
    assert payload["surface"], "no surface for the viewer to render"
    assert "cell_count" in payload["quality"]
    # it must survive the trip through object storage as JSON
    assert json.loads(json.dumps(payload, default=str))["engine"] == engine


@pytest.mark.parametrize("engine", ENGINES)
def test_the_payload_is_planned_as_a_required_artifact(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    plan = au._build_plan(uuid.uuid4(), ws, [], engine)
    viewer = [p for p in plan if p.logical_key == VIEWER_LOGICAL_KEY]

    assert len(viewer) == 1
    assert viewer[0].required, "a mesh the user cannot view is not a completed delivery"
    assert viewer[0].content_type == "application/json"


@pytest.mark.parametrize("engine", ENGINES)
def test_the_payload_outlives_the_workspace(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    plan = au._build_plan(uuid.uuid4(), ws, [], engine)
    blob = next(p.local_path for p in plan if p.logical_key == VIEWER_LOGICAL_KEY).read_bytes()

    shutil.rmtree(ws)
    assert not ws.exists()

    payload = json.loads(blob)
    assert payload["engine"] == engine and payload["surface"]


@pytest.mark.parametrize("engine", ENGINES)
def test_the_payload_carries_no_workspace_path(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    blob = json.dumps(build_viewer_payload(ws), default=str)
    assert str(tmp_path) not in blob, "a worker path was persisted into the viewer payload"


def test_a_workspace_with_no_renderable_surface_blocks_delivery(tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    for stl in ws.rglob("*.stl"):
        stl.unlink()
    with pytest.raises(ViewerPayloadError):
        build_viewer_payload(ws)
    with pytest.raises(au.BundleContractError, match="viewer payload"):
        au._build_plan(uuid.uuid4(), ws, [], "gmsh")


def test_a_workspace_without_a_manifest_produces_no_payload(tmp_path):
    ws = build_workspace(tmp_path, "cfmesh")
    (ws / "mesh_manifest.json").unlink()
    with pytest.raises(ViewerPayloadError):
        build_viewer_payload(ws)


@pytest.mark.parametrize("engine", ["snappy_multiregion", "vmtk", "gmsh"])
def test_engine_specific_viewer_paths_are_exercised(tmp_path, engine):
    ws = build_workspace(tmp_path, engine)
    payload = build_viewer_payload(ws)
    assert payload["surface"].get("parts"), f"{engine} produced no renderable parts"
