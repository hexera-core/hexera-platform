# Responsibility: Verify on the real kernel that the two saddle rims of a cross-drilled bar pair as one through-hole, and that two unrelated curved windows do not.
# Boundaries: the native tier, because a saddle rim only exists as real cylinder-cylinder geometry.
from __future__ import annotations

import pytest
from tests.cad_fixtures import (
    write_cross_drilled_cylinder_step,
    write_two_unrelated_curved_holes_step,
)

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import loop
from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.localize import localize_scan
from meshpipeline.cad.repair.operations import FILL_OPS
from meshpipeline.cad.repair.plan import paired_rim_fingerprints

pytest.importorskip("OCP.BRepFill", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 6)   # not saved by stopping early
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _rims(found):
    return [d for d in found if d.code is DefectCode.open_shell
            and (d.measurements or {}).get("boundary_edges")]


def test_a_cross_drilled_cylinders_saddle_rims_are_never_filled(tmp_path, monkeypatch):
    # TWO SADDLES, NO FLAT RIM. Neither loop lies in a plane, so the planar rule could not see the
    # pair; the non-planar fill would close both and seal the cross-hole. They must PAIR - a real
    # answer, not the "could not run" fail-closed stopgap.
    for flag in ("CAD_REPAIR_FILL_PLANAR_HOLES", "CAD_REPAIR_FILL_NONPLANAR_HOLES"):
        monkeypatch.setattr(rcfg, flag, True)
    shape = _read(write_cross_drilled_cylinder_step(tmp_path / "x.step"))
    found, _ = localize_scan(shape)
    rims = _rims(found)
    assert len(rims) == 2
    paired = paired_rim_fingerprints(shape, found)
    assert not getattr(paired, "unknown", False), "the saddle pair was not judged, only refused"
    assert paired == {r.fingerprint for r in rims}
    run = loop.run(shape, tolerance_mm=0.01)
    assert not [v for v in run.verdicts if v.kept and v.op in FILL_OPS]
    refused = [r for r in run.refusals if r.get("reason") == "paired_rim"]
    assert len(refused) == 2, run.refusals
    assert not any("could not run" in r.get("detail", "") for r in refused), refused


def test_two_curved_holes_off_one_axis_do_not_pair(tmp_path, monkeypatch):
    # Two windows 40 mm apart along the bar: the line between them runs along the skin, so no
    # passage leaves through either. They are two holes, and their fills are judged on their own.
    for flag in ("CAD_REPAIR_FILL_PLANAR_HOLES", "CAD_REPAIR_FILL_NONPLANAR_HOLES"):
        monkeypatch.setattr(rcfg, flag, True)
    shape = _read(write_two_unrelated_curved_holes_step(tmp_path / "w.step"))
    found, _ = localize_scan(shape)
    assert len(_rims(found)) == 2
    paired = paired_rim_fingerprints(shape, found)
    assert not getattr(paired, "unknown", False)
    assert paired == frozenset()
    run = loop.run(shape, tolerance_mm=0.01)
    assert not [r for r in run.refusals if r.get("reason") == "paired_rim"], run.refusals
    assert any(v.op == "fill_nonplanar_hole" for v in run.verdicts), (
        [(v.op, v.status, v.reason) for v in run.verdicts])


SLOTS = ["cotter1", "cotter2", "tubeslot1", "tubeslot2"]


@pytest.mark.parametrize("kind", SLOTS)
def test_a_slot_wrapped_around_a_curved_wall_is_one_passage(tmp_path, kind):
    # A NARROW RIM ON A CURVED WALL. The loop's smallest spread is the slot's width (along the
    # bar), so a best-fit plane through its points points the wrong way and both rules missed the
    # pair. Its normal is the WALL's, read from the face that owns its edges.
    from tests.cad_fixtures import write_missing_passage_wall_step

    shape = _read(write_missing_passage_wall_step(tmp_path / f"{kind}.step", kind))
    found, _ = localize_scan(shape)
    rims = _rims(found)
    assert len(rims) == 2
    paired = paired_rim_fingerprints(shape, found)
    assert not getattr(paired, "unknown", False), "the slot was not judged, only refused"
    assert paired == {r.fingerprint for r in rims}


@pytest.mark.parametrize("kind", SLOTS)
def test_a_slot_through_a_curved_wall_is_never_sealed_by_the_rules(tmp_path, monkeypatch, kind):
    from tests.cad_fixtures import write_missing_passage_wall_step

    from meshpipeline.cad.repair.conservative import RepairRefused, repair_step_file

    for flag in ("CAD_REPAIR_FILL_PLANAR_HOLES", "CAD_REPAIR_FILL_NONPLANAR_HOLES"):
        monkeypatch.setattr(rcfg, flag, True)
    source = write_missing_passage_wall_step(tmp_path / f"{kind}.step", kind)
    out = tmp_path / f"{kind}-repaired.step"
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)
    measured = refused.value.measurements
    assert measured["operations_kept"] == 0, f"the {kind} slot was sealed"
    rims = [r for r in measured["refusals"] if r.get("reason") == "paired_rim"]
    assert len(rims) == 2 and not any("could not run" in r["detail"] for r in rims), rims
    assert not out.exists()


@pytest.mark.parametrize("kind", SLOTS)
def test_a_directive_cannot_seal_a_slot_through_a_curved_wall(tmp_path, monkeypatch, kind):
    # A model directive aimed at each rim, at the shipped stall limit: the directive's fill meets
    # the same rim guard, so neither end of the slot is patched.
    from tests.cad_fixtures import write_missing_passage_wall_step

    from meshpipeline.cad.repair.conservative import RepairRefused, repair_step_file

    monkeypatch.undo()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    for flag in ("CAD_REPAIR_FILL_PLANAR_HOLES", "CAD_REPAIR_FILL_NONPLANAR_HOLES"):
        monkeypatch.setattr(rcfg, flag, True)
    source = write_missing_passage_wall_step(tmp_path / f"{kind}.step", kind)
    found, _ = localize_scan(_read(source))
    directives = tuple(
        {"op": "fill_nonplanar_hole", "targets": [d.fingerprint], "defect_code": "open_shell",
         "defect_fingerprint": d.fingerprint, "source": "llm-planner", "item_id": "",
         "planner_confidence": 0.9}
        for d in _rims(found))
    out = tmp_path / f"{kind}-directed.step"
    try:
        report = repair_step_file(source, out, directives=directives)
    except RepairRefused as refused:
        operations = refused.measurements["operations"]
    else:
        operations = report.operations
    kept = [o for o in operations if o["name"] in FILL_OPS and o["status"] == "kept"]
    assert not kept, f"a directive sealed one rim of the {kind} slot: {kept}"
