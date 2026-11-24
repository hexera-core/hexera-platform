"""INS-40 and VER-9: a separate passage no inlet opens into is not meshed and is told before meshing, while
another passage has an inlet and an outlet; and separate passages meshed side by side are not a fault in
the verdict - the regions limit is the passages the plan meshed."""
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.cad.fluid_passages import (
    NO_INLET_WAY_OUT,
    PASSAGES_FILE,
    PASSAGES_READ_FILE,
    openings_not_meshed,
    passages_side_by_side,
    plan_passages,
    plan_staged,
    read_passages,
)
from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary
from meshpipeline.engines.verdict_limits import inputs_for, words

_DECL = [{"name": "inlet", "type": "inlet"}, {"name": "outlet_1", "type": "outlet"},
         {"name": "outlet_2", "type": "outlet"}, {"name": "outlet_3", "type": "outlet"}]


def _box2(lo, hi):
    """A closed box as (wall, lid a, lid b): two of its faces are openings."""
    t = _box_triangles(lo, hi)
    return t[:-4], t[-4:-2], t[-2:]


def _write(tmp_path: Path, groups: dict[str, list]) -> dict[str, str]:
    out = {}
    for name, tris in groups.items():
        p = tmp_path / f"{name}.stl"
        write_stl_binary(p, tris)
        out[name] = str(p)
    return out


def _two_cavities(tmp_path: Path) -> dict[str, str]:
    """Cavity A holds the inlet and outlet_1; cavity B, apart from it, holds outlet_2 and outlet_3 only."""
    wa, ia, oa = _box2((0, 0, 0), (1, 1, 4))
    wb, ob2, ob3 = _box2((3, 0, 0), (4, 1, 2))
    return _write(tmp_path, {"wall": wa + wb, "inlet": ia, "outlet_1": oa, "outlet_2": ob2, "outlet_3": ob3})


def test_a_cavity_with_no_inlet_is_not_meshed_and_its_openings_are_told(tmp_path):
    plan = plan_passages(read_passages(_two_cavities(tmp_path)), inlet_names=("inlet",))
    assert [sorted(p.ports) for p in plan.meshed] == [["inlet", "outlet_1"]]
    assert [sorted(p.ports) for p in plan.no_inlet] == [["outlet_2", "outlet_3"]]
    assert not plan.several and plan.refusal() == ""
    told = plan.not_meshed()
    assert sorted(told) == ["outlet_2", "outlet_3"]
    assert "no inlet opens into" in told["outlet_2"] and "was not meshed" in told["outlet_2"]
    assert NO_INLET_WAY_OUT in told["outlet_3"]
    assert any("outlet_2, outlet_3 sit in a separate cavity" in n for n in plan.notes())


def test_no_passage_with_an_inlet_and_an_outlet_leaves_the_plan_as_it_was(tmp_path):
    wa, ia, _ = _box2((0, 0, 0), (1, 1, 4))
    wb, ob2, ob3 = _box2((3, 0, 0), (4, 1, 2))
    srcs = _write(tmp_path, {"wall": wa + wb, "inlet": ia, "outlet_2": ob2, "outlet_3": ob3})
    plan = plan_passages(read_passages(srcs), inlet_names=("inlet",))
    assert len(plan.meshed) == 2 and not plan.no_inlet and plan.not_meshed() == {}


def test_without_declared_inlets_nothing_is_left_unmeshed(tmp_path):
    plan = plan_passages(read_passages(_two_cavities(tmp_path)))
    assert len(plan.meshed) == 2 and not plan.no_inlet


def test_the_staging_records_the_told_openings_for_the_gates_and_the_verdict(tmp_path):
    srcs = _two_cavities(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    plan_staged(ws, srcs, "wall", _DECL)
    rec = json.loads((ws / PASSAGES_READ_FILE).read_text())
    assert sorted(rec["not_meshed"]) == ["outlet_2", "outlet_3"] and len(rec["passages"]) == 1
    assert sorted(openings_not_meshed(ws)) == ["outlet_2", "outlet_3"]
    assert openings_not_meshed(tmp_path / "nothing_here") == {}


def test_the_cfmesh_staging_meshes_only_the_passage_with_the_inlet(tmp_path):
    from meshpipeline.engines.cfmesh.cfmesh_runner import _stage_passages
    srcs = _two_cavities(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    out = _stage_passages(ws, srcs, "wall", _DECL)
    assert sorted(out["srcs"]) == ["inlet", "outlet_1", "wall"]
    assert not (ws / PASSAGES_FILE).exists() and passages_side_by_side(ws) == 1
    assert any("was not meshed" in n for n in out["notes"])


def _row(inputs: dict, key: str) -> dict:
    return next(r for r in inputs["limits"] if r["key"] == key)


def _verdict_inputs(quality: dict) -> dict:
    return inputs_for(engine="cfmesh", topology="internal", manifest={"quality": quality}, valid=True)


def test_regions_equal_to_the_passages_meshed_side_by_side_are_met_and_said():
    from meshpipeline.contracts.decision_record import limit_met
    row = _row(_verdict_inputs({"regions": 2, "passages_side_by_side": 2}), "regions")
    assert row["limit"] == 2 and limit_met(row["measured"], row["op"], row["limit"]) is True
    assert "2 separate flow volumes" in row["said"]


def test_more_regions_than_passages_is_still_a_miss_and_one_passage_keeps_the_limit_of_one():
    from meshpipeline.contracts.decision_record import limit_met
    row = _row(_verdict_inputs({"regions": 3, "passages_side_by_side": 2}), "regions")
    assert row["limit"] == 2 and limit_met(row["measured"], row["op"], row["limit"]) is False
    assert "said" not in row and "2 separate passages" in words(row)
    one = _row(_verdict_inputs({"regions": 2}), "regions")
    assert one["limit"] == 1 and limit_met(one["measured"], one["op"], one["limit"]) is False


def test_openings_left_unmeshed_are_a_told_concern_never_silently_met():
    from meshpipeline.contracts.decision_record import OUTCOME_CONCERNS, verdict
    said = "outlet_2, outlet_3 sit in a separate cavity that no inlet opens into"
    inputs = _verdict_inputs({"regions": 1, "openings_not_meshed": ["outlet_2", "outlet_3"],
                              "openings_not_meshed_words": [said]})
    row = _row(inputs, "openings_not_meshed")
    assert row["measured"] == ["outlet_2", "outlet_3"] and words(row).startswith("Outlet_2, outlet_3 sit")
    assert verdict(inputs)[0] == OUTCOME_CONCERNS
    assert not [r for r in _verdict_inputs({"regions": 1})["limits"] if r["key"] == "openings_not_meshed"]


def test_the_launch_check_holds_the_case_to_the_openings_it_was_staged_for(tmp_path, monkeypatch):
    from meshpipeline.engines import case_contract, workspace_facts
    from meshpipeline.engines.case_contract import CaseBoundary
    monkeypatch.setattr(workspace_facts, "contract_patches", lambda ws: [
        {"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
        {"name": "outlet_1", "type": "outlet"}, {"name": "outlet_2", "type": "outlet"}])
    monkeypatch.setattr(case_contract, "read_case_boundary", lambda ws, engine: CaseBoundary(
        patches={"wall": "wall", "inlet": "inlet", "outlet_1": "outlet"}, authored_by="test", kind="roles"))
    problems, _ = case_contract.check(tmp_path, "cfmesh")
    assert any("'outlet_2'" in p for p in problems)            # not told: still a mismatch
    (tmp_path / PASSAGES_READ_FILE).write_text(json.dumps(
        {"read": True, "not_meshed": {"outlet_2": "outlet_2 sits in a separate cavity"}}))
    assert case_contract.check(tmp_path, "cfmesh")[0] == []


def test_the_cad_check_holds_the_mesh_to_the_passage_meshed_when_a_cavity_was_told(tmp_path):
    from meshpipeline.engines.cad_fidelity import cad_reference
    from meshpipeline.engines.cfmesh.cfmesh_runner import _stage_passages
    srcs = _two_cavities(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    write_stl_binary(ws / "fluid_boundary.stl", list(_box_triangles((0, 0, 0), (1, 1, 4)))
                     + list(_box_triangles((3, 0, 0), (4, 1, 2))))
    assert cad_reference(ws, "internal")[1] == "staged fluid boundary"      # nothing told: the whole boundary
    _stage_passages(ws, srcs, "wall", _DECL)
    _, source, fluid = cad_reference(ws, "internal")
    assert source == "staged fluid boundary, the passage meshed" and fluid
    # cfMesh reads p0 into its geom.stl and removes p0: geom.stl is then the passage meshed
    import shutil
    shutil.rmtree(ws / "_passages" / "p0")
    write_stl_binary(ws / "geom.stl", list(_box_triangles((0, 0, 0), (1, 1, 4))))
    assert cad_reference(ws, "internal")[1] == "staged fluid boundary, the passage meshed"


def test_the_manifest_drops_the_told_openings_and_records_them_and_the_passages(tmp_path):
    from meshpipeline.engines.manifest import write_manifest
    (tmp_path / PASSAGES_READ_FILE).write_text(json.dumps(
        {"read": True, "not_meshed": {"outlet_2": "outlet_2 sits in a separate cavity"}}))
    (tmp_path / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"]}))
    m = write_manifest(tmp_path, patch_types={"wall": "wall", "inlet": "inlet", "outlet_1": "outlet",
                                              "outlet_2": "outlet"},
                       patch_entities={}, bbox=(0, 0, 0, 1, 1, 1), quality={"cells": 10}, mesh_units="m")
    assert "outlet_2" not in m["patch_types"] and "outlet_2" not in m["validation"]["patch_validation"]
    assert m["quality"]["openings_not_meshed"] == ["outlet_2"]
    assert m["quality"]["passages_side_by_side"] == 2
