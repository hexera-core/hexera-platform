# Responsibility: Verify the Repair receipt against records the REAL producer returned - that the
#                 shape `pipeline/repair_attempt.py` forwards is the shape
#                 `pipeline/repair_receipt.py` reads, on the repaired path and on the refused one.
# Boundaries: the seam between those two modules, and the publisher double that carries the
#             result. The rows' own wording is test_repair_receipt's; the repair is
#             test_repair_attempt's.
#
# WHY THIS FILE EXISTS. Every test in test_repair_receipt.py hand-builds `{"repair_attempt": {...}}`
# in the shape `cad/repair/conservative.py` produces, not the shape `repair_attempt.py` FORWARDS -
# and the two had quietly diverged. `repair_attempt` whitelisted `before`/`after` out of the loop's
# measurements, so `declared_area_mm2` never reached the receipt, the declared-change branch was
# dead in production, and a customer whose approved repair declared 5% and added 5% was told it had
# broken a 2% limit. Hand-built dicts cannot catch that. These drive the producer.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import RepairRefused
from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport
from meshpipeline.pipeline import repair_receipt as R
from meshpipeline.pipeline.repair_attempt import _attempt


def _state(tmp_path, **over) -> dict:
    report = {"status": "repairable",
              "report": {"summary": "inspected",
                         "defects": [{"code": "open_shell", "severity": "error"}],
                         "entities": [
                             {"code": "open_shell", "severity": "error", "entity": "face:small",
                              "region": "unknown", "message": "an opening",
                              "measurements": {"boundary_length": 2.0}, "location": {}},
                             {"code": "open_shell", "severity": "error", "entity": "face:rim",
                              "region": "unknown", "message": "an opening",
                              "measurements": {"boundary_length": 40.0}, "location": {}}]}}
    state = {"job_id": "job-1", "engine": "snappy",
             "geometry": geometry_state(tmp_path),
             "repair_status": "repairable",
             "repair_report": report}
    state.update(over)
    return state


def _op(name: str, code: str, *targets: str, status: str = "kept", reason: str = "") -> dict:
    return {"name": name, "status": status, "defect_code": code, "targets": list(targets),
            "mutated": status == "kept", "reason": reason, "basis": "kernel",
            "evidence": {}, "delta": {}}


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (True, ""))


def _loop(monkeypatch, **measured):
    """Make the bounded repair return a report carrying exactly `measured`, in the shape
    `cad/repair/conservative.repair_step_file` builds it: a flat name/value measurement list."""
    operations = measured.pop("operations", ())

    def _repair(source, destination, **kw):
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return RepairReport(
            defects=(),
            measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measured.items()),
            operations=tuple(operations), summary="Repaired within the conservative caps.",
            entities=())

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)


def _refuses(monkeypatch, message: str, **measured):
    def _refuse(source, destination, **kw):
        raise RepairRefused(message, measurements=dict(measured))

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)


def _rows(state) -> list[dict]:
    out = asyncio.run(_attempt(state))
    return R.repair_receipt_rows(state, out)


def _find(rows, needle: str) -> dict:
    return next(r for r in rows if needle in r["label"])


# THE CONTRACT: what the loop measured is what the receipt can read


def test_a_declared_change_reaches_the_receipt_through_the_real_producer(tmp_path, monkeypatch):
    """THE REGRESSION THIS GUARDS, end to end across the seam. `caps._accounted` subtracts what
    the kept operations declared before judging, so a fill that declares 5% and adds 5% passes a
    2% residual. The node used to drop `declared_area_mm2` on the way out, so the receipt had
    nothing to subtract and accused an approved repair of breaking its own limit."""
    _loop(monkeypatch,
          operations=[_op("fill_planar_hole", "open_shell", "face:small")],
          before={"surface_area_mm2": 1000.0, "volume_mm3": 500.0, "solids": 1},
          after={"surface_area_mm2": 1050.0, "volume_mm3": 500.0, "solids": 1},
          declared_area_mm2=50.0, declared_volume_mm3=0.0,
          stopped_because="resolved", resolved=True, complete_scan=True, planner="rules-v1",
          refusals=[], operations_attempted=1, operations_kept=1, tolerance_requested_mm=0.01)

    state = _state(tmp_path)
    out = asyncio.run(_attempt(state))

    forwarded = out["repair_attempt"]["measurements"]
    assert forwarded["declared_area_mm2"] == 50.0, \
        "the receipt cannot separate accounted-for change from unexplained change without this"

    area = _find(R.repair_receipt_rows(state, out), "Surface area")
    assert area["value"] == "5%", "the part really did change by five percent; say so"
    assert "the repairs declared 5% of it" in area["why"]
    assert "under 0.01% of that is not accounted for" in area["why"]
    assert R._percent(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO) in area["why"]


def test_a_key_the_loop_grows_later_is_forwarded_rather_than_silently_dropped(tmp_path,
                                                                             monkeypatch):
    """THE STRUCTURAL FIX. `measurements` is a DENYLIST now: the whitelist it replaced dropped
    every key `conservative.py` added after it was written, which is how the declared-change fix
    came to be dead code in production the day it shipped."""
    _loop(monkeypatch,
          operations=[_op("fill_planar_hole", "open_shell", "face:small")],
          before={"surface_area_mm2": 1000.0}, after={"surface_area_mm2": 1001.0},
          declared_area_mm2=0.0, stopped_because="resolved", resolved=True,
          a_measurement_invented_tomorrow_mm=7.5)

    forwarded = asyncio.run(_attempt(_state(tmp_path)))["repair_attempt"]["measurements"]
    assert forwarded["a_measurement_invented_tomorrow_mm"] == 7.5


def test_what_the_record_states_itself_is_not_also_stored_inside_measurements(tmp_path,
                                                                             monkeypatch):
    """The denylist's other half: a fact the record carries as a field of its own is not copied
    into `measurements` as well, where the two copies could disagree."""
    _loop(monkeypatch,
          operations=[_op("fill_planar_hole", "open_shell", "face:small")],
          before={"surface_area_mm2": 1000.0}, after={"surface_area_mm2": 1001.0},
          stopped_because="resolved", resolved=True,
          refusals=[{"defect": "face:rim", "reason": "paired_rim"}])

    record = asyncio.run(_attempt(_state(tmp_path)))["repair_attempt"]
    assert record["refusals"] and record["stopped_because"] == "resolved"
    for owned in ("refusals", "operations", "resolved", "stopped_because"):
        assert owned not in record["measurements"]


def test_a_kept_row_from_a_real_record_speaks_only_for_what_it_fixed(tmp_path, monkeypatch):
    """The whole receipt in one part: a 2 mm opening filled and kept, a 40 mm paired-rim opening
    of the SAME code refused. The kept row used to print the refused opening's 40 mm span one
    line above the refusal of it."""
    _loop(monkeypatch,
          operations=[_op("fill_planar_hole", "open_shell", "face:small")],
          before={"surface_area_mm2": 1000.0, "volume_mm3": 500.0, "solids": 1},
          after={"surface_area_mm2": 1004.0, "volume_mm3": 500.0, "solids": 1},
          declared_area_mm2=4.0, stopped_because="no_candidate", resolved=False,
          refusals=[{"defect": "face:rim", "reason": "paired_rim"}])

    rows = _rows(_state(tmp_path))
    kept = next(r for r in rows if r["value"] == "1")
    assert "2 mm" in kept["why"], "the span it did close is its own to state"
    assert "40" not in kept["why"], "the 40 mm opening is still in the part and was refused"
    assert _find(rows, "Did not fill an opening")["value"] == "Refused"
    assert _find(rows, "Everything found was fixed")["value"] == "No"


# THE REFUSED PATH, through the producer that writes it


def test_the_refused_path_produces_a_receipt_from_the_record_the_node_returns(tmp_path,
                                                                             monkeypatch):
    """The decline `_declined` returns carries the loop in `detail.measurements` and sets neither
    `attempted` nor `refusals`, so `repair_receipt_rows` returned [] for it - and the run that
    most needs an account said nothing."""
    _refuses(monkeypatch, "the run's surface area ended 0.4100 of the original away",
             operations=[_op("fill_planar_hole", "open_shell", "face:small"),
                         _op("sew_faces", "open_shell", "face:rim", status="rolled_back",
                             reason="cap:area_change 0.41 > 0.02")],
             refusals=[{"defect": "face:rim", "reason": "paired_rim"}],
             before={"surface_area_mm2": 1000.0, "volume_mm3": 500.0, "solids": 1},
             after={"surface_area_mm2": 1410.0, "volume_mm3": 500.0, "solids": 1},
             declared_area_mm2=0.0, declared_volume_mm3=0.0, result_discarded=True,
             stopped_because="no_candidate", resolved=False)

    state = _state(tmp_path)
    out = asyncio.run(_attempt(state))
    assert out["repair_attempt"]["attempted"] is False, "it is still a decline"

    rows = R.repair_receipt_rows(state, out)
    assert rows, "a wholesale rollback is exactly the run a customer needs the receipt for"
    assert _find(rows, "The repair was undone")["value"] == "Nothing changed"
    assert _find(rows, "Did not fill an opening")["value"] == "Refused"
    assert _find(rows, "Tried to fix")["value"] == "Rolled back"
    # nothing is claimed as kept, and nothing is claimed as fixed
    assert not [r for r in rows if r["label"].startswith("Filled")]
    assert "Everything found was fixed" not in [r["label"] for r in rows]
    # the shape change is stated as the undone result's, not the customer's part's
    assert _find(rows, "surface area")["label"] == "The undone repair changed the surface area"


def test_the_refused_records_reason_never_reaches_the_page(tmp_path, monkeypatch):
    """The recorded reason is a caps message full of ratios, kernel measurements and our own
    vocabulary. The rows are written from the record's STRUCTURE, never from its prose."""
    from meshpipeline.contracts.narration import leaks

    _refuses(monkeypatch,
             "the run's volume ended 0.9000 of the original away from what the operations "
             "accounted for (500.000 + 0.000 declared = 500.000, measured 950.000)",
             operations=[_op("fill_planar_hole", "open_shell", "face:small")],
             before={"surface_area_mm2": 1000.0, "volume_mm3": 500.0, "solids": 1},
             after={"surface_area_mm2": 1001.0, "volume_mm3": 950.0, "solids": 1},
             result_discarded=True)

    state = _state(tmp_path)
    rows = R.repair_receipt_rows(state, asyncio.run(_attempt(state)))
    assert rows
    for row in rows:
        assert not leaks(row["label"]) and not leaks(row.get("why", ""))
        assert "0.9000" not in row.get("why", "") and "950.000" not in row.get("why", "")


def test_a_decline_with_no_loop_behind_it_still_sends_nothing(tmp_path, monkeypatch):
    """Triage routing this to a person records a decline with a `detail` of its own and no
    measurements. There is nothing measured to account for, and the lane already says so."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", False)
    state = _state(tmp_path)
    assert R.repair_receipt_rows(state, asyncio.run(_attempt(state))) == []


# THE DOUBLE THAT CARRIES IT


def test_a_publisher_double_missing_a_protocol_method_fails_loudly(tmp_path):
    """`repair_receipt.apublish` swallows every exception, because the user stream must never fail
    a run. A double missing `receipt` therefore swallowed its own AttributeError and the test
    passed having published NOTHING. The fake fails from BaseException instead."""
    from conftest import FakePublisher, _publisher_protocol

    pub = FakePublisher("job-1", "repair")
    pub.receipt("Repair", [{"label": "Closed gaps", "value": "3"}], op_id="repair:receipt")
    assert pub.events[-1]["type"] == "receipt"
    assert pub.events[-1]["rows"] == [{"label": "Closed gaps", "value": "3"}]
    pub.mesh_partial("surface", "jobs/j/partial.glb", op_id="mesh:partial")
    assert pub.events[-1]["type"] == "mesh_partial"

    # THE STRUCTURAL GUARD: the double answers every name the protocol calls through to, so the
    # day one is added this line fails here rather than being swallowed somewhere far away.
    missing = sorted(n for n in _publisher_protocol() if not hasattr(FakePublisher, n))
    assert missing == [], f"the publisher protocol grew {missing}: add them to FakePublisher"

    # the fallback is called directly, because every protocol name is implemented now - which is
    # the point of it: this is what happens the day the protocol grows one that is not
    assert "receipt" in _publisher_protocol()
    with pytest.raises(BaseException) as err:            # noqa: PT011 - BaseException is the point
        try:
            FakePublisher.__getattr__(pub, "receipt")()
        except Exception:                                # noqa: BLE001 - what apublish does
            pytest.fail("the missing method was swallowed: the gap would never be noticed")
    assert "does not implement" in str(err.value)

    # and ONLY the protocol's names reach it: the optional seams are probed with
    # `getattr(publish, "...", None)` (engines/fea_driver.py, contracts/narration.py), which a
    # double answering every attribute with a callable would turn into a false positive
    assert getattr(pub, "ameshing", None) is None
    assert getattr(pub, "session_events", None) is None
