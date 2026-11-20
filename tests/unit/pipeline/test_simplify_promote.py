# Responsibility: Verify simplified geometry becomes the run's geometry only after it provably stages, refuses the cases repair promotion refuses, and never touches the repair lineage it follows.
from __future__ import annotations

import pytest
from tests._geometry_support import geometry_state, source_ref

from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, MaterializedGeometry
from meshpipeline.pipeline.simplify_promote import PromotionRefused, derived_interpretation, promote


def _original(tmp_path, **over):
    from meshpipeline.pipeline.geometry_state import materialized
    state = {"job_id": "job-1", "engine": "gmsh", "geometry": geometry_state(tmp_path), **over}
    return state, materialized(state)


def _simplified(tmp_path, original, *, marker="simplified", unit=None, scale=None):
    ref = source_ref(tmp_path=tmp_path, filename="part-simplified.step", marker=marker,
                     owner_id=original.ref.owner_id)
    interpretation = derived_interpretation(original, source_id=ref.source_id)
    if unit is not None:
        interpretation = GeometryInterpretationRef(
            interpretation_id=interpretation.interpretation_id, geometry_source_id=ref.source_id,
            unit=unit, scale_to_metres=scale, basis=interpretation.basis,
            evidence=interpretation.evidence)
    return MaterializedGeometry(ref=ref, interpretation=interpretation,
                                local_path=tmp_path / "part-simplified.step")


@pytest.fixture
def staging(monkeypatch):
    seen: list = []
    answer = {"ok": True}

    def _stages(geometry, *, engine="", budget_s=None):
        seen.append((geometry.ref.sha256, engine, budget_s))
        return (True, "") if answer["ok"] else (False, "RuntimeError: empty shape")

    monkeypatch.setattr("meshpipeline.pipeline.simplify_promote.stages_for", _stages)
    return seen, answer


def test_simplified_geometry_that_stages_becomes_the_geometry_with_its_lineage(tmp_path, staging):
    state, original = _original(tmp_path)
    simplified = _simplified(tmp_path, original)

    update = promote(state, simplified=simplified, engine="gmsh", attempt=1, features=("hole0",),
                     estimated_change=-2e-5)

    assert update["geometry"]["ref"]["sha256"] == simplified.ref.sha256
    lineage = update["simplify_lineage"]
    assert lineage["original"]["sha256"] == original.ref.sha256
    assert lineage["simplified"]["sha256"] == simplified.ref.sha256
    assert (lineage["engine_staged_for"], lineage["attempt"]) == ("gmsh", 1)
    assert lineage["features"] == ["hole0"] and lineage["estimated_change"] == -2e-5
    assert staging[0] == [(simplified.ref.sha256, "gmsh", 600.0)]


def test_the_repair_lineage_before_it_is_left_alone(tmp_path, staging):
    state, original = _original(tmp_path, repair_lineage={"original": {"sha256": "a" * 64}})
    update = promote(state, simplified=_simplified(tmp_path, original), engine="gmsh")
    assert "repair_lineage" not in update


def test_the_lineage_keeps_the_replaced_file_so_the_repaired_cad_is_still_delivered(tmp_path, staging):
    """After repair -> simplify, `geometry` is the simplified file; the repaired one is delivered
    from the lineage, so it must still materialise there."""
    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    state, original = _original(tmp_path, repair_lineage={"original": {"sha256": "a" * 64}})
    update = promote(state, simplified=_simplified(tmp_path, original), engine="gmsh")
    replaced = MaterializedGeometry.from_state(update["simplify_lineage"]["replaced_geometry"])
    assert replaced is not None
    assert replaced.ref.sha256 == original.ref.sha256
    assert str(replaced.path) == str(original.path)


def test_it_refuses_what_repair_promotion_refuses(tmp_path, staging):
    state, original = _original(tmp_path)
    with pytest.raises(PromotionRefused, match="byte-identical"):
        promote(state, simplified=original, engine="gmsh")
    with pytest.raises(PromotionRefused, match="reinterpret scale"):
        promote(state, simplified=_simplified(tmp_path, original, unit="m", scale=1.0), engine="gmsh")
    with pytest.raises(PromotionRefused, match="no simplified"):
        promote(state, simplified=None, engine="gmsh")
    with pytest.raises(PromotionRefused, match="no geometry"):
        promote({"job_id": "j"}, simplified=_simplified(tmp_path, original), engine="gmsh")
    staging[1]["ok"] = False
    with pytest.raises(PromotionRefused, match="does not stage for gmsh"):
        promote(state, simplified=_simplified(tmp_path, original), engine="gmsh")
