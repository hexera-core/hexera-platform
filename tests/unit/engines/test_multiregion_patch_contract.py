# Responsibility: Verify a multi-region wall role comes from its OpenFOAM type, and interfaces are not user patches.
from __future__ import annotations

import json

from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.registry import get_spec
from meshpipeline.engines.snappy_multiregion import multiregion_runner as mr
from meshpipeline.engines.snappy_multiregion.gates import (
    _gate_cht_region_contract,
    _gate_multiregion_patch_contract,
)


def _write_region_mesh(ws, name: str, *, complete: bool = True) -> None:
    from pathlib import Path as _P

    from meshpipeline.engines.snappy_multiregion.regions import POLYMESH_COMPONENTS
    poly = _P(ws) / "constant" / name / "polyMesh"
    poly.mkdir(parents=True, exist_ok=True)
    for component in (POLYMESH_COMPONENTS if complete else ("owner",)):
        # never clobber a component the test wrote itself - a real `boundary` carries the
        # patch table these suites parse.
        if not (poly / component).exists():
            (poly / component).write_text("x")


_SPEC = get_spec("snappy_multiregion")


def _gate(key):
    return next(g for g in _SPEC.gates if g.key == key)


# the real boundary parser + delivered-role derivation

def _write_boundary(path, patches: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = "\n".join(
        f"    {name}\n    {{\n        type {of_type};\n        nFaces {nf};\n        startFace 0;\n    }}"
        for name, (of_type, nf) in patches.items())
    path.write_text(f"FoamFile{{}}\n{len(patches)}\n(\n{blocks}\n)\n")


def test_the_boundary_parser_reads_names_and_of_types(tmp_path):
    b = tmp_path / "boundary"
    _write_boundary(b, {"wing": ("wall", 100), "farfield": ("patch", 50),
                        "air_to_solid": ("mappedWall", 20)})
    assert mr._parse_boundary_nfaces(b) == {"wing": 100, "farfield": 50, "air_to_solid": 20}
    assert mr._parse_boundary_of_types(b) == {"wing": "wall", "farfield": "patch",
                                              "air_to_solid": "mappedWall"}


def test_delivered_role_is_of_derived_for_walls_and_declared_for_flow_patches():
    # wall / symmetry / empty are proven by the mesh itself, independent of intake
    assert mr._delivered_user_role("wall", "farfield") == "wall"      # OF wins - a re-role is caught
    assert mr._delivered_user_role("patch", "wall") == "patch"       # declared wall, generic patch delivered → NOT echoed back (re-role caught)
    assert mr._delivered_user_role("symmetryPlane", None) == "symmetry"
    assert mr._delivered_user_role("empty", None) == "empty"
    # a generic flow patch carries the user's declared role (OF cannot witness inlet vs outlet)
    assert mr._delivered_user_role("patch", "inlet") == "inlet"
    assert mr._delivered_user_role("patch", "farfield") == "farfield"
    assert mr._delivered_user_role("patch", None) == "patch"


# finalize's delivered user-boundary set (interfaces excluded)

def _real_delivered(tmp_path, region_boundaries: dict, intake_patches: list) -> dict:
    for region, patches in region_boundaries.items():
        _write_boundary(tmp_path / "constant" / region / "polyMesh" / "boundary", patches)
        _write_region_mesh(tmp_path, region)
    return mr.delivered_user_boundary_types(tmp_path, intake_patches)


def test_region_interfaces_are_excluded_from_the_user_boundary_set(tmp_path):
    intake = [{"name": "wing", "type": "wall"}, {"name": "inlet", "type": "inlet"}]
    delivered = _real_delivered(tmp_path, {
        "air":   {"wing": ("wall", 100), "inlet": ("patch", 40), "air_to_metal": ("mappedWall", 20)},
        "metal": {"metal_to_air": ("mappedWall", 20)},
    }, intake)
    assert delivered == {"wing": "wall", "inlet": "inlet"}, \
        "interfaces (*_to_*) must not appear in the user contract"
    assert "air_to_metal" not in delivered and "metal_to_air" not in delivered


def test_the_real_derivation_uses_of_types_not_a_literal_patch(tmp_path):
    intake = [{"name": "wing", "type": "wall"}, {"name": "inlet", "type": "inlet"},
              {"name": "outlet", "type": "outlet"}]
    delivered = _real_delivered(tmp_path, {
        "air": {"wing": ("wall", 100), "inlet": ("patch", 40), "outlet": ("patch", 40)},
    }, intake)
    assert delivered == {"wing": "wall", "inlet": "inlet", "outlet": "outlet"}
    # a wall the mesh actually delivered as a generic patch reads as a NON-wall → contract catches it
    reroled = _real_delivered(tmp_path, {"air": {"wing": ("patch", 100)}},
                              [{"name": "wing", "type": "wall"}])
    assert reroled == {"wing": "patch"} and reroled["wing"] != "wall"


# the gate chain: patch_contract and region_contract are independent

def _manifest(tmp_path, patch_types: dict, regions: list | None = None):
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({
        "schema_version": "2.1", "patch_types": patch_types,
        "patches": {n: [] for n in patch_types},
        "quality": {"cells": 5000, "fatal": [],
                    "regions": [{"name": r["name"], "type": r["type"]} for r in (regions or [])]},
    }))


_INTAKE = [{"name": "wing", "type": "wall"}, {"name": "fuselage", "type": "wall"},
           {"name": "inlet", "type": "inlet"}]
_REGIONS = [{"name": "air", "type": "fluid"}, {"name": "metal", "type": "solid"}]


def test_correct_user_boundaries_plus_interface_pass(tmp_path):
    # finalize would have EXCLUDED the interface, so the manifest's user set is just the three
    _manifest(tmp_path, {"wing": "wall", "fuselage": "wall", "inlet": "inlet"}, _REGIONS)
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE,
                  engine_params={"_regions": _REGIONS})
    assert _gate_multiregion_patch_contract(ctx)[0] is True


def test_merged_walls_fail_patch_contract(tmp_path):
    _manifest(tmp_path, {"body": "wall", "inlet": "inlet"}, _REGIONS)
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE,
                  engine_params={"_regions": _REGIONS})
    ok, diag = _gate_multiregion_patch_contract(ctx)
    assert ok is False and "CONTRACT" in diag


def test_renamed_wall_fails_patch_contract(tmp_path):
    _manifest(tmp_path, {"leftwing": "wall", "fuselage": "wall", "inlet": "inlet"}, _REGIONS)
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE,
                  engine_params={"_regions": _REGIONS})
    assert _gate_multiregion_patch_contract(ctx)[0] is False


def test_a_missing_external_wall_is_not_satisfied_by_an_interface(tmp_path):
    _manifest(tmp_path, {"fuselage": "wall", "inlet": "inlet"}, _REGIONS)   # wing gone
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE,
                  engine_params={"_regions": _REGIONS})
    ok, diag = _gate_multiregion_patch_contract(ctx)
    assert ok is False and "wing" in diag


def test_wall_re_roled_to_farfield_is_caught_independently(tmp_path):
    # the mesh delivered 'wing' as a generic patch (farfield), not a wall - caught by OF type
    _manifest(tmp_path, {"wing": "farfield", "fuselage": "wall", "inlet": "inlet"}, _REGIONS)
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE,
                  engine_params={"_regions": _REGIONS})
    assert _gate_multiregion_patch_contract(ctx)[0] is False


def test_missing_manifest_fails_when_patches_were_declared(tmp_path):
    # no manifest written at all → not a skip, a failure (the user declared boundaries)
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE)
    assert _gate_multiregion_patch_contract(ctx)[0] is False


def test_the_two_contracts_are_independent():
    import tempfile
    from pathlib import Path

    # (a) correct user patches, WRONG region topology
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _manifest(ws, {"wing": "wall", "fuselage": "wall", "inlet": "inlet"},
                  [{"name": "air", "type": "fluid"}])   # 'metal' region missing
        ctx = GateCtx(workspace=ws, engine="snappy_multiregion", intake_patches=_INTAKE,
                      engine_params={"_regions": _REGIONS})
        assert _gate_multiregion_patch_contract(ctx)[0] is True, "user patches are correct"
        assert _gate_cht_region_contract(ctx)[0] is False, "region topology is wrong"

    # (b) WRONG user patches, correct region topology
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        _manifest(ws, {"body": "wall", "inlet": "inlet"}, _REGIONS)   # walls merged
        ctx = GateCtx(workspace=ws, engine="snappy_multiregion", intake_patches=_INTAKE,
                      engine_params={"_regions": _REGIONS})
        assert _gate_multiregion_patch_contract(ctx)[0] is False, "user patches are wrong"
        assert _gate_cht_region_contract(ctx)[0] is True, "region topology is correct"


def test_empty_intake_patches_cannot_be_erased_to_pass_a_declared_contract(tmp_path):
    _manifest(tmp_path, {"body": "wall"}, _REGIONS)
    # declared non-empty → merged delivery fails
    ctx = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=_INTAKE,
                  engine_params={"_regions": _REGIONS})
    assert _gate_multiregion_patch_contract(ctx)[0] is False
    # only a GENUINELY empty declaration is not-applicable
    ctx_empty = GateCtx(workspace=tmp_path, engine="snappy_multiregion", intake_patches=[])
    assert _gate_multiregion_patch_contract(ctx_empty)[0] is True
