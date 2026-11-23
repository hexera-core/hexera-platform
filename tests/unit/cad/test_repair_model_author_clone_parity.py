"""Clone/delivery parity: a package the clone PROVED meshing is not refused by the answer table.

The clone probe records `meshes_after_target_remains` for an edit that cured the part while the
kernel fingerprint it aimed at is still present. Delivery then ran the catalogue's "does this op
answer that defect" table and refused the same edit as `op_does_not_answer`. Hermetic: no kernel.
"""
from __future__ import annotations

import pytest

from meshpipeline.cad.repair import model_author as ma
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import catalogue as op_catalogue
from meshpipeline.cad.repair.regions import Region

ORIGINAL = Measure(valid=False, solids=1, shells=1, faces=10, edges=20, vertices=12,
                   diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=5000.0,
                   volume_mm3=20000.0)
DEFECT = EntityDefect(
    code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error, entity_type="face",
    entity_index=1, message="overlapping facets", measurements={"stage": "2d"},
    location={"bbox_min": (0.0, 0.0, 1.0), "bbox_max": (1.0, 1.0, 2.0)}, fingerprint="fp1")
REGION = Region(index=0, fingerprints=frozenset({"fp1"}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(1.0, 1.0, 3.0), face_indices=frozenset({1}))
BODY = {"intent": "split the closed faces", "plan": [{"op": "split_closed_faces",
                                                      "fingerprint": "fp1"}],
        "cleared": ["fp1"], "unchanged": [], "max_area_delta_mm2": 1.0,
        "max_volume_delta_mm3": 1.0}


def _guards() -> ma.PackageGuards:
    guards = ma.PackageGuards((DEFECT,), reserved=frozenset(), rims=frozenset(),
                              original=ORIGINAL)
    guards.ops = {op.name: op for op in op_catalogue(split_closed_faces=True)}
    return guards


def test_clone_cured_package_not_refused_op_does_not_answer():
    ctx = ma.RegionContext(REGION, (DEFECT,))
    package, _ = ma.validate_package(dict(BODY), ctx, _guards(), label="t", cured_on_clone=True)
    assert [item["op"].name for item in package.plan] == ["split_closed_faces"]
    # The identical package, with no cure recorded, is still refused - and so is the default.
    for kwargs in ({"cured_on_clone": False}, {}):
        with pytest.raises(ma.Refusal) as refused:
            ma.validate_package(dict(BODY), ctx, _guards(), label="t", **kwargs)
        assert refused.value.code == "op_does_not_answer"


def test_only_a_kept_edit_with_the_cure_reason_counts_as_a_clone_cure():
    from types import SimpleNamespace as NS

    from meshpipeline.cad.repair import investigator
    from meshpipeline.cad.repair.session import MESHES_AFTER_TARGET_REMAINS

    def scratch(*edits):
        return NS(edits=list(edits))

    cure = NS(kept=True, reason=MESHES_AFTER_TARGET_REMAINS)
    assert investigator._cured_on(scratch(cure))
    assert not investigator._cured_on(scratch(NS(kept=True, reason="")))
    assert not investigator._cured_on(scratch(NS(kept=False, reason=MESHES_AFTER_TARGET_REMAINS)))
    assert not investigator._cured_on(None)


@pytest.mark.parametrize("reason,expected", [("meshes_after_target_remains", True), ("", False)])
def test_conclude_hands_the_clone_cure_to_validate_package(monkeypatch, reason, expected):
    """The wiring: `_conclude` reads the clone's record and passes it as `cured_on_clone`."""
    from types import SimpleNamespace as NS

    from meshpipeline.cad.repair import investigator

    seen: dict = {}

    def spy(claim, ctx, guards, **kwargs):
        seen.update(kwargs)
        return "package", {"plan": claim["plan"]}

    monkeypatch.setattr(ma, "validate_package", spy)
    scratch = NS(edits=[NS(kept=True, reason=reason)],
                 kept=lambda: ({"op": "split_closed_faces", "fingerprint": "fp1",
                                "defect_code": "mesh_overlapping_facets"},))
    inv = NS(attempted=1, model_calls=1, conclusion=None)
    package, done = investigator._conclude(
        {"package": {"intent": "x", "cleared": ["fp1"], "unchanged": [],
                     "max_area_delta_mm2": 1.0, "max_volume_delta_mm3": 1.0}},
        inv, ctx=ma.RegionContext(REGION, (DEFECT,)), scratch=scratch, region=REGION,
        defects=(DEFECT,), state={"shape": None, "part_rims": frozenset()}, refusals=[],
        verdicts=[], escalations=[], held_codes=frozenset(), original=ORIGINAL)
    assert done and package == "package"
    assert seen["cured_on_clone"] is expected
