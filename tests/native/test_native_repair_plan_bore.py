# Responsibility: Verify the planner refuses to patch either rim of a missing bore wall, and says so.
# Boundaries: the native tier, because the discriminator measures real planes on real geometry.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_missing_bore_wall_step, write_open_shell_step

import meshpipeline.settings.cad_repair as rcfg

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _filling_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def test_neither_rim_of_a_missing_bore_wall_is_ever_proposed_for_filling(tmp_path):
    # THE PART-RUINING CASE. Each rim is a perfectly fillable flat loop; patching both SEALS the
    # customer's bolt hole. It used to be a silent skip inside the fill executor, where nothing
    # downstream could see it. Now it is a PLANNING decision and it is reported.
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory, paired_rim_fingerprints

    shape = _read(write_missing_bore_wall_step(tmp_path / "bore.step"))
    defects, _ = localize_scan(shape)

    rims = paired_rim_fingerprints(shape, defects)
    assert len(rims) >= 2, f"the discriminator found {len(rims)} rims, expected a pair"

    planner = RulesPlanner()
    history = RunHistory()
    seen: set[str] = set()
    for _ in range(10):
        candidate = planner.propose(defects, history, shape=shape)
        if candidate is None:
            break
        assert not (candidate.op.name == "fill_planar_hole"
                    and candidate.defect_fingerprint in rims), (
            "the planner proposed patching a rim of a missing bore wall")
        seen.add(f"{candidate.defect_fingerprint}|{candidate.op.name}")
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)

    assert any(r["reason"] == "paired_rim" for r in history.refusals), (
        "the refusal was not recorded, so nothing downstream can report it")


def test_an_ordinary_hole_is_still_proposed_for_filling(tmp_path):
    # The guard must not cost the case filling exists for. A single opening has nothing to pair
    # with, so it stays fillable.
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory, paired_rim_fingerprints

    shape = _read(write_open_shell_step(tmp_path / "holed.step"))
    defects, _ = localize_scan(shape)

    assert paired_rim_fingerprints(shape, defects) == frozenset()

    planner, history = RulesPlanner(), RunHistory()
    proposed = set()
    for _ in range(10):
        candidate = planner.propose(defects, history, shape=shape)
        if candidate is None:
            break
        proposed.add(candidate.op.name)
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)

    assert "fill_planar_hole" in proposed or "sew_faces" in proposed
