# Responsibility: Prove on the REAL kernel that (1) the topology test behind "do not propose make_volume / sew" never
#                 rules out a call the kernel then makes, and does rule out the ones it declines; (2) a three-hole part
#                 reaches `clean` in the op count pinned here, with no ops spent on the declines Task 47 removed.
# Boundaries: the native tier. No model, nothing faked. The kernel functions are called in-process on shapes built
#             here (nothing in this file asserts an answer the kernel did not give).
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shell  # noqa: E402
from tests.native._v4_flush_patch_part import flush_patch_box, three_hole_box, write_three_hole_box  # noqa: E402

import meshpipeline.cad.repair_v4 as v4  # noqa: E402
import meshpipeline.cad.repair_v4.kernel_calls  # noqa: E402,F401  (import order: registers the kernel functions)
from meshpipeline.cad.repair_v4 import ws_kernel  # noqa: E402
from meshpipeline.cad.repair_v4.ops import shells  # noqa: E402
from meshpipeline.cad.repair_v4.sites_kernel import volume_possibility  # noqa: E402


def _faces(shape: Any) -> list:
    out, e = [], TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        out.append(TopoDS.Face_s(e.Current()))
        e.Next()
    return out


def _compound(faces: list) -> Any:
    c, b = TopoDS_Compound(), BRep_Builder()
    b.MakeCompound(c)
    for f in faces:
        b.Add(c, f)
    return c


def _polygon_face(points: list[tuple[float, float, float]]) -> Any:
    p = BRepBuilderAPI_MakePolygon()
    for q in points:
        p.Add(gp_Pnt(*q))
    p.Close()
    return BRepBuilderAPI_MakeFace(p.Wire()).Face()


def _unsewn_box() -> Any:
    """Six faces built one by one: coincident edges, but no shared edge between any two faces."""
    v = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
    quads = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return _compound([_polygon_face([v[i] for i in q]) for q in quads])


def _gapped_box(gap: float = 0.002) -> Any:
    """Five faces of a real box (they share edges) and a sixth built separately `gap` mm above where the lid belongs:
    eight free edges that only a sew within `gap` can join. A SHELL: the sewing op measures free edges of shells and
    reports none for loose faces, however far apart."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    shell, builder = TopoDS_Shell(), BRep_Builder()
    builder.MakeShell(shell)
    for face in _faces(BRepPrimAPI_MakeBox(10, 10, 10).Shape()):
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        if abs(props.CentreOfMass().Z() - 10.0) > 1e-6 or abs(props.Mass() - 100.0) > 1e-6:
            builder.Add(shell, face)  # every face but the lid (the one centred at z = 10)
    top = 10.0 + gap
    builder.Add(shell, _polygon_face([(0, 0, top), (10, 0, top), (10, 10, top), (0, 10, top)]))
    return shell


def _shapes() -> dict[str, Any]:
    box = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
    cyl = _faces(BRepPrimAPI_MakeCylinder(3, 10).Shape())
    flap = _polygon_face([(0, 0, 0), (-5, 0, 0), (-5, 0, 5), (0, 0, 5)])
    far = _faces(BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 5, 5, 5).Shape())
    return {
        "closed shell of loose faces": _compound(_faces(box)),
        "box missing one face": _compound(_faces(box)[:5]),
        "box of six unsewn faces": _unsewn_box(),
        "closed box plus a dangling flap": _compound([*_faces(box), flap]),
        "cylinder (seam) closed": _compound(cyl),
        "cylinder missing a cap": _compound(cyl[:2]),
        "one solid only": box,
        "closed box beside an open one": _compound([*_faces(box), *far[:5]]),
        "flush-patch solid": flush_patch_box(),
        "three-hole part": three_hole_box(),
        "box with a 0.002 mm gap": _gapped_box(),
    }


def _payload(tmp: Path, shape: Any, **extra: Any) -> dict:
    brep = tmp / "in.brep"
    ws_kernel.write_brep(shape, str(brep))
    return {"brep_path": str(brep), "out_path": str(tmp / f"out-{time.monotonic_ns()}.brep"), "quantum": 1e-6,
            "params": {}, "targets": [], "ids": {}, **extra}


def _make_volume(tmp: Path, shape: Any) -> dict:
    return shells._make_volume(_payload(tmp, shape))


def test_the_topology_test_never_rules_out_a_volume_the_kernel_makes_and_rules_out_the_ones_it_declines(
        tmp_path: Path) -> None:
    ruled_out, made = [], []
    for name, shape in _shapes().items():
        facts, result = volume_possibility(shape), _make_volume(tmp_path, shape)
        if "declined" not in result:
            made.append(name)
            assert facts["loose"], f"{name}: the kernel made a volume the test had ruled out ({facts})"
        elif not facts["loose"]:
            ruled_out.append(name)
    # The guard must be able to fail BOTH ways: a test that always said "possible" would pass the first assertion.
    assert {"closed shell of loose faces", "closed box plus a dangling flap", "closed box beside an open one",
            "cylinder (seam) closed"} <= set(made), made
    assert {"box missing one face", "box of six unsewn faces", "cylinder missing a cap", "three-hole part",
            "one solid only", "box with a 0.002 mm gap"} <= set(ruled_out), ruled_out


def test_free_edges_counts_edges_bordering_one_face_and_not_a_seam() -> None:
    shapes = _shapes()
    assert volume_possibility(shapes["closed shell of loose faces"])["free_edges"] == 0
    assert volume_possibility(shapes["cylinder (seam) closed"])["free_edges"] == 0, "a seam is not a free edge"
    assert volume_possibility(shapes["box missing one face"])["free_edges"] == 4
    assert volume_possibility(shapes["three-hole part"])["free_edges"] == 12


def test_free_edges_do_not_mean_sew_declines_and_no_free_edge_means_it_does(tmp_path: Path) -> None:
    """The gapped box has free edges and `sew` closes it: a free edge is not grounds to withhold a sew. A closed
    shell has none, and `sew` declines it."""
    shapes = _shapes()
    ladder = {"tol_ladder": [1e-4, 1e-3, 1e-2]}
    unsewn = shells._sew(_payload(tmp_path, shapes["box with a 0.002 mm gap"], params=ladder))
    assert "declined" not in unsewn and volume_possibility(shapes["box with a 0.002 mm gap"])["free_edges"] > 0
    closed = shells._sew(_payload(tmp_path, shapes["closed shell of loose faces"], params=ladder))
    assert "declined" in closed and "no free edges" in closed["declined"]
    assert volume_possibility(shapes["closed shell of loose faces"])["free_edges"] == 0


def test_sew_declines_a_part_that_carries_a_solid(tmp_path: Path) -> None:
    """The rule the generator applies from the workspace's `B` ids."""
    result = shells._sew(_payload(tmp_path, _shapes()["one solid only"], params={"tol_ladder": [1e-3]}))
    assert "declined" in result and "solid" in result["declined"]


# --- the three-hole part, end to end ---------------------------------------------------------------------------------


def _run(source: Path, tmp: Path, monkeypatch: pytest.MonkeyPatch, ops: int) -> list[dict]:
    monkeypatch.setattr(v4, "ARM", "search_det")
    result = v4.repair_v4(str(source), str(tmp / "out.step"),
                          budget=v4.Budget(wall_s=280.0, ops=ops, model_calls=0, model_usd=0.0),
                          deadline_s=time.monotonic() + 300.0)
    assert result.trace_path
    return json.loads(Path(result.trace_path).read_text())["events"]


def _stop(events: list[dict]) -> dict:
    return next(e for e in events if e.get("kind") == "stop")


def test_a_three_hole_part_costs_fourteen_ops_and_wastes_none_of_the_declines_that_were_predictable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = write_three_hole_box(tmp_path / "three.step")
    events = _run(source, tmp_path, monkeypatch, ops=14)
    stop = _stop(events)
    assert stop["reason"] == "clean" and stop["spent"]["ops"] == 14, stop
    cands = [e for e in events if e.get("kind") == "candidate"]
    expansions = [e for e in events if e.get("kind") == "expand"]
    wasted = [(e["op"], e["outcome"]) for e in cands if e["outcome"] in ("rejected", "duplicate")]
    # Before Task 47: 70 ops, 20 make_volume declines, 6 fill duplicates, 10 sew declines, 14 orient duplicates.
    assert ("make_volume", "rejected") not in wasted, wasted
    assert ("fill_loop", "duplicate") not in wasted, wasted
    assert len(wasted) <= 4, wasted  # what is left is the sew declines and one orient no-op: discovered, not predicted
    assert len(cands) / len(expansions) <= 3.5, (len(cands), len(expansions))


def test_twelve_ops_is_not_enough_for_the_three_hole_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the figure from below too: if the op count were ever lower, the test above would not notice.

    Lane C5 moved the floor from 14 to 13 by ORDER alone: the members that fail without a place now make a coarse
    `open` site, which sorts before layer 1's own, so the last expansion proposes `make_volume` before its
    `orient_and_close`, and a 13-op budget cuts the batch after the repair instead of before it. The same 14
    candidates run at a 14-op budget (the test above)."""
    source = write_three_hole_box(tmp_path / "three.step")
    stop = _stop(_run(source, tmp_path, monkeypatch, ops=12))
    assert stop["reason"] == "ops" and stop["spent"]["ops"] == 12, stop
    at_13 = _stop(_run(source, tmp_path, monkeypatch, ops=13))
    assert at_13["reason"] == "clean" and at_13["spent"]["ops"] == 13, at_13

