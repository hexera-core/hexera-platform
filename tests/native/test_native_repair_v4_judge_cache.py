# Responsibility: Task 22b on the REAL kernel and the REAL panel: the engine's judge meshes a clean STEP once and
#                 serves the second judgement from the verdict cache. Counted in mesher invocations, never time.
# Boundaries: the native tier. Only a counting proxy wraps each real panel member; nothing is faked below it.
from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.IFSelect import IFSelect_RetDone  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402

from meshpipeline.cad.repair_v4 import search, stages  # noqa: E402
from meshpipeline.cad.repair_v4.search import default_judge  # noqa: E402
from meshpipeline.cad.repair_v4.verdict import assemble  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.cache import MemoryStore, VerdictCache  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=180)
    try:
        yield p
    finally:
        p.close()


@dataclass
class Counting:
    """Wraps a real panel member; counts how many times it was asked to mesh."""

    inner: Any
    runs: int = 0

    @property
    def name(self) -> str:
        return str(self.inner.name)

    @property
    def config(self) -> Any:
        return self.inner.config

    def run(self, pool: Any, step_path: str, *, timeout_s: float) -> Any:
        self.runs += 1
        return self.inner.run(pool, step_path, timeout_s=timeout_s)


def _box_workspace(pool: WorkerPool, tmp_path: Path) -> Workspace:
    """The way the engine builds one: a STEP is read through `read_step_xcaf` (a COMPOUND root, as the real
    reader yields), never a bare solid BREP, which the STEP writer would export as a shell with no volume."""
    step = tmp_path / "in.step"
    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40.0, 30.0, 20.0).Shape(), STEPControl_AsIs)
    assert writer.Write(str(step)) == IFSelect_RetDone
    read = pool.call("read_step_xcaf", {"path": str(step)}, timeout_s=120)
    assert read.ok and read.value is not None
    return Workspace.from_brep(pool, read.value["brep"], read.value["labels"])


def test_the_second_judgement_of_a_real_box_meshes_nothing(pool: WorkerPool, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _box_workspace(pool, tmp_path)
    members = {tier: [Counting(m) for m in assemble.default_members(tier)] for tier in ("cheap", "full")}
    monkeypatch.setattr(assemble, "default_members", lambda tier: members[tier])
    j = default_judge(h_mm=2.0, cache=VerdictCache(MemoryStore()))
    first = j.full(pool, ws, str(tmp_path / "in.step"))
    after_first = [m.runs for m in members["full"]]
    assert members["full"] and all(n == 1 for n in after_first), after_first  # every member meshed once
    second = j.full(pool, ws, str(tmp_path / "in.step"))
    assert [m.runs for m in members["full"]] == after_first  # HIT: no mesher invoked
    assert [r.status for r in second.panel] == [r.status for r in first.panel]
    assert [r.min_sicn for r in second.panel] == [r.min_sicn for r in first.panel]
    assert not first.inconclusive


def test_step_for_is_a_function_of_the_geometry_alone(pool: WorkerPool, tmp_path: Path) -> None:
    """OCCT's writer stamps FILE_NAME with the current second. Without `stages._deterministic` two exports of one
    BREP differ, so a durable verdict cache could never hit and equal geometry at two paths never shared a key."""
    ws = _box_workspace(pool, tmp_path)
    copy = tmp_path / "copy.brep"
    shutil.copyfile(ws.brep_path, copy)
    first = stages.step_for(pool, ws.brep_path)
    time.sleep(1.5)
    second = stages.step_for(pool, str(copy))
    assert first != second
    assert hashlib.sha256(Path(first).read_bytes()).hexdigest() == hashlib.sha256(Path(second).read_bytes()).hexdigest()


def test_h_mm_of_a_real_part_is_a_tenth_of_its_diagonal_in_mm(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _box_workspace(pool, tmp_path)  # 40 x 30 x 20 mm
    assert search.h_mm_for(pool, ws) == pytest.approx((40.0**2 + 30.0**2 + 20.0**2) ** 0.5 / 10.0, rel=1e-6)
