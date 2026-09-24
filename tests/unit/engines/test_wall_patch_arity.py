# Responsibility: Verify wall-patch arity is refused from the engine's own declaration and the geometry, never a hardcoded rule.
from __future__ import annotations

import dataclasses
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary  # noqa: E402
from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402

_TWO_WALLS = [("wing", "wall"), ("fuselage", "wall"), ("farfield", "farfield")]


def _ev(engine, patches, facts=None):
    return AdmissionEvidence(
        engine=engine, purpose="external_cfd", input_kind="body-surface",
        dimensionality="3D", surface_analysis=facts,
        patches=tuple(PatchSummary(n, t) for n, t in patches))


def _refused(spec, patches, facts=None) -> bool:
    return any(r.code == "multiple_wall_patches_unsupported"
               for r in spec.admit(_ev(spec.name, patches, facts)))


# the engine's own declaration

def test_an_engine_that_declares_it_cannot_is_refused():
    # No engine ships False today, so the rule is exercised against a spec that declares it -
    # the alternative is trusting a branch nothing runs.
    cannot = dataclasses.replace(get_spec("snappy"), supports_multiple_wall_patches=False,
                                 single_wall_patch_reason="declared for this test.")
    assert _refused(cannot, _TWO_WALLS)
    rejection = next(r for r in cannot.admit(_ev("snappy", _TWO_WALLS))
                     if r.code == "multiple_wall_patches_unsupported")
    assert rejection.phase == "declared"            # caught at intake, not after a build
    assert "wing" in rejection.message and "fuselage" in rejection.message
    assert "single wall patch" in rejection.fix_hint.lower()


def test_every_engine_declares_the_capability_explicitly():
    # A claim inherited from the dataclass default is nobody's decision. Each spec states it, so
    # flipping one is a deliberate act with a reason beside it.
    for name in engine_names():
        source = (APP / "engines" / name / "spec.py").read_text()
        assert "supports_multiple_wall_patches=" in source, \
            f"{name} inherits the capability instead of declaring it"


def test_an_engine_that_declares_it_can_is_not_refused_on_capability():
    for name in engine_names():
        spec = get_spec(name)
        if spec.supports_multiple_wall_patches:
            assert not _refused(spec, _TWO_WALLS), f"{name} declares it can, yet was refused"


def test_a_single_wall_patch_is_never_an_arity_problem():
    assert not _refused(get_spec("snappy"), [("aircraft", "wall"), ("farfield", "farfield")])


# the geometry, which the engine's capability cannot substitute for

def test_a_geometry_with_too_few_regions_is_refused_however_capable_the_engine():
    spec = get_spec("snappy")
    assert spec.supports_multiple_wall_patches is True
    assert _refused(spec, _TWO_WALLS, {"region_count": 1, "region_names": ["body"]})


def test_a_geometry_with_enough_regions_is_admitted():
    spec = get_spec("snappy")
    assert not _refused(spec, _TWO_WALLS,
                        {"region_count": 2, "region_names": ["wing", "fuselage"]})


def test_unknown_geometry_is_not_treated_as_an_absence():
    # A caller that supplied no facts is not asserting there are no regions. Refusing on silence
    # would block a request that may be perfectly deliverable.
    assert not _refused(get_spec("snappy"), _TWO_WALLS, None)


def test_the_rule_branches_on_declarations_not_engine_names():
    import re
    base = (APP / "engines" / "base.py").read_text()
    rule = base[base.index("_walls = [p.name for p in evidence.patches"):
                base.index("STRUCTURAL patch requirements")]
    code = re.sub(r"#.*", "", rule)          # a comment may name snappy as the example; code may not
    assert "snappy" not in code, "the shared admission rule branches on an engine name"
    assert "_supplies_fewer_regions" in code


# the names, which a count cannot stand in for

def test_regions_that_do_not_carry_the_declared_names_are_refused():
    # Six regions called a..f cannot become "wing" and "fuselage". The patch is written under its
    # region's name, so a count that fits proves nothing - and admitting this costs a full build
    # before the patch contract rejects it.
    spec = get_spec("snappy")
    assert _refused(spec, _TWO_WALLS,
                    {"region_count": 6, "region_names": list("abcdef")})


def test_matching_names_are_admitted_whatever_their_capitalisation():
    # A CAD exporter's capitalisation is not a difference the user meant.
    spec = get_spec("snappy")
    assert not _refused(spec, _TWO_WALLS,
                        {"region_count": 2, "region_names": ["WING", "Fuselage"]})


def test_the_refusal_names_what_the_file_actually_offers():
    spec = get_spec("snappy")
    rejection = next(r for r in spec.admit(
        _ev("snappy", _TWO_WALLS, {"region_count": 3, "region_names": ["hub", "shroud", "blade"]}))
        if r.code == "multiple_wall_patches_unsupported")
    assert "hub, shroud, blade" in rejection.message, rejection.message
    assert "wing" in rejection.message and "fuselage" in rejection.message


# WHAT THE REFUSAL SAYS ABOUT THE FILE, which is not the same question as whether to refuse

def test_a_file_with_several_unnamed_regions_is_not_called_one_region():
    """THE DECISION WAS RIGHT AND THE SENTENCE WAS FALSE, which is the worst of the two to leave.

    `region_count` is how many regions the file NAMES, and the measurement floors it at 1 because an
    unnamed file delivers one wall patch whatever is in it. This refusal read that 1 as the number of
    regions and said "Your geometry is one region with no component names" - false about a part with two,
    and it sends the customer to re-export as several regions when what they need is the names kept.
    MEASURED on tests/fixtures/geometry/cht_enclosing_2region.step: `facts.regions` is 2 and every name on
    it is a generated `Open CASCADE STEP translator` name, which the measurement drops.
    """
    spec = get_spec("snappy")
    facts = {"region_count": 1, "region_names": [], "connected_components": 2}
    assert _refused(spec, _TWO_WALLS, facts), "a file that names no region still cannot deliver two patches"
    message = next(r for r in spec.admit(_ev("snappy", _TWO_WALLS, facts))
                   if r.code == "multiple_wall_patches_unsupported").message
    assert "2 separate regions but names none of them" in message, message
    assert "is one region with no component names" not in message, message
    assert "names kept" in message, "the refusal has to say which of the two things to fix"


def test_a_file_that_really_is_one_region_still_says_so():
    spec = get_spec("snappy")
    for facts in ({"region_count": 1, "region_names": [], "connected_components": 1},
                  {"region_count": 1, "region_names": []}):
        message = next(r for r in spec.admit(_ev("snappy", _TWO_WALLS, facts))
                       if r.code == "multiple_wall_patches_unsupported").message
        assert "is one region with no component names" in message, (facts, message)


def test_a_measurement_stored_before_the_key_existed_changes_no_decision():
    """The key is absent on every row an older image wrote, and absent must mean SAYS NOTHING. Read as a
    zero it would claim a file has no regions; read as a one it would repeat the bug under a new name."""
    spec = get_spec("snappy")
    old = {"region_count": 2, "region_names": ["wing", "fuselage"]}
    assert not _refused(spec, _TWO_WALLS, old)
    for absent in (None, "", 0, True, "two"):
        facts = {**old, "connected_components": absent} if absent is not None else dict(old)
        assert not _refused(spec, _TWO_WALLS, facts), facts


def test_the_projection_the_agent_writes_carries_both_numbers():
    """The two numbers are only ever telling the truth together, and the platform drops every key it does
    not list (`cad/regions._PROJECTION_KEYS`), which is how the second one was lost."""
    from meshpipeline.cad.regions import _PROJECTION_KEYS
    assert "connected_components" in _PROJECTION_KEYS and "region_count" in _PROJECTION_KEYS
