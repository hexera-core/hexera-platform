# Responsibility: Verify the manifest follows the delivered boundary once createPatch has folded the
# layer policy's class regions back into the declared wall.
# The policy splits a wall into <wall>_thin / <wall>_razor so each class carries its own layers;
# createPatch merges those patches back after snappyHexMesh (see test_snappy_class_patch_merge).
# finalize then read the layer record, declared body_thin / body_razor as wall patches, and the
# manifest gate refused the mesh for patches with zero faces - five corpus rotors, all with a
# production-grade mesh (jobs c757c39f, 432b6737, 859694e0).
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.engines.snappy.finalize import folded_class_regions

_BOUNDARY = """FoamFile {{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }}
{n}
(
{entries}
)
"""


def _boundary(ws: Path, *names: str) -> None:
    pm = ws / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    entries = "\n".join(
        f"{nm}\n{{\n    type            {'wall' if nm != 'farfield' else 'patch'};\n"
        f"    nFaces          10;\n    startFace       {10 * i};\n}}" for i, nm in enumerate(names))
    (pm / "boundary").write_text(_BOUNDARY.format(n=len(names), entries=entries))


def _policy(**regions: str) -> dict:
    return {"region_patches": dict(regions)}


def test_merged_class_regions_are_folded_into_their_wall(tmp_path):
    _boundary(tmp_path, "body", "farfield")
    folded = folded_class_regions(
        tmp_path, _policy(body="normal", body_thin="thin", body_razor="razor"))
    assert folded == {"body_thin": "body", "body_razor": "body"}


def test_a_class_region_the_boundary_still_carries_is_a_real_patch(tmp_path):
    # no merge happened (or it failed): the region IS in the mesh and must stay declared
    _boundary(tmp_path, "body", "body_thin", "farfield")
    folded = folded_class_regions(tmp_path, _policy(body="normal", body_thin="thin"))
    assert folded == {}


def test_real_cad_solids_are_never_folded(tmp_path):
    # a user's own patch that went missing must keep failing the manifest gate downstream
    _boundary(tmp_path, "body", "farfield")
    folded = folded_class_regions(tmp_path, _policy(body="normal", wall_inner="normal"))
    assert folded == {}


def test_without_a_boundary_nothing_is_folded(tmp_path):
    folded = folded_class_regions(tmp_path, _policy(body="normal", body_thin="thin"))
    assert folded == {}


def test_no_policy_folds_nothing(tmp_path):
    _boundary(tmp_path, "body", "farfield")
    assert folded_class_regions(tmp_path, None) == {}
    assert folded_class_regions(tmp_path, {}) == {}


def test_the_layer_record_on_disk_is_the_shape_the_helper_reads(tmp_path):
    # the record finalize loads is layer_policy.json with a region_patches map (layer_policy.py)
    rec = {"region_patches": {"body": "normal", "body_razor": "razor"}, "classes": {}}
    (tmp_path / "layer_policy.json").write_text(json.dumps(rec))
    _boundary(tmp_path, "body", "farfield")
    loaded = json.loads((tmp_path / "layer_policy.json").read_text())
    assert folded_class_regions(tmp_path, loaded) == {"body_razor": "body"}
