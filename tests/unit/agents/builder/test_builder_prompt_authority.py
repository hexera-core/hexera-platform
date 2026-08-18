# Responsibility: Verify the builder loads its role prompt through the shared authority; each engine keeps its pack.
from __future__ import annotations

import importlib

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.registry import engine_names, get_spec

PACKS = {
    "cfmesh": "CFMESH_SYSTEM",
    "snappy": "SNAPPY_SYSTEM",
    "gmsh": "GMSH_SYSTEM",
    "vmtk": "VMTK_SYSTEM",
    "snappy_multiregion": "SNAPPY_MULTIREGION_SYSTEM",
}

#: Cross-engine Builder rules. Each must appear in the packaged role prompt, and none may be
#: re-stated by every engine - that duplication is the defect.
GENERAL_RULES = [
    "hand-write",            # tool-use discipline
    "COMPUTE, DON'T GUESS",  # measurement discipline
    "METRES",                # unit contract
    "ENGINE IS THE USER'S CHOICE",
    "ITERATE ONLY ON CONCRETE FAILURE",
    "CLAIM ONLY WHAT THE ENGINE OFFERS",
    "independent review renderer",
]


def _pack_text(engine: str) -> str:
    return getattr(importlib.import_module(f"meshpipeline.engines.{engine}.pack"), PACKS[engine])


def _assembled(engine: str) -> str:
    return "\n\n".join((polcfg.prompts.builder_system.strip(),
                        get_spec(engine).system_prompt.strip()))


# the shared authority

def test_the_builder_loads_its_role_prompt_through_the_same_authority_as_intake_and_reviewer():
    keys = dict(polcfg.REQUIRED_PROMPTS)
    assert keys["builder_system"] == "builder/system.txt"
    for role in ("reviewer_system", "intake_system", "builder_system"):
        text = getattr(polcfg.prompts, role)
        assert text and text.strip(), f"{role} loaded empty"
    # the three roles are symmetric: one packaged general prompt each
    assert {"reviewer_system", "intake_system", "builder_system"} <= set(keys)


def test_the_general_prompt_states_the_cross_engine_rules():
    text = polcfg.prompts.builder_system
    missing = [r for r in GENERAL_RULES if r not in text]
    assert not missing, f"the Builder role contract does not state: {missing}"


def test_the_packaged_prompt_is_shipped_and_a_missing_one_fails_loudly(tmp_path, monkeypatch):
    import meshpipeline.settings.env as env
    from meshpipeline.settings.env import ConfigurationError

    shipped = env.PROMPTS_DIR / "builder" / "system.txt"
    assert shipped.exists(), f"the Builder role prompt is not present at {shipped}"

    # a prompts directory that lacks it must refuse to load, not silently degrade
    (tmp_path / "reviewer").mkdir(parents=True)
    (tmp_path / "intake").mkdir(parents=True)
    (tmp_path / "reviewer" / "system.txt").write_text("r")
    (tmp_path / "intake" / "system.txt").write_text("i")
    monkeypatch.setattr(env, "PROMPTS_DIR", tmp_path)
    with pytest.raises((ConfigurationError, FileNotFoundError)):
        polcfg._load_all_prompts()


# specialization survives

@pytest.mark.parametrize("engine", sorted(PACKS))
def test_every_engine_still_contributes_its_own_specialization(engine):
    pack = _pack_text(engine)
    assert pack.strip(), f"{engine} contributes no engine text at all"
    # it must still name its own mesher and its own workflow
    assert any(k in pack for k in ("SEQUENCE", "WORKFLOW", "pipeline")), (
        f"{engine} no longer declares its own workflow")
    assembled = _assembled(engine)
    assert pack.strip() in assembled and polcfg.prompts.builder_system.strip() in assembled


def test_the_five_engines_remain_distinguishable():
    packs = {e: _pack_text(e) for e in PACKS}
    for a, b in ((x, y) for x in packs for y in packs if x < y):
        assert packs[a] != packs[b], f"{a} and {b} now carry identical engine text"


@pytest.mark.parametrize("engine", sorted(PACKS))
def test_engine_specific_instructions_were_not_centralized(engine):
    general = polcfg.prompts.builder_system
    engine_only = {
        "cfmesh": ["meshDict", "cartesianMesh", "max_cell_factor"],
        "snappy": ["snappyHexMesh", "surface_level"],
        "gmsh": ["gmsh_spec.json", "element_order"],
        "vmtk": ["centerline", "vmtk"],
        "snappy_multiregion": ["splitMeshRegions", "regionProperties"],
    }[engine]
    leaked = [t for t in engine_only if t in general]
    assert not leaked, f"{engine}-specific vocabulary leaked into the shared prompt: {leaked}"


# no duplication remains

@pytest.mark.parametrize("rule", GENERAL_RULES)
def test_no_general_rule_is_still_repeated_by_every_engine(rule):
    restating = [e for e in PACKS if rule in _pack_text(e)]
    assert len(restating) < len(PACKS), (
        f"every engine still states the general rule {rule!r} ({restating}); it belongs in "
        f"prompts/builder/system.txt")


@pytest.mark.parametrize("engine", sorted(PACKS))
def test_the_assembled_message_carries_the_role_contract_exactly_once(engine):
    assembled = _assembled(engine)
    assert assembled.count("independent review renderer") == 1, (
        f"{engine}: the review-renderer boundary appears more than once in the assembled prompt")


def test_every_registered_engine_is_covered_by_this_contract():
    assert set(engine_names()) == set(PACKS), (
        "an engine was added or removed without updating the Builder prompt contract")
