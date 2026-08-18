# Responsibility: Verify the catalogue and the shipped bundles agree, and a planned engine ships nothing that can run.
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from meshpipeline.engines.quality_criteria import criteria_for
from meshpipeline.engines.registry import ENGINE_CATALOG, engine_names, get_spec

ENGINES_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "engines"


def test_the_catalog_and_the_shipped_bundles_agree():
    folders = {p.name for p in ENGINES_DIR.iterdir()
               if p.is_dir() and not p.name.startswith("__") and (p / "spec.py").exists()}
    assert folders == set(ENGINE_CATALOG), (
        f"packages without catalog rows: {folders - set(ENGINE_CATALOG)}; "
        f"rows without packages: {set(ENGINE_CATALOG) - folders}")
    for name, spec in ENGINE_CATALOG.items():
        assert spec.name == name, f"{name} registers itself as {spec.name!r}"


@pytest.mark.parametrize("name", sorted(engine_names()))
def test_every_implemented_engine_exposes_the_uniform_adapter_seam(name):
    mod = importlib.import_module(f"meshpipeline.engines.{name}.adapter")
    eng = mod.ENGINE()
    assert eng.name == name, f"engines/{name}/adapter.ENGINE.name mismatch"
    assert callable(eng.run_cartesian_mesh), (
        f"engines/{name}/adapter must forward the uniform run_cartesian_mesh seam")


@pytest.mark.parametrize("name", sorted(engine_names()))
def test_every_implemented_engine_declares_what_the_shared_layers_read(name):
    spec = get_spec(name)
    assert spec.gates, f"{name} declares no gates - its failures cannot be classified"
    assert spec.criteria, f"{name} exposes no criteria through its spec"
    assert tuple(criteria_for(name)) == tuple(spec.criteria)
    assert spec.review_rubric, f"{name} declares no review rubric"


def test_a_planned_engine_ships_a_spec_and_nothing_that_can_run():
    for name, spec in ENGINE_CATALOG.items():
        if spec.implemented:
            continue
        assert (ENGINES_DIR / name / "spec.py").exists()
        with pytest.raises(Exception):
            importlib.import_module(f"meshpipeline.engines.{name}.adapter").ENGINE()
