# Responsibility: Pin the meshability registry's contract - registration, claim order, rungs,
#                 spending, consent flags and provider loading - with no kernel.
# Boundaries: pure. Entries are stubs; the real gap entry is checked for presence and its claim.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import meshability as mb
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(mb, "_REGISTRY", {})
    monkeypatch.setattr(mb, "_loaded", True)


def _defect(**measurements) -> EntityDefect:
    return EntityDefect(code=DefectCode.mesh_edge_through_face, severity=DefectSeverity.error,
                        entity_type="face", entity_index=1, message="", fingerprint="face:a",
                        location={"bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1]},
                        measurements=dict(measurements))


def _entry(name, *, claims=True, priority=100, rungs=1, on=True):
    return mb.MeshabilityOp(name=name, claims=lambda d, c=claims: c, build=lambda r, n=name: (n, r),
                            enabled=lambda o=on: o, rungs=rungs, priority=priority)


def test_entries_come_back_in_priority_then_name_order(clean):
    mb.register(_entry("b", priority=10))
    mb.register(_entry("a", priority=10))
    mb.register(_entry("c", priority=1))
    assert [e.name for e in mb.registered()] == ["c", "a", "b"]


def test_a_disabled_entry_is_invisible(clean):
    mb.register(_entry("off", on=False))
    assert mb.registered() == () and mb.claimant(_defect()) is None
    assert [e.name for e in mb.registered(enabled_only=False)] == ["off"]


def test_registering_the_same_entry_twice_is_fine_but_a_different_one_raises(clean):
    entry = mb.register(_entry("x"))
    mb.register(entry)
    with pytest.raises(ValueError):
        mb.register(_entry("x", priority=5))
    with pytest.raises(ValueError):
        mb.register(_entry("y", rungs=0))


def test_the_claimant_respects_claims_rungs_and_spending(clean):
    mb.register(_entry("first", priority=1, claims=False))
    mb.register(_entry("ladder", priority=2, rungs=3))
    mb.register(_entry("fallback", priority=3))
    assert mb.claimant(_defect())[0].name == "ladder"
    assert mb.claimant(_defect(), rung_of={"ladder": 2}) == (mb._REGISTRY["ladder"], 2)
    assert mb.claimant(_defect(), rung_of={"ladder": 3})[0].name == "fallback"
    assert mb.claimant(_defect(), spent=lambda n, fp: n == "ladder")[0].name == "fallback"


def test_a_claim_that_raises_is_no_claim(clean):
    def boom(_):
        raise RuntimeError("x")

    mb.register(mb.MeshabilityOp(name="bad", claims=boom, build=lambda r: r,
                                 enabled=lambda: True))
    assert mb.claimant(_defect()) is None


def test_the_gap_operation_is_registered_by_its_provider(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", True)
    names = [e.name for e in mb.registered()]
    assert "resolve_sub_mesh_gap" in names
    from meshpipeline.cad.repair.gap_split import SAG_LADDER

    entry = next(e for e in mb.registered() if e.name == "resolve_sub_mesh_gap")
    assert entry.rungs == len(SAG_LADDER) and entry.build(1).sag_share == SAG_LADDER[1]
    gap = _defect(located_by="surface-mesh-clash", clash_kind="gap", stage="3d")
    assert entry.claims(gap)
    assert not entry.claims(_defect(located_by="surface-mesh-clash", clash_kind="crossing",
                                    stage="3d"))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False)
    assert "resolve_sub_mesh_gap" not in [e.name for e in mb.registered()]
