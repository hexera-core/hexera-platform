# Responsibility: Verify the type baseline holds no stale or ghost entry, and no blanket ignore replaced a fix.
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RATCHET = REPO / "devtools" / "quality" / "mypy_ratchet.py"
BASELINE = REPO / "devtools" / "quality" / "mypy_baseline.txt"


def _run() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RATCHET)], cwd=REPO,
                          capture_output=True, text=True)


@pytest.fixture()
def restore_baseline():
    original = BASELINE.read_bytes()
    yield
    BASELINE.write_bytes(original)


def test_the_baseline_has_no_stale_entries_right_now():
    r = _run()
    assert r.returncode == 0, f"the ratchet is not green:\n{r.stdout}\n{r.stderr}"
    assert "no longer occur" not in r.stdout


def test_a_reintroduced_stale_entry_is_rejected(restore_baseline):
    cured = ('src/meshpipeline/engines/vmtk/vmtk_runner.py::union-attr::Item "None" of '
             '"RunPolicy | None" has no attribute "fail_hint"')
    with BASELINE.open("a") as fh:
        fh.write(cured + "\n")
    r = _run()
    assert r.returncode == 1, "a stale baseline entry was accepted"
    assert "no longer occur" in r.stdout
    assert "fail_hint" in r.stdout, "the offending entry must be named, not just counted"


def test_a_ghost_path_is_still_rejected(restore_baseline):
    with BASELINE.open("a") as fh:
        fh.write('src/meshpipeline/does_not_exist.py::attr-defined::whatever\n')
    r = _run()
    assert r.returncode == 1
    assert "do not exist" in r.stdout


def test_the_baseline_is_the_only_one(restore_baseline):
    found = [p for p in (REPO / "devtools").rglob("*mypy*baseline*") if p.is_file()]
    assert found == [BASELINE], f"expected exactly one baseline, found {found}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_no_blanket_ignores_were_added_in_place_of_fixing(restore_baseline):
    text = BASELINE.read_text()
    assert "# type: ignore" not in text
    for entry in text.splitlines():
        assert entry.count("::") >= 2, f"malformed baseline entry: {entry!r}"
