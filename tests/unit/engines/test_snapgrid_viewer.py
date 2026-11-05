# Responsibility: Verify a delivered snap-grid mesh reaches the viewer as the file's parts: every region and every domain patch a surface of its own, the wrapping ones flagged, and the quality heatmap measured exactly as from the polyMesh.
from __future__ import annotations

import base64
import json
import re

import numpy as np
import pytest
from tests.unit.cad.ecxml_models import Ecxml, ducted_board, tiny_board

from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml
from meshpipeline.engines.snapgrid.viewer import VIEWER_NAME, snapgrid_viewer
from meshpipeline.render import face_quality as FQ

FIELDS = ("non_ortho_b64", "skewness_b64", "aspect_ratio_b64")


def _case(tmp_path, doc: Ecxml):
    src = tmp_path / "model.ecxml"
    src.write_bytes(doc.xml())
    result = mesh_ecxml(src, tmp_path / "case", G.GridPlan(), binary=False)
    return tmp_path / "case", result


def _boundary_faces(polymesh) -> dict[str, int]:
    text = (polymesh / "boundary").read_text()
    return {m.group(1): int(m.group(2)) for m in
            re.finditer(r"(\w[\w.\-]*)\s*\{[^{}]*?nFaces\s+(\d+);", text)}


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


def test_every_region_and_every_domain_patch_is_its_own_surface(tmp_path):
    case, result = _case(tmp_path, tiny_board())
    resp = snapgrid_viewer(case, roles={}, units="m")
    drawn = {p["name"]: p for p in resp["patches"]}
    regions = list(result.report["fluid_regions"]) + list(result.report["solid_regions"])
    whole = _boundary_faces(case / "constant" / "polyMesh")
    assert set(drawn) == set(regions) | set(whole)
    # a region's surface is its WHOLE boundary - interfaces and domain sides - which is exactly
    # the boundary of the region's own polyMesh the solver reads
    for name in regions:
        assert drawn[name]["face_count"] == sum(_boundary_faces(
            case / "constant" / name / "polyMesh").values()), name
    for name, n in whole.items():
        assert drawn[name]["face_count"] == n, name


def test_the_surfaces_that_wrap_the_parts_say_so_and_the_parts_do_not(tmp_path):
    case, result = _case(tmp_path, tiny_board())
    resp = snapgrid_viewer(case, roles={}, units="m")
    wraps = {p["name"] for p in resp["patches"] if p.get("encloses_domain")}
    solids = set(result.report["solid_regions"])
    assert "air" in wraps
    assert not wraps & solids, "a placed part opens hidden"
    assert set(_boundary_faces(case / "constant" / "polyMesh")) <= wraps


def test_the_files_named_patches_are_drawn_under_their_mesh_names(tmp_path):
    case, result = _case(tmp_path, ducted_board())
    drawn = {p["name"] for p in snapgrid_viewer(case, roles={}, units="m")["patches"]}
    assert {"Inlet_fan", "Outlet", "Low_X", "High_X"} <= drawn


def test_the_heatmap_is_the_one_the_polymesh_itself_measures(tmp_path):
    case, _ = _case(tmp_path, tiny_board())
    resp = snapgrid_viewer(case, roles={}, units="m")
    qf = resp["quality_fields"]
    ref = FQ.quality_fields(case / "constant" / "polyMesh", non_ortho_limit=65.0)
    for m in ("non_ortho", "skewness", "aspect_ratio"):
        assert qf["metrics"][m]["max"] == pytest.approx(ref["metrics"][m]["max"], rel=1e-9)
        assert qf["metrics"][m]["n_over"] == ref["metrics"][m]["n_over"]
        assert qf["metrics"][m]["n_faces"] == ref["metrics"][m]["n_faces"]
    # one value per drawn polygon, on every surface
    for p in resp["patches"]:
        f = qf["patches"][p["name"]]
        for field in FIELDS:
            assert len(_f32(f[field])) == p["face_count"], (p["name"], field)
    # a domain patch is a boundary patch of the polyMesh: the same faces, the same numbers
    for name in _boundary_faces(case / "constant" / "polyMesh"):
        for field in FIELDS:
            assert np.allclose(_f32(qf["patches"][name][field]),
                               _f32(ref["patches"][name][field])), (name, field)


def test_a_region_surface_reads_the_cells_on_its_own_side(tmp_path):
    case, _ = _case(tmp_path, tiny_board())
    qf = snapgrid_viewer(case, roles={}, units="m")["quality_fields"]
    # the Chip's whole surface reads Chip cells: their face counts are those of the Chip's own
    # cells (6, or more where a hanging node splits a side) - never the air's across the face
    chip = np.frombuffer(base64.b64decode(qf["patches"]["Chip"]["cell_faces_b64"]), np.uint8)
    region = FQ.read_polymesh(case / "constant" / "Chip" / "polyMesh")
    nfc = np.bincount(np.concatenate([region.owner, region.neighbour]),
                      minlength=region.n_cells)
    assert set(chip.tolist()) <= set(nfc.tolist())


def test_a_viewer_file_written_before_the_flags_and_the_heatmap_still_draws(tmp_path):
    # the old file: each region under its name, an air region as its domain sides ("patch")
    case, result = _case(tmp_path, tiny_board())
    regions = set(result.report["fluid_regions"]) | set(result.report["solid_regions"])
    with np.load(case / VIEWER_NAME) as z:
        keep = [k for k, name in enumerate(z["names"].tolist()) if name in regions]
        old = {"names": z["names"][keep], "types": z["types"][keep],
               "counts": z["counts"][keep], "cell_size": z["cell_size"]}
        for i, k in enumerate(keep):
            old[f"p{i}"], old[f"f{i}"] = z[f"p{k}"], z[f"f{k}"]
    np.savez_compressed(case / VIEWER_NAME, **old)
    resp = snapgrid_viewer(case, roles={}, units="m")
    assert resp and resp["patches"] and "quality_fields" not in resp
    wraps = {p["name"] for p in resp["patches"] if p.get("encloses_domain")}
    assert wraps == set(result.report["fluid_regions"])


def test_a_skipped_surface_takes_its_heatmap_with_it(tmp_path):
    case, _ = _case(tmp_path, tiny_board())
    resp = snapgrid_viewer(case, roles={}, units="m", skip_names=("air",))
    assert "air" not in {p["name"] for p in resp["patches"]}
    assert "air" not in resp["quality_fields"]["patches"]


def test_the_viewer_payload_lists_every_region_as_a_part(tmp_path):
    from meshpipeline.application.viewer_payload import _attach_facts

    case, result = _case(tmp_path, tiny_board())
    resp = snapgrid_viewer(case, roles={}, units="m")
    resp["is_mesh"] = True
    regions = [{"name": n, "type": t, "cells": 1}
               for t, key in (("fluid", "fluid_regions"), ("solid", "solid_regions"))
               for n in result.report[key]]
    manifest = {"mesh_mode": "snapgrid", "mesh_units": "m",
                "patch_types": dict.fromkeys(_boundary_faces(case / "constant" / "polyMesh"),
                                             "patch"),
                "quality": {"regions": regions}}
    _attach_facts(resp, json.loads(json.dumps(manifest)))
    parts = {p["id"]: p for p in resp["parts"]}
    for r in regions:
        part = parts[f"region:{r['name']}"]
        assert part["renderable"] and part["label"] == r["name"]
        assert part["default_visibility"] == ("hidden" if r["type"] == "fluid" else "visible")
    for name in manifest["patch_types"]:
        assert parts[f"boundary:{name}"]["renderable"]
        assert parts[f"boundary:{name}"]["default_visibility"] == "hidden"
    drawn = {p["name"] for p in resp["patches"]}
    assert drawn == {p["metadata"]["name"] for p in resp["parts"] if p["renderable"]}
    assert resp["quality"]["regions"] == len(regions)
