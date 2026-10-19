# Responsibility: Verify a delivered mesh whose CAD fidelity measures missed a bar is delivered with the miss said in plain words, that every fidelity number - and the size note's reading - lands in the result record, and that a clean or unvalidated mesh carries no note.
# Boundaries: final_result's predicate, record and words; the fidelity record is a plain dict.
from __future__ import annotations

from meshpipeline.application.final_result import (
    CAD_FIDELITY,
    FinalResult,
    RunOutcome,
    TerminalStatus,
    cad_fidelity_caveat,
    cad_fidelity_record,
    render_message,
)

_CONCERN = {"version": 1, "flow": "internal", "verdict": "concern",
            "verdicts": {"mesh_to_cad": "pass", "cad_to_mesh": "concern", "volume": "pass"},
            "surface": {"mesh_to_cad": {"wall_off_share": 0.0, "p95_cells": 0.1, "max_cells": 0.4},
                        "cad_to_mesh": {"cad_missed_share": 0.023, "p95_cells": 0.3}},
            "volume": {"what": "fluid", "cad_m3": 1.0, "mesh_m3": 0.99, "error": -0.01}}
_CLEAN = {**_CONCERN, "verdict": "pass",
          "verdicts": {"mesh_to_cad": "pass", "cad_to_mesh": "pass", "volume": "pass"}}


def _outcome(rec, **kw):
    return RunOutcome(executor_success=True, flow_topology="internal",
                      quality={"cad_fidelity": rec}, body_size_m=(0.4, 0.1, 0.1), **kw)


def test_a_concern_is_said_beside_the_delivered_mesh():
    c = cad_fidelity_caveat(_outcome(_CONCERN))
    assert c is not None and c["kind"] == CAD_FIDELITY
    assert c["verdicts"] == {"cad_to_mesh": "concern"}
    assert any("2.3% of your CAD surface is not reached" in i for i in c["items"])
    fr = FinalResult(schema_version=5, job_id="j", owner_id="o", status=TerminalStatus.succeeded,
                     executor_success=True, requirement_caveats=[c])
    text = render_message(fr)
    assert "how closely it follows your CAD" in text and "2.3% of your CAD surface" in text


def test_a_clean_or_unvalidated_mesh_carries_no_note():
    assert cad_fidelity_caveat(_outcome(_CLEAN)) is None
    assert cad_fidelity_caveat(RunOutcome(executor_success=False,
                                          quality={"cad_fidelity": _CONCERN})) is None
    assert cad_fidelity_caveat(RunOutcome(executor_success=True, failed_gate="x",
                                          quality={"cad_fidelity": _CONCERN})) is None
    assert cad_fidelity_caveat(RunOutcome(executor_success=True, quality={})) is None


def test_every_number_and_the_size_reading_land_in_the_result_record():
    rec = cad_fidelity_record(_outcome(_CLEAN))
    assert rec["volume"]["error"] == -0.01 and rec["surface"]["cad_to_mesh"]["cad_missed_share"]
    assert rec["size"] == {"longest_m": 0.4, "verdict": "pass"}
    size = {"what": "a car", "too": "small", "other_unit": "m", "other_longest_m": 4.7}
    assert cad_fidelity_record(_outcome(_CLEAN), size_caveat=size)["size"]["verdict"] == "concern"
    assert cad_fidelity_record(RunOutcome(executor_success=False)) == {}
    fr = FinalResult(schema_version=5, job_id="j", owner_id="o", status=TerminalStatus.succeeded,
                     executor_success=True, cad_fidelity=rec)
    assert FinalResult.from_dict(fr.to_dict()).cad_fidelity == rec


def test_a_size_no_rule_could_judge_is_unmeasured_not_passed():
    # a structural part the request names nothing for: no flow, no word - nothing to judge it by
    oc = RunOutcome(executor_success=True, flow_topology="", quality={"cad_fidelity": _CLEAN},
                    body_size_m=(0.4, 0.1, 0.1), words="")
    assert cad_fidelity_record(oc)["size"]["verdict"] == "unmeasured"
