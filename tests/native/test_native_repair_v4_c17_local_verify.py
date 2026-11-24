# Responsibility: lane C17 - search_seq's local verification on real OCCT: an overlap cut away is `gone` on its pair
#                 with no new hard code, the touching neighbour is looked at, an unrepaired defect is `still there`,
#                 and a mesher defect meshes the touched region alone with the member that failed.
# Boundaries: the native tier; real OCCT in spawned workers, real ops through their own `apply`.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.local_verify import Defect, touched_bodies, verify_local  # noqa: E402
from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 600


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _box(x: float, dx: float) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, 0.0, 0.0), dx, 40.0, 30.0).Shape()


def _write(shapes: list[Any], path: Path) -> str:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for s in shapes:
        builder.Add(compound, s)
    assert BRepTools.Write_s(compound, str(path))
    return str(path)


def _body_at(pool: WorkerPool, ws: Workspace, x: float) -> str:
    """The stable id of the body containing (x, 20, 15)."""
    got = pool.call("locate_points", {"brep": ws.brep_path, "xyz": [[x, 20.0, 15.0]], "tol_mm": 1e-6}, timeout_s=60)
    (index,) = got.value["solids"]
    return next(s for s, i in ws.indices.items() if s.startswith("B") and i == index)


@pytest.fixture(scope="module")
def overlap_case(pool: WorkerPool, tmp_path_factory) -> tuple[Workspace, Workspace, Defect, str]:
    """A overlaps B by 4 mm; C touches B face to face (contact, not overlap). The repair cuts B by A."""
    path = tmp_path_factory.mktemp("c17") / "three.brep"
    ws = Workspace.from_brep(pool, _write([_box(0, 50), _box(46, 50), _box(96, 30)], path))
    a, b, c = _body_at(pool, ws, 10), _body_at(pool, ws, 80), _body_at(pool, ws, 110)
    defect = Defect("s1", "validity", "body_overlap", (a, b), pairs=((a, b),), measurement={"depth_mm": 4.0})
    after, diff = REGISTRY["conformalise"].apply(pool, ws, [a, b], {"mode": f"cut_by:{a}"}, timeout_s=TIMEOUT)
    assert diff.params.get("declined") is None, diff.params
    return ws, after, defect, c


def test_an_overlap_cut_away_is_gone_on_its_pair_with_no_new_hard_code(pool: WorkerPool, overlap_case) -> None:
    before, after, defect, c = overlap_case
    a, b = defect.entities
    got = verify_local(pool, before, after, defect, pair=(a, b), timeout_s=TIMEOUT)
    assert got.gone and got.new_hard == [] and got.outcome == "gone", got.detail
    assert len(got.pairs) == 1 and got.pairs[0].gone
    assert "body_overlap" in " ".join(got.detail["before"]) and not any("body_overlap" in f for f in got.detail["after"])
    assert c in got.detail["neighbours"], "the body touching the cut one must be looked at"
    assert got.line().startswith("gone (")


def test_the_touched_set_is_the_cut_pair_plus_its_neighbour(pool: WorkerPool, overlap_case) -> None:
    before, after, defect, c = overlap_case
    touched = touched_bodies(pool, before, after, defect)
    assert c in touched and len(touched) == 3


def test_a_defect_no_op_touched_is_still_there(pool: WorkerPool, overlap_case) -> None:
    before, _after, defect, _c = overlap_case
    got = verify_local(pool, before, before, defect, pair=defect.pairs[0], timeout_s=TIMEOUT)
    assert not got.gone and got.outcome == "still_there" and got.new_hard == []
    assert got.pairs and not got.pairs[0].gone and got.pairs[0].detail["volume"] > 0


def test_a_mesher_defect_meshes_the_touched_region_alone_with_the_member_that_failed(pool: WorkerPool,
                                                                                    overlap_case) -> None:
    before, after, _defect, c = overlap_case
    defect = Defect("s2", "mesher", "overlapping_facets", (c,), member="gmsh_default")
    got = verify_local(pool, before, after, defect, touched=[c], timeout_s=TIMEOUT)
    assert got.detail["member"] == "gmsh_default" and got.detail["touched"] == [c]
    assert got.gone and got.detail["member_status"] == "pass"
    assert got.detail["region_passed_before"] is True  # a box meshes alone: the local mesh cannot see a defect here
