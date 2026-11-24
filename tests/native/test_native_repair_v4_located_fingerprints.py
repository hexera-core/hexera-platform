# Responsibility: The panel's surface locations reach `find_sites` as live faces. A gmsh member names a failing surface
#                 by the FINGERPRINT of its box (`mesh_child._located`); `find_sites` translates that through `ws.ids`.
#                 This pins, on the real kernel and real gmsh, that the two fingerprints agree for the profiles that
#                 mesh in millimetres - the premise the translation rests on (lane C4: every surface location was being
#                 dropped because the fingerprint was read as a stable id).
# Boundaries: the native tier. gmsh runs in this process only to read surface boxes (no meshing).
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")
gmsh = pytest.importorskip("gmsh")

from meshpipeline.cad.repair.identity import quantum_for  # noqa: E402
from meshpipeline.cad.repair_v4.sites import find_sites  # noqa: E402
from meshpipeline.cad.repair_v4.verdict import mesh_child  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.assemble import Verdict  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.panel import Located, MemberResult  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _part(tmp_path: Path) -> str:
    """A filleted box with a through hole: planar, cylindrical and toroidal faces, as STEP."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(60.0, 40.0, 20.0).Shape()
    fillet = BRepFilletAPI_MakeFillet(box)
    edges = TopExp_Explorer(box, TopAbs_EDGE)
    fillet.Add(3.0, TopoDS.Edge_s(edges.Current()))
    shape = BRepAlgoAPI_Cut(fillet.Shape(), BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(30, 20, -1), gp_Dir(0, 0, 1)), 6.0, 22.0).Shape()).Shape()
    path = str(tmp_path / "part.step")
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert int(writer.Write(path)) == 1
    return path


def _surface_boxes(step: str) -> dict[int, tuple]:
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(step)
        gmsh.model.occ.synchronize()
        return {int(t): tuple(gmsh.model.getBoundingBox(2, t)) for _, t in gmsh.model.getEntities(2)}
    finally:
        gmsh.finalize()


def test_every_gmsh_surface_fingerprint_names_a_live_face_and_find_sites_uses_it(pool: WorkerPool,
                                                                                  tmp_path: Path) -> None:
    step = _part(tmp_path)
    read = pool.call("read_step_xcaf", {"path": step, "brep_out": str(tmp_path / "in.brep")}, timeout_s=300)
    assert read.ok and read.value is not None, read.error
    ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
    boxes = _surface_boxes(step)
    # exactly as the GmshDefault / GmshAlt members locate (scale 1.0: the file's millimetres)
    failure = SimpleNamespace(kind="overlapping_facets", surfaces=tuple(boxes), surfaces_from_log=False,
                              stage="2d", point=None)
    located = mesh_child._located(failure, boxes, 1.0, quantum_for(mesh_child._diagonal(boxes)))
    faces = {sid for sid in ws.ids if sid[0] == "F"}
    assert len(located) == len(boxes) == len(faces)
    exact = sum(loc["entity"] in {ws.ids[f] for f in faces} for loc in located)
    member = MemberResult("gmsh_default", "fail", "overlapping_facets", "x",
                          [Located(loc["entity"], None, loc["mesher_tag"]) for loc in located])
    verdict = Verdict(ValidReport(True, [], [], "mm", 1, True), [member], None, False, "full")
    sites = find_sites(pool, ws, verdict, h_mm=5.0)
    assert all(s.kind != "unlocated" for s in sites)
    named = {e for s in sites for e in s.entities if e[0] == "F"}
    # every surface gmsh named reaches the site, including the ones whose box sits on a half-cell (exact < all here)
    assert named == faces, (exact, len(faces), sorted(faces - named))


def test_a_located_gmsh_failure_gets_a_defeature_of_exactly_the_face_it_named(pool: WorkerPool,
                                                                              tmp_path: Path) -> None:
    """D16, end to end on the real kernel: gmsh names the hole's cylinder surfaces (OCCT splits it in two) with an `other` failure, which
    is an `invalid` site, a kind the D14 rule left out. The site carries that face as `mesher_faces`, the det sweep
    proposes removing exactly those faces (source `located`) ahead of every other defeature row, and the op removes it."""
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState

    step = _part(tmp_path)
    read = pool.call("read_step_xcaf", {"path": step, "brep_out": str(tmp_path / "in.brep")}, timeout_s=300)
    assert read.ok and read.value is not None, read.error
    ws = Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})
    boxes = _surface_boxes(step)
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(step)
        gmsh.model.occ.synchronize()
        hole = tuple(t for _, t in gmsh.model.getEntities(2) if gmsh.model.getType(2, t) == "Cylinder")
    finally:
        gmsh.finalize()
    assert hole
    failure = SimpleNamespace(kind="other", surfaces=hole, surfaces_from_log=False, stage="2d", point=None)
    locs = mesh_child._located(failure, boxes, 1.0, quantum_for(mesh_child._diagonal(boxes)))
    member = MemberResult("gmsh_prod", "fail", "other", "x",
                          [Located(loc["entity"], None, loc["mesher_tag"]) for loc in locs])
    verdict = Verdict(ValidReport(True, [], [], "mm", 1, True), [member], None, False, "full")
    sites = [s for s in find_sites(pool, ws, verdict, h_mm=5.0) if s.graph.get("mesher_faces")]
    # each site's mesher faces are its own faces, and together they are exactly the surfaces gmsh named
    assert all(set(s.graph["mesher_faces"]) <= set(s.entities) for s in sites)
    assert sum(len(s.graph["mesher_faces"]) for s in sites) == len(hole)
    site = sites[0]
    named = list(site.graph["mesher_faces"])
    assert site.kind == "invalid"

    gen = DetGenerator()
    gen.bind_pool(pool)
    state = SearchState.__new__(SearchState)
    state.ws, state.sites, state.lineage, state.depth = ws, [site], (), 0  # type: ignore[attr-defined]
    rows = [c for c in gen.propose(state, 999) if c.op == "defeature"]
    assert rows and rows[0].params["source"] == "located" and rows[0].targets == named
    new, diff = REGISTRY["defeature"].apply(pool, ws.fork(), named, dict(rows[0].params), timeout_s=120)
    assert set(named) <= set(diff.declared_removed) and diff.params["removed_groups"]
