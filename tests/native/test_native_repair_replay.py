# Responsibility: Verify against the real OCC kernel that an agent-driven run's RECORDED PACKAGES
#                 replay to the same result - the same committed count, the same `valid`, the same
#                 solid count - which is the replay contract that byte-identical replay cannot
#                 give for a run an agent authored.
# Boundaries: the native tier. The bookkeeping (the miss, the recorded verdict standing in for the
#             review, the posture gate, the attempt allowance) is unit-tested in
#             tests/unit/cad/test_repair_replay.py and tests/unit/cad/test_repair_budget_attempts.py;
#             what is proved here is that the kernel really re-applies the same plan to the same
#             geometry. NO MODEL IS CALLED on either side: the author is SCRIPTED, so this file is
#             about the replay and not about an investigation, and it spawns no mesher - the probe
#             postures stay off, so this is not a corpus pass.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_missing_bore_wall_step

import meshpipeline.settings.cad_repair as rcfg

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair import region_loop as RL  # noqa: E402
from meshpipeline.cad.repair import replay as R  # noqa: E402
from meshpipeline.cad.repair.caps import measure  # noqa: E402
from meshpipeline.cad.repair.loop import LoopBudget  # noqa: E402
from meshpipeline.cad.repair.rules_author import rules_author  # noqa: E402

TOLERANCE_MM = 0.05


@pytest.fixture(autouse=True)
def _postures(monkeypatch):
    # THE AGENT POSTURE IS WHAT TURNS THE RECORD ON, and it is set WITHOUT a model router: the
    # author below is the deterministic one, so the posture only opens the record and no
    # investigation runs. The probes stay off - a replay test may not spawn a mesher.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "live", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "live", raising=False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", "off")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "off")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TOLERANT_PROBE", "off")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def test_replayed_packages_reproduce_the_commits(tmp_path):
    """THE PILLAR, against the kernel: the recorded packages, re-applied to the same upload
    through a fresh session, commit the same number of times and leave the same part."""
    source = tmp_path / "part.step"
    write_missing_bore_wall_step(source)

    shape = _read(source)
    original = measure(shape)
    run = RL.run_regions(
        shape, author=rules_author(shape, original=original), tolerance_mm=TOLERANCE_MM,
        budget=LoopBudget(max_ops=6, stall_limit=2, timeout_s=600.0))

    committed = sum(1 for p in run.packages if p.committed)
    assert committed, "the run committed nothing, so there is nothing to replay"
    # THE ALLOWANCE IS RECORDED, and names the tier that bought it.
    assert run.attempt_budget["rule"] == RL.BUDGET_RULE_TIER
    assert run.attempt_budget["attempts"] >= 1
    # THE RECORD IS ONE ENTRY PER COMMITTED PACKAGE.
    assert len(run.packages_replay) == committed

    first = measure(run.shape)
    report = R.replay_packages(source, tmp_path / "replayed.step",
                              [dict(r) for r in run.packages_replay])

    values = {m.name: m.value for m in report.measurements}
    assert values["replay_misses"] == 0.0, report.diagnostics["replay"]
    assert values["replay_committed"] == float(committed)
    # THE SAME RESULT, by the two things a customer's geometry is judged on.
    assert report.diagnostics["valid"] is bool(first.valid)
    assert values["solids"] == float(first.solids)
    assert (tmp_path / "replayed.step").exists()
