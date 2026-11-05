# Responsibility: Verify the manifest reports the prepared domain box rather than the octree-padded mesh bounds.
import json

from meshpipeline.engines.cfmesh.cfmesh_runner import write_manifest  # noqa: E402
from meshpipeline.engines.domain_extent_gate import check_domain_extents  # noqa: E402


def _fake_workspace(tmp_path):
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("dummy")
    (pm / "boundary").write_text(
        "2\n(\n  body { type wall; nFaces 100; }\n  farfield { type patch; nFaces 50; }\n)\n"
    )
    return tmp_path


def test_manifest_domain_box_is_prepared_box_not_mesh_bounds(tmp_path):
    ws = _fake_workspace(tmp_path)
    # body chord = 1 (x in [0,1]); requested 15c up / 25c down / 20c lateral.
    body_bbox = ([0.0, 0.0, 0.0], [1.0, 0.2, 0.1])
    requested_box = [[-15.0, -20.0, -10.0], [26.0, 20.2, 10.1]]
    # cfMesh octree-pads the mesh OUTWARD (this is what used to be measured).
    mesh_bounds = [-15.6, -20.6, -10.6, 26.6, 20.8, 10.7]  # [xmin,ymin,zmin,xmax,ymax,zmax]

    m = write_manifest(
        ws, patch_types={"body": "wall", "farfield": "farfield"},
        patch_entities={"body": [1], "farfield": [2]},
        bbox=(0,) * 6, quality={"cells": 1000}, domain="external aero",
        body_bbox=body_bbox, mesh_bounds=mesh_bounds, mesh_units="m", requested_box=requested_box,
    )
    geom = m["geometry"]
    # domain_box == the PREPARED box (exact), not the padded mesh bounds.
    assert geom["domain_box"]["xmin"] == -15.0
    assert geom["domain_box"]["xmax"] == 26.0
    assert geom["chord"] == 1.0
    # box_* keys keep the actual padded mesh extent (info), distinct from domain_box.
    assert geom["box_xmin"] == -15.6 and geom["box_xmax"] == 26.6


def test_gate_passes_for_domain_prepared_exactly_to_spec(tmp_path):
    ws = _fake_workspace(tmp_path)
    body_bbox = ([0.0, 0.0, 0.0], [1.0, 0.2, 0.1])
    requested_box = [[-15.0, -20.0, -10.0], [26.0, 20.2, 10.1]]
    mesh_bounds = [-15.6, -20.6, -10.6, 26.6, 20.8, 10.7]  # padded
    m = write_manifest(
        ws, patch_types={"body": "wall", "farfield": "farfield"},
        patch_entities={"body": [1], "farfield": [2]},
        bbox=(0,) * 6, quality={"cells": 1000}, domain="external aero",
        body_bbox=body_bbox, mesh_bounds=mesh_bounds, mesh_units="m", requested_box=requested_box,
    )
    ok, diag = check_domain_extents(
        {"upstream": 15, "downstream": 25, "lateral": 20}, m)
    assert ok, f"a domain prepared exactly to spec must pass, got: {diag}"


def test_prepare_surface_writes_geom_box(tmp_path):
    # geom_box.json is the contract between prepare_surface and the executor.
    box = {"domain_min": [-15.0, -20.0, -10.0], "domain_max": [26.0, 20.2, 10.1]}
    (tmp_path / "geom_box.json").write_text(json.dumps(box))
    loaded = json.loads((tmp_path / "geom_box.json").read_text())
    assert loaded["domain_min"][0] == -15.0 and loaded["domain_max"][0] == 26.0
