# Responsibility: Verify the render stub is installed only when the package is genuinely absent, and shadows nothing.
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: Every module that used to install its own stand-in. None may do so again.
FORMERLY_STUBBING = [
    "tests/unit/pipeline/test_data_contract.py",
    "tests/unit/pipeline/test_graph_real.py",
    "tests/unit/infra/test_import_sweep.py",
    "tests/unit/worker/test_terminal_authority_mutations.py",
    "tests/unit/worker/test_final_result_containment.py",
    "tests/unit/worker/test_dispute_flow.py",
    "tests/unit/worker/test_worker_terminal_matrix.py",
    "tests/unit/engines/test_user_engine_control.py",
]


def test_no_test_module_installs_a_render_stub_of_its_own():
    offenders = []
    for rel in FORMERLY_STUBBING:
        src = (REPO / rel).read_text()
        for dep in ('"pyvista"', "'pyvista'", '"PIL"', "'PIL'"):
            if f"sys.modules[{dep}]" in src:
                offenders.append(f"{rel} assigns sys.modules[{dep}]")
    assert not offenders, (
        "render stand-ins must be installed only by tests/unit/conftest.py: " + "; ".join(offenders))


def test_the_conftest_stub_never_shadows_an_installed_package():
    from tests.unit.conftest import _stub_if_absent

    sentinel = types.ModuleType("json")
    sentinel.marker = "STUB"
    installed = _stub_if_absent("json", lambda: {"json": sentinel})
    assert installed is False, "a stub was installed over an importable package"
    assert not hasattr(importlib.import_module("json"), "marker"), "the real json was replaced"


def test_the_stub_is_installed_when_the_package_is_genuinely_absent():
    from tests.unit.conftest import _stub_if_absent

    name = "meshpipeline_no_such_render_dep"
    made = types.ModuleType(name)
    made.marker = "STUB"
    try:
        assert _stub_if_absent(name, lambda: {name: made}) is True
        assert sys.modules[name].marker == "STUB"
    finally:
        sys.modules.pop(name, None)


def test_this_process_resolved_the_real_render_dependencies_when_present():
    from tests.unit.conftest import RENDER_STUBS_INSTALLED

    for name in ("pyvista", "PIL"):
        if RENDER_STUBS_INSTALLED[name]:
            continue                      # genuinely absent here - nothing to check
        mod = importlib.import_module(name)
        assert getattr(mod, "__file__", None), (
            f"{name} reports no __file__, so this process is running a stand-in while the real "
            f"package is importable")


# negative control

def test_restoring_the_leak_makes_the_ordered_pair_fail_again():
    leak = (
        "import sys, types\n"
        "_pv = types.ModuleType('pyvista')\n"
        "_pv.PolyData = object\n"
        "sys.modules['pyvista'] = _pv\n"
    )
    conftest = REPO / "tests" / "unit" / "conftest.py"
    original = conftest.read_bytes()
    try:
        conftest.write_bytes(original + b"\n\n# MUTATION\n" + leak.encode())
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
             "--no-header", "tests/unit/engines/test_consolidated_matrix.py"],
            # PYTEST_ADDOPTS removed for the same reason the plugin is disabled above: CI sets it
            # to --randomly-seed=<n>, which a child running -p no:randomly cannot parse.
            cwd=REPO, capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"})
    finally:
        conftest.write_bytes(original)
    assert conftest.read_bytes() == original, "the mutation was not restored byte-for-byte"
    assert r.returncode != 0, (
        "re-introducing the unconditional pyvista stub did NOT break the matrix tests, so this "
        "contract is not load-bearing")
    assert "has no attribute" in (r.stdout + r.stderr), (
        "the failure was not the shadowing defect: " + (r.stdout + r.stderr)[-600:])
