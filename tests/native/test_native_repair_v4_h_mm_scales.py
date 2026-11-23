# Responsibility: The engine's mesh size follows the part at two scales decades apart, on the real kernel: `base_size`
#                 takes METRES and `h_mm` is MILLIMETRES, and a factor-of-1000 slip is invisible on one small part.
# Boundaries: the native tier; the real worker pool and `ws_diagonal`. Nothing is faked but a spy on `default_judge`.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402

import meshpipeline.cad.repair_v4 as v4  # noqa: E402
from meshpipeline.cad.repair_v4 import search as search_mod  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402
from meshpipeline.engines.fea_plan import base_size  # noqa: E402


def _box_ws(pool: WorkerPool, edge_mm: float, tmp: Path) -> Workspace:
    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(edge_mm, edge_mm, edge_mm).Shape(), STEPControl_AsIs)
    step = tmp / f"box_{edge_mm:g}.step"
    assert writer.Write(str(step)) == 1
    read = pool.call("read_step_xcaf", {"path": str(step), "brep_out": str(tmp / f"b_{edge_mm:g}.brep")}, timeout_s=120)
    assert read.ok, read.error
    return Workspace.from_brep(pool, read.value["brep"], read.value["labels"])


def test_h_mm_scales_with_the_part_across_three_decades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr(search_mod, "default_judge", lambda *, h_mm, cache=None: seen.append(h_mm) or "judge")
    pool = WorkerPool(1)
    try:
        for edge in (10.0, 10_000.0):  # a 10 mm part and a 10 m part
            ws = _box_ws(pool, edge, tmp_path)
            assert v4._make_judge(pool, ws) == "judge"
    finally:
        pool.close()
    diag = [10.0 * 3 ** 0.5, 10_000.0 * 3 ** 0.5]
    assert seen == pytest.approx([1000 * base_size(d / 1000) for d in diag])
    assert seen[0] == pytest.approx(1.7320508, rel=1e-6)
    assert seen[1] / seen[0] == pytest.approx(1000.0, rel=0.05)
