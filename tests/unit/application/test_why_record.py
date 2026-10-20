# Responsibility: Verify every run writes a "why" record whose decisions each name a rule in force, and that replaying the recorded inputs through the rule functions gives the same decisions.
# Boundaries: contracts/decision_record (the rules, the replay), application/why_record (reading a final state), application/review_delivery (the delivery account cites its rule), application/run_reproducibility (the record carries the `why` section).
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from meshpipeline.application import review_delivery as rd
from meshpipeline.application import run_reproducibility, why_record
from meshpipeline.contracts import decision_record as dr

BOOK = Path(__file__).resolve().parents[3] / "docs" / "RULES.md"
_ROW = re.compile(r"^\|\s*([A-Z]+-[A-Za-z0-9-]+)\s*\|\s*in force\s*\|", re.I)
IN_FORCE = {m.group(1) for line in BOOK.read_text(encoding="utf-8").splitlines()
            if (m := _ROW.match(line))}


def _round_trip(why: dict) -> dict:
    """The record as it is stored and read back (run_reproducibility.json, the capture row)."""
    return json.loads(json.dumps(why, default=str))


def _cites_only_rules_in_force(why: dict) -> None:
    rules = dr.cited_rules(why["decisions"])
    assert rules and all(rules), f"a decision names no rule: {why['decisions']}"
    assert set(rules) <= IN_FORCE, f"rules not in force in docs/RULES.md: {set(rules) - IN_FORCE}"


# #
# ENG: the engine a run starts on
# #

@pytest.mark.parametrize("source,approved,recommended,own,rule", [
    ("user_direct", "gmsh", "snappy", [], "ENG-6"),
    ("dispute", "cfmesh", "snappy", [], "ENG-6"),
    ("suggested_confirmed", "snappy", "snappy", [], "ENG-4"),
    ("suggested_confirmed", "snapgrid", "", ["snapgrid"], "ENG-7"),
    ("suggested_confirmed", "cfmesh", "snappy", [], "ENG-9"),
    ("suggested_confirmed", "cfmesh", "", [], "ENG-9"),
    ("system", "vmtk", "vmtk", [], "ENG-4"),
    ("system", "snappy", "", [], "ENG-8"),
    ("system", "snappy", "cfmesh", [], "ENG-8"),
])
def test_the_engine_pick_names_its_rule(source, approved, recommended, own, rule):
    value, got = dr.engine_pick({"engine_source": source, "approved": approved, "measured": True,
                                 "recommended": recommended, "own_format": own})
    assert (value, got) == (approved, rule)
    assert dr.engine_pick_source(got) == ("user" if rule in ("ENG-6", "ENG-9") else "rule")


def test_a_ranking_without_a_measured_geometry_is_not_called_a_lab_record():
    base = {"approved": "snappy", "recommended": "snappy", "measured": False, "own_format": []}
    assert dr.engine_pick({**base, "engine_source": "system"}) == ("snappy", "ENG-8")
    assert dr.engine_pick({**base, "engine_source": "suggested_confirmed"}) == ("snappy", "ENG-9")


def test_a_pick_on_little_evidence_says_so(tmp_path):
    state = _final_state(tmp_path)
    state["engine_ladder"]["order_reasons"]["snappy"] = "little lab evidence on shapes like this"
    pick = why_record.build("job-why", state)["decisions"][0]
    assert pick["rule"] == "ENG-4" and "on little lab evidence" in pick["words"]


# #
# SW: an engine switch
# #

@pytest.mark.parametrize("why,rule,moved", [
    ("this engine's mesh of this shape came out broken", "SW-1", True),
    ("this engine could not build a mesh", "SW-2", True),
    ("the same failure repeated here", "SW-3", True),
    ("the shared attempts go to the untried engine", "SW-4", True),
    ("too little time left for a cfMesh run", "SW-13", False),
    ("no untried engine meshes 3 separate passages in one job", "SW-11", False),
    ("the engine is not ours to change (source user_direct)", "SW-8", False),
])
def test_a_switch_reason_maps_to_its_rule(why, rule, moved):
    kind = "engine" if rule in ("SW-1", "SW-2") else "fixable"
    value, got = dr.switch({"why": why, "from": "snappy", "to": "cfmesh", "kind": kind})
    assert got == rule
    assert value == ("cfmesh" if moved else "snappy")
    assert dr.switch_source(got) == ("guard-clock" if rule == "SW-13" else "rule")


def test_an_unknown_switch_reason_has_no_rule():
    assert dr.switch_rule("because the moon was full") == ""


@pytest.mark.parametrize("kind,to", [("never", "cfmesh"), ("review", "cfmesh"), ("engine", "snappy"),
                                     ("engine", "")])
def test_a_switch_whose_facts_do_not_fit_its_reason_has_no_rule(kind, to):
    entry = {"stage": "switch", "value": to, "rule": "SW-2",
             "inputs": {"why": "this engine could not build a mesh", "kind": kind,
                        "from": "snappy", "to": to}}
    assert dr.replay([entry]), "a move the rule would not make passed its replay"


# #
# DEL: the delivered attempt - every position of the fixed ranking
# #

def _c(attempt: int, **facts) -> rd.Candidate:
    base = {"max_non_ortho": 40.2, "max_skewness": 1.0, "layer_coverage_pct": 90.0}
    return rd.Candidate(attempt=attempt, engine="snappy", workspace=f"/ws/{attempt}", manifest={},
                        **{**base, **facts})


@pytest.mark.parametrize("winner,loser,rule", [
    (_c(2), _c(1, wrong_problem=1), "DEL-1"),
    (_c(2), _c(1, failed_checks=("max non-orthogonality",)), "DEL-2"),
    (_c(2, failed_checks=("a",)), _c(1, failed_checks=("a", "b")), "DEL-3"),
    (_c(2), _c(1, unmet=("no wake refinement behind the body",)), "DEL-4"),
    (_c(2, max_non_ortho=40.2), _c(1, max_non_ortho=45.0), "DEL-5"),
    (_c(2, max_non_ortho=40.3), _c(1, max_non_ortho=40.2, review_left_concerns=True), "DEL-6"),
    (_c(2, max_non_ortho=40.2), _c(1, max_non_ortho=40.3), "DEL-7"),
    (_c(2, max_skewness=0.8), _c(1, max_skewness=1.2), "DEL-8"),
    (_c(2, layer_coverage_pct=95.0), _c(1, layer_coverage_pct=80.0), "DEL-9"),
    (_c(1), _c(2), "DEL-10"),
    (_c(2, max_non_ortho=None, max_skewness=None), _c(1, max_non_ortho=None, max_skewness=None,
                                                       layer_coverage_pct=None), "DEL-9"),
])
def test_the_delivered_attempt_names_the_fact_that_decided_and_replays(winner, loser, rule):
    pool = [loser, winner]
    best = min(pool, key=rd.rank_key)
    assert best is winner
    account = rd._account({"retry_count": 2}, best, pool)
    assert account["rule"] == rule
    entry = why_record.delivered_entry({"engine_ladder": {"delivered_from": account}})
    why = _round_trip(dr.record([entry]))
    assert why["decisions"][0]["value"] == winner.attempt
    assert dr.replay(why["decisions"]) == []
    _cites_only_rules_in_force(why)


def test_a_single_valid_mesh_is_delivered_by_del_0():
    entry = why_record.delivered_entry({"executor_success": True, "executor_failed_gate": "",
                                        "retry_count": 1, "engine": "cfmesh"})
    assert entry is not None and entry.rule == "DEL-0" and entry.value == 1
    assert dr.replay([entry.as_dict()]) == []


def test_a_run_with_no_valid_mesh_records_no_delivery():
    assert why_record.delivered_entry({"executor_success": False}) is None


# #
# a whole run
# #

def _final_state(tmp_path: Path, *, source: str = "suggested_confirmed") -> dict:
    ws = tmp_path / "attempt_2"
    ws.mkdir()
    a1, a2 = _c(1, max_non_ortho=72.0), _c(2, max_non_ortho=55.0)
    account = rd._account({"retry_count": 2}, a2, [a1, a2])
    return {
        "job_id": "job-why", "engine": "cfmesh", "engine_source": source,
        "retry_count": 2, "executor_success": True, "executor_failed_gate": "",
        "openfoam_workspace": str(ws),
        "geometry": {"ref": {"original_filename": "duct.step"}},
        "engine_ladder": {
            "approved": "snappy", "source": source,
            "order": ["snappy", "cfmesh", "gmsh"], "order_source": "measured",
            "order_recommended": "snappy", "order_shape": "a plain duct",
            "order_table": "t1",
            "order_reasons": {"snappy": "passed 14 of 19", "cfmesh": "passed 9 of 15"},
            "attempts": [{"attempt": 1, "engine": "snappy", "kind": "engine",
                          "cause": "engine_crashed"}],
            "switches": [{"attempt": 2, "from": "snappy", "to": "cfmesh",
                          "because": "engine_crashed", "kind": "engine",
                          "reason": "it stopped before it finished the mesh",
                          "why": "this engine could not build a mesh"}],
            "delivered_from": account,
        },
    }


def test_a_run_records_why_for_every_decision_and_the_record_replays(tmp_path):
    why = _round_trip(why_record.build("job-why", _final_state(tmp_path)))
    assert why["rules_version"] == dr.RULES_VERSION
    stages = [(d["stage"], d["value"], d["rule"]) for d in why["decisions"]]
    assert stages == [("engine_pick", "snappy", "ENG-4"), ("switch", "cfmesh", "SW-2"),
                      ("delivered", 2, "DEL-5")]
    assert dr.replay(why["decisions"]) == []
    _cites_only_rules_in_force(why)
    assert why["sentences"][0].startswith("Engine snappy, rule ENG-4: it ranked first")
    assert "passed 14 of 19" in why["sentences"][0]
    assert why["decisions"][0]["also"] == ["ENG-1"]
    assert why["decisions"][1]["also"] == ["ENG-5"]


def test_a_tampered_record_fails_its_replay(tmp_path):
    why = _round_trip(why_record.build("job-why", _final_state(tmp_path)))
    why["decisions"][0]["rule"] = "ENG-8"
    why["decisions"][2]["inputs"]["candidates"][0]["rank"][4] = 1
    problems = dr.replay(why["decisions"])
    assert len(problems) == 2, problems


def test_the_users_own_engine_is_recorded_as_theirs(tmp_path):
    why = why_record.build("job-why", _final_state(tmp_path, source="user_direct"))
    pick = why["decisions"][0]
    assert (pick["rule"], pick["source"]) == ("ENG-6", "user")


def test_an_engine_that_reads_the_upload_itself_is_recorded_by_eng_7(tmp_path):
    state = _final_state(tmp_path)
    state["geometry"] = {"ref": {"original_filename": "board.ecxml"}}
    state["engine_ladder"] = {"approved": "snapgrid", "order": ["snapgrid", "snappy"],
                              "order_recommended": "snapgrid"}
    why = why_record.build("job-why", state)
    assert why["decisions"][0]["rule"] == "ENG-7"
    assert dr.replay(why["decisions"]) == []


def test_a_decision_cited_elsewhere_joins_the_record(tmp_path):
    dr.cite("job-cited", stage="plan", decided="max_cells", value=4_000_000, rule="ENG-4",
            inputs={"tier": "standard"})
    why = why_record.build("job-cited", _final_state(tmp_path))
    assert why["decisions"][-1]["stage"] == "plan"
    assert dr.replay(why["decisions"]) == []


def test_the_reproducibility_record_carries_the_why_section(tmp_path):
    rec = run_reproducibility.build("job-why", _final_state(tmp_path))
    assert rec["version"] == 2
    assert [d["rule"] for d in rec["why"]["decisions"]] == ["ENG-4", "SW-2", "DEL-5"]


def test_a_state_that_cannot_be_read_gives_an_empty_record_not_an_error():
    why = why_record.build("job-x", {"engine_ladder": "garbage", "geometry": 7})
    assert why["decisions"] == [] and why["rules_version"] == dr.RULES_VERSION
