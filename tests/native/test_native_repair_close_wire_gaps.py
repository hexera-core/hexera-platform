# Responsibility: Verify on the REAL kernel that close_wire_gaps closes a genuinely open wire (a
#                 synthetic face whose outer wire has a sub-tolerance gap, sharing an edge with a
#                 neighbour), measures the vertex move it made, leaves a valid shape the session
#                 keeps, declines a gap wider than the tolerance and a loop that is already closed,
#                 and report what the region loop does with it on the open-loop corpus file.
# Boundaries: the native tier; the claim, decline order, flag gating and the rules baseline's
#             fallback chain are unit-tested hermetically (tests/unit/cad/test_repair_close_wire_gaps.py).
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepBuilderAPI", reason="the CAD kernel is a native-tier dependency")

import meshpipeline.cad.repair.operations as operations  # noqa: E402
import meshpipeline.cad.repair.rules_author as rules_author_module  # noqa: E402
import meshpipeline.settings.cad_repair as rcfg  # noqa: E402
from meshpipeline.cad.repair.caps import measure  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for  # noqa: E402
from meshpipeline.cad.repair.operations import (  # noqa: E402
    AREA_DELTA,
    CloseWireGaps,
    box_of,
    catalogue,
    sub_shape,
)
from meshpipeline.cad.repair.rules_author import mesh_fallbacks, mesh_ops  # noqa: E402

#: The gap built into the synthetic wire, mm: far above any vertex tolerance (1e-7 here), well
#: inside the run tolerance the tests hand the operation.
GAP_MM = 0.05
#: The square's side, mm. Large enough that the closure's change to the part's box stays inside
#: the session's deviation cap (0.001 of the diagonal): measured on a 10 mm square, the same
#: closure moved the diagonal by 0.0042 of it and the session rightly rolled it back.
SIDE_MM = 100.0


@pytest.fixture
def closing_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_CLOSE_WIRE_GAPS", True)


def _open_shell(gap_mm: float = GAP_MM):
    """A shell of two planar faces sharing one edge. Face A, the SIDE_MM square in the XY plane, has
    an OPEN outer wire: its last edge ends `gap_mm` short of the first edge's start vertex, at a
    DIFFERENT vertex. Face B, in the XZ plane, shares A's first edge (and so the vertex that must
    move). Built with BRep_Builder, because BRepBuilderAPI_MakeWire would refuse the gap."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeVertex,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopoDS import TopoDS_Shell, TopoDS_Wire

    def vertex(x, y, z):
        return BRepBuilderAPI_MakeVertex(gp_Pnt(x, y, z)).Vertex()

    def edge(a, b):
        return BRepBuilderAPI_MakeEdge(a, b).Edge()

    s = SIDE_MM
    v0, v1, v2, v3 = vertex(0, 0, 0), vertex(s, 0, 0), vertex(s, s, 0), vertex(0, s, 0)
    loose = vertex(-gap_mm, 0, 0)
    shared = edge(v0, v1)
    builder = BRep_Builder()
    wire = TopoDS_Wire()
    builder.MakeWire(wire)
    for piece in (shared, edge(v1, v2), edge(v2, v3), edge(v3, loose)):
        builder.Add(wire, piece)
    face_a = BRepBuilderAPI_MakeFace(gp_Pln(), wire, True).Face()

    v4, v5 = vertex(s, 0, s), vertex(0, 0, s)
    wire_b = BRepBuilderAPI_MakeWire(shared, edge(v1, v4), edge(v4, v5), edge(v5, v0)).Wire()
    plane_b = gp_Pln(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0), gp_Dir(1, 0, 0)))
    face_b = BRepBuilderAPI_MakeFace(plane_b, wire_b, True).Face()

    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    builder.Add(shell, face_a)
    builder.Add(shell, face_b)
    return shell


def _ref(shape, index: int) -> EntityRef:
    diagonal = measure(shape).diagonal_mm
    return EntityRef(entity_type="face", entity_index=index,
                     fingerprint=fingerprint_of("face", box_of(sub_shape(shape, "face", index)),
                                                quantum=quantum_for(diagonal)))


def _name(on, index: int, *, diagonal_mm: float) -> str:
    """Face `index` of `on` named at a GIVEN diagonal's quantum - the pre-op part's, which is what
    `rebuilt` and the session's probe name every face by (the closure moves the box a little)."""
    return fingerprint_of("face", box_of(sub_shape(on, "face", index)),
                          quantum=quantum_for(diagonal_mm))


def _open_face_index(shape) -> int:
    open_faces = [i for i in (1, 2) if operations._wire_gap_mm(sub_shape(shape, "face", i)) > 0]
    assert len(open_faces) == 1, open_faces
    return open_faces[0]


def test_the_fixture_is_genuinely_open():
    # The guard on every test below: the gap is real, the kernel calls the shell invalid, and the
    # measured gap is the one built in.
    shape = _open_shell()
    index = _open_face_index(shape)
    assert operations._wire_gap_mm(sub_shape(shape, "face", index)) == pytest.approx(GAP_MM)
    assert not operations._is_valid(shape)


def test_an_open_wire_is_closed_by_a_measured_bounded_move(closing_on):
    shape = _open_shell()
    index = _open_face_index(shape)
    outcome = CloseWireGaps().apply(shape, (_ref(shape, index),), tolerance_mm=0.1)

    assert "declined" not in outcome.evidence, outcome.evidence
    # THE GAP IS CLOSED and the part is valid - the open face and its neighbour alike.
    closed = sub_shape(outcome.shape, "face", index)
    assert operations._wire_gap_mm(closed) == 0.0
    assert operations._is_valid(outcome.shape)
    after = measure(outcome.shape)
    assert after.faces == 2 and after.valid
    # THE MOVE IS MEASURED: FixConnected merges the two loose vertices at their midpoint, so each
    # moved half the gap - two vertices, the largest move GAP/2, never more than the gap.
    assert outcome.evidence["vertices_moved"] == 2
    assert outcome.evidence["max_vertex_move_mm"] == pytest.approx(GAP_MM / 2, rel=1e-6)
    assert outcome.evidence["max_vertex_move_mm"] <= GAP_MM
    assert outcome.evidence["gap_mm"] == pytest.approx(GAP_MM)
    # The merge leaves the merged vertex's tolerance raised to cover the move - reported, and
    # inside the session's CAD_REPAIR_MAX_TOLERANCE_MM (0.1) cap.
    assert outcome.evidence["max_tolerance_mm"] == pytest.approx(GAP_MM / 2, rel=1e-3)
    assert after.max_tolerance_mm <= rcfg.CAD_REPAIR_MAX_TOLERANCE_MM
    # The area moved by the sliver the merge added or removed - measured, and tiny.
    assert abs(outcome.evidence[AREA_DELTA]) < GAP_MM * 10.0
    # Both faces were rewritten: the closed face and the neighbour holding the moved vertex.
    assert outcome.evidence["faces_touched"] == 2
    assert len(outcome.rebuilt) == 2
    assert outcome.added == ()
    # The shared edge is still shared: the neighbour was re-stitched, not left behind.
    assert _shared_edges(outcome.shape) == 1


def test_the_input_is_never_written_into(closing_on):
    shape = _open_shell()
    index = _open_face_index(shape)
    vertices = [operations._vertices_of(shape)]
    points = _points(shape)
    CloseWireGaps().apply(shape, (_ref(shape, index),), tolerance_mm=0.1)
    assert _points(shape) == points
    assert operations._wire_gap_mm(sub_shape(shape, "face", index)) == pytest.approx(GAP_MM)
    assert len(operations._vertices_of(shape)) == len(vertices[0])


def test_the_session_keeps_the_closure(monkeypatch, closing_on):
    """A real session, real caps, real rescans. The localiser reports this open face as
    `invalid_brep` (BRepCheck UnorientableShape) - no BRepCheck status maps to `wire_gap` today -
    so the session is told the target carries the wire gap (`_carries`, the heal tests' seam);
    everything after that - the apply, the validity and accounting caps, the rescan - is real."""
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.session import RepairSession

    shape = _open_shell()
    index = _open_face_index(shape)
    ref = _ref(shape, index)
    session = RepairSession(shape)
    before = {(d.code, d.fingerprint) for d in session.defects()}
    assert (DefectCode.invalid_brep, ref.fingerprint) in before
    monkeypatch.setattr(session, "_carries", lambda targets, code: True)
    verdict = session.try_op(CloseWireGaps(), (ref,), DefectCode.wire_gap, tolerance_mm=0.1)
    assert verdict.kept, (verdict.reason, verdict.evidence)
    assert verdict.evidence["max_vertex_move_mm"] == pytest.approx(GAP_MM / 2, rel=1e-6)
    # The kernel's own complaint about the face is gone with the gap.
    assert not any(d.code is DefectCode.invalid_brep for d in session.defects())


def test_a_gap_wider_than_the_tolerance_is_declined_and_nothing_moves(closing_on):
    shape = _open_shell()
    index = _open_face_index(shape)
    outcome = CloseWireGaps().apply(shape, (_ref(shape, index),), tolerance_mm=0.04)
    assert outcome.evidence["declined"].startswith("gaps_exceed_tolerance"), outcome.evidence
    assert outcome.evidence["gap_mm"] == pytest.approx(GAP_MM)
    assert outcome.shape is shape


def test_a_closed_face_is_declined_not_closed(closing_on):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    outcome = CloseWireGaps().apply(shape, (_ref(shape, 1),), tolerance_mm=0.1)
    assert outcome.evidence["declined"].startswith("not_closed"), outcome.evidence
    assert outcome.shape is shape


def _curve_gap_shell(gap_mm: float = GAP_MM):
    """The same two faces, every vertex SHARED, but the shared edge's LINE starts `gap_mm` short of
    its start vertex: a pure curve gap on the edge both faces hold. Closing it reshapes that curve
    and moves no vertex, so a moved-vertex test would never see the neighbour it renames."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeVertex,
    )
    from OCP.Geom import Geom_Line
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS_Edge, TopoDS_Shell, TopoDS_Wire

    builder = BRep_Builder()

    def vertex(x, y, z):
        return BRepBuilderAPI_MakeVertex(gp_Pnt(x, y, z)).Vertex()

    def edge(a, b):
        return BRepBuilderAPI_MakeEdge(a, b).Edge()

    s = SIDE_MM
    v0, v1, v2, v3 = vertex(0, 0, 0), vertex(s, 0, 0), vertex(s, s, 0), vertex(0, s, 0)
    shared = TopoDS_Edge()
    builder.MakeEdge(shared, Geom_Line(gp_Pnt(-gap_mm, 0, 0), gp_Dir(1, 0, 0)), 1e-7)
    builder.Add(shared, v0.Oriented(TopAbs_FORWARD))
    builder.Add(shared, v1.Oriented(TopAbs_REVERSED))
    builder.Range(shared, 0.0, s + gap_mm)

    def wire_of(*pieces):
        wire = TopoDS_Wire()
        builder.MakeWire(wire)
        for piece in pieces:
            builder.Add(wire, piece)
        return wire

    face_a = BRepBuilderAPI_MakeFace(
        gp_Pln(), wire_of(shared, edge(v1, v2), edge(v2, v3), edge(v3, v0)), True).Face()
    v4, v5 = vertex(s, 0, s), vertex(0, 0, s)
    plane_b = gp_Pln(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0), gp_Dir(1, 0, 0)))
    face_b = BRepBuilderAPI_MakeFace(
        plane_b, wire_of(shared, edge(v1, v4), edge(v4, v5), edge(v5, v0)), True).Face()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    builder.Add(shell, face_a)
    builder.Add(shell, face_b)
    return shell


def test_a_pure_curve_gap_on_a_shared_edge_names_the_renamed_neighbour_in_rebuilt(closing_on):
    shape = _curve_gap_shell()
    # Both faces hold the gapped edge; the target is face 1, the neighbour face 2.
    before = {i: _ref(shape, i).fingerprint for i in (1, 2)}
    outcome = CloseWireGaps().apply(shape, (_ref(shape, 1),), tolerance_mm=0.1)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert outcome.evidence["vertices_moved"] == 0          # a curve reshaped, no vertex moved
    assert operations._is_valid(outcome.shape)
    diagonal = measure(shape).diagonal_mm
    after = {i: _name(outcome.shape, i, diagonal_mm=diagonal) for i in (1, 2)}
    # THE NEIGHBOUR WAS RENAMED, and `rebuilt` says so - structurally, not by a vertex test.
    assert after[2] != _name(shape, 2, diagonal_mm=diagonal) == before[2]
    assert set(outcome.rebuilt) == {after[1], after[2]}
    assert outcome.evidence["faces_touched"] == 2


def _stub_probe(monkeypatch, shape, target_index: int, *, plant_on_neighbour: bool):
    """`mesh_probe.mesh_defects` stubbed: the first (pre-op) probe reports gmsh's open loop on the
    target, in the 2D stage; every later probe reports nothing - or, planted, an overlapping-facets
    finding on the NEIGHBOUR face of whatever shape it is handed (its post-op name)."""
    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect

    neighbour = 2 if target_index == 1 else 1
    calls: list[int] = []

    def finding(on, index, code, diagonal_mm):
        return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                            entity_index=index, message=code.value,
                            fingerprint=_name(on, index, diagonal_mm=diagonal_mm),
                            location=box_of(sub_shape(on, "face", index)),
                            measurements={"stage": mesh_probe.STAGE_2D})

    def probe(on, *, diagonal_mm, budget_s, within=None):
        calls.append(1)
        if len(calls) == 1:
            return (finding(on, target_index, DefectCode.mesh_open_loop, diagonal_mm),)
        if plant_on_neighbour:
            return (finding(on, neighbour, DefectCode.mesh_overlapping_facets, diagonal_mm),)
        return ()

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(mesh_probe, "mesh_defects", probe)
    return calls


def test_the_session_keeps_an_open_loop_closure_with_both_faces_rebuilt(monkeypatch, closing_on):
    """THE PRODUCTION PATH: a real session judging the mesh code MESH_OPS aims the op at."""
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.session import RepairSession

    shape = _open_shell()
    index = _open_face_index(shape)
    _stub_probe(monkeypatch, shape, index, plant_on_neighbour=False)
    session = RepairSession(shape)
    ref = _ref(shape, index)
    assert any(d.code is DefectCode.mesh_open_loop and d.fingerprint == ref.fingerprint
               for d in session.defects())
    applied: list = []
    real = CloseWireGaps.apply

    def spy(self, *args, **kwargs):
        applied.append(real(self, *args, **kwargs))
        return applied[-1]

    monkeypatch.setattr(CloseWireGaps, "apply", spy)
    verdict = session.try_op(CloseWireGaps(), (ref,), DefectCode.mesh_open_loop, tolerance_mm=0.1)
    assert verdict.kept, (verdict.reason, verdict.evidence)
    (outcome,) = applied
    diagonal = measure(shape).diagonal_mm
    both = {_name(outcome.shape, i, diagonal_mm=diagonal) for i in (1, 2)}
    assert set(outcome.rebuilt) == both
    # Both names are NEW: neither face crossed the closure unchanged.
    assert not both & {_name(shape, i, diagonal_mm=diagonal) for i in (1, 2)}


def test_a_mesh_finding_on_the_renamed_neighbour_is_damage_not_revealed(monkeypatch, closing_on):
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.session import RepairSession

    shape = _open_shell()
    index = _open_face_index(shape)
    _stub_probe(monkeypatch, shape, index, plant_on_neighbour=True)
    session = RepairSession(shape)
    verdict = session.try_op(CloseWireGaps(), (_ref(shape, index),), DefectCode.mesh_open_loop,
                             tolerance_mm=0.1)
    # The neighbour's post-op name is in `rebuilt` and did not exist before: the op's own doing.
    assert not verdict.kept
    assert verdict.reason == "new_defect", (verdict.reason, verdict.delta)
    assert not (verdict.delta or {}).get("revealed")


def test_a_gap_inside_the_run_tolerance_but_past_the_session_caps_is_rolled_back(monkeypatch,
                                                                                closing_on):
    """THE TOLERANCE BOUNDARY: a 0.15 mm gap under a 0.2 mm run tolerance passes the op's own
    bound - and the session's caps, not the op, are what refuse it. Measured: the deviation cap,
    the part's box moved by 0.00125 of its diagonal against the 0.001 cap (each vertex 0.075 mm)."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.session import RepairSession

    shape = _open_shell(gap_mm=0.15)
    index = _open_face_index(shape)
    ref = _ref(shape, index)
    outcome = CloseWireGaps().apply(shape, (ref,), tolerance_mm=0.2)
    assert "declined" not in outcome.evidence, outcome.evidence
    session = RepairSession(shape)
    session._carries = lambda targets, code: True   # the heal tests' seam (see the session test)
    verdict = session.try_op(CloseWireGaps(), (ref,), DefectCode.wire_gap, tolerance_mm=0.2)
    assert not verdict.kept
    assert verdict.reason.startswith("cap:"), (verdict.reason, verdict.evidence)
    assert "diagonal" in verdict.reason


def _points(shape) -> list:
    from OCP.BRep import BRep_Tool
    from OCP.TopoDS import TopoDS

    out = []
    for vertex in operations._vertices_of(shape):
        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertex))
        out.append((p.X(), p.Y(), p.Z()))
    return sorted(out)


def _shared_edges(shape) -> int:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, owners)
    return sum(1 for i in range(1, owners.Extent() + 1) if owners.FindFromIndex(i).Size() > 1)


# THE REAL MESH FAILURE

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()


def _corpus(name):
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


#: The open-loop family's single-solid file: 33 faces, the failure located on face 29 (a cylinder
#: the kernel calls valid - devtools/validation/evidence/2026-11-15-open-loop-reach).
_OPEN_LOOP = "abc/00508775_58991615a878da0f8225af98_step_009.step"


def test_close_wire_gaps_aimed_by_hand_at_the_corpus_face_declines_not_closed(closing_on):
    """The evidence's finding, re-measured through the operation: face 29 is closed at its own
    joint precision, so there is no gap to close and the operation says so instead of keeping a
    no-op."""
    from meshpipeline.cad.repair.brep import _read_shape

    shape = _read_shape(_corpus(_OPEN_LOOP))
    assert operations._wire_gap_mm(sub_shape(shape, "face", 29)) == 0.0
    outcome = CloseWireGaps().apply(shape, (_ref(shape, 29),), tolerance_mm=0.1)
    assert outcome.evidence["declined"].startswith("not_closed"), outcome.evidence


def test_corpus_open_loop_file_under_the_region_loop(monkeypatch, closing_on, capsys):
    """THE END-TO-END OUTCOME on the real open-loop file, measured 2026-11-19 (darwin-arm64,
    gmsh 4.15.2): run 10.1 s, stopped `unmeshable`.

    Region loop, probe live, flag on: the rules baseline proposes close_wire_gaps for the 2D-stage
    open loop on face 29, the operation declines `not_closed` (no gap is measurable: the face is
    closed at its own tolerance), the fallback `fix_face_boundary` is tried and rolled back
    `target_remains`, and the open loop is still on face 29 (2d) after. Asserted as measured, so a
    change in either direction is seen here.
    """
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.loop import LoopBudget
    from meshpipeline.cad.repair.region_loop import run_regions
    from meshpipeline.cad.repair.rules_author import rules_author

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    # BELOW THE RUN'S 300 s: the walk keeps one probe timeout + 10 s in hand for its final probe.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 60.0)
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(close_wire_gaps=True))
    monkeypatch.setattr(rules_author_module, "MESH_FALLBACKS",
                        mesh_fallbacks(close_wire_gaps=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(close_wire_gaps=True)})

    shape = _read_shape(_corpus(_OPEN_LOOP))
    started = time.monotonic()
    author = rules_author(shape, original=measure(shape))
    run = run_regions(shape, author=author, tolerance_mm=0.1,
                      budget=LoopBudget(max_ops=6, stall_limit=2, timeout_s=300.0))
    elapsed = time.monotonic() - started
    verdicts = [(v.op, v.status, v.reason) for p in run.packages for v in p.verdicts]
    loops = [d for d in run.defects_after if d.code is DefectCode.mesh_open_loop]
    with capsys.disabled():
        print(f"\n[{_OPEN_LOOP}] run {elapsed:.1f}s stopped={run.stopped_because} "
              f"verdicts={verdicts} refusals={[r['reason'] for r in author.refusals]} "
              f"open_loops_after={[(d.entity_index, d.measurements.get('stage')) for d in loops]}")

    assert elapsed < 300.0
    proposed = [v for v in verdicts if v[0] == "close_wire_gaps"]
    assert proposed, verdicts
    assert all(status != "kept" for _, status, _ in proposed), proposed
    assert loops, "the open loop persists: the face is closed at its own tolerance"
