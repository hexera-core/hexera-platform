# Responsibility: Prove on the REAL kernel, pool, judge and search that a part with three holes is repaired: the
#                 search spends one `fill_loop` per hole, each of which ties the score, and still reaches
#                 `make_volume`. Patience scales with the work the part presents (Task 46); it is not a constant.
# Boundaries: the native tier (`meshpipeline-mesh:native`). No model, nothing faked: `repair_v4()` end to end on the
#             deterministic arm, the default `Budget` of the e2e file (12 ops, 120 s).
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from tests.native._v4_flush_patch_part import write_flush_patch_box, write_three_hole_box  # noqa: E402

import meshpipeline.cad.repair_v4 as v4  # noqa: E402

#: ops the three-hole repair needs. Measured: 70 under Task 46's breadth-first order of tied states; 14 once the search
#: goes deep on ties and stops proposing what the state rules out (Task 47; clean at 14, `ops` at 13). Not a widened
#: engine budget: the exact number the repair costs, so a regression in waste fails this test.
OPS_FOR_THREE_HOLES = 14


def _run(source: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch, *, ops: int = 12,
         wall_s: float = 120.0) -> tuple[Any, list[dict]]:
    monkeypatch.setattr(v4, "ARM", "search_det")
    dest = str(tmp / "out.step")
    result = v4.repair_v4(str(source), dest, budget=v4.Budget(wall_s=wall_s, ops=ops, model_calls=0, model_usd=0.0),
                          deadline_s=time.monotonic() + 300.0)
    events = json.loads(Path(result.trace_path).read_text())["events"] if result.trace_path else []
    return result, events


def _summary(events: list[dict]) -> dict:
    stop = next(e for e in events if e.get("kind") == "stop")
    return {"reason": stop["reason"], "ops": stop["spent"]["ops"], "verdict_candidates": stop["verdict_candidates"],
            "patience": next(e for e in events if e.get("kind") == "start").get("patience"),
            "make_volume_evaluated": any(e.get("kind") == "candidate" and e.get("op") == "make_volume"
                                         and e.get("outcome") == "evaluated" for e in events)}


def test_a_three_hole_part_reaches_make_volume_and_ends_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = write_three_hole_box(tmp_path / "three_holes.step")
    result, events = _run(source, tmp_path, monkeypatch, ops=OPS_FOR_THREE_HOLES, wall_s=280.0)
    s = _summary(events)
    assert s["make_volume_evaluated"], f"a 3-hole part never reached make_volume: {s}"
    assert s["reason"] == "clean" and result.verdict_summary["search"]["clean"] is True and result.delivered, s


def test_with_the_default_twelve_ops_a_three_hole_part_is_cut_off_by_the_op_budget_not_by_patience(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins WHERE the 12-op budget ends this part, so nobody reads the stop as patience: it is `ops`."""
    source = write_three_hole_box(tmp_path / "three_holes.step")
    _, events = _run(source, tmp_path, monkeypatch, ops=12)
    s = _summary(events)
    assert s["reason"] == "ops" and not s["make_volume_evaluated"], s
    patience = next(e for e in events if e.get("kind") == "patience")
    assert patience["open_sites"] == 3 and patience["patience"] == 5, patience


def test_the_one_hole_case_now_costs_five_ops_and_four_verdict_candidates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = write_flush_patch_box(tmp_path / "box.step")
    from tests.native.test_native_repair_v4_end_to_end import _devtools

    case = _devtools("inject").inject(source, "face_delete", 3, tmp_path)
    result, events = _run(case.path, tmp_path, monkeypatch, ops=12)
    s = _summary(events)
    assert s["reason"] == "clean" and s["make_volume_evaluated"], s
    assert (s["ops"], s["verdict_candidates"], s["patience"]) == (5, 4, None), s
    assert next(e for e in events if e.get("kind") == "patience")["open_sites"] == 1
