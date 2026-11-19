# Responsibility: Verify tournament rehearsal against the real OCC kernel - a rehearsal really
#                 runs on its own copy, two candidates in one tournament really cannot meet, and
#                 the session's shape is byte-identical after a tournament that edited geometry.
# Boundaries: the native tier. The RULES (the budget split, K, the tier-2 floor, the scoped
#             accounting, "never raises") are unit-tested in
#             tests/unit/cad/test_repair_tournament_rehearse.py; what is proved here is that the
#             kernel's own copy is what `rehearse` assumes it is.
#             THE PROBE RUNGS ARE NOT ASKED HERE. Tier 1 and tier 2 spawn a mesher, which is
#             proved in test_native_repair_ladder.py and test_native_repair_mesh_probe.py; a
#             rehearsal that paid for them would make this file a corpus pass.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_missing_bore_wall_step

import meshpipeline.settings.cad_repair as rcfg

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair import caps, ladder  # noqa: E402
from meshpipeline.cad.repair import session as sess  # noqa: E402
from meshpipeline.cad.repair import tournament as T  # noqa: E402
from meshpipeline.cad.repair.contracts import DefectCode  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef  # noqa: E402
from meshpipeline.cad.repair.operations import OPERATIONS  # noqa: E402
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
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _brep_bytes(shape, tmp_path, name: str) -> bytes:
    from OCP.BRepTools import BRepTools

    out = tmp_path / name
    BRepTools.Write_s(shape, str(out))
    return out.read_bytes()


def _whole_region(shape, defects) -> Region:
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


def _fill_item(shape, defect) -> dict:
    op = next(o for o in OPERATIONS if o.name == "fill_planar_hole")
    return {"op": op,
            "targets": (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                                  entity_index=int(defect.entity_index)),),
            "defect_code": DefectCode.open_shell, "tolerance_mm": TOLERANCE_MM}


def _candidate(source, shape, region, defects, items) -> T.Candidate:
    predicate = Predicate(cleared=frozenset(d.fingerprint for d in defects),
                          reclassified=frozenset(), unchanged=frozenset(),
                          max_area_delta_mm2=1e9, max_volume_delta_mm3=1e9)
    return T.candidate(source, TargetPackage(region=region, intent="fill the rim",
                                             plan=tuple(items), predicate=predicate,
                                             budget=PackageBudget(max_ops=3, timeout_s=60.0,
                                                                  model_calls=0,
                                                                  deviation_mm=5.0)))


def _tier0_only(shape, tier, *, diagonal_mm, within=None, budget_s):
    """The real tier 0 and nothing else. The probe rungs spawn a mesher and are proved in the
    ladder's own native tier; see this file's header."""
    if str(tier) == "0":
        return ladder.run_tier(shape, "0", diagonal_mm=diagonal_mm, budget_s=budget_s)
    from meshpipeline.cad.repair.ladder import TierResult

    return TierResult(str(tier), None, 0.0, (), {"error": "not asked in this tier"})


def _rehearse(session, cand, region, *, budget_s=14.0):
    return T.rehearse(cand, session_shape=session.shape, original=session.original,
                      defects=session.defects(), region=region, tolerance_mm=TOLERANCE_MM,
                      budget_s=budget_s, run_tier=_tier0_only)


# --- the session is never reached ---------------------------------------------------------------

def test_rehearsals_never_reach_session_shape_bytes(tmp_path):
    """THE PILLAR, against the kernel. Two real candidates, both editing real geometry, and the
    session's shape is byte-identical afterwards - nothing is recorded against it either."""
    session, rims = _bore_session(tmp_path)
    region = _whole_region(session.shape, session.defects())
    before_bytes = _brep_bytes(session.shape, tmp_path, "before.brep")
    before_shape = session.shape
    before_measure = session.original.to_dict()

    cands = [_candidate(T.RULES, session.shape, region, [rims[0]],
                        [_fill_item(session.shape, rims[0])]),
             _candidate(T.ONE_SHOT, session.shape, region, [rims[1]],
                        [_fill_item(session.shape, rims[1])]),
             _candidate(T.INVESTIGATOR, session.shape, region, rims,
                        [_fill_item(session.shape, rims[0]),
                         _fill_item(session.shape, rims[1])])]
    out = T.rehearse_all(cands, wall_s=42.0, limit=3, session_shape=session.shape,
                         original=session.original, defects=session.defects(), region=region,
                         tolerance_mm=TOLERANCE_MM, run_tier=_tier0_only)

    assert [r.status for r in out] == [T.REHEARSED] * 3, [r.reason for r in out]
    assert [r.kept_ops for r in out] == [1, 1, 2]
    assert session.shape is before_shape
    assert _brep_bytes(session.shape, tmp_path, "after.brep") == before_bytes
    assert session.original.to_dict() == before_measure
    assert session.log == [] and session.packages == []


def test_two_clones_do_not_share_tshapes(tmp_path):
    """Candidates cannot meet: an edit on one clone leaves the other's bytes unchanged, and no
    face of one is a face of the other.

    A BYTE COMPARISON ALONE WOULD PROVE NOTHING about the copy, because no OCP operation mutates
    its input - the bytes stay identical even when "the clone" IS the part. So this asserts
    object identity and an empty TShape intersection as well.
    """
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.investigator_tools import Scratch
    from meshpipeline.cad.repair.localize import _indexed

    session, rims = _bore_session(tmp_path)
    region = _whole_region(session.shape, session.defects())
    a = Scratch.of(session.shape, original=session.original, defects=session.defects())
    b = Scratch.of(session.shape, original=session.original, defects=session.defects())

    assert a.shape is not b.shape and a.shape is not session.shape
    mine, theirs = _indexed(a.shape, TopAbs_FACE), _indexed(b.shape, TopAbs_FACE)
    assert mine.Extent() == theirs.Extent()
    assert not any(theirs.Contains(mine.FindKey(i)) for i in range(1, mine.Extent() + 1))

    b_bytes = _brep_bytes(b.shape, tmp_path, "b-before.brep")
    item = _fill_item(a.shape, rims[0])
    verdict = a.session.try_op(item["op"], item["targets"], item["defect_code"],
                               tolerance_mm=TOLERANCE_MM, within=region)
    assert verdict.status == sess.KEPT, verdict.reason
    assert caps.measure(a.shape).to_dict() != caps.measure(b.shape).to_dict()
    assert _brep_bytes(b.shape, tmp_path, "b-after.brep") == b_bytes


# --- a rehearsal predicts the commit ------------------------------------------------------------

def test_rehearsal_matches_the_real_commit(tmp_path):
    """The winning plan, replayed through the REAL session's `try_package`, clears what its
    rehearsal said it would - and the rehearsal itself could never have committed it."""
    session, rims = _bore_session(tmp_path)
    region = _whole_region(session.shape, session.defects())
    cand = _candidate(T.RULES, session.shape, region, rims,
                      [_fill_item(session.shape, rims[0]), _fill_item(session.shape, rims[1])])

    rehearsal = _rehearse(session, cand, region, budget_s=42.0)
    assert rehearsal.status == T.REHEARSED, rehearsal.reason
    assert rehearsal.kept_ops == 2
    assert rehearsal.tier0 is not None and rehearsal.tier0.passed is True
    assert T.eligible(rehearsal) is True
    assert T.winner([rehearsal]) is rehearsal

    verdict = session.try_package(cand.package)
    assert verdict.status == sess.COMMITTED, verdict.reason
    assert [v.status for v in verdict.verdicts] == [sess.KEPT] * rehearsal.kept_ops
    remaining = {d.fingerprint for d in session.defects()}
    assert rehearsal.cleared and not (rehearsal.cleared & remaining)


def test_tier2_is_skipped_on_a_short_rehearsal(tmp_path):
    """The whole-part rung is never started on a rehearsal that cannot finish it - which is also
    why the rest of this file can ask for 14 seconds and spawn no mesher at all."""
    session, rims = _bore_session(tmp_path)
    region = _whole_region(session.shape, session.defects())
    cand = _candidate(T.RULES, session.shape, region, [rims[0]],
                      [_fill_item(session.shape, rims[0])])
    rehearsal = _rehearse(session, cand, region, budget_s=14.0)
    assert rehearsal.status == T.REHEARSED, rehearsal.reason
    assert rehearsal.tier2 is None and "no_time" in rehearsal.reason
