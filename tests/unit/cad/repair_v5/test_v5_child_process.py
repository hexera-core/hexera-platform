# Responsibility: Pin how CRS v5 runs from a DAEMONIC caller (the product's Celery prefork child): as its own child
#                 process, its progress read live from the event log, its result handed back as JSON; and that a
#                 child that fails is `V5Unavailable` (the caller's cue to run v3).
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

import meshpipeline.cad.repair_v5.engine as engine
from meshpipeline.cad.repair_v5.result import RepairResultV5

CONTRACT = RepairResultV5(engine="v5", choice="meshing", outcome="repaired", meshers_passing=4, meshers_total=4)

FAKE_CHILD = r"""
import json, os, sys
src, dest, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
log = os.environ["CRS_V5_EVENT_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"event": "v5_start"}) + "\n")
    fh.write(json.dumps({"event": "diagnosed", "defects": 2}) + "\n")
    fh.write(json.dumps({"event": "phase", "phase": "tournament", "problem_index": 1, "problem_total": 2,
                         "defect_class": "close_faces"}) + "\n")
open(dest, "wb").write(b"repaired")
mode = os.environ.get("FAKE_CHILD_MODE", "ok")
if mode == "ok":
    json.dump({"delivered": True, "result": CONTRACT, "trace": {"lines": ["x"]}, "progress": []},
              open(result_path, "w"))
elif mode == "error":
    json.dump({"error": "V5Unavailable: the checker could not run", "unavailable": True}, open(result_path, "w"))
    sys.exit(3)
else:
    sys.exit(9)
"""


@pytest.fixture
def fake_child(monkeypatch, tmp_path):
    real = subprocess.Popen
    script = tmp_path / "fake_child.py"
    script.write_text("CONTRACT = " + repr(CONTRACT.to_dict()) + "\n" + FAKE_CHILD)
    seen: dict = {}

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        assert cmd[1:3] == ["-m", "meshpipeline.runtime.crs_v5_child"]
        return real([sys.executable, str(script), *cmd[3:]], **kw)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(engine, "_daemonic", lambda: True)
    monkeypatch.delenv("CRS_V5_EVENT_LOG", raising=False)
    return seen


def _files(tmp_path):
    src, dest = tmp_path / "in.step", tmp_path / "out.step"
    src.write_bytes(b"ISO-10303-21;")
    return src, dest


def test_a_daemonic_caller_runs_v5_as_a_child_and_hears_its_progress(fake_child, tmp_path):
    src, dest = _files(tmp_path)
    heard: list = []
    run = engine.repair_file(src, dest, deadline_s=time.monotonic() + 600, on_progress=heard.append)
    assert run.delivered and run.result == CONTRACT and run.trace == {"lines": ["x"]}
    assert dest.read_bytes() == b"repaired"
    assert [e["text"] for e in heard] == ["Checking your CAD", "Found 2 problems to fix",
                                          "Repairing a thin gap - problem 1 of 2"]
    assert fake_child["cmd"][-2:] == ["meshing", "none"]


@pytest.mark.parametrize("mode", ["error", "crash"])
def test_a_failed_child_is_v5_unavailable(fake_child, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CHILD_MODE", mode)
    src, dest = _files(tmp_path)
    with pytest.raises(engine.V5Unavailable):
        engine.repair_file(src, dest, deadline_s=time.monotonic() + 600)


def test_the_contract_round_trips_through_json():
    from meshpipeline.cad.repair_v5.result import Location, Problem

    full = RepairResultV5(engine="v5", choice="standalone", outcome="partly_repaired", problems=(
        Problem(id="p1", type="thin_gap", location=Location(faces=("F1",), bodies=("B1",), points=((1.0, 2.0, 3.0),)),
                status="fixed", reason_text="Fixed", change_mm=0.01),), flags=("volume_changed",), spend_usd=0.3)
    assert RepairResultV5.from_dict(json.loads(json.dumps(full.to_dict()))) == full


def test_the_child_entry_reports_a_failure_in_its_result_file(tmp_path, monkeypatch):
    from meshpipeline.runtime import crs_v5_child

    monkeypatch.setattr("meshpipeline.runtime.composition.install_adapters", lambda: None)

    def boom(*a, **k):
        raise engine.V5Unavailable("pool")

    monkeypatch.setattr(engine, "repair_in_process", boom)
    out = tmp_path / "r.json"
    assert crs_v5_child.main(["a", "b", str(out), "10", "meshing"]) == 3
    assert json.loads(out.read_text()) == {"error": "V5Unavailable: pool", "unavailable": True}
