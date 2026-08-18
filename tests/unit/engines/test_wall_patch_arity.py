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
