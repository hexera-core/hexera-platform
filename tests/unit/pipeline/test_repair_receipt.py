# Responsibility: Verify the Repair receipt states what the repair kept, undid and REFUSED - each row traced to a recorded value, with no row invented where nothing was measured.
# Boundaries: the rows and the one publication; the repair itself is test_repair_attempt's and the lane's single line is test_repair_words'.
from __future__ import annotations

import asyncio

import pytest

import meshpipeline.events as E
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.contracts.narration import leaks
from meshpipeline.pipeline import repair_receipt as R

_REPORT = {"status": "repairable",
           "report": {"summary": "inspected",
                      "defects": [{"code": "wire_gap", "severity": "error", "count": 1}],
                      "entities": []}}


def _state(**over) -> dict:
    return {"job_id": "job-1", "repair_report": dict(_REPORT), **over}


def _op(name: str, code: str, *targets: str, status: str = "kept", reason: str = "") -> dict:
    return {"name": name, "status": status, "defect_code": code, "targets": list(targets),
            "mutated": status == "kept", "reason": reason, "basis": "kernel",
            "evidence": {}, "delta": {}}


def _out(**over) -> dict:
    record = {"attempted": True, "outcome": "repaired", "route": "conservative_repair",
              "operations": [], "targets": [], "refusals": [], "resolved": True,
              "stopped_because": "resolved"}
    record.update(over)
    return {"repair_attempt": record}


def _labels(rows) -> list[str]:
    return [r["label"] for r in rows]


def _find(rows, needle: str) -> dict:
    return next(r for r in rows if needle in r["label"])


# what it did


def test_a_kept_operation_becomes_a_row_with_its_count_and_the_defect_it_answered():
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("fix_face_boundary", "wire_gap", "edge:1", "edge:2"),
                    _op("fix_face_boundary", "wire_gap", "edge:7")],
        targets=[{"code": "wire_gap", "measurements": {"boundary_length": 2.4321}},
                 {"code": "wire_gap", "measurements": {"boundary_length": 0.5}}]))

    gaps = _find(rows, "gaps")
    assert gaps["label"] == "Closed gaps", "the label says what was done, without the count"
    assert gaps["value"] == "3", "the value is the count of targets, not of operations"
    assert "a gap where two edges did not meet" in gaps["why"]
    assert "2.43 mm" in gaps["why"], "the measured span travels with the row"


def test_an_operation_the_vocabulary_does_not_know_is_still_counted_never_named():
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("unfold_hyperspace", "wire_gap_v2", "face:3")]))
    row = _find(rows, "defect")
    assert row["label"] == "Fixed defect" and row["value"] == "1"
    assert all(not leaks(r["label"]) for r in rows)


def test_a_rolled_back_attempt_says_it_was_undone_and_why_in_plain_words():
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("fill_planar_hole", "small_face", "face:3",
                        status="rolled_back", reason="cap:area_change 0.41 > 0.02")],
        resolved=False, stopped_because="stalled"))
    row = _find(rows, "Tried to fix")
    assert row["value"] == "Rolled back"
    assert row["why"] == "the result moved the shape further than a repair is allowed to"
    assert not leaks(row["why"]), "the recorded reason is a code; it never reaches the user raw"


# what it refused - the line this receipt exists for


def test_the_paired_rim_refusal_survives_with_its_reason_intact():
    rows = R.repair_receipt_rows(_state(), _out(
        attempted=False, outcome="refused",
        refusals=[{"defect": "edge:12", "reason": "paired_rim",
                   "detail": "this loop pairs with another as the two ends of one passage"}]))
    row = _find(rows, "Did not fill an opening")
    assert row["value"] == "Refused"
    assert "seal a through-hole" in row["why"]
    assert not leaks(row["why"])


def test_identical_refusals_are_collapsed_rather_than_repeated():
    rows = R.repair_receipt_rows(_state(), _out(
        refusals=[{"defect": f"edge:{k}", "reason": "paired_rim"} for k in range(6)]))
    assert _labels(rows).count("Did not fill an opening") == 1


def test_a_refusal_whose_reason_is_unknown_still_says_it_refused():
    rows = R.repair_receipt_rows(_state(), _out(
        refusals=[{"defect": "edge:1", "reason": "something_new",
                   "detail": "the check could not be made on this shape"}]))
    row = _find(rows, "Refused a repair")
    assert row["value"] == "Refused"
    assert row["why"] == "the check could not be made on this shape"


def test_a_refusal_detail_that_would_leak_loses_the_detail_not_the_row():
    rows = R.repair_receipt_rows(_state(), _out(
        refusals=[{"defect": "edge:1", "reason": "something_new",
                   "detail": "snappyHexMesh refused /tmp/part_two.step"}]))
    row = _find(rows, "Refused a repair")
    assert row.get("why", "") == "", "a leaking reason is dropped; the refusal itself is not"


# the measured shape change


def test_the_shape_change_states_the_total_and_caps_only_the_unexplained_part():
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1")],
        measurements={"before": {"surface_area_mm2": 1000.0, "volume_mm3": 500.0, "solids": 1},
                      "after": {"surface_area_mm2": 1004.0, "volume_mm3": 500.0, "solids": 1}}))
    area = _find(rows, "Surface area")
    assert area["value"] == "0.4%", "the total change is still the headline number"
    # nothing was declared, so all of it is unexplained and the cap applies to all of it
    assert R._percent(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO) in area["why"]
    assert "not accounted for by the repairs" in area["why"]
    assert _find(rows, "Volume changed")["value"] == "under 0.01%"


def test_a_declared_change_is_not_reported_as_breaking_the_cap_it_passed():
    """THE REGRESSION THIS GUARDS. `caps._accounted` subtracts what the kept operations declared
    before judging, so a fill that declares 5% and adds 5% is fully accounted for and PASSES a 2%
    residual. Printing 5% beside "at most 2%" accused an approved repair of breaking its own
    limit."""
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("fill_planar_hole", "open_shell", "face:1")],
        measurements={"before": {"surface_area_mm2": 1000.0, "volume_mm3": 500.0, "solids": 1},
                      "after": {"surface_area_mm2": 1050.0, "volume_mm3": 500.0, "solids": 1},
                      "declared_area_mm2": 50.0}))
    area = _find(rows, "Surface area")
    assert area["value"] == "5%", "the part really did change by five percent; say so"
    assert "the repairs declared 5% of it" in area["why"]
    assert "under 0.01% of that is not accounted for" in area["why"], \
        "every bit of the change was declared, so nothing is unexplained"


@pytest.mark.parametrize("before_solids,after_solids", [(0, 0), (1, 2), (1, 0)])
def test_a_volume_the_guard_never_compared_is_not_presented_as_checked(before_solids, after_solids):
    """An open shell has no inside - OpenCASCADE returns a number for it that is not a volume -
    and a body that CLOSED is new volume no operation could have declared. `cumulative_refusal`
    skips both, so the receipt must not imply a limit was applied."""
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1")],
        measurements={"before": {"surface_area_mm2": 1000.0, "volume_mm3": 800.0,
                                 "solids": before_solids},
                      "after": {"surface_area_mm2": 1004.0, "volume_mm3": 1000.0,
                                "solids": after_solids}}))
    volume = _find(rows, "Volume changed")
    assert "not checked against a limit here" in volume["why"]
    assert R._percent(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO) not in volume["why"]


def test_an_operation_that_raised_is_not_reported_as_a_measured_rollback():
    """`session.py` marks an operation FAILED when it raised while running: there may be no
    after-measurement at all. Calling that "the result did not hold up when it was measured"
    reports a measurement nobody took, and dresses a tool fault as a verdict on the part."""
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1", status="failed",
                        reason="RuntimeError: /tmp/work/x.step kernel abort")]))
    row = _find(rows, "Tried to fix")
    assert row["value"] == "Could not run"
    assert "measured" not in row["why"]
    assert "stopped while it was working" in row["why"]
    # and the kernel's own words never reach the page
    assert not leaks(row["why"]) and "RuntimeError" not in row["why"] and "/tmp" not in row["why"]


def test_a_rollback_and_a_failure_are_not_collapsed_into_one_row():
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1", status="rolled_back",
                        reason="cap:area"),
                    _op("fix_small_edges", "open_shell", "face:2", status="failed")]))
    values = {r["value"] for r in rows if "Tried to fix" in r["label"]}
    assert values == {"Rolled back", "Could not run"}


def test_an_unmeasured_shape_change_omits_the_row_rather_than_guessing():
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1")]))
    assert not [r for r in rows if "Surface area" in r["label"] or "Volume" in r["label"]]

    half = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1")],
        measurements={"before": {"surface_area_mm2": 1000.0},
                      "after": {"surface_area_mm2": 1002.0}}))
    assert "Surface area changed" in _labels(half)
    assert "Volume changed" not in _labels(half), "volume was never measured, so it is not stated"


# whether the loop finished


@pytest.mark.parametrize("resolved,stopped,value,why", [
    (True, "resolved", "Yes", "every error the scan found was cleared"),
    (False, "op_budget", "No", "the repair reached its limit on operations"),
])
def test_the_receipt_says_whether_everything_was_fixed(resolved, stopped, value, why):
    rows = R.repair_receipt_rows(_state(), _out(
        operations=[_op("sew_faces", "open_shell", "face:1")],
        resolved=resolved, stopped_because=stopped))
    row = _find(rows, "Everything found was fixed")
    assert row["value"] == value and row["why"] == why


# nothing true to say


@pytest.mark.parametrize("state,out", [
    ({"job_id": "j"}, _out()),                                       # nothing was inspected
    (_state(), {"repair_attempt": {"attempted": False, "outcome": "no_report"}}),
    (_state(), {"repair_attempt": {"attempted": False, "outcome": "no_geometry"}}),
    (_state(), {"repair_attempt": {"attempted": False, "outcome": "off", "refusals": []}}),
    (_state(), {}),
])
def test_there_is_no_receipt_when_there_is_nothing_recorded_to_show(state, out):
    assert R.repair_receipt_rows(state, out) == []


# the caps, enforced by the event constructor


def test_the_event_drops_a_leaking_row_and_keeps_the_rest():
    event = E.receipt("repair", "Repair", [
        {"label": "Closed gaps", "value": "3"},
        {"label": "Sewed /tmp/part.step", "value": "1"},
        {"label": "Refused", "value": "1", "why": "snappyHexMesh would not take it"},
    ])
    assert [r["label"] for r in event.data["rows"]] == ["Closed gaps"]


def test_the_event_caps_the_rows_and_every_field():
    event = E.receipt("repair", "R" * 80,
                      [{"label": "L" * 90, "value": "V" * 90, "why": "W" * 400}] * 30)
    rows = event.data["rows"]
    assert len(rows) == E.MAX_RECEIPT_ROWS
    assert len(event.data["title"]) <= E.MAX_RECEIPT_TITLE + 16
    assert rows[0]["label"].startswith("L" * E.MAX_RECEIPT_LABEL)
    assert rows[0]["value"].startswith("V" * E.MAX_RECEIPT_VALUE)
    assert rows[0]["why"].startswith("W" * E.MAX_RECEIPT_WHY)


def test_a_value_carries_its_number_without_being_judged_as_prose():
    # `value` is exempt from the leak guard: "0.4%" and a bare count are measurements, not prose
    event = E.receipt("repair", "Repair", [{"label": "Volume changed", "value": "0.4%"}])
    assert event.data["rows"] == [{"label": "Volume changed", "value": "0.4%"}]


def test_an_empty_row_set_still_constructs_so_the_caller_decides():
    event = E.receipt("repair", "Repair", [])
    assert event.wire()["type"] == "receipt" and event.wire()["stage"] == "repair"
    assert event.wire()["rows"] == [] and event.wire()["title"] == "Repair"


# the publication


def test_the_receipt_is_published_once_on_the_repair_lane(monkeypatch):
    calls: list[tuple] = []

    class _Pub:
        async def areceipt(self, title, rows, op_id=""):
            calls.append((title, rows, op_id))

    import meshpipeline.application.execution_publisher as ep
    monkeypatch.setattr(ep, "execution_publisher", lambda job_id, agent=None: _Pub())

    rows = [{"label": "Closed gaps", "value": "3"}]
    asyncio.run(R.apublish("job-1", rows))
    assert calls == [("Repair", rows, "repair:receipt")]

    asyncio.run(R.apublish("job-1", []))
    assert len(calls) == 1, "an empty receipt is never sent"


def test_a_lost_claim_stops_the_node_while_any_other_failure_is_swallowed(monkeypatch):
    import meshpipeline.application.execution_publisher as ep
    from meshpipeline.contracts.event_stream import StaleExecutionPublish

    class _Stale:
        async def areceipt(self, title, rows, op_id=""):
            raise StaleExecutionPublish("superseded")

    class _Broken:
        async def areceipt(self, title, rows, op_id=""):
            raise RuntimeError("redis is away")

    rows = [{"label": "Closed gaps", "value": "3"}]
    monkeypatch.setattr(ep, "execution_publisher", lambda job_id, agent=None: _Stale())
    with pytest.raises(StaleExecutionPublish):
        asyncio.run(R.apublish("job-1", rows))

    monkeypatch.setattr(ep, "execution_publisher", lambda job_id, agent=None: _Broken())
    asyncio.run(R.apublish("job-1", rows))        # the user stream is never load-bearing
