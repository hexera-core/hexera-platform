# Responsibility: Pin that the repair node carries CRS v5's result contract on the job's repair record (delivered and
#                 refused alike), relays its plain progress to the job stream, and waits v5's clock plus v3's.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import RESULT_V5_KEY, RepairRefused
from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport, RepairStatus
from meshpipeline.pipeline.repair_attempt import node_repair_attempt

CONTRACT = {"engine": "v5", "choice": "meshing", "outcome": "repaired", "problems": [], "meshers_passing": 4,
            "meshers_total": 4, "max_change_mm": 0.0, "allowed_change_mm": 0.5, "volume_ratio": 1.0,
            "size_ratio": 1.0, "flags": [], "seconds": 3.0, "spend_usd": 0.0, "needs_review": False,
            "auto_accepted": True}


def _state(tmp_path) -> dict:
    report = {"report": {"summary": "inspected", "defects": [{"code": "wire_gap", "severity": "error"}],
                         "entities": [{"code": "wire_gap", "severity": "error", "entity": "edge:8",
                                       "region": "unknown", "message": "a gap", "measurements": {},
                                       "location": {"centroid": [1.0, 2.0, 3.0]}}]}}
    return {"job_id": "job-1", "engine": "snappy", "geometry": geometry_state(tmp_path),
            "repair_status": RepairStatus.repairable.value, "repair_report": report}


@pytest.fixture(autouse=True)
def _v5_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="", budget_s=None: (True, ""))

    async def quiet(*a, **k):
        return None

    # the lane's closing line and receipt are other modules' concern (and need a bound execution)
    monkeypatch.setattr("meshpipeline.pipeline.repair_words.asay", quiet)
    monkeypatch.setattr("meshpipeline.pipeline.repair_receipt.apublish", quiet)
    # ...and so is the typed `repair_progress` lane (pipeline/repair_progress.Lane)
    monkeypatch.setattr("meshpipeline.pipeline.repair_progress.apublish", quiet)


def test_a_delivered_v5_repair_carries_the_contract_trace_and_progress(tmp_path, monkeypatch):
    seen: dict = {}

    def repair(source, destination, *, on_progress=None, **kw):
        seen["on_progress"] = on_progress
        if on_progress is not None:
            on_progress({"stage": "checking", "problem_index": None, "problem_total": None, "problem_type": None,
                         "text": "Checking your CAD"})
        destination.write_bytes(source.read_bytes() + b"\n/* v5 */\n")
        return RepairReport(defects=(), operations=(), summary="repaired: 0 of 0 problems fixed",
                            measurements=(RepairMeasurement("engine", "v5"), RepairMeasurement("resolved", True)),
                            diagnostics={"engine": "v5", RESULT_V5_KEY: CONTRACT, "v5_trace": {"lines": ["x"]},
                                         "repair_progress": [{"text": "Checking your CAD"}]})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", repair)
    out = asyncio.run(node_repair_attempt(_state(tmp_path)))
    record = out["repair_attempt"]
    assert record["attempted"] is True and record["engine"] == "v5"
    assert record[RESULT_V5_KEY] == CONTRACT
    assert record["v5_trace"] == {"lines": ["x"]}
    assert callable(seen["on_progress"])


def test_a_refused_v5_run_still_carries_the_contract(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise RepairRefused("CRS v5 kept no repair",
                            measurements={"engine": "v5", RESULT_V5_KEY: {**CONTRACT, "outcome": "unrepairable"}})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", refuse)
    out = asyncio.run(node_repair_attempt(_state(tmp_path)))
    record = out["repair_attempt"]
    assert record["attempted"] is False and record["outcome"] == "refused"
    assert record[RESULT_V5_KEY]["outcome"] == "unrepairable" and record["engine"] == "v5"


def test_the_node_waits_v5s_wall_plus_v3s(monkeypatch):
    from meshpipeline.cad.repair.conservative import repair_timeout_s

    monkeypatch.setattr(rcfg, "CRS_V5_FILE_WALL_S", 100.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 20.0)
    assert repair_timeout_s() == 120.0


def test_the_relay_publishes_plain_step_rows_in_order(monkeypatch):
    from meshpipeline.pipeline import repair_progress as RP

    said: list = []

    class Pub:
        async def anarrate(self, kind, text, *, n=None, of=None, unit="", op_id=""):
            said.append((kind, text, n, of, op_id))

    monkeypatch.setattr("meshpipeline.application.execution_publisher.execution_publisher",
                        lambda job_id, agent=None: Pub())

    async def go() -> list:
        relay = RP.ProgressRelay("job-1").start()
        relay.push({"stage": "repairing", "problem_index": 3, "problem_total": 12, "problem_type": "thin_gap",
                    "text": "Repairing a thin gap - problem 3 of 12"})
        relay.push({"stage": "done", "text": "Repair finished: 10 of 12 fixed"})
        await asyncio.sleep(0.05)
        await relay.stop()
        return relay.events

    events = asyncio.run(go())
    assert [e["text"] for e in events] == ["Repairing a thin gap - problem 3 of 12", "Repair finished: 10 of 12 fixed"]
    assert said[0] == ("refine", "Repairing a thin gap - problem 3 of 12", 3, 12, RP.OP_ID)
    assert said[1][0] == "check"
