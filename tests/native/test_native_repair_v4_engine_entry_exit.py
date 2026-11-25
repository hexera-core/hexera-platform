# Responsibility: Prove the engine's entry and exit on the REAL kernel through the real entry point
#                 `repair_v4()`: a real STEP is read through XCAF into a Workspace (real pool, real
#                 `ws_index`), and a delivered result is written back as a STEP that reads again;
#                 a passthrough leaves the upload's bytes at `dest`.
# Boundaries: the native tier. The checker and the middle (staged healers + search) are fakes at the
#             seam - they are other lanes' code and need a mesher; this test is about getting in and out.
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

import meshpipeline.cad.repair_v4 as v4  # noqa: E402
from meshpipeline.cad.repair_v4 import search as search_mod  # noqa: E402
from meshpipeline.cad.repair_v4.trace import Trace  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


def _box_step(path: Path) -> str:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape(), STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def _verdict(q: int) -> Any:
    return SimpleNamespace(q=q, clean=False, tier="full", inconclusive=False, valid=SimpleNamespace(ok=True),
                           panel=(SimpleNamespace(name="gmsh", status="pass"),), faithful=None)


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"changed": True, "seen_ws": None}

    def find_best(pool: Any, ws: Any, source: str, *, budget: Any, deadline_s: float, judge: Any,
                  input_verdict: Any = None) -> Any:
        state["seen_ws"] = ws
        trace = Trace()
        trace.record("start")
        return search_mod.SearchResult(best=ws, best_verdict=_verdict(3), clean=False, trace=trace,
                                       changed=state["changed"], reason="patience")

    monkeypatch.setattr(v4, "_make_judge", lambda pool, ws: SimpleNamespace(full=lambda p, w, o: _verdict(1)))
    monkeypatch.setattr(v4, "_find_best", find_best)
    monkeypatch.setattr(search_mod, "score", lambda v: (1, v.q, 0.0, 0.0, 0))
    return state


def _run(src: str, dest: str) -> v4.V4Result:
    budget = v4.Budget(wall_s=600.0, ops=6, model_calls=0, model_usd=0.0)
    return v4.repair_v4(src, dest, budget=budget, deadline_s=time.monotonic() + 600.0)


def test_real_step_in_and_a_real_step_out(seam: dict, tmp_path: Path) -> None:
    src, dest = _box_step(tmp_path / "in.step"), str(tmp_path / "out.step")
    result = _run(src, dest)
    assert result.delivered is True
    ws = seam["seen_ws"]
    assert len([i for i in ws.ids if i.startswith("F")]) == 6  # the real kernel named the box's faces
    assert Path(dest).read_bytes().startswith(b"ISO-10303-21")
    # the written STEP reads again, to the same six faces
    pool = WorkerPool(1)
    try:
        back = pool.call("read_step_xcaf", {"path": dest, "brep_out": str(tmp_path / "back.brep")}, timeout_s=120)
        assert back.ok, back.error
        again = Workspace.from_brep(pool, back.value["brep"])
    finally:
        pool.close()
    assert len([i for i in again.ids if i.startswith("F")]) == 6
    assert result.trace_path is not None and result.trace_path.is_file()


def test_real_passthrough_is_byte_identical(seam: dict, tmp_path: Path) -> None:
    seam["changed"] = False
    src, dest = _box_step(tmp_path / "in.step"), str(tmp_path / "out.step")
    result = _run(src, dest)
    assert result.delivered is False
    assert Path(dest).read_bytes() == Path(src).read_bytes()
