# Responsibility: Verify the scratch clone and the `edit` tool - that an edit can only ever reach
#                 a throwaway copy, that a scratch session can never commit, that the kept-edit log
#                 records only what actually survived, and that the read-only guarantee is intact.
"""Hermetic: no kernel. `_clone` and the count seams are replaced, the operation catalogue is a
double, and the session's scanner and measurer are scripted - so what is tested is the RULES."""
from __future__ import annotations

from dataclasses import replace

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair import model_author
from meshpipeline.cad.repair import session as sess
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityMapping, EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region

ORIGINAL = Measure(valid=False, solids=1, shells=1, faces=20, edges=60, vertices=40,
                   diagonal_mm=200.0, max_tolerance_mm=0.01,
                   surface_area_mm2=1000.0, volume_mm3=8000.0)


class _Shape:
    """A shape stand-in with identity, so "the clone is not the part" is checkable."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<{self.name}>"


def _defect(code=DefectCode.small_edge, *, fingerprint="edge:aaaa", index=1, stage=None,
            kind="edge") -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.warning, entity_type=kind,
                        entity_index=index, message="x", fingerprint=fingerprint,
                        measurements=({"stage": stage} if stage is not None else {}),
                        location={"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (1.0, 1.0, 1.0)})


class _Op:
    """A catalogue operation double: `plan_entry` reads its name and `answers`, the session its
    `apply`. Records every call, because "never applied" is most of what is asserted here."""

    scope = 1

    def __init__(self, name="fix_small_edges", answers=(DefectCode.small_edge,), *,
                 outcomes=None, raises=None) -> None:
        self.name = name
        self.answers = frozenset(answers)
        self._outcomes = list(outcomes or [])
        self._raises = raises
        self.calls: list = []

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls.append(shape)
        if self._raises is not None:
            raise self._raises
        if self._outcomes:
            return self._outcomes.pop(0)
        return OpOutcome(shape=_Shape("edited"))


def _region(fingerprints=("edge:aaaa",)) -> Region:
    return Region(index=0, fingerprints=frozenset(fingerprints), bbox_min=(0.0, 0.0, 0.0),
                  bbox_max=(2.0, 2.0, 2.0), face_indices=frozenset({1, 2}))


def _cleared_outcome(fingerprint="edge:aaaa", shape_name="edited", evidence=None) -> OpOutcome:
    return OpOutcome(shape=_Shape(shape_name),
                     mapping=EntityMapping(moved={}, gone=frozenset({fingerprint}),
                                           basis="kernel"),
                     evidence=dict(evidence or {}))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Nothing in this file may reach OCP, and nothing may read the real catalogue."""
    monkeypatch.setattr(it, "_clone", lambda shape: _Shape(f"clone-of-{shape.name}"))
    monkeypatch.setattr(it, "_counts_of", lambda shape: {"faces": 20, "solids": 1})
    monkeypatch.setattr(it, "_solids_of", lambda shape: 1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)


def _catalogue(monkeypatch, *ops) -> None:
    monkeypatch.setattr(model_author, "op_catalogue", lambda: tuple(ops))


def _scratch(*, defects, scans=((), True), source=None, reserved=frozenset(), measures=None):
    """A `Scratch` over `source`, whose session's scanner and measurer are scripted.

    `defects` SEEDS the session's list - the clone starts knowing what the part knows - so
    `scans` is the script of RE-INSPECTIONS, one per edit that reaches one. A scratch that is
    never edited consumes none of it.
    """
    scan_queue = list(scans if isinstance(scans, list) else [scans])
    measure_queue = list(measures or [])
    spied = {"scans": 0, "measures": 0}

    def scanner(_shape, valid=None):
        spied["scans"] += 1
        return scan_queue.pop(0) if len(scan_queue) > 1 else scan_queue[0]

    def measurer(_shape):
        spied["measures"] += 1
        return measure_queue.pop(0) if len(measure_queue) > 1 else (
            measure_queue[0] if measure_queue else ORIGINAL)

    scratch = it.Scratch.of(source or _Shape("part"), original=ORIGINAL, defects=tuple(defects),
                            reserved=frozenset(reserved), measurer=measurer, scanner=scanner)
    return scratch, spied


def _workspace(scratch, *, defects, region=None, tolerance_mm=0.01,
               rims=frozenset()) -> it.Workspace:
    region = region or _region(tuple(d.fingerprint for d in defects))
    return it.Workspace(region=region, defects=tuple(defects), attempts=(),
                        shape=scratch.shape, original=ORIGINAL,
                        baseline_shape=scratch.shape,
                        editor=it.editor(scratch, tolerance_mm=tolerance_mm, rims=rims))


# --- the read-only guarantee -------------------------------------------------------------------

def test_read_only_still_contains_no_mutating_tool():
    """THE GUARANTEE. `READ_ONLY` is what every reader trusts when it says a tool cannot change a
    shape; `edit` is a sibling, so adding it here would withdraw that silently."""
    assert [spec.name for spec in it.READ_ONLY] == ["look", "measure", "probe"]
    assert all(spec.read_only for spec in it.READ_ONLY)
    assert it.EDIT not in it.READ_ONLY
    assert "edit" not in {spec.name for spec in it.READ_ONLY}
    assert it.EDIT.read_only is False
    assert [spec.name for spec in it.EDITING] == ["edit"]
    assert it.ALL_TOOLS == it.READ_ONLY + it.EDITING


def test_edit_is_declared_to_both_transports():
    schemas = {s.name: s for s in it.tool_schemas()}
    assert "edit" in schemas
    properties = schemas["edit"].parameters["properties"]
    assert properties["op"] == {"type": "string"}
    assert properties["reset"] == {"type": "boolean"}
    # No unconditional requirement: the form is {op, fingerprint} OR {reset}.
    assert schemas["edit"].parameters["required"] == []
    prose = it.tool_prose()
    assert '- "edit" args:' in prose
    assert "reset (boolean, optional)" in prose


def test_edit_without_a_clone_is_refused_not_raised():
    ws = it.Workspace(region=_region(), defects=(_defect(),), attempts=(), shape=_Shape("part"),
                      original=ORIGINAL, baseline_shape=_Shape("part"))
    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
                      budget_s=1.0)
    assert out.ok is False
    assert "scratch" in out.error
    assert out.data == {}


def test_a_non_boolean_reset_is_refused_by_name():
    ws = it.Workspace(region=_region(), defects=(_defect(),), attempts=(), shape=_Shape("part"),
                      original=ORIGINAL, baseline_shape=_Shape("part"))
    out = it.run_tool(ws, "edit", {"reset": 1}, budget_s=1.0)
    assert out.ok is False
    assert "'reset'" in out.error and "boolean" in out.error


# --- a scratch session can never commit --------------------------------------------------------

def _package(op, defect, region) -> TargetPackage:
    return TargetPackage(
        region=region, intent="close it",
        plan=({"op": op, "targets": op.targets_for(defect), "defect_code": defect.code,
               "tolerance_mm": 0.01},),
        predicate=Predicate(cleared=frozenset({defect.fingerprint}),
                            reclassified=frozenset(), unchanged=frozenset(),
                            max_area_delta_mm2=1.0, max_volume_delta_mm3=1.0),
        budget=PackageBudget(max_ops=6, timeout_s=60.0, model_calls=1, deviation_mm=0.1))


def test_scratch_session_cannot_commit():
    defect = _defect()
    op = _Op(outcomes=[_cleared_outcome()])
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)])
    package = _package(op, defect, _region())

    verdict = scratch.session.try_package(package)

    assert verdict.status == sess.REVERTED
    assert verdict.reason == "refused: a scratch session cannot commit"
    assert op.calls == [], "nothing may be applied: the refusal is before the plan is read"
    assert scratch.session.log == [], "and nothing may be reviewed or recorded as an op"
    assert scratch.session.packages == [verdict]


def test_the_same_package_commits_on_a_real_session():
    """THE GUARD IS CONDITIONAL. Without this, a guard that refused every package would pass the
    test above and break every repair."""
    defect = _defect()
    op = _Op(outcomes=[_cleared_outcome()])
    scans = [((defect,), True), ((), True)]
    real = sess.RepairSession(_Shape("part"), measurer=lambda _s: ORIGINAL,
                              scanner=lambda _s, valid=None: scans.pop(0)
                              if len(scans) > 1 else scans[0])
    assert real.is_scratch is False

    verdict = real.try_package(_package(op, defect, _region()))

    assert len(op.calls) == 1
    assert verdict.status == sess.COMMITTED
    assert verdict.reason != "refused: a scratch session cannot commit"


def test_scratch_does_not_measure_or_scan_first():
    defect = _defect()
    scratch, spied = _scratch(defects=(defect,), scans=[((), True)])

    assert spied == {"scans": 0, "measures": 0}, "opening a clone must cost nothing"
    assert scratch.session.scan_count == 0
    assert scratch.original is ORIGINAL
    assert scratch.session.original is ORIGINAL, "the UPLOAD's measure caps the exploration"
    # The seeded list is the clone's list, served without a scan.
    assert scratch.defects() == (defect,)
    assert spied["scans"] == 0


# --- the clone is the only thing an edit reaches -----------------------------------------------

def test_an_edit_leaves_the_original_shape_untouched(monkeypatch):
    """THE PILLAR. The real shape must be unreachable from an edit until the conclusion."""
    defect = _defect()
    op = _Op(outcomes=[_cleared_outcome()])
    _catalogue(monkeypatch, op)
    part = _Shape("part")
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)], source=part)
    clone = scratch.shape
    assert clone is not part and clone.name == "clone-of-part"
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
                      budget_s=1.0)

    assert out.ok is True, out.error
    assert out.data["status"] == sess.KEPT
    # The operation was handed the CLONE, never the part.
    assert op.calls == [clone]
    assert part.name == "part"
    assert scratch.shape is not part
    assert scratch.shape.name == "edited"
    # And the workspace now looks at the edited clone, not the part.
    assert ws.shape is scratch.shape
    assert ws.baseline_shape is clone


def test_the_guards_read_the_clone_not_the_part(monkeypatch):
    """`PackageGuards.solids_of` must read the CLONE: a fill earlier in the rehearsal can create a
    solid, and judging `fragment_bodies` against the part's count would misjudge it."""
    defect = _defect(DefectCode.mesh_overlapping_facets, fingerprint="face:bbbb", stage="3d",
                     kind="face")
    op = _Op("fragment_bodies", (DefectCode.mesh_overlapping_facets,))
    _catalogue(monkeypatch, op)
    scratch, _ = _scratch(defects=(defect,), source=_Shape("part"))
    seen: list = []
    monkeypatch.setattr(it, "_solids_of", lambda shape: seen.append(shape) or 1)
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fragment_bodies", "fingerprint": "face:bbbb"},
                      budget_s=1.0)

    assert out.ok is False
    assert "needs_multiple_bodies" in out.error
    assert seen and all(s is scratch.shape for s in seen)
    assert op.calls == []


# --- refusals reuse the author's own validation ------------------------------------------------

def test_edit_refusal_reuses_the_precondition_map(monkeypatch):
    """A 2d-stage operation aimed at a 3d-stage defect: refused by `model_author`'s own map, by
    its own name, with nothing applied."""
    defect = _defect(DefectCode.mesh_overlapping_facets, fingerprint="face:bbbb", stage="3d",
                     kind="face")
    op = _Op("rebuild_face", (DefectCode.mesh_overlapping_facets,))
    _catalogue(monkeypatch, op)
    scratch, _ = _scratch(defects=(defect,))
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "rebuild_face", "fingerprint": "face:bbbb"},
                      budget_s=1.0)

    assert out.ok is False
    assert out.error.startswith("stage_precondition")
    assert "2d" in out.error
    assert op.calls == [], "an invalid proposal costs no execution"
    assert scratch.edits == []


def test_edit_cannot_aim_at_a_held_fingerprint(monkeypatch):
    defect = _defect()
    op = _Op(outcomes=[_cleared_outcome()])
    _catalogue(monkeypatch, op)
    scratch, _ = _scratch(defects=(defect,),
                          reserved=frozenset({DefectCode.small_edge}))
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
                      budget_s=1.0)

    assert out.ok is False
    assert "held for review" in out.error
    assert "edge:aaaa" in out.error
    assert op.calls == []


def test_an_unknown_op_and_a_foreign_fingerprint_are_named(monkeypatch):
    defect = _defect()
    _catalogue(monkeypatch, _Op())
    scratch, _ = _scratch(defects=(defect,))
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "sew_everything", "fingerprint": "edge:aaaa"},
                      budget_s=1.0)
    assert out.ok is False and "sew_everything" in out.error and "catalogue" in out.error

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:zzzz"},
                      budget_s=1.0)
    assert out.ok is False and "edge:zzzz" in out.error


def test_the_edit_form_refusal_names_the_keys(monkeypatch):
    defect = _defect()
    _catalogue(monkeypatch, _Op())
    scratch, _ = _scratch(defects=(defect,))
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges"}, budget_s=1.0)
    assert out.ok is False
    assert "fingerprint" in out.error

    out = it.run_tool(ws, "edit", {"reset": True, "op": "fix_small_edges"}, budget_s=1.0)
    assert out.ok is False
    assert "reset" in out.error

    out = it.run_tool(ws, "edit", {"reset": False}, budget_s=1.0)
    assert out.ok is False
    assert "reset" in out.error


# --- the kept log ------------------------------------------------------------------------------

def test_kept_log_skips_declined_and_rolled_back(monkeypatch):
    """[declined, kept, rolled_back, kept] -> `kept()` holds the two keeps, in order."""
    a, b = _defect(fingerprint="edge:aaaa"), _defect(fingerprint="edge:bbbb", index=2)
    op = _Op(outcomes=[
        OpOutcome(shape=_Shape("untouched"), evidence={"declined": "nothing to do here"}),
        _cleared_outcome("edge:aaaa", "k1"),
        OpOutcome(shape=_Shape("spoiled")),          # the defect remains -> rolled back
        _cleared_outcome("edge:bbbb", "k2"),
    ])
    _catalogue(monkeypatch, op)
    # The declined edit never reaches a re-inspection, so there are three: the keep that
    # clears aaaa, the rollback that still sees bbbb, and the keep that clears bbbb.
    scans = [((b,), True), ((b,), True), ((), True)]
    scratch, _ = _scratch(defects=(a, b), scans=scans)
    ws = _workspace(scratch, defects=(a, b))

    calls = [{"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
             {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
             {"op": "fix_small_edges", "fingerprint": "edge:bbbb"},
             {"op": "fix_small_edges", "fingerprint": "edge:bbbb"}]
    statuses = [it.run_tool(ws, "edit", args, budget_s=1.0).data["status"] for args in calls]

    assert statuses == [sess.ROLLED_BACK, sess.KEPT, sess.ROLLED_BACK, sess.KEPT]
    assert [e.reason.startswith("declined:") for e in scratch.edits] == [True, False, False, False]
    assert len(scratch.edits) == 4
    assert scratch.kept() == ({"op": "fix_small_edges", "fingerprint": "edge:aaaa",
                               "defect_code": "small_edge"},
                              {"op": "fix_small_edges", "fingerprint": "edge:bbbb",
                               "defect_code": "small_edge"})
    assert scratch.shape.name == "k2", "a rolled-back edit left the clone where the keep put it"


def test_a_kept_edit_reports_what_cleared_and_what_remains(monkeypatch):
    a, b = _defect(fingerprint="edge:aaaa"), _defect(fingerprint="edge:bbbb", index=2)
    new = _defect(fingerprint="edge:cccc", index=3)
    op = _Op(outcomes=[_cleared_outcome("edge:aaaa")])
    _catalogue(monkeypatch, op)
    scratch, _ = _scratch(defects=(a, b), scans=[((b, new), True)])
    ws = _workspace(scratch, defects=(a, b),
                    region=_region(("edge:aaaa", "edge:bbbb", "edge:cccc")))

    data = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
                       budget_s=1.0).data

    assert data["status"] == sess.KEPT and data["kept"] is True
    assert data["cleared"] == ["edge:aaaa"]
    assert data["remaining"] == ["edge:bbbb"]
    assert data["new"] == ["edge:cccc"]
    assert data["counts"] == {"faces": 20, "solids": 1}
    assert data["kept_edits"] == 1
    assert data["declared"] == {}


def test_a_kept_edit_reports_its_declarations(monkeypatch):
    defect = _defect()
    outcome = OpOutcome(shape=_Shape("edited"),
                        mapping=EntityMapping(moved={}, gone=frozenset({"edge:aaaa"}),
                                              basis="kernel"),
                        rebuilt=("face:1",),
                        evidence={"area_delta_mm2": 15.0})
    op = _Op(outcomes=[outcome])
    _catalogue(monkeypatch, op)
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)])
    ws = _workspace(scratch, defects=(defect,))

    data = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
                       budget_s=1.0).data

    assert data["status"] == sess.KEPT
    assert data["declared"]["area_delta_mm2"] == 15.0
    assert scratch.session.accounting.declared_area_mm2 == 15.0, (
        "the declaration accounting works on the clone exactly as it does on the part")
    assert ws.edited_faces == ("face:1",), "`measure deviation` is told which faces moved"


def test_edit_cap_is_max_ops(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 2)
    fingerprints = [f"edge:{i}" for i in range(4)]
    defects = [_defect(fingerprint=fp, index=i + 1) for i, fp in enumerate(fingerprints)]
    op = _Op(outcomes=[_cleared_outcome(fp, f"k{i}") for i, fp in enumerate(fingerprints)])
    _catalogue(monkeypatch, op)
    # Each rescan clears the one aimed at and leaves the rest.
    scans = [(tuple(defects[i + 1:]), True) for i in range(4)]
    scratch, _ = _scratch(defects=tuple(defects), scans=scans)
    ws = _workspace(scratch, defects=tuple(defects), region=_region(tuple(fingerprints)))

    results = [it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": fp}, budget_s=1.0)
               for fp in fingerprints]

    assert [r.ok for r in results] == [True, True, False, False]
    assert len(scratch.kept()) == 2
    third = results[2].error
    assert "2" in third, "the refusal names the BOUND that was violated"
    assert "CAD_REPAIR_MAX_OPS" in third or "kept edit" in third
    assert len(op.calls) == 2, "the cap refuses before the kernel is called"


# --- reset ------------------------------------------------------------------------------------

def test_reset_clears_edits_and_restores_the_copy(monkeypatch):
    defect = _defect()
    op = _Op(outcomes=[_cleared_outcome()])
    _catalogue(monkeypatch, op)
    part = _Shape("part")
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)], source=part)
    ws = _workspace(scratch, defects=(defect,))
    it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"}, budget_s=1.0)
    assert scratch.shape.name == "edited" and len(scratch.kept()) == 1

    out = it.run_tool(ws, "edit", {"reset": True}, budget_s=1.0)

    assert out.ok is True, out.error
    assert out.data["reset"] is True
    assert scratch.edits == [] and scratch.kept() == ()
    # A FRESH COPY of the SAME source, and the seeded defect list back as it was.
    assert scratch.shape.name == "clone-of-part"
    assert scratch.shape is not part
    assert scratch.defects() == (defect,)
    assert scratch.session.is_scratch is True
    assert scratch.session.log == []
    assert scratch.session.accounting.declared_area_mm2 == 0.0
    assert ws.shape is scratch.shape and ws.edited_faces == ()
    assert ws.baseline_shape is scratch.shape


# --- the strict-schema filler -------------------------------------------------------------------
# A strict function-calling transport makes the model send EVERY declared property. Measured on a
# live run at 0ff8db35 (CAD_REPAIR_TIMEOUT_S=420): every edit arrived as
# {op, fingerprint, defect_code, reset: false} and every reset as
# {op: "", fingerprint: "", defect_code: "", reset: true}, both were refused as a mixture, the
# region ran away on identical calls and no package was ever concluded. `reset: false` and empty
# strings are the transport's "unset", not a second form.
def test_an_edit_sent_with_reset_false_is_an_edit(monkeypatch):
    defect = _defect()
    _catalogue(monkeypatch, _Op(outcomes=[_cleared_outcome()]))
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)])
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa",
                                   "defect_code": "", "reset": False}, budget_s=1.0)

    assert out.ok is True, out.error
    assert len(scratch.kept()) == 1


def test_a_reset_sent_with_empty_string_fillers_is_a_reset(monkeypatch):
    defect = _defect()
    _catalogue(monkeypatch, _Op(outcomes=[_cleared_outcome()]))
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)], source=_Shape("part"))
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "", "fingerprint": "", "defect_code": "",
                                   "reset": True}, budget_s=1.0)

    assert out.ok is True, out.error
    assert out.data["reset"] is True


def test_a_real_mixture_is_still_refused(monkeypatch):
    defect = _defect()
    _catalogue(monkeypatch, _Op())
    scratch, _ = _scratch(defects=(defect,))
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa",
                                   "reset": True}, budget_s=1.0)
    assert out.ok is False and "reset" in out.error


# --- the author is judged on the MEASURED delta, so it is told the measured delta ---------------
# FixSmallFaces reports `area_removed_mm2` (the removed faces' own area) and deliberately no
# `area_delta_mm2`, so the accounting declares 0 and `package.evaluate` judges the WHOLE movement.
# Measured live on 0053_00531146: the tool said 0.48387, the model claimed exactly that, the shape
# moved 64.8064, and the package was reverted. The author was judged on a number it never saw.
def test_an_edit_reports_the_measured_area_and_volume_delta(monkeypatch):
    defect = _defect()
    before = ORIGINAL
    after = replace(before, surface_area_mm2=before.surface_area_mm2 + 64.8064,
                    volume_mm3=before.volume_mm3 + 3.5)
    _catalogue(monkeypatch, _Op(outcomes=[_cleared_outcome(evidence={
        "faces_removed": 1, "area_removed_mm2": 0.48387})]))
    scratch, _ = _scratch(defects=(defect,), scans=[((), True)], measures=[before, after])
    ws = _workspace(scratch, defects=(defect,))

    out = it.run_tool(ws, "edit", {"op": "fix_small_edges", "fingerprint": "edge:aaaa"},
                      budget_s=1.0)

    assert out.ok is True, out.error
    # The op's own figure is KEPT - the reviewer reads it - and the measured movement is beside it.
    assert out.data["declared"]["area_removed_mm2"] == 0.48387
    assert out.data["measured_area_delta_mm2"] == pytest.approx(64.8064)
    assert out.data["measured_volume_delta_mm3"] == pytest.approx(3.5)


def test_the_edit_prose_tells_the_author_to_claim_the_measured_delta():
    prose = it.tool_prose()
    assert "measured_area_delta_mm2" in prose
    assert "measured_volume_delta_mm3" in prose


def test_a_package_claiming_the_measured_delta_is_accepted_by_the_predicate():
    """THE POINT OF REPORTING IT. The same movement that reverted the live package passes once the
    claim is the measured delta rather than the operation's `area_removed_mm2`."""
    from meshpipeline.cad.repair import package as pkg

    before = ORIGINAL
    after = replace(before, surface_area_mm2=before.surface_area_mm2 + 64.8064)
    copied = Predicate(cleared=frozenset({"edge:aaaa"}), unchanged=frozenset(),
                       reclassified=frozenset(),
                       max_area_delta_mm2=0.48387, max_volume_delta_mm3=0.0)
    measured = replace(copied, max_area_delta_mm2=64.8064)

    for predicate, accepted in ((copied, False), (measured, True)):
        refusal = pkg.evaluate(predicate, before=before, after=after,
                               defects_before=("edge:aaaa",), defects_after=(),
                               declared_area_mm2=0.0, declared_volume_mm3=0.0)
        assert (refusal == "") is accepted, refusal
