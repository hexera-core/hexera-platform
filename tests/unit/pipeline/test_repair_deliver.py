# Responsibility: Verify a standalone repair job ENDS with what the customer gets - the repaired
#                 file and the goal's finishing verdict - and that an unrepairable part comes back
#                 saying so rather than quietly.
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from meshpipeline.pipeline.repair_deliver import (
    REPAIRED,
    UNREPAIRABLE,
    node_repair_deliver,
)


def _run(state) -> dict:
    return asyncio.run(node_repair_deliver(state))["repair_delivery"]


_VERDICT = {"profile": "standalone", "kernel_clean": True, "score": 1.0,
            "meshability": {"meshes": True}, "grind": {"tier": "small"}, "review": {}}


def _repaired_state(tmp_path, **over) -> dict:
    path = tmp_path / "part-repaired.step"
    path.write_text("ISO-10303-21;")
    attempt = {"attempted": True, "resolved": True, "stopped_because": "resolved",
               "repaired_path": str(path), "measurements": {"goal": _VERDICT},
               "summary": "Repaired within the conservative caps."}
    attempt.update(over.pop("attempt", {}))
    state = {"job_id": "job-1", "repair_goal": {"goal": "standalone"},
             "repair_attempt": attempt}
    state.update(over)
    return state


def test_a_repaired_part_comes_back_with_the_goals_verdict(tmp_path):
    out = _run(_repaired_state(tmp_path))
    assert out["outcome"] == REPAIRED
    assert out["goal"] == _VERDICT
    assert out["path"].endswith("part-repaired.step") and out["suffix"] == ".step"


def test_the_verdict_is_the_one_the_repair_made_not_a_recomputed_one(tmp_path):
    # the finishing verdict is `cad/repair/goal.finishing_verdict`'s; this node reports it and
    # must not invent a score of its own
    verdict = {"profile": "standalone", "error": "RuntimeError"}
    out = _run(_repaired_state(tmp_path, attempt={"measurements": {"goal": verdict}}))
    assert out["goal"] == verdict


def test_an_unrepairable_part_comes_back_saying_so(tmp_path):
    out = _run({"job_id": "job-1", "repair_goal": {"goal": "standalone"},
                "repair_attempt": {"attempted": False, "outcome": "triage",
                                   "reason": "triage routed this to operator_repair"}})
    assert out["outcome"] == UNREPAIRABLE
    assert out["path"] == ""
    assert "triage routed this to operator_repair" in out["why"]


def test_a_repair_that_did_not_resolve_is_not_reported_as_repaired(tmp_path):
    state = _repaired_state(tmp_path, attempt={"resolved": False,
                                               "stopped_because": "out_of_operations"})
    out = _run(state)
    assert out["outcome"] == UNREPAIRABLE
    # the file it DID produce still travels: a partial repair is worth handing back, labelled
    assert out["path"].endswith("part-repaired.step")
    assert "out_of_operations" in out["why"]


def test_a_repaired_file_that_is_gone_is_not_claimed_as_a_deliverable(tmp_path):
    state = _repaired_state(tmp_path)
    Path(state["repair_attempt"]["repaired_path"]).unlink()
    out = _run(state)
    assert out["path"] == ""


@pytest.mark.parametrize("state", [
    {},
    {"repair_attempt": None},
    {"repair_attempt": "wat"},
    {"repair_attempt": {"measurements": "wat", "repaired_path": 7}},
])
def test_the_ending_never_raises(state):
    out = _run(state)
    assert out["outcome"] == UNREPAIRABLE and out["path"] == ""


def test_the_node_delivers_nothing_itself():
    # THE FENCE IS THE ORCHESTRATOR'S. A node that uploaded bytes or wrote an artifact row would
    # do it outside this worker's fence, which is why the terminal path performs what this names.
    src = inspect.getsource(node_repair_deliver)
    for forbidden in ("deliver_repaired_cad", "deliver_repair_report", "AsyncSessionLocal",
                      "get_object_store"):
        assert forbidden not in src, f"the ending node reached for {forbidden}"


def test_the_orchestrator_hands_back_what_the_ending_named():
    import meshpipeline.application.pipeline_run as wt
    body = Path(inspect.getfile(wt)).read_text()
    block = body[body.index("deliver_repair_report("):]
    block = block[:block.index("if final_status == JobStatus.succeeded:")]
    # a standalone run promotes nothing, so `repair_lineage` is empty on it: the repaired bytes
    # reach the customer through the ending the deliver node wrote, or not at all
    assert 'final_state.get("repair_delivery")' in block
    assert "deliver_repaired_cad(" in block
