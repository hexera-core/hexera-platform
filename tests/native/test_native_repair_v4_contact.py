# Responsibility: A11 step 2 - the proximity kernel must FIND a contact (two faces at gap 0 sharing no edge) and
#                 must still ignore faces that share an edge. The whole aimed-op change rests on this.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: E402,F401  (import order: as a worker does)
from meshpipeline.cad.repair_v4.sites_kernel import sites_proximity  # noqa: E402


def _write(shape, path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _compound(*shapes):
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _pairs(path, max_dist=50.0):
    return sites_proximity({"brep_path": path, "max_dist": max_dist, "max_pairs": 50})["pairs"]


def test_two_boxes_meeting_at_one_corner_are_a_contact(tmp_path) -> None:
    """The A11 geometry: they touch (gap 0) and share a vertex, so the old rule DROPPED them. 9 of 14 real
    parts carried this class and no op could ever be aimed at it."""
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 10), 10.0, 10.0, 10.0).Shape()   # corner-to-corner
    pairs = _pairs(_write(_compound(a, b), tmp_path / "corner.brep"))

    touching = [p for p in pairs if p["touching"]]
    assert touching, "a corner contact must be reported"
    for p in touching:
        assert p["dist"] == pytest.approx(0.0, abs=1e-7)
        assert p["area_a_mm2"] > 0 and p["area_b_mm2"] > 0


def test_the_faces_of_one_sound_box_are_never_a_clash(tmp_path) -> None:
    """Every pair there either shares an edge (excluded) or is a long way off: a sound part must stay silent."""
    pairs = _pairs(_write(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape(), tmp_path / "box.brep"),
                   max_dist=1.0)
    assert [p for p in pairs if p["touching"]] == []


def test_a_near_miss_is_reported_and_is_not_called_a_contact(tmp_path) -> None:
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(10.5, 0, 0), 10.0, 10.0, 10.0).Shape()   # 0.5 mm apart
    pairs = _pairs(_write(_compound(a, b), tmp_path / "near.brep"))
    near = [p for p in pairs if not p["touching"]]
    assert near and min(p["dist"] for p in near) == pytest.approx(0.5, abs=1e-6)
    assert all(not p["touching"] for p in near)


def test_coincident_duplicate_faces_are_a_contact_with_equal_areas(tmp_path) -> None:
    """A11 found exactly this on 7e573047c1a6 (faces 108/109, identical area 9032.0789 mm2)."""
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    same = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    pairs = _pairs(_write(_compound(box, same), tmp_path / "dup.brep"), max_dist=1.0)
    twins = [p for p in pairs if p["touching"] and p["area_a_mm2"] == pytest.approx(p["area_b_mm2"])]
    assert twins, "two copies of one face must come back as a contact of equal areas"


# --- A12: the WHOLE path, kernel -> find_sites -> site graph -> candidate ------------------------------------------
#
# Lane C12 found the contact path dead on the tip: the kernel measured `touching` / `area_a_mm2` / `area_b_mm2` and
# `sites._attach_graphs` rebuilt every clash entry without them, so no contact row could ever be proposed in
# production. Nothing covered that hop - the kernel test checked the kernel's pairs, and the unit test handed the
# sweep a graph with `touching` already filled in. This test spans the whole path on real geometry, so a future
# change that drops the keys anywhere between the two fails here.


def _verdict_failing_on(entities: list[str], ws) -> object:
    """A full verdict whose single panel member failed, located on `entities` (as C5's members report)."""
    from meshpipeline.cad.repair_v4.verdict.assemble import Verdict
    from meshpipeline.cad.repair_v4.verdict.panel import Located, MemberResult
    from meshpipeline.cad.repair_v4.verdict.valid import ValidReport

    member = MemberResult("gmsh_default", "fail", "low_quality", "x",
                          [Located(ws.ids[e], None, f"surface {i}") for i, e in enumerate(entities, start=1)])
    return Verdict(ValidReport(True, [], [], "mm", 1, True), [member], None, False, "full")


def test_a_measured_contact_reaches_the_site_graph_and_becomes_a_candidate(tmp_path) -> None:
    """kernel -> sites -> sweep, on two boxes sharing a face region: the clash the kernel measured must arrive with
    `touching` set, and the sweep must turn it into the ops a zero gap allows."""
    from meshpipeline.cad.repair_v4.ops import validate_call
    from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
    from meshpipeline.cad.repair_v4.sites import find_sites
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace

    # face-to-face contact: the second box starts exactly where the first ends, so the touching faces share no edge
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(10.0, 2.0, 2.0), 6.0, 6.0, 6.0).Shape()
    brep = _write(_compound(a, b), tmp_path / "contact.brep")

    pool = WorkerPool(1)
    try:
        ws = Workspace.from_brep(pool, brep)
        faces = sorted((s for s in ws.ids if s[0] == "F"), key=lambda s: int(s[1:]))
        sites = find_sites(pool, ws, _verdict_failing_on(faces[:2], ws), h_mm=2.0)
        assert sites, "a located failure must make a site"

        clashes = [c for s in sites for c in (s.graph.get("clashes") or [])]
        assert clashes, f"the kernel measures clashes on this part: {[s.graph.keys() for s in sites]}"
        # THE DEFECT: every entry must carry the keys the kernel measured, all the way to here
        for c in clashes:
            assert "touching" in c, f"the clash lost `touching` between the kernel and the site: {c}"
        touching = [c for c in clashes if c["touching"]]
        assert touching, f"the gap-0 pair must arrive labelled as a contact: {clashes}"
        assert any(c.get("area_a_mm2") is not None for c in touching), (
            "the proximity pass measures both areas, so the smaller face can be named")

        gen = DetGenerator()
        gen.bind_pool(pool)
        state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=sites, depth=0,
                            parent=None, via=None)
        cands = gen.propose(state, 60)
        dups = [c for c in cands if c.op == "remove_duplicate_faces"]
        assert dups, f"a contact must propose remove_duplicate_faces: {sorted({c.op for c in cands})}"
        for c in dups:   # and the proposal must be one the op itself accepts
            assert validate_call(c.op, list(c.targets), dict(c.params), ws) == [], (c.targets, c.params)
            assert c.params["pairs"], "the op acts on pairs; an empty list is refused"
        aimed = [c for c in cands if c.op == "defeature" and c.params.get("source") == "located"]
        assert aimed, "the smaller face of the contact must be offered for removal, through the located source"
    finally:
        pool.close()
