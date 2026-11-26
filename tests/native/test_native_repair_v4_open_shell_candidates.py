# Responsibility: Prove, on the real kernel and the real judge, that an OPEN SHELL (a box with one face deleted, the canonical
#                 injected defect) yields site(s) the det sweep can act on, and that candidates reach a VERDICT.
# Boundaries: native tier (OCCT, gmsh, Netgen). No model, no network. A candidate that the op declines or the judge does not
#             improve stays a NEGATIVE result here: this test asserts that candidates are generated and judged, not that
#             the box is repaired (a whole missing face is outside every op's fidelity bound; see the Task 36 report).
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools_ReShape  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402
from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402

from meshpipeline.cad.repair_v4 import search as S  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


def _open_box(path: Path) -> str:
    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    face = TopoDS.Face_s(TopExp_Explorer(box, TopAbs_FACE).Current())
    reshape = BRepTools_ReShape()
    reshape.Remove(face)
    writer = STEPControl_Writer()
    writer.Transfer(reshape.Apply(box), STEPControl_AsIs)
    assert writer.Write(str(path)) == 1
    return str(path)


def test_an_open_shell_yields_an_open_site_and_judged_candidates(tmp_path: Path) -> None:
    src = _open_box(tmp_path / "open_box.step")
    pool = WorkerPool(2)
    try:
        read = pool.call("read_step_xcaf", {"path": src, "brep_out": str(tmp_path / "o.brep")}, timeout_s=120)
        assert read.ok, read.error
        ws = Workspace.from_brep(pool, read.value["brep"], read.value["labels"])
        h_mm = S.h_mm_for(pool, ws)
        judge = S.default_judge(h_mm=h_mm)
        verdict = judge.full(pool, ws, src)
        sites = judge.sites(pool, ws, verdict)
        # the layer-1 finding names a shell (H1), which has no stable id: the site is whole-part, and its KIND survives
        assert "open" in {s.kind for s in sites}, f"an open shell produced sites {[(s.id, s.kind) for s in sites]}"
        generators, _ = S.arm_generators("search_det")
        result = S.search(pool, ws, src, generators, S.Budget(wall_s=120.0, ops=12, model_calls=0, model_usd=0.0),
                          workers=2, judge=judge, h_mm=h_mm)
    finally:
        pool.close()
    assert result.verdict_candidates > 0, (
        f"no candidate reached a verdict on an open shell (reason {result.reason!r}); "
        f"outcomes {[(e.get('op'), e.get('outcome'), e.get('stage')) for e in result.trace.of_kind('candidate')]}")
    judged = [e for e in result.trace.of_kind("candidate") if e.get("score") is not None]
    assert judged, "candidates were generated but none was scored"
    assert result.infrastructure_failed is False
