# Responsibility: Verify a thermal model's mesh names its outside from the file - each domain side a patch, each fan, vent and plate on a side its own patch - and that no 3D mesh passes with an `empty` patch.
# Every ECXML mesh on the lab came back with its whole outside as one patch "defaultFaces" of type
# empty (ECXML-TEST, 2026-10-06): checkMesh fails such a region, and the fan inlet, the grille
# outlet and the side plates the file declares were nowhere in the mesh.
from __future__ import annotations

import json

import pytest
from tests.unit.cad.ecxml_models import ducted_board, tiny_board

from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
from meshpipeline.engines.snappy_multiregion import native as N


def _sidecar(doc) -> dict:
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_build import build

    return build(read_ecxml(doc.xml())).sidecar


def test_the_files_sides_fans_vents_and_plates_become_exterior_patches():
    rects = {r["name"]: r for r in R.thermal_exterior_patches(_sidecar(ducted_board()), tol=1e-9)}
    # the duct: four plate walls, a fan blowing in at +y, a grille letting out at -y - no bare side
    assert {n: (r["type"], r["face"]) for n, r in rects.items()} == {
        "Low_X": ("wall", "-x"), "High_X": ("wall", "+x"), "Low_Z": ("wall", "-z"),
        "High_Z": ("wall", "+z"), "Inlet_fan": ("patch", "+y"), "Outlet": ("patch", "-y")}
    fan = rects["Inlet_fan"]
    assert fan["lo"] == pytest.approx([-0.06, 0.12, -0.006], abs=1e-8)
    assert fan["hi"] == pytest.approx([0.06, 0.12, 0.03], abs=1e-8)
    # a board in open air: its six domain sides, each an open patch
    sides = R.thermal_exterior_patches(_sidecar(tiny_board()), tol=1e-9)
    assert [(r["name"], r["type"], r["kind"]) for r in sides] == [
        (f"domain_{a}{m}", "patch", "side") for a in "xyz" for m in ("min", "max")]


def test_a_side_keeps_only_what_its_devices_leave_and_a_declared_port_names_its_rectangle():
    rects = [{"name": "Fan", "kind": "device", "face": "+x", "type": "patch",
              "lo": [1.0, 0.2, 0.2], "hi": [1.0, 0.4, 0.4]},
             {"name": "domain_xmax", "kind": "side", "face": "+x", "type": "patch",
              "lo": [1.0, 0.0, 0.0], "hi": [1.0, 1.0, 1.0]},
             {"name": "domain_xmin", "kind": "side", "face": "-x", "type": "patch",
              "lo": [0.0, 0.0, 0.0], "hi": [0.0, 1.0, 1.0]}]
    ports = [{"name": "outlet", "type": "outlet", "near_mm": [1000.0, 300.0, 300.0]}]
    acts, made = N.file_patch_actions(rects, ports)
    assert made == [("outlet", "patch", "file_0"), ("domain_xmax", "patch", "file_1"),
                    ("domain_xmin", "patch", "file_2")]
    text = "\n".join(acts)
    # the +x side gives up the fan's faces; the -x side has no device to give up
    assert "name file_1; type faceSet; action delete; source boxToFace; box (1 0.2 0.2) (1 0.4 0.4);" in text
    assert "name file_2; type faceSet; action delete" not in text


def _region(ws, name, patches):
    pm = ws / "constant" / name / "polyMesh"
    pm.mkdir(parents=True)
    body = "\n".join(f"    {p}\n    {{\n        type {t};\n        nFaces 10;\n        startFace 0;\n    }}"
                     for p, t in patches)
    (pm / "boundary").write_text(f"{len(patches)}\n(\n{body}\n)\n")


def test_the_fluids_outside_is_cut_into_the_files_patches(tmp_path):
    ws = tmp_path
    (ws / R.EXTERIOR_PATCHES).write_text(json.dumps(
        R.thermal_exterior_patches(_sidecar(ducted_board()), tol=1e-9)))
    _region(ws, "air", [("air_to_Board", "mappedWall"), ("exterior", "wall")])
    _region(ws, "Board", [("Board_to_air", "mappedWall")])
    plan = dict(N.name_exterior(ws, [{"name": "air", "type": "fluid"},
                                      {"name": "Board", "type": "solid"}]))
    assert [c for c, _ in plan["air"]][:2] == ["topoSet -region air",
                                               "createPatch -region air -overwrite"]
    patches = (ws / "system" / "air" / "createPatchDict").read_text()
    assert "name Inlet_fan; patchInfo { type patch; } constructFrom set;" in patches
    assert "name Outlet; patchInfo { type patch; }" in patches
    assert "name Low_X; patchInfo { type wall; }" in patches
    assert "Board" not in plan                     # no exterior: nothing to name


def test_a_3d_multiregion_mesh_with_an_empty_patch_fails(tmp_path, monkeypatch):
    ws = tmp_path
    (ws / ".regions.json").write_text(json.dumps([{"name": "air", "type": "fluid", "solids": [0]},
                                                  {"name": "chip", "type": "solid", "solids": [1]}]))
    _region(ws, "air", [("air_to_chip", "mappedWall"), ("defaultFaces", "empty")])
    _region(ws, "chip", [("chip_to_air", "mappedWall")])
    monkeypatch.setattr(R, "_single_region_check_mesh", lambda ws, region: {"cells": 10, "fatal": []})
    monkeypatch.setattr(R, "_region_dirs", lambda ws: ["air", "chip"])
    q = R.check_mesh(ws)
    assert q["mesh_ok"] is False
    assert q["fatal"] == ["air:empty patch(es) ['defaultFaces'] on a 3D mesh"]


@pytest.mark.parametrize("engine", ["snappy", "cfmesh", "snappy_multiregion"])
def test_checkmesh_saying_not_1d_or_2d_is_fatal_in_every_openfoam_engine(engine):
    import importlib

    fatal = importlib.import_module(f"meshpipeline.engines.{engine}.foam_exec")._FATAL
    said = ("***Total number of faces on empty patches is not divisible by the number of cells "
            "in the mesh. Hence this mesh is not 1D or 2D.").lower()
    assert [label for marker, label in fatal if marker in said] == ["empty patches on a 3D mesh"]
