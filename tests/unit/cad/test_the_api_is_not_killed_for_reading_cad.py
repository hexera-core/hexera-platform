# Responsibility: Verify the bounded measurement answers exactly as the in-process one does, and
# that it reports rather than raises when it runs out of time or cannot read the file.
# Boundaries: cad/shape_traits.measure_file_bounded and its child entry; no API and no uvicorn here.
"""READING CAD IN THE API WORKER GETS THE WORKER KILLED.

OpenCASCADE's import and tessellation do not release the GIL, so `asyncio.to_thread` moves the
work off the event loop but not off the interpreter. uvicorn's multi-worker supervisor pings each
worker over a pipe every 0.5 s and allows 5 s for an answer (supervisors/multiprocess.py:
`is_alive` is `process.is_alive() and self.ping(timeout=5)`). A worker that cannot answer is
SIGKILLed - the source says "process is hung, kill it" - and then logged as `Child process [pid]
died`, which reads like a crash and is not one.

MEASURED END TO END on the first customer assembly, 2026-10-30 (ColdPlateAssembly_CLEAN.step,
15,883,539 bytes, 6 solids, 9,226 faces), on the turn straight after the customer presses Proceed:

    uvicorn --workers 2 (what we ship)   the worker is SIGKILLed 11-15 s in, 3 runs of 3
    uvicorn --workers 1 (no supervisor)  HTTP 200 in 44 s
    a plain python process               exit 0, same answer

faulthandler was armed for the two-worker run and printed nothing, which rules out every catchable
fatal signal, and the container never ran out of memory (596 MB peak of 15.48 GiB). A ping nobody
could answer is the only thing that fits all three rows.
"""
from __future__ import annotations

import json

import pytest

try:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.shape_traits import (
    MEASURE_CHILD_BUDGET_S,
    measure_file,
    measure_file_bounded,
)

PORTS = [{"name": "in", "type": "inlet", "diameter_mm": 20.0},
         {"name": "out", "type": "outlet", "diameter_mm": 20.0},
         {"name": "wall", "type": "wall"}]


def _tube(path):
    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 10.0, 200.0).Shape()
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def test_the_child_measures_the_same_shape_as_the_caller_would(tmp_path):
    """The whole point: moving the read out of the caller's interpreter must not change the answer."""
    p = _tube(tmp_path / "tube.step")
    here = measure_file(p, flow="internal", input_kind="fluid-domain", patches=PORTS)
    there = measure_file_bounded(p, flow="internal", input_kind="fluid-domain", patches=PORTS)
    assert there is not None, "the bounded measurement gave nothing on a readable file"
    assert there.as_dict() == here.as_dict()


def test_running_out_of_time_is_reported_and_not_raised(tmp_path):
    """Traits are advice. `rank_in_time` carries on without them and says so in the turn, so a
    child that cannot finish must come back as None rather than take the turn down."""
    p = _tube(tmp_path / "tube.step")
    # no child can start a fresh interpreter, import OCP and read a STEP in a millisecond
    assert measure_file_bounded(p, budget_s=0.001, flow="internal", patches=PORTS) is None


def test_a_file_that_is_not_there_comes_back_as_traits_that_say_so(tmp_path):
    """measure_file never raises for an unreadable file - it answers with traits whose notes say
    why - and the child must carry that answer rather than turn it into a None the caller cannot
    tell apart from running out of time."""
    got = measure_file_bounded(tmp_path / "nothing.step", flow="internal", patches=PORTS)
    assert got is not None
    assert any("not there" in n for n in got.notes), got.notes


def test_the_budget_is_under_the_callers_own_patience():
    """`measured_choice.rank_in_time` waits MEASURE_WAIT_S for the whole recommendation. A child
    still running past that is work nobody is waiting for."""
    from meshpipeline.agents.intake.measured_choice import MEASURE_WAIT_S
    assert MEASURE_CHILD_BUDGET_S >= MEASURE_WAIT_S, (
        "a child that is killed before the caller gives up wastes the measurement")


def test_the_child_entry_writes_an_answer_a_caller_can_read(tmp_path):
    """The child is `python -m meshpipeline.cad.shape_traits <task> <answer>`; this is that
    contract, without the subprocess."""
    from meshpipeline.cad import shape_traits as ST

    p = _tube(tmp_path / "tube.step")
    spec, out = tmp_path / "task.json", tmp_path / "answer.json"
    spec.write_text(json.dumps({"path": str(p), "flow": "internal", "patches": PORTS}))
    assert ST.main([str(spec), str(out)]) == 0
    answer = json.loads(out.read_text())
    assert "error" not in answer, answer
    assert ST.ShapeTraits.from_dict(answer["traits"]).form == "cad"


def test_the_api_path_asks_for_a_child_and_the_worker_does_not(monkeypatch, tmp_path):
    """`rank_in_time` is the path that runs inside the API, so it must pass in_child=True; a
    direct `rank` (the geometry worker, where nothing is pinging) must keep the in-process read
    and its cache."""
    import asyncio

    from meshpipeline.agents.intake import measured_choice as MC

    seen = {}

    def _spy(path, **kw):
        seen["bounded"] = True
        return measure_file(path, **{k: v for k, v in kw.items() if k != "budget_s"})

    def _spy_plain(path, **kw):
        seen["in_process"] = True
        return measure_file(path, **kw)

    monkeypatch.setattr("meshpipeline.cad.shape_traits.measure_file_bounded", _spy)
    monkeypatch.setattr("meshpipeline.cad.shape_traits.measure_file", _spy_plain)

    p = _tube(tmp_path / "tube.step")
    from meshpipeline.agents.intake.agent import _IMPLEMENTED_ENGINES
    kw = dict(engines=_IMPLEMENTED_ENGINES, purpose="conjugate_heat_transfer",
              input_kind="solid-assembly", form="cad", path=p, patches=PORTS)

    seen.clear()
    try:
        asyncio.run(MC.rank_in_time(timeout_s=120.0, **kw))
    except Exception:  # noqa: BLE001 - ranking may decline for other reasons; the read is the point
        pass
    assert seen.get("bounded"), "the API path measured in its own interpreter"

    seen.clear()
    try:
        MC.rank(**kw)
    except Exception:  # noqa: BLE001
        pass
    assert seen.get("in_process"), "the worker path paid for a child it does not need"
    assert not seen.get("bounded")
