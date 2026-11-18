"""A volume that was left behind on the way home is not published, and neither are cuts through it.

WHY THIS EXISTS. `engines/manifest._published_volume` carries the rule in its own docstring -
"Absence is a real, stated state; a stand-in path is not" - and then only ever tested `is_dir()`.
`_published_surface`, written later and directly below it, tests `is_file()`. So the guard rejected
the case that does not happen and passed the one that does.

MEASURED, job 0ff6f9e5, 2026-11-16. `engines/return_scope` gave up the fluid interior to get the
archive under its ceiling and UNLINKED it; its own return_scope.json records

    "left_out": [{"file": "VTK/fluid/cr_ws_vgcm8hv7_0/internal.vtu", "bytes": 10366611525}]

The report still carried the path the export had been written at, this guard still published it
because a deleted file is not a directory, and `inspection_regions` was gated on that same truthy
path - so the manifest promised a volume that was gone and three cuts through it. The review then
refused with "3 region(s) not offered - no volume artifact", which is the engine looking as though
it had produced evidence right up to the moment of being asked for it.

AND THE OTHER HALF: a cut no longer needs the volume to come home. The mesher draws the cuts where
the mesh is and sends PNGs, so a run with no volume and real pictures keeps its promise - while a
run with neither still promises nothing.
"""
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.manifest import write_manifest

_MSH = b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n"
_VTU = b'<?xml version="1.0"?>\n<VTKFile type="UnstructuredGrid"></VTKFile>\n'
_PNG = bytes.fromhex("89504e470d0a1a0a")  # a PNG magic number is enough: nothing opens it here


def _write(ws: Path, **kw):
    (ws / "mesh.msh").write_bytes(_MSH)
    return write_manifest(
        ws, patch_types={"wall_outer": "wall"}, patch_entities={},
        bbox=(0, 0, 0, 1, 1, 1),
        quality={"cells": 283_410_955, "max_non_ortho": 84.08, "fatal": [],
                 "skew_fraction": 0.0},
        mesh_units="m", mesh_mode="cfmesh_conjugate", **kw)


def _cut(name="across_the_flow", image=None):
    frames = [{"scope": "whole", "window_m": [0.1349, 0.1012], "metres_per_pixel": 1.054e-4,
               "um_per_pixel": 105.42},
              {"scope": "zoom", "window_m": [0.00805, 0.00604], "metres_per_pixel": 6.2895e-6,
               "um_per_pixel": 6.2895}]
    if image is not None:
        frames[1]["image"] = image
    return {"name": name, "kind": "slice", "normal": [1, 0, 0], "origin": [0.5, 0.5, 0.5],
            "frames": frames}


def test_a_volume_that_was_left_behind_is_not_published(tmp_path):
    """Job 0ff6f9e5 exactly: the path the export was written at, and no file there any more."""
    gone = tmp_path / "VTK" / "fluid" / "cr_ws_vgcm8hv7_0" / "internal.vtu"
    gone.parent.mkdir(parents=True)
    m = _write(tmp_path, volume_path=str(gone))
    assert m["mesh_paths"]["volume"] is None, (
        "the manifest promises a volume that return_scope deleted to get the archive home")


def test_no_cut_is_promised_through_a_volume_that_is_not_there(tmp_path):
    """The three cuts job 0ff6f9e5 promised and could not keep."""
    gone = tmp_path / "VTK" / "fluid" / "x" / "internal.vtu"
    gone.parent.mkdir(parents=True)
    m = _write(tmp_path, volume_path=str(gone),
               inspection_regions=[_cut("across_the_flow"), _cut("through_the_plate"),
                                   _cut("along_the_flow")])
    assert m["inspection_regions"] == [], (
        "cuts are promised with no volume to cut and no picture of one")


def test_a_volume_that_is_there_is_still_published(tmp_path):
    vtu = tmp_path / "VTK" / "fluid" / "x" / "internal.vtu"
    vtu.parent.mkdir(parents=True)
    vtu.write_bytes(_VTU)
    m = _write(tmp_path, volume_path=str(vtu))
    assert m["mesh_paths"]["volume"] == str(vtu)


def test_cuts_the_mesher_drew_are_promised_without_any_volume(tmp_path):
    """The whole point of cutting on the VM: the interior is 10.4 GB and cannot come home, and the
    pictures are kilobytes and can."""
    shot = tmp_path / "review_cuts" / "across_the_flow_zoom.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(_PNG)
    m = _write(tmp_path, volume_path=None,
               inspection_regions=[_cut(image="review_cuts/across_the_flow_zoom.png")])
    assert [r["name"] for r in m["inspection_regions"]] == ["across_the_flow"]
    zoom = m["inspection_regions"][0]["frames"][1]
    assert zoom["image"] == "review_cuts/across_the_flow_zoom.png"
    # the window travels with the picture, or the claim cannot be checked against it
    assert zoom["window_m"] == [0.00805, 0.00604]
    assert zoom["um_per_pixel"] == 6.2895


def test_a_frame_naming_a_picture_that_is_not_there_keeps_its_window_and_loses_the_name(tmp_path):
    """Same rule as the surface and the volume, one level down: a dangling name sends a reader
    looking for a bug that is not there."""
    shot = tmp_path / "review_cuts" / "kept.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(_PNG)
    m = _write(tmp_path, volume_path=None,
               inspection_regions=[_cut("across_the_flow", image="review_cuts/kept.png"),
                                   _cut("along_the_flow", image="review_cuts/gone.png")])
    by = {r["name"]: r for r in m["inspection_regions"]}
    assert "image" in by["across_the_flow"]["frames"][1]
    missing = by["along_the_flow"]["frames"][1]
    assert "image" not in missing, "a frame names a picture that is not on disk"
    assert missing["um_per_pixel"] == 6.2895, "the window is true of the cut either way"
