# Responsibility: Pin the CRS v5 beta's product seam: CAD_REPAIR_ENGINE=v5 runs v5 and carries `RepairResultV5`, a v5
#                 that raises hands the file to v3 (recorded), and a v5 that kept nothing is a refusal, not a failure.
from __future__ import annotations

from pathlib import Path

import pytest

import meshpipeline.cad.repair.conservative as conservative
import meshpipeline.cad.repair_v5.engine as engine
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport
from meshpipeline.cad.repair_v5.result import RepairResultV5


def _contract(outcome: str = "repaired") -> RepairResultV5:
    return RepairResultV5(engine="v5", choice="meshing", outcome=outcome, meshers_passing=4, meshers_total=4,
                          spend_usd=0.12)


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "in.step"
    src.write_bytes(b"ISO-10303-21;")
    return src, tmp_path / "out.step"


def test_v5_runs_and_carries_the_contract(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")

    def fake(source, destination, **kw):
        Path(destination).write_bytes(b"repaired")
        return engine.V5Run(delivered=True, result=_contract(), trace={"lines": ["x"]}, progress=[{"text": "t"}])

    monkeypatch.setattr(engine, "repair_file", fake)
    monkeypatch.setattr(conservative, "_repair_step_file",
                        lambda *a, **k: pytest.fail("v3 ran although v5 delivered"))
    report = conservative.repair_step_file(src, dest)
    assert report.diagnostics["engine"] == "v5"
    assert report.diagnostics[conservative.RESULT_V5_KEY]["outcome"] == "repaired"
    assert report.diagnostics[conservative.TRACE_V5_KEY] == {"lines": ["x"]}
    assert dest.read_bytes() == b"repaired"


def test_a_v5_that_raises_falls_back_to_v3_and_says_so(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")

    def broken(*a, **k):
        raise engine.V5Unavailable("pool did not start")

    v3 = RepairReport(defects=(), measurements=(RepairMeasurement("resolved", True),), operations=(), summary="v3")
    monkeypatch.setattr(engine, "repair_file", broken)
    monkeypatch.setattr(conservative, "_repair_step_file", lambda *a, **k: v3)
    report = conservative.repair_step_file(src, dest)
    assert report.diagnostics["engine"] == "v3"
    assert "pool did not start" in report.diagnostics["v5_failed"]
    assert report.diagnostics[conservative.RESULT_V5_KEY]["engine"] == "v3"
    assert {m.name for m in report.measurements} >= {"engine", "v5_failed", "resolved"}


def test_the_force_fail_hook_reaches_v3(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")
    monkeypatch.setenv("CRS_V5_FORCE_FAIL", "1")
    v3 = RepairReport(defects=(), measurements=(), operations=(), summary="v3")
    monkeypatch.setattr(conservative, "_repair_step_file", lambda *a, **k: v3)
    report = conservative.repair_step_file(src, dest)
    assert report.diagnostics["engine"] == "v3"
    assert "CRS_V5_FORCE_FAIL" in report.diagnostics["v5_failed"]


def test_v3_refusal_after_v5_failure_carries_the_contract(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")
    monkeypatch.setattr(engine, "repair_file", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))

    def refuse(*a, **k):
        raise conservative.RepairRefused("nothing kept")

    monkeypatch.setattr(conservative, "_repair_step_file", refuse)
    with pytest.raises(conservative.RepairRefused) as err:
        conservative.repair_step_file(src, dest)
    assert err.value.measurements["engine"] == "v3"
    assert err.value.measurements[conservative.RESULT_V5_KEY]["outcome"] == "unrepairable"


def test_a_v5_that_kept_nothing_is_a_refusal_and_v3_is_not_asked(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")
    monkeypatch.setattr(engine, "repair_file",
                        lambda *a, **k: engine.V5Run(delivered=False, result=_contract("unrepairable")))
    monkeypatch.setattr(conservative, "_repair_step_file", lambda *a, **k: pytest.fail("v3 second-guessed v5"))
    with pytest.raises(conservative.RepairRefused) as err:
        conservative.repair_step_file(src, dest)
    assert err.value.measurements["engine"] == "v5"
    assert err.value.measurements[conservative.RESULT_V5_KEY]["outcome"] == "unrepairable"
    assert err.value.measurements["delivery_source"] == conservative.PASSTHROUGH_SOURCE


def test_an_explicit_engine_overrides_the_setting(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")
    monkeypatch.setattr(engine, "repair_file", lambda *a, **k: pytest.fail("v5 ran for a pinned v3 call"))
    v3 = RepairReport(defects=(), measurements=(), operations=(), summary="v3")
    monkeypatch.setattr(conservative, "_repair_step_file", lambda *a, **k: v3)
    assert conservative.repair_step_file(src, dest, engine="v3") is v3


def test_the_v5_clock_leaves_v3_its_own(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v5")
    monkeypatch.setattr(rcfg, "CRS_V5_FILE_WALL_S", 1800.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 420.0)
    assert conservative.repair_timeout_s() == 2220.0
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v3")
    assert conservative.repair_timeout_s() == 420.0


def test_the_beta_spend_cap_is_five_dollars_and_within_the_ceiling():
    from meshpipeline.cad.repair_v4.proposer import FILE_USD_CEILING

    assert rcfg.CRS_V5_FILE_USD_CAP == 5.0
    assert 0 < rcfg.CRS_V5_FILE_USD_CAP <= FILE_USD_CEILING
    assert rcfg.CRS_V5_AGENT is False
    assert rcfg.CRS_V5_FALLBACK is True


def test_a_cap_above_the_ceiling_refuses_the_v5_run(monkeypatch, files):
    src, dest = files
    monkeypatch.setattr(rcfg, "CRS_V5_FILE_USD_CAP", 50.0)
    with pytest.raises(engine.V5Unavailable, match="CRS_V5_FILE_USD_CAP"):
        engine.repair_file(src, dest, deadline_s=1e12)
