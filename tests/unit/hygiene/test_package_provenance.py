# Responsibility: Verify the imported package comes from this checkout, and every package-data glob matches real files.
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).parents[3]


def test_the_imported_package_comes_from_this_checkout():
    import meshpipeline
    actual = Path(meshpipeline.__file__).resolve()
    assert actual == (REPO / "src" / "meshpipeline" / "__init__.py").resolve(), (
        f"meshpipeline resolved to {actual}, which is not this checkout ({REPO})")


def test_a_subprocess_also_imports_this_checkout():
    code = (
        "import pathlib, meshpipeline;"
        "print(pathlib.Path(meshpipeline.__file__).resolve());"
        "print(meshpipeline.__version__)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                         capture_output=True, text=True, check=True)
    path_line = out.stdout.strip().splitlines()[0]
    assert Path(path_line) == (REPO / "src" / "meshpipeline" / "__init__.py").resolve(), (
        f"subprocess imported {path_line}, not this checkout")


def test_the_composition_root_imports():
    # The composition root wires every adapter behind its contract. It is the one module whose
    # import exercises the full DI graph, so an import-time break here is a boot failure.
    import meshpipeline
    import meshpipeline.runtime.composition  # noqa: F401
    assert Path(meshpipeline.__file__).parent.name == "meshpipeline"


def test_every_declared_package_data_glob_matches_real_files():
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    root = REPO / "src" / "meshpipeline"
    for vals in cfg["tool"]["setuptools"]["package-data"].values():
        for g in vals:
            assert list(root.glob(g)), f"package-data glob matches nothing: {g}"


def test_no_stale_build_artifacts_shadow_the_source():
    for stale in ("build", "dist"):
        p = REPO / stale
        assert not p.exists(), (
            f"{stale}/ is present in the repo root and can shadow the source tree - remove it "
            "(it is generated, and .gitignore already excludes it)")
