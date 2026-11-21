# Responsibility: Verify the scratch clone against the real OCC kernel - a real copy really is
#                 independent of the part, real edits on it leave the session's shape byte-identical,
#                 the fingerprints survive the copy, and a kept-edit log really does replay.
# Boundaries: the native tier. The RULES (the commit guard, the refusals, the cap, the kept log) are
#             unit-tested in tests/unit/cad/test_repair_investigator_edit.py; what is proved here is
#             that the kernel's own copy is what the design assumes it is.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_missing_bore_wall_step

import meshpipeline.settings.cad_repair as rcfg

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair import caps, model_author  # noqa: E402  # noqa: E402
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair import session as sess
from meshpipeline.cad.repair.contracts import DefectCode  # noqa: E402
from meshpipeline.cad.repair.package import (  # noqa: E402
    PackageBudget,
    Predicate,
    TargetPackage,
)
from meshpipeline.cad.repair.regions import Region  # noqa: E402

TOLERANCE_MM = 0.02


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    # The bore fixture's two rims are what `fill_planar_hole` exists for (see
    # test_native_repair_session.py for why the bore and not the open shell).
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _brep_bytes(shape, tmp_path, name: str) -> bytes:
    """The shape serialised. A BYTE COMPARISON is the strongest statement available about "this
    shape was not touched": it covers the geometry, the topology and the tolerances."""
    from OCP.BRepTools import BRepTools

    out = tmp_path / name
    BRepTools.Write_s(shape, str(out))
    return out.read_bytes()


def _whole_region(shape, defects) -> Region:
    """A region covering the whole part, so the scoped re-inspection an edit pays for really does
    look at the geometry the edit touched."""
    from meshpipeline.cad.repair.operations import _bnd

    box = _bnd(shape)
    low = (box.CornerMin().X(), box.CornerMin().Y(), box.CornerMin().Z())
    high = (box.CornerMax().X(), box.CornerMax().Y(), box.CornerMax().Z())
    return Region(index=0, fingerprints=frozenset(d.fingerprint for d in defects),
                  bbox_min=low, bbox_max=high, face_indices=frozenset())


def _bore_session(tmp_path) -> tuple:
    session = sess.RepairSession(_read(write_missing_bore_wall_step(tmp_path / "bore.step")))
    rims = [d for d in session.defects() if d.code is DefectCode.open_shell]
    assert len(rims) == 2, f"expected two rims, found {len(rims)}"
    return session, rims


def _workspace(session, scratch, defects) -> it.Workspace:
    region = _whole_region(session.shape, session.defects())
    return it.Workspace(region=region, defects=tuple(defects), attempts=(),
                        shape=scratch.shape, original=session.original,
                        baseline_shape=scratch.shape,
                        editor=it.editor(scratch, tolerance_mm=TOLERANCE_MM))


def _scratch_of(session) -> it.Scratch:
    return it.Scratch.of(session.shape, original=session.original, defects=session.defects())


# --- the copy is a real copy --------------------------------------------------------------------

def test_fingerprints_match_across_the_copy(tmp_path):
    """THE ASSUMPTION THE WHOLE REHEARSAL RESTS ON. A kept-edit log names entities by fingerprint
    and is replayed against the REAL shape, so the copy's fingerprints must be the part's."""
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.operations import _copied

    shape = _read(write_missing_bore_wall_step(tmp_path / "bore.step"))
    clone = _copied(shape)

    assert clone is not shape
    original, _ = localize_scan(shape)
    copied, _ = localize_scan(clone)

    assert [d.fingerprint for d in copied] == [d.fingerprint for d in original]
    assert [(d.code, d.entity_type, d.entity_index) for d in copied] == \
           [(d.code, d.entity_type, d.entity_index) for d in original]
    assert caps.measure(clone).to_dict() == caps.measure(shape).to_dict()


def test_the_clone_shares_no_topology_with_the_part(tmp_path):
    """`_copied(..., True, False)` copies the geometry too, so no TShape is in common: an edit on
    the clone cannot nudge a tolerance on the part through a shared sub-shape."""
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed
    from meshpipeline.cad.repair.operations import _copied

    shape = _read(write_missing_bore_wall_step(tmp_path / "bore.step"))
    clone = _copied(shape)
    mine = _indexed(shape, TopAbs_FACE)
    theirs = _indexed(clone, TopAbs_FACE)

    assert mine.Extent() == theirs.Extent()
    assert not any(theirs.Contains(mine.FindKey(i)) for i in range(1, mine.Extent() + 1))


# --- the real shape is never reached ------------------------------------------------------------

def test_edits_never_reach_session_shape_bytes(tmp_path):
    """THE PILLAR, against the kernel: the session's shape is byte-identical after real edits."""
    session, rims = _bore_session(tmp_path)
    before_bytes = _brep_bytes(session.shape, tmp_path, "before.brep")
    before_shape = session.shape
    before_measure = session.original.to_dict()

    scratch = _scratch_of(session)
    # A COPY, AT THE POINT OF CONSTRUCTION. Without this the test would still pass if the
    # "clone" were the part itself, because no kernel operation mutates its input - but the
    # tolerance creep a shared sub-shape carries (see `RepairSession`'s docstring) would be
    # real, and `reset` would hand back an edited shape.
    assert scratch.shape is not session.shape
    ws = _workspace(session, scratch, session.defects())
    statuses = [
        it.run_tool(ws, "edit",
                    {"op": "fill_planar_hole", "fingerprint": rim.fingerprint,
                     "defect_code": "open_shell"}, budget_s=30.0)
        for rim in rims]

    kept = [out for out in statuses if out.ok and out.data.get("kept")]
    assert kept, [out.error or out.data.get("reason") for out in statuses]
    assert scratch.shape is not session.shape, "an edit must not hand the part back"
    # THE CLONE MOVED and the part did not.
    assert caps.measure(scratch.shape).to_dict() != before_measure
    assert session.shape is before_shape
    assert session.original.to_dict() == before_measure
    assert _brep_bytes(session.shape, tmp_path, "after.brep") == before_bytes
    # And nothing was recorded against the real session by any of it.
    assert session.log == [] and session.packages == []


def test_a_scratch_session_refuses_to_commit_a_real_package(tmp_path):
    session, rims = _bore_session(tmp_path)
    scratch = _scratch_of(session)
    region = _whole_region(session.shape, session.defects())
    package = _plan_package([{"op": "fill_planar_hole", "fingerprint": rims[0].fingerprint,
                             "defect_code": "open_shell"}], scratch.session, region)

    verdict = scratch.session.try_package(package)

    assert verdict.status == sess.REVERTED
    assert verdict.reason == "refused: a scratch session cannot commit"
    assert scratch.session.log == [], "nothing may be applied by a refused commit"


# --- the rehearsal predicts the real run --------------------------------------------------------

def _plan_package(entries, target_session, region: Region) -> TargetPackage:
    """`entries` (a `kept()` log) as a package plan, validated against `target_session`'s own
    defects by the author's validator - the same route a real authored package takes."""
    ctx = model_author.RegionContext(region, target_session.defects())
    guards = model_author.PackageGuards(
        target_session.defects(), reserved=frozenset(), rims=frozenset(),
        original=target_session.original,
        solids_of=lambda: it._solids_of(target_session.shape))
    plan = []
    for entry in entries:
        item, _ = model_author.plan_entry(dict(entry), ctx, guards)
        plan.append({**item, "tolerance_mm": TOLERANCE_MM})
    return TargetPackage(
        region=region, intent="patch the rims the rehearsal patched",
        plan=tuple(plan),
        predicate=Predicate(cleared=frozenset(e["fingerprint"] for e in entries),
                            reclassified=frozenset(), unchanged=frozenset(),
                            max_area_delta_mm2=float(guards.area_cap),
                            max_volume_delta_mm3=float(guards.volume_cap)),
        budget=PackageBudget(max_ops=6, timeout_s=120.0, model_calls=1, deviation_mm=0.1))


def test_clone_rehearses_the_replay(tmp_path):
    """WHAT THE REHEARSAL IS FOR. The kept-edit log, replayed through a REAL session's
    `try_package`, gives the same per-operation statuses it gave on the clone."""
    session, rims = _bore_session(tmp_path)
    scratch = _scratch_of(session)
    ws = _workspace(session, scratch, session.defects())
    for rim in rims:
        it.run_tool(ws, "edit", {"op": "fill_planar_hole", "fingerprint": rim.fingerprint,
                                 "defect_code": "open_shell"}, budget_s=30.0)
    rehearsed = scratch.kept()
    assert rehearsed, [e.reason for e in scratch.edits]
    on_the_clone = [(e.op, e.fingerprint, e.status) for e in scratch.edits if e.kept]

    region = _whole_region(session.shape, session.defects())
    session.try_package(_plan_package(list(rehearsed), session, region))

    on_the_part = [(v.op, v.targets[0], v.status) for v in session.log]
    assert on_the_part == on_the_clone
