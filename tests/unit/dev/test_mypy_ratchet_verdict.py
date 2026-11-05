# Responsibility: Prove the type ratchet reports a crashed analysis as no verdict, never as a cured baseline.
# Boundaries: the ratchet's own execution contract; what mypy decides about the code is elsewhere.
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RATCHET = REPO / "devtools" / "quality" / "mypy_ratchet.py"


def _module():
    spec = importlib.util.spec_from_file_location("mypy_ratchet_probe", RATCHET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_mypy_that_cannot_run_is_not_read_as_an_empty_error_set(monkeypatch):
    mod = _module()

    class _Crashed:
        returncode = 2
        stdout = ""
        stderr = "error: INTERNAL ERROR -- mypy could not write its cache"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Crashed())
    with pytest.raises(mod.MypyDidNotRun) as exc:
        mod.collect()
    assert "without producing a verdict" in str(exc.value)


@pytest.mark.parametrize("code", [0, 1])
def test_real_verdicts_are_still_accepted(monkeypatch, code):
    mod = _module()

    class _Ran:
        returncode = code
        stdout = "src/meshpipeline/x.py:1: error: something [attr-defined]"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Ran())
    assert mod.collect() == {"src/meshpipeline/x.py::attr-defined::something"}


def test_the_entry_point_exits_distinctly_when_no_verdict_was_reached(monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "_run", lambda: (_ for _ in ()).throw(mod.MypyDidNotRun("boom")))
    assert mod.main() == 2, "a crashed analysis must not share an exit code with a real ratchet event"


def test_the_semantic_target_is_pinned_by_the_project_not_the_interpreter():
    # Both supported interpreters (3.12 from `make setup`, 3.11 in the validation image) must
    # analyse the SAME language target, or the baseline is only valid where it was generated.
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'python_version = "3.11"' in text, (
        "the mypy language target is no longer pinned in the project configuration, so the "
        "baseline would depend on whichever interpreter happened to launch mypy")


def test_the_ratchet_runs_under_this_interpreter_and_agrees_with_the_baseline():
    done = subprocess.run([sys.executable, str(RATCHET)], cwd=str(REPO),
                          capture_output=True, text=True, timeout=1200)
    if done.returncode == 2 and "CANNOT CHECK" in done.stdout:
        pytest.skip("this checkout is read-only, so mypy cannot cache; covered by the unit above")
    assert done.returncode == 0, f"{done.stdout[-1500:]}\n{done.stderr[-800:]}"
    assert "0 new" in done.stdout
