# Responsibility: Verify region interfaces are reciprocal and conformal, and a generated one is never a user boundary.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.snappy_multiregion import multiregion_runner as R

_BOUNDARY_HDR = "FoamFile{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }\n"


def _boundary(patches: dict[str, tuple[str, int]]) -> str:
    body = [f"{len(patches)}", "("]
    for name, (typ, nfaces) in patches.items():
        body.append(f"    {name}\n    {{\n        type {typ};\n        nFaces {nfaces};\n"
                    f"        startFace 0;\n    }}")
    body.append(")")
    return _BOUNDARY_HDR + "\n".join(body) + "\n"


def _region(ws, name, patches):
    poly = ws / "constant" / name / "polyMesh"
    poly.mkdir(parents=True, exist_ok=True)
    # A delivered region is the COMPLETE, non-empty polyMesh set - `owner`
    # alone is what a partial split leaves behind and must not count as a region.
    from meshpipeline.engines.snappy_multiregion.regions import POLYMESH_COMPONENTS
    for _component in POLYMESH_COMPONENTS:
        (poly / _component).write_text("x\n")
    (poly / "boundary").write_text(_boundary(patches))


def _cht_workspace(tmp_path, *, fluid_iface=("fluid_to_solid", 40), solid_iface=("solid_to_fluid", 40),
                   fluid_extra=None, solid_extra=None):
    regions = [{"name": "fluid", "type": "fluid", "solids": [0]},
               {"name": "solid", "type": "solid", "solids": [1]}]
    (tmp_path / "constant").mkdir(parents=True, exist_ok=True)
    (tmp_path / "constant" / ".regions.json").write_text(json.dumps(regions))   # not used by reader
    (tmp_path / ".regions.json").write_text(json.dumps(regions))
    fluid_patches = {"outer": ("wall", 96), fluid_iface[0]: ("mappedWall", fluid_iface[1])}
    fluid_patches.update(fluid_extra or {})
    solid_patches = {solid_iface[0]: ("mappedWall", solid_iface[1])}
    solid_patches.update(solid_extra or {})
    _region(tmp_path, "fluid", fluid_patches)
    _region(tmp_path, "solid", solid_patches)
    (tmp_path / "constant" / "regionProperties").write_text(R.render_region_properties(regions))
    return regions


@pytest.fixture
def stub_region_checkmesh(monkeypatch):
    state = {"cells": 500, "fatal": []}

    def _fake(ws, region=""):
        return {"cells": state["cells"], "fatal": list(state["fatal"]),
                "skew_fraction": 0.0, "max_non_ortho": 30.0}

    monkeypatch.setattr(R, "_single_region_check_mesh", _fake)
    return state


# baseline
def test_a_valid_two_region_cht_reconciles(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path)
    q = R.check_mesh(tmp_path)
    assert q["regions_missing"] == [] and q["regions_undeclared"] == []
    assert q["interface_ok"] is True and q["mesh_ok"] is True
    assert {r["name"] for r in q["regions"]} == {"fluid", "solid"}


def test_region_directories_correspond_exactly_to_declared_regions(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path)
    assert set(R._region_dirs(tmp_path)) == {"fluid", "solid"}
    assert "fluid" in R.render_region_properties(
        [{"name": "fluid", "type": "fluid", "solids": [0]},
         {"name": "solid", "type": "solid", "solids": [1]}])


# MUTATIONS
def test_removing_a_region_directory_fails_as_missing(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "constant" / "solid")
    q = R.check_mesh(tmp_path)
    assert q["regions_missing"] == ["solid"] and q["mesh_ok"] is False


def test_an_undeclared_region_is_never_silently_ignored(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path)
    _region(tmp_path, "domain0", {"defaultFaces": ("wall", 12),
                                  "domain0_to_fluid": ("mappedWall", 8)})
    q = R.check_mesh(tmp_path)
    assert q["regions_undeclared"] == ["domain0"] and q["mesh_ok"] is False


def test_renaming_one_interface_side_breaks_reciprocity(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path, solid_iface=("solid_to_FLUID_typo", 40))
    q = R.check_mesh(tmp_path)
    assert q["interface_ok"] is False and q["mesh_ok"] is False


def test_removing_one_side_of_an_interface_pair_fails(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path)
    # drop the solid-side interface patch entirely
    _region(tmp_path, "solid", {"solid_ground": ("wall", 10)})
    q = R.check_mesh(tmp_path)
    assert q["interface_ok"] is False and q["mesh_ok"] is False


def test_disagreeing_reciprocal_face_counts_fail(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path, fluid_iface=("fluid_to_solid", 40), solid_iface=("solid_to_fluid", 41))
    q = R.check_mesh(tmp_path)
    assert "fluid_to_solid" in q["interface_mismatch"] and q["mesh_ok"] is False


def test_a_generated_interface_is_never_counted_as_a_user_boundary(tmp_path):
    _cht_workspace(tmp_path, fluid_extra={"inlet": ("patch", 4), "heater_wall": ("wall", 20)})
    delivered = R.delivered_user_boundary_types(tmp_path, intake_patches=[
        {"name": "inlet", "type": "inlet"}, {"name": "heater_wall", "type": "wall"},
        {"name": "outer", "type": "wall"}])
    assert "fluid_to_solid" not in delivered and "solid_to_fluid" not in delivered
    assert delivered["heater_wall"] == "wall" and delivered["outer"] == "wall"
    assert delivered["inlet"] == "inlet"


def test_an_empty_region_is_rejected(tmp_path, stub_region_checkmesh):
    _cht_workspace(tmp_path)
    stub_region_checkmesh["cells"] = 0
    stub_region_checkmesh["fatal"] = ["no cells in mesh"]
    q = R.check_mesh(tmp_path)
    assert q["mesh_ok"] is False and any("no cells" in f for f in q["fatal"])


def test_regionProperties_lists_exactly_the_declared_regions():
    rp = R.render_region_properties([{"name": "coolant", "type": "fluid", "solids": [0]},
                                     {"name": "chip", "type": "solid", "solids": [1]}])
    assert "fluid       (coolant)" in rp and "solid       (chip)" in rp
    assert "domain0" not in rp
