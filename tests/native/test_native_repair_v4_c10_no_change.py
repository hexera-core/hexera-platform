# Responsibility: Lane C10 - a body op that leaves the shape as it was DECLINES `no_change`. On dev abc/0051_00514513 (v2,
#                 lane B13) conformalise ran 11 times, returned the input each time, and the search recorded duplicates and
#                 spent the ops. Reproduces that no-op on the real kernel (fragment of two bodies that already share a
#                 face; cut_by with nothing left to cut) and pins the decline.
# Boundaries: the native tier; real OCCT in spawned workers through the ops' own `apply`.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.search import _fingerprint  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 300


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _two_touching_boxes(path: Path) -> str:
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape())
    builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10.0, 10.0, 10.0).Shape())
    BRepTools.Write_s(compound, str(path))
    return str(path)


def _bodies(ws: Workspace) -> list[str]:
    return sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))


def _conformal(pool: WorkerPool, tmp_path: Path) -> Workspace:
    """Two boxes sharing their contact face: the first fragment's own output (a real change)."""
    ws = Workspace.from_brep(pool, _two_touching_boxes(tmp_path / "two.brep"))
    once, diff = REGISTRY["conformalise"].apply(pool, ws, _bodies(ws), {"mode": "fragment"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params
    assert _fingerprint(once) != _fingerprint(ws)
    return once


def test_a_fragment_of_bodies_that_already_share_their_face_declines_no_change(pool: WorkerPool,
                                                                               tmp_path: Path) -> None:
    once = _conformal(pool, tmp_path)
    again, diff = REGISTRY["conformalise"].apply(pool, once, _bodies(once), {"mode": "fragment"}, timeout_s=TIMEOUT)
    assert diff.params.get("declined") == "no_change", diff.params  # before C10: a "success" whose state was the input
    assert again is once


def test_a_cut_by_with_nothing_to_cut_declines_no_change(pool: WorkerPool, tmp_path: Path) -> None:
    once = _conformal(pool, tmp_path)
    keep = _bodies(once)[0]
    again, diff = REGISTRY["conformalise"].apply(pool, once, _bodies(once), {"mode": f"cut_by:{keep}"},
                                                 timeout_s=TIMEOUT)
    assert diff.params.get("declined") == "no_change", diff.params
    assert again is once


def test_a_real_fragment_is_still_applied(pool: WorkerPool, tmp_path: Path) -> None:
    _conformal(pool, tmp_path)  # the guard must not decline the first fragment, which does share the face
