# Responsibility: Verify every engine maps every fidelity tier within bounds, and no gate or criterion reads fidelity.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.registry import get_spec

SRC = Path(__file__).parents[3] / "src" / "meshpipeline"
_ENGINES = ["cfmesh", "snappy", "snappy_multiregion", "vmtk", "gmsh"]

# A minimal analyze_surface()-shaped dict the recommenders consume.
_ANALYSIS = {"L": 1.0, "diag": 1.0, "min_feature": 0.01, "thin_gap": 0.02,
             "extent": [1.0, 1.0, 1.0], "surface_area": 6.0, "n_triangles": 1000,
             "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("tier", ["draft", "standard", "max"])
def test_every_engine_maps_every_tier_and_stays_bounded(engine, tier):
    import meshpipeline.settings.policy as polcfg
    rec = get_spec(engine).recommend_authoring(_ANALYSIS, fidelity=tier)
    assert isinstance(rec, dict) and rec, f"{engine}/{tier} produced no recommendation"
    for key in ("max_cells", "cell_budget", "element_budget"):
        if key in rec:
            assert 0 < int(rec[key]) <= polcfg.CELL_HARD_LIMIT


@pytest.mark.parametrize("engine", _ENGINES)
def test_tiers_are_ordered_finer(engine):
    d = get_spec(engine).recommend_authoring(_ANALYSIS, fidelity="draft")
    m = get_spec(engine).recommend_authoring(_ANALYSIS, fidelity="max")
    moved = False
    if "cell_sizes_m" in d:                       # cfmesh: larger size = coarser
        moved = d["cell_sizes_m"]["base"] > m["cell_sizes_m"]["base"]
    elif "surface_level" in d:                    # snappy family: higher level = finer
        moved = max(d["surface_level"]) < max(m["surface_level"])
    elif "edge_length_factor" in d:               # vmtk: lower factor = finer
        moved = d["edge_length_factor"] > m["edge_length_factor"]
    elif "target_element_size_m" in d:            # gmsh: larger size = coarser
        moved = d["target_element_size_m"] > m["target_element_size_m"]
    assert moved, f"{engine}: draft and max produced the same detail"




@pytest.mark.parametrize("engine", _ENGINES)
def test_no_tier_and_unknown_tier_fall_back_to_standard_shaped(engine):
    assert get_spec(engine).recommend_authoring(_ANALYSIS)                       # no tier
    assert get_spec(engine).recommend_authoring(_ANALYSIS, fidelity="bogus")     # unknown → standard


def test_gmsh_tier_never_changes_element_order():
    for tier in ("draft", "standard", "max"):
        rec = get_spec("gmsh").recommend_authoring(_ANALYSIS, fidelity=tier)
        assert rec.get("element_order") == "unchanged"


# the tier does NOT reach any gate, the Reviewer, or artifact policy
def _reads_fidelity(path: Path) -> bool:
    src = path.read_text()
    return any(tok in src for tok in ("mesh_fidelity", "MeshFidelity", "effective_mesh_fidelity"))


def test_no_engine_gate_reads_fidelity():
    for engine in _ENGINES:
        for name in ("gates.py", "flow_gates.py"):
            p = SRC / "engines" / engine / name
            if p.exists():
                assert not _reads_fidelity(p), f"{engine}/{name} must not read fidelity"


def test_no_engine_criteria_or_review_reads_fidelity():
    for engine in _ENGINES:
        for name in ("criteria.py", "review_targets.py", "review_renderer.py"):
            p = SRC / "engines" / engine / name
            if p.exists():
                assert not _reads_fidelity(p), f"{engine}/{name} must not read fidelity"
