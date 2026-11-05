# Responsibility: Verify the loop repairs real broken geometry end to end, leaves alone what it cannot safely repair, and never seals a bore.
from __future__ import annotations

import pytest
from tests.cad_fixtures import (
    write_loose_faces_step,
    write_missing_bore_wall_step,
    write_open_shell_step,
    write_small_edge_step,
    write_step,
)

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.loop import RESOLVED, run

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _attempts(result):
    return [(v.op, v.status, v.reason) for v in result.verdicts]


def _assert_unchanged(before, after, message):
    """Every measured quantity equal, floats to a relative 1e-9.

    NOT BIT-EXACT, MEASURED: kernel operations share TShape handles with their input, so a run
    whose every attempt was rolled back can still move a tolerance by one ulp (1e-07 became
    1.0000000000000002e-07 on the bore fixture). Nothing a cap or a person could see.
    """
    a, b = before.to_dict(), after.to_dict()
    assert a.keys() == b.keys(), message
    for key in a:
        assert b[key] == pytest.approx(a[key], rel=1e-9), f"{message}: {key} {a[key]} -> {b[key]}"


def test_a_sound_part_comes_out_of_the_loop_untouched(tmp_path):
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    before = measure(shape)

    result = run(shape, tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED
    assert result.verdicts == (), f"a sound part had operations run on it: {_attempts(result)}"
    assert measure(result.shape).to_dict() == before.to_dict()


def test_the_loop_leaves_the_sliver_fixture_undamaged_and_says_it_did_not_repair_it(tmp_path):
    # AN HONEST NON-REPAIR, AND THE UNDAMAGED SHAPE IS THE POINT. The fixture carries only
    # WARNINGS (small_edge, small_face), so the planner still proposes `fix_small_edges` (an
    # earlier draft stopped as resolved with zero operations). But the solid's topology forbids
    # removing the sliver, so the operation achieves nothing, the session rolls it back, and the
    # run stops after two rolled-back attempts. The last assertion is the one that would have caught the defect where
    # the fixer, run on bare faces, "removed" the sliver by blowing the part up to twice its size
    # (diagonal 141.77 -> 270.00 mm, tolerance 1e-7 -> 80 mm): the shape must come back identical.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read(write_small_edge_step(tmp_path / "sliver.step"))
    before = measure(shape)
    assert [d for d in localize_scan(shape)[0] if d.code is DefectCode.small_edge], \
        "the fixture carries no sliver"

    result = run(shape, tolerance_mm=0.02)

    assert [d for d in result.defects if d.code is DefectCode.small_edge], \
        "the sliver vanished without a kept operation being responsible"
    assert not any(v.kept for v in result.verdicts), _attempts(result)
    assert len(result.verdicts) == 2, _attempts(result)
    # WHY IT IS UNRESOLVED, not merely that it is: no error-severity defect remains (the fixture
    # has only warnings), yet the loop ABANDONED the sliver after two rolled-back attempts and it
    # is still in the final list. `resolved` means the loop succeeded, so this is `stalled`.
    assert result.stopped_because == "stalled", f"{result.stopped_because} {_attempts(result)}"
    assert not result.resolved
    tried = {fp for v in result.verdicts for fp in v.targets}
    assert tried & {d.fingerprint for d in result.defects}, "no abandoned target is still reported"
    _assert_unchanged(before, measure(result.shape), "the part was damaged")


def test_the_loop_sews_the_loose_faces_fixture(tmp_path):
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_loose_faces_step(tmp_path / "loose.step"))
    before = measure(shape)
    # The reader hands back six one-face shells and no solid; sewing collapses them to one shell,
    # which `_as_solid_if_closed` then promotes.
    assert before.shells == 6 and before.solids == 0

    result = run(shape, tolerance_mm=0.02)

    after = measure(result.shape)
    assert after.shells == 1 and after.solids == 1, \
        f"nothing was joined; stopped {result.stopped_because}, {_attempts(result)}"
    assert any(v.op == "sew_faces" and v.kept for v in result.verdicts)
    # THE ONE GENUINE REPAIR IN THIS SUITE MUST PROVE IT DID NOT DAMAGE THE PART, not merely that it
    # joined the shells: a sew that inflated a tolerance or moved the extent would pass the lines
    # above exactly as an unchecked FixSmallEdges once did. The declared area is summed from the
    # kept operations' own evidence, so the full cumulative check is available at loop level.
    from meshpipeline.cad.repair.caps import cumulative_refusal, sanity_refusal
    from meshpipeline.cad.repair.operations import AREA_DELTA

    declared = sum(v.evidence.get(AREA_DELTA, 0.0) for v in result.verdicts if v.kept)
    assert sanity_refusal(before, after) == "", sanity_refusal(before, after)
    assert after.diagonal_mm == pytest.approx(before.diagonal_mm, rel=1e-6)
    assert after.max_tolerance_mm <= max(before.max_tolerance_mm, rcfg.CAD_REPAIR_MAX_TOLERANCE_MM)
    refusal = cumulative_refusal(before, after, declared_area_mm2=declared, declared_volume_mm3=0.0)
    assert refusal == "", refusal


def test_the_loop_leaves_a_missing_wall_alone_and_says_why(tmp_path):
    # AN HONEST NON-REPAIR, and the loop must be able to produce one. The open-shell fixture is a
    # box with a whole face gone: filling declines it as a missing wall (0.8165 of the diagonal,
    # over the 0.75 cap), sewing has nothing to stitch a missing face to, and the whole-shape
    # fallback does not close an opening either. What the loop owes the operator is a clear
    # reason rather than a silent no-op or a flat lid.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read(write_open_shell_step(tmp_path / "holed.step"))
    before = measure(shape)
    assert [d for d in localize_scan(shape)[0] if d.code is DefectCode.open_shell]

    result = run(shape, tolerance_mm=0.02)

    assert [d for d in result.defects if d.code is DefectCode.open_shell]
    assert not result.resolved
    assert result.stopped_because in ("no_candidate", "stalled", "op_budget")
    # NOTHING WAS LIDDED. A patch here would be a different part handed back to the customer.
    assert not any(v.kept for v in result.verdicts), _attempts(result)
    # Every attempt was rolled back, so the shape is byte-identical - which also catches a silent
    # tolerance or diagonal change that a face count alone would let through.
    _assert_unchanged(before, measure(result.shape), "a missing wall was lidded over")


def test_the_loop_never_seals_a_missing_bore_wall(tmp_path):
    # THE SAFETY CLAIM OF THE WHOLE SLICE, end to end. The plate must still have its bolt hole.
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_missing_bore_wall_step(tmp_path / "bore.step"))
    before = measure(shape)

    result = run(shape, tolerance_mm=0.02)

    assert not any(v.op == "fill_planar_hole" and v.kept for v in result.verdicts), \
        f"a rim of the bore was patched: {_attempts(result)}"
    assert any(r["reason"] == "paired_rim" for r in result.refusals), \
        "the refusal reached nothing a report could state"
    # Every attempt was rolled back, so the shape is byte-identical. That subsumes the volume check
    # (the bore is about 2010 mm3; sealing it would show) and also catches a silent tolerance or
    # diagonal change.
    assert not any(v.kept for v in result.verdicts), _attempts(result)
    _assert_unchanged(before, measure(result.shape), "the bore plate was altered")


def test_the_loop_is_not_worse_than_the_single_whole_shape_pass_it_replaces(tmp_path):
    # THE FLOOR. If the whole-shape fallback alone improves a file, the loop must not do worse.
    # Measured on the SLIVER fixture: neither approach repairs a missing wall, so comparing them
    # there proves nothing.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.operations import FixShapeGlobal

    shape = _read(write_small_edge_step(tmp_path / "sliver.step"))
    single_shape = FixShapeGlobal().apply(shape, (), tolerance_mm=0.02).shape
    single = measure(single_shape)
    single_slivers = len([d for d in localize_scan(single_shape)[0]
                          if d.code is DefectCode.small_edge])

    result = run(shape, tolerance_mm=0.02)
    looped = measure(result.shape)
    looped_slivers = len([d for d in result.defects if d.code is DefectCode.small_edge])

    assert looped.faces >= single.faces
    assert looped.solids >= single.solids
    assert looped_slivers <= single_slivers, (
        f"the loop left {looped_slivers} slivers where the single pass left {single_slivers}")
