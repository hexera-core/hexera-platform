# Responsibility: Verify the platform layer holds only cross-cutting seams and no generated directory is tracked.
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.splitlines() if p.strip()]


def _platform_files() -> list[str]:
    d = REPO / "tests" / "unit" / "platform"
    return [p.name for p in d.glob("test_*.py")]


# CI runs `pytest tests/unit/platform` as its own named BLOCKING step (.github/workflows/ci.yml),
# whose whole claim is that both shipped backends of every cross-cutting seam are covered. If those
# files were moved or deleted the step would still pass - green, and covering nothing. This asserts
# the step has something to run.
def test_platform_holds_only_cross_cutting_seams():
    here = set(_platform_files())
    for seam in ("test_object_store.py", "test_mesh_execution_backends.py",
                 "test_pipeline_execution_seam.py", "test_redis_capabilities.py",
                 "test_web_search_provider.py", "test_model_router_contract.py"):
        assert seam in here, f"{seam} is a cross-cutting seam and must stay in tests/unit/platform/"


# generated / runtime directories are never tracked
_GENERATED_PREFIXES = ("data/", "workspaces/", "deploy/output/", "deploy/gcp/.rendered/")


@pytest.mark.parametrize("prefix", _GENERATED_PREFIXES)
def test_generated_runtime_dirs_are_not_tracked(prefix):
    tracked = [rel for rel in _tracked() if rel.startswith(prefix)]
    assert not tracked, (
        f"{prefix} is runtime-generated (gitignored) state - these files must not be tracked: "
        f"{tracked[:10]}")


def _working_tree_sources(*prefixes: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    seen: list[Path] = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        if not rel or not rel.endswith(".py"):
            continue
        if prefixes and not rel.startswith(prefixes):
            continue
        path = REPO / rel
        if path.is_file():                      # skip paths deleted in the working tree
            seen.append(path)
    return seen


# production source never imports a test module
def test_production_source_never_imports_a_test_module():
    offenders: list[str] = []
    for path in _working_tree_sources("src/meshpipeline/"):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(("import tests", "from tests ", "from tests.")):
                offenders.append(f"{rel}: {s}")
    assert not offenders, f"production source imports test code: {offenders}"
