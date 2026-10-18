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
