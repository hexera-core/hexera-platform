# Responsibility: Verify PLAN-fea-36 reading A on the FEA solid route - "finer" with no number at a place means finer than the mesh AROUND it (half the size round it); a place the accuracy rules already make that fine keeps the rules' sizes, by this rule and by the AI (AI-fea-1's refine there is not applied); a typed size still wins; nothing typed changes nothing; and each outcome is told.
# Boundaries: the filleted bracket built in the test (tests/fea_fixtures.py: a 4 mm inside fillet), measured and planned in-process; no model, no mesher.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_ai as FA  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_part_sizes as PS  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402
from meshpipeline.engines import fea_plan_options as PO  # noqa: E402
from meshpipeline.engines import fea_requirements as FR  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402

#: the AI's answer on the cast knuckle (25 Nov): the fillets refined for the user's word alone
REFINE = {"status": "used", "applied": [],
          "decision": {"analysis": "static", "zones": {"H1": {"priority": "refine",
                                                              "reason": "User requested finer fillets."}},
                       "features": {}}}


@pytest.fixture(scope="module")
def bracket(tmp_path_factory):
    import gmsh

    ws = tmp_path_factory.mktemp("bracket")
    src = fx.write_step(fx.bracket(4.0), ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    record, surface, diag = FD.measure_staged(ws)
    return record, surface, diag


def _global(mm: float) -> dict:
    req = MR.empty()
    req["sizes"]["global_mm"] = mm
    return MR.section("used", requirements=req)


def _fillets(record: dict, entry: dict | None = None) -> dict:
    spot = next(h for h in record.get("hot_spots") or [] if h.get("type") == "fillet")
    return {"status": "used", "features": {"Feature_1": list(spot["faces"])},
            "parts": [{"id": "Feature_1", "feature": True, "name": "the fillets"}],
            "entries": [entry or {"ask": "refine", "parts": ["Feature_1"], "quote": "finer at the fillets"}]}


def _plan(record, surface, diag, asked, parts, ai=None):
    sizes, scaled, _controls = FD.requirement_sizes(record, asked, parts, diag)
    rec = {**record, FA.SETTLED_KEY: PS.settled(scaled)} if PS.finer_outcomes(scaled) else record
    return FD.choose_plan(rec, surface, diag, sizes=sizes or None, ai=ai), PS.finer_outcomes(scaled)


def _zone(plan: dict, zid: str) -> float:
    return next(float(z["size_m"]) for z in plan["zones"] if z["id"] == zid)


def test_a_place_the_rules_already_make_finer_than_half_the_mesh_round_it_keeps_its_size(bracket):
    """The knuckle's case: "3 mm elements overall" and "finer at the fillets", the fillet at 1.05 mm by its rules
    (6 per quarter-turn of 4 mm) - under half of 3 mm. Before: halved to 0.52 mm and an area zone, 36,157 elements
    planned against the rules' 9,635. Now nothing is refined, and the user is told."""
    record, surface, diag = bracket
    rules = FD.choose_plan(record, surface, diag)
    plan, finer = _plan(record, surface, diag, _global(3.0), _fillets(record))
    assert _zone(plan, "H1") == _zone(rules, "H1")                     # the rules' own 1.05 mm, not refined
    assert not [z for z in plan["zones"] if z["kind"] == "area"]
    est, est_rules = plan["estimate"]["elements"], rules["estimate"]["elements"]
    assert abs(est - est_rules) <= 0.05 * est_rules                     # within a few percent of the rules' plan
    line = ("You asked for finer elements at the fillets: the mesh there is already about 1 mm, finer than half of "
            "your 3 mm, so it keeps that size. Give a size if you want it finer still.")
    assert [(r["changed"], r["there_mm"], r["target_mm"], r["words"]) for r in finer] == [(False, 1.047, 1.5, line)]
    assert line in plan["words"]
    # the plan step's chat reads the same outcome off requirements_cost
    got = PO.requirements_cost(record, surface, diag, before=rules, asked=_global(3.0), part_sizes=_fillets(record))
    assert got["finer"] == finer and got["after"]["elements"] == est


def test_a_place_the_rules_left_coarse_is_made_half_the_mesh_round_it(bracket):
    """At "1 mm elements overall" the fillet's rules (1.05 mm) are coarser than half of 1 mm: it is brought to 0.5 mm,
    and the area gets a zone of its own at that size."""
    record, surface, diag = bracket
    plan, finer = _plan(record, surface, diag, _global(1.0), _fillets(record))
    alone, _ = _plan(record, surface, diag, _global(1.0), None)
    assert _zone(alone, "H1") == 0.001 and _zone(plan, "H1") == 0.0005
    assert [z["size_m"] for z in plan["zones"] if z["kind"] == "area"] == [0.0005]
    assert [(r["changed"], r["target_mm"]) for r in finer] == [(True, 0.5)]
    assert finer[0]["words"] == ("You asked for finer elements at the fillets: the mesh there is now about 0.5 mm, half "
                                 "of your 1 mm; what was already finer there keeps its size.")


def test_a_typed_size_at_the_place_still_wins(bracket):
    record, surface, diag = bracket
    typed = {"ask": "size", "parts": ["Feature_1"], "min_mm": 0.2, "max_mm": 0.2, "quote": "0.2 mm at the fillets"}
    plan, finer = _plan(record, surface, diag, _global(3.0), _fillets(record, typed))
    assert _zone(plan, "H1") == 0.0002 and finer == []


def test_the_ais_refine_from_the_users_finer_is_not_applied_on_a_place_already_fine(bracket):
    record, surface, diag = bracket
    plan, _finer = _plan(record, surface, diag, _global(3.0), _fillets(record), ai=REFINE)
    rules = FD.choose_plan(record, surface, diag)
    assert _zone(plan, "H1") == _zone(rules, "H1")                     # not 10 per quarter-turn
    row = next(a for a in plan["ai"]["applied"] if a["id"] == "H1")
    assert row["done"] is False and row["settled_by"] == "PLAN-fea-36" and row["reason"] == "User requested finer fillets."
    assert any("were not applied: the rules size those places" in w for w in plan["words"])
    # the same answer without the user's finer there is applied as before (AI-fea-1)
    free = FD.choose_plan(record, surface, diag, ai=REFINE)
    assert _zone(free, "H1") < _zone(rules, "H1")


def test_nothing_typed_is_the_same_plan_to_the_byte(bracket):
    record, surface, diag = bracket
    sizes, scaled, controls = FD.requirement_sizes(record, MR.section("none"), None, diag)
    assert (sizes, scaled, controls) == ({}, {}, None) and PS.finer_outcomes(scaled) == []
    assert FA.SETTLED_KEY not in record
    a = FD.choose_plan(record, surface, diag, sizes=sizes or None)
    b = FD.choose_plan(record, surface, diag)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    # and the AI is shown what it always was without notes
    assert FA.SETTLED_FACT not in json.dumps(FA.brief(record, {}, P.zones(record), diag_m=diag, limit=1_000_000))


def test_the_asked_against_built_row_says_what_was_done():
    req = {"rows": [{"id": "r1", "field": "local", "asked": "finer at the fillets"}],
           "local": [{"id": "r1", "where": "the fillets", "what": "finer", "quote": "finer at the fillets"}]}
    kept = {"id": "Feature_1", "quote": "finer at the fillets", "faces": ["Face_3"], "target_mm": 1.5,
            "there_mm": 0.47, "there_low_mm": 0.47, "around_mm": 3.0, "yours": True, "changed": False}
    mesh = SimpleNamespace(faces={}, node_ids=[], xyz=[])
    got = FR._finer_row(req["rows"][0], req, [kept], mesh, face_tags=None, mm_per_unit=1000.0)
    assert got["delivered"] == ("kept at the 0.47 mm the accuracy rules give there, already finer than half of your "
                                "3 mm; give a size to go finer")
    changed = {**kept, "changed": True, "there_mm": 3.0}
    got = FR._finer_row(req["rows"][0], req, [changed], mesh, face_tags=None, mm_per_unit=1000.0)
    assert got["delivered"] == "made about 1.5 mm there, half of your 3 mm"
