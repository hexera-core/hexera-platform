# Responsibility: Pin the CAD_REPAIR_ENGINE switch - the default is v3, an unknown value is rejected,
#                 `search` dispatches to repair_v4 and nothing else does, and the production block
#                 of the deploy workflow never states the flag.
# Boundaries: hermetic; reads settings, the inventory and deploy.yml, and runs `repair_step_file`
#             with the v4 entry point replaced. No kernel, no search.
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import meshpipeline.cad.repair.conservative as conservative
import meshpipeline.cad.repair_v4 as v4
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import RepairReport
from meshpipeline.settings.env import ConfigurationError
from meshpipeline.settings.inventory import all_vars

ROOT = Path(__file__).parents[4]
SRC = ROOT / "src"
WF = ROOT / ".github" / "workflows" / "deploy.yml"


def _read_engine(value: str | None) -> subprocess.CompletedProcess:
    """Import the settings module in a fresh interpreter, so the module-level read is what's tested."""
    env = {k: v for k, v in os.environ.items() if k != "CAD_REPAIR_ENGINE"}
    if value is not None:
        env["CAD_REPAIR_ENGINE"] = value
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c",
         "import meshpipeline.settings.cad_repair as c; print(c.CAD_REPAIR_ENGINE)"],
        env=env, capture_output=True, text=True, cwd=ROOT)


def test_default_engine_is_v3():
    unset = _read_engine(None)
    assert unset.returncode == 0, unset.stderr
    assert unset.stdout.strip() == "v3"
    blank = _read_engine("")
    assert blank.returncode == 0, blank.stderr
    assert blank.stdout.strip() == "v3"
    entry = next(e for e in all_vars() if e.name == "CAD_REPAIR_ENGINE")
    assert entry.default == "v3"
    assert entry.exposure == "internal"


@pytest.mark.parametrize("value", ["search", " Search "])
def test_search_value_is_accepted(value):
    done = _read_engine(value)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "search"


@pytest.mark.parametrize("value", ["v4", "V5", "v3,search", "on", "true", "live"])
def test_unknown_value_rejected(value):
    done = _read_engine(value)
    assert done.returncode != 0
    assert "CAD_REPAIR_ENGINE" in done.stderr
    with pytest.raises(ConfigurationError):
        rcfg._engine_posture_of(value)


def test_search_engine_dispatches(monkeypatch, tmp_path):
    src, dest = tmp_path / "in.step", tmp_path / "out.step"
    src.write_bytes(b"ISO-10303-21;")
    seen: dict = {}

    def fake(source, destination, *, budget, deadline_s):
        seen.update(source=source, destination=destination, budget=budget, deadline_s=deadline_s)
        Path(destination).write_bytes(b"repaired")
        return v4.V4Result(trace_path=tmp_path / "trace.jsonl",
                           verdict_summary={"status": "pass"}, delivered=True)

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "search")
    monkeypatch.setattr(v4, "repair_v4", fake)

    def v3_must_not_run(*a, **k):
        raise AssertionError("v3 ran under CAD_REPAIR_ENGINE=search")

    monkeypatch.setattr(conservative, "_repair_step_file", v3_must_not_run)
    report = conservative.repair_step_file(src, dest)
    assert isinstance(report, RepairReport)
    assert seen["source"] == str(src) and seen["destination"] == str(dest)
    assert seen["deadline_s"] > 0
    assert report.diagnostics["engine"] == "search"
    assert report.diagnostics["verdict"] == {"status": "pass"}
    assert dest.read_bytes() == b"repaired"


def test_v3_engine_never_reaches_v4(monkeypatch, tmp_path):
    ran: list = []
    sentinel = RepairReport(defects=(), measurements=(), operations=(), summary="v3")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v3")
    monkeypatch.setattr(conservative, "_repair_step_file", lambda *a, **k: sentinel)
    monkeypatch.setattr(v4, "repair_v4", lambda *a, **k: ran.append(1))
    assert conservative.repair_step_file(tmp_path / "a", tmp_path / "b") is sentinel
    assert ran == []


def test_undelivered_result_is_a_refusal_not_a_report(monkeypatch, tmp_path):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "search")
    monkeypatch.setattr(
        v4, "repair_v4",
        lambda s, d, *, budget, deadline_s: v4.V4Result(
            trace_path=None, verdict_summary={"status": "fail"}, delivered=False))
    with pytest.raises(conservative.RepairRefused) as err:
        conservative.repair_step_file(tmp_path / "a", tmp_path / "b")
    assert err.value.measurements["engine"] == "search"


def test_unwired_search_is_unavailable_not_a_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "search")
    with pytest.raises(conservative.RepairUnavailable):
        conservative.repair_step_file(tmp_path / "a", tmp_path / "b")


def _prod_block() -> str:
    """The text of the `v*` release branch of the target picker - what a production run exports."""
    text = WF.read_text()
    start = text.index('echo "environment=prod"')
    # walk back to the start of the branch, forward to the end of the picker's prod arm
    head = text.rfind("\n", 0, text.rfind("if ", 0, start))
    end_candidates = [m.start() for m in re.finditer(r"\n\s+fi\n", text[start:])]
    assert end_candidates, "could not find the end of the prod branch"
    return text[head:start + end_candidates[0]]


def test_prod_posture_does_not_set_engine():
    block = _prod_block()
    assert 'deployment_id=prod' in block and 'project=hexera-prod' in block, \
        "parsed the wrong block: not the production arm"
    assert "CAD_REPAIR_ENGINE" not in block
    assert "cad-repair-engine" not in block
