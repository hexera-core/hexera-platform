# Responsibility: Verify every producer states the mesh unit explicitly, and no consumer defaults or coerces one.
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.mesh_units import (
    COMPLETED_MESH_UNIT,
    MESH_UNITS_KEY,
    MeshUnitsError,
    completed_mesh_unit,
    validate_completed_mesh_unit,
)

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"

#: The five shipped engines. Named explicitly so adding a sixth without stating its unit is a
#: failure here rather than a discovery in production.
ENGINES = ("cfmesh", "snappy", "gmsh", "vmtk", "snappy_multiregion")


# the canonical vocabulary

def test_the_completed_mesh_unit_is_the_existing_metre_value_not_a_new_one():
    assert COMPLETED_MESH_UNIT is LengthUnit.metre
    assert COMPLETED_MESH_UNIT.value == "m"


def test_the_other_input_units_remain_valid_geometry_units():
    assert {u.value for u in LengthUnit} == {"m", "mm", "cm", "in"}


# producers

def _write_manifest_calls() -> dict[str, list[ast.Call]]:
    found: dict[str, list[ast.Call]] = {e: [] for e in ENGINES}
    for path in (SRC / "engines").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                      # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "write_manifest":
                continue
            for engine in ENGINES:
                if f"/engines/{engine}/" in str(path):
                    found[engine].append(node)
    return found


@pytest.mark.parametrize("engine", ENGINES)
def test_every_engine_states_its_unit_explicitly(engine):
    calls = _write_manifest_calls()[engine]
    assert calls, f"{engine} has no write_manifest call to inspect"
    for call in calls:
        kw = {k.arg for k in call.keywords}
        assert MESH_UNITS_KEY in kw, f"{engine} writes a manifest without stating {MESH_UNITS_KEY}"


def test_the_producer_has_no_default_so_omission_cannot_ship():
    import inspect

    from meshpipeline.engines.manifest import write_manifest
    sig = inspect.signature(write_manifest)
    p = sig.parameters[MESH_UNITS_KEY]
    assert p.default is inspect.Parameter.empty, "the producer default is back"
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


def test_producer_omission_raises_rather_than_writing_an_unlabelled_artifact(tmp_path):
    from meshpipeline.engines.manifest import write_manifest
    with pytest.raises(TypeError, match=MESH_UNITS_KEY):
        write_manifest(tmp_path, patch_types={}, patch_entities={},
                       bbox=(0, 0, 0, 1, 1, 1), quality={})
    assert not (tmp_path / "mesh_manifest.json").exists(), "an unlabelled manifest reached disk"


def test_a_produced_manifest_round_trips_the_typed_metre_value(tmp_path):
    from meshpipeline.engines.manifest import write_manifest
    write_manifest(tmp_path, patch_types={}, patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
                   quality={}, mesh_units=COMPLETED_MESH_UNIT.value)
    stored = json.loads((tmp_path / "mesh_manifest.json").read_text())
    assert stored[MESH_UNITS_KEY] == "m"
    assert completed_mesh_unit(stored) is COMPLETED_MESH_UNIT


@pytest.mark.parametrize("bad", ["mm", "cm", "in", "M", "metre", "units", "", "  ", None,
                                 1, 1.0, True, [], {}, ["m"], {"unit": "m"}])
def test_the_producer_refuses_anything_that_is_not_canonical_metres(tmp_path, bad):
    from meshpipeline.engines.manifest import write_manifest
    with pytest.raises(MeshUnitsError):
        write_manifest(tmp_path, patch_types={}, patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
                       quality={}, mesh_units=bad)
    assert not (tmp_path / "mesh_manifest.json").exists()


# the single reader

@pytest.mark.parametrize("bad", ["mm", "cm", "in", "M", "metre", "units", "", "  ",
                                 None, 1, 1.0, True, [], {}, ["m"]])
def test_the_reader_refuses_every_malformed_value(bad):
    with pytest.raises(MeshUnitsError):
        validate_completed_mesh_unit(bad)
    with pytest.raises(MeshUnitsError):
        completed_mesh_unit({MESH_UNITS_KEY: bad})


def test_a_missing_key_is_refused_rather_than_defaulted():
    with pytest.raises(MeshUnitsError):
        completed_mesh_unit({})
    with pytest.raises(MeshUnitsError):
        completed_mesh_unit(None)


def test_a_valid_manifest_reads_as_the_typed_unit():
    assert completed_mesh_unit({MESH_UNITS_KEY: "m"}) is COMPLETED_MESH_UNIT


def test_no_malformed_value_is_ever_coerced_into_metres():
    for bad in ("mm", "cm", "in", "M", "", None, 3):
        try:
            got = validate_completed_mesh_unit(bad)
        except MeshUnitsError:
            continue
        pytest.fail(f"{bad!r} was coerced to {got!r} instead of refused")


# consumers agree

def _valid_manifest() -> dict:
    return {MESH_UNITS_KEY: "m", "mesh_mode": "cfmesh", "cell_count": 10,
            "patch_types": {"wall": "wall"}, "patch_face_counts": {"wall": 4},
            "quality": {"cells": 10, "faces": 40}}


def test_every_durable_consumer_reports_metres_for_a_valid_artifact():
    from meshpipeline.render.mesh_facts import quality as mesh_quality
    from meshpipeline.sandbox.render_inputs import RenderMetadata

    man = _valid_manifest()
    assert mesh_quality(man)["units"] == "m"
    assert RenderMetadata.from_manifest(man).mesh_units == "m"


@pytest.mark.parametrize("man", [{}, {MESH_UNITS_KEY: "mm"}, {MESH_UNITS_KEY: ""},
                                 {MESH_UNITS_KEY: None}])
def test_the_renderer_refuses_a_malformed_artifact_instead_of_inventing_mm(man):
    from meshpipeline.sandbox.render_inputs import RenderMetadata
    with pytest.raises(MeshUnitsError):
        RenderMetadata.from_manifest(man)


@pytest.mark.parametrize("man", [{}, {MESH_UNITS_KEY: "mm"}, {MESH_UNITS_KEY: 5}])
def test_mesh_facts_refuses_a_malformed_artifact_instead_of_dropping_the_unit(man):
    from meshpipeline.render.mesh_facts import quality as mesh_quality
    with pytest.raises(MeshUnitsError):
        mesh_quality(man)


def test_the_viewer_payload_refuses_a_malformed_stored_manifest(tmp_path):
    from meshpipeline.application.viewer_payload import _build_surface_payload
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({MESH_UNITS_KEY: "mm"}))
    with pytest.raises(MeshUnitsError):
        _build_surface_payload(tmp_path)


def test_the_input_skin_still_renders_when_no_mesh_has_been_delivered(tmp_path):
    from meshpipeline.application.viewer_payload import _build_surface_payload
    assert _build_surface_payload(tmp_path) is None      # nothing staged, but no raise


# fitness: no fallbacks

#: Where a unit fallback would actually do damage. `cad/` and the geometry-normalisation layer are
#: deliberately NOT here: they interpret INPUT units, where mm/cm/in are correct and expected.
_CONSUMER_AREAS = ("application", "api", "agents/reviewer", "render", "sandbox", "engines")


def _production_files() -> list[Path]:
    out: list[Path] = []
    for area in _CONSUMER_AREAS:
        out.extend(sorted((SRC / area).rglob("*.py")))
    return out


def test_the_fitness_rule_actually_scanned_the_real_producers_and_consumers():
    files = _production_files()
    assert len(files) > 50, f"the scan found only {len(files)} files - it is pointed somewhere wrong"
    mentioning = [p for p in files if MESH_UNITS_KEY in p.read_text()]
    names = {p.name for p in mentioning}
    # The known authority sites, by name, so a move that loses one is visible. The API route is
    # deliberately absent: it serves the stored viewer artifact and resolves no unit of its own,
    # so a mention there would mean the authority had leaked back into transport.
    for expected in ("manifest.py", "viewer_payload.py", "mesh_facts.py",
                     "render_inputs.py", "visual.py", "render_runtime.py"):
        assert expected in names, f"{expected} no longer mentions {MESH_UNITS_KEY} - authority moved?"
    assert len(mentioning) >= 10


def test_no_consumer_restores_a_defaulted_get():
    offenders = []
    for p in _production_files():
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == MESH_UNITS_KEY):
                offenders.append(f"{p.relative_to(SRC)}:{node.lineno}")
    assert not offenders, (
        "a defaulted mesh_units read is back - one reader, no defaults: " + ", ".join(offenders))


@pytest.mark.parametrize("literal", ["mm", "units", "cm", "in"])
def test_no_fallback_unit_literal_is_substituted_for_a_missing_value(literal):
    import re
    patterns = [re.compile(p) for p in (
        rf'\bor\s+["\']{literal}["\']',            # x or "mm"
        rf',\s*["\']{literal}["\']\s*\)',           # .get(k, "mm")
        rf'mesh_units\s*[:=]\s*str\s*=\s*["\']{literal}["\']',   # field default
        rf'mesh_units\s*=\s*["\']{literal}["\']',    # mesh_units="mm"
    )]
    offenders = []
    for p in _production_files():
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(rx.search(line) for rx in patterns):
                offenders.append(f"{p.relative_to(SRC)}:{i}  {line.strip()[:70]}")
    assert not offenders, f"{literal!r} reappeared as a unit fallback: {offenders}"


def test_the_geometry_normalisation_layer_is_untouched_by_this_rule():
    units_src = (SRC / "contracts" / "geometry_units.py").read_text()
    assert '"mm"' in units_src and '"in"' in units_src, (
        "the INPUT vocabulary lost its non-metre units - this rule has overreached")
    assert "SCALE_TO_METRES" in units_src


def test_no_second_key_became_an_alternative_authority():
    offenders = []
    for p in _production_files():
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "units"):
                offenders.append(f"{p.relative_to(SRC)}:{node.lineno}")
    assert not offenders, f'"units" is being read as a manifest key: {offenders}'


# geometry is untouched

def test_this_contract_performs_no_conversion_arithmetic():
    src = (SRC / "contracts" / "mesh_units.py").read_text()
    # Executable lines only - the module docstring legitimately EXPLAINS that it does not convert.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    code = code.split('"""', 2)[-1]          # drop the module docstring
    for forbidden in ("SCALE_TO_METRES", "* 1e", "/ 1e", "0.0254", "* 1000", "/ 1000"):
        assert forbidden not in code, f"{forbidden!r} appears in the completed-mesh unit contract"


def test_the_contract_adds_no_third_party_dependency():
    src = (SRC / "contracts" / "mesh_units.py").read_text()
    for forbidden in ("import pint", "import numpy", "import astropy", "unyt"):
        assert forbidden not in src
