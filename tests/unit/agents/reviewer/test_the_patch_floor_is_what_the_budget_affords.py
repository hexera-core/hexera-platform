"""One patch of any kind was the whole floor on four engines, and now the budget sets it.

cfmesh_conjugate's PATCH obligation was fixed first, because its renderable entities are its two
regions and exact coverage costs two toggles. The other four carried the identical obligation -
`TargetObligation(kind=PATCH)`, kind-only, min_count falling through to ONE - under the identical
provenance string, "every renderable boundary patch". The reason they were left is arithmetic:
they render one entity per body's patch (cfMesh, snappy) or one per placed part (snap-grid,
snappy_multiregion), and a generated 2,000-part board cannot be inspected part by part inside a
64-image session.

WHAT THE SPLIT IS. An obligation feeds two checks that cost completely different things.
`missing_target_obligations` compares the named ids against what the session discovered: a set
comparison, before the model is asked, costing nothing - and it is the check that catches an
entity the mesh HAS and the review surface LOST, which is the shape job 575f76e6 came back in.
`_check_obligation` demands a real inspection per id, one image each out of 64. Tying them
together is what forced the choice between 1 and N. Named apart, every entity is named and the
inspection floor is what the budget affords.

The floor and its arithmetic are TargetObligation.PATCH_INSPECTION_FLOOR; what it buys on the
counts real jobs actually deliver is the first section below.
"""
from __future__ import annotations

import pytest

from meshpipeline.agents.reviewer.eligibility import (
    _check_obligation,
    missing_target_obligations,
)
from meshpipeline.contracts.evidence_ledger import (
    PATCH_INSPECTION_FLOOR,
    EvidenceLedger,
    InspectionTargetRef,
    TargetObligation,
)
from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines.manifest import renderable_entities
from meshpipeline.engines.registry import engine_names, get_spec

#: Every engine whose review surface is drawn patch by patch - the four that were left on the
#: kind-only obligation, plus the conjugate one that was fixed first. All five read one rule now.
PATCH_ENGINES = ("cfmesh", "snappy", "snappy_multiregion", "snapgrid", "cfmesh_conjugate")

#: A board far past the inspection floor and still under the renderer's own MAX_PATCHES
#: truncation, so these tests measure the floor and not the cap (which has its own test below).
MANY = 300


def _manifest(n_entities: int, n_slices: int = 3) -> dict:
    """A manifest shaped like the ones finalize writes: `patches` maps every declared boundary
    to the review surface's entity tags, and a name with an EMPTY list is one the mesh has and
    the review surface does not carry (snappy's far field)."""
    return {
        "patches": {f"body_{i}": [i + 1] for i in range(n_entities)},
        "quality": {"regions": [{"name": f"body_{i}"} for i in range(n_entities)]},
        "mesh_paths": {"volume": "VTK/case_0/internal.vtu"},
        "inspection_regions": [{"name": f"slice_{i}"} for i in range(n_slices)],
    }


def _patch_obligation(engine: str, manifest: dict) -> TargetObligation:
    obs = [o for o in get_spec(engine).expected_target_obligations(manifest, {}, "")
           if o.kind is TargetKind.PATCH]
    assert len(obs) == 1, f"{engine} declares {len(obs)} patch obligations"
    return obs[0]


# #
# what the floor is, and what it buys on the counts real jobs deliver
# #
def test_the_floor_is_a_third_of_what_the_view_budget_leaves():
    """64 images, less the opening view, less at most four declared cuts, is 59. An inspection
    is one image and two when framed first, so a floor of k costs about 2k. A third of 59 is 19
    images, about 10 targets - and a floor that ate the budget would leave nothing to look twice
    with."""
    from meshpipeline.engines.snapgrid.results import HOT_PARTS
    from meshpipeline.sandbox.review_session import MAX_IMAGES_PER_SESSION

    left = MAX_IMAGES_PER_SESSION - 1 - (HOT_PARTS + 1)
    assert left == 59
    assert PATCH_INSPECTION_FLOOR * 2 <= left / 3 + 1


@pytest.mark.parametrize("engine", PATCH_ENGINES)
@pytest.mark.parametrize(("delivered", "note"), [
    (2, "snappy_multiregion bb5c9179, plate + derived air"),
    (3, "the body-patch rosters: body + farfield, or inlet + outlet + wall"),
    (9, "snap-grid f8542f71, set_top_box.ecxml"),
])
def test_every_count_a_real_job_delivered_is_held_to_exact_coverage(engine, delivered, note):
    """The floor is not a sample on anything that has actually been shipped. Every delivered
    entity count on this box is under it, so all five engines demand all of them."""
    o = _patch_obligation(engine, _manifest(delivered))
    assert o.required_inspections() == delivered, note
    assert not o.samples


@pytest.mark.parametrize("engine", PATCH_ENGINES)
@pytest.mark.parametrize("stress", [19, MANY, 2000])
def test_only_the_stress_cases_sample_and_they_sample_ten_not_one(engine, stress):
    """A 19-solid assembly and a generated 2,000-part board. The obligation they had asked for
    ONE inspection of ONE entity."""
    o = _patch_obligation(engine, _manifest(stress))
    assert o.samples
    assert o.required_inspections() == PATCH_INSPECTION_FLOOR == 10


@pytest.mark.parametrize("engine", PATCH_ENGINES)
def test_no_engine_is_left_on_a_kind_only_patch_obligation(engine):
    o = _patch_obligation(engine, _manifest(4))
    assert o.exact_ids, f"{engine} still names no patch"
    assert o.required_count() == 4, "every entity must be named for the production check"


# #
# the production check keeps full strength however low the inspection floor goes
# #
@pytest.mark.parametrize("engine", PATCH_ENGINES)
def test_a_lost_entity_is_refused_before_the_model_is_asked(engine):
    """The free half. 2,000 named, 10 to be inspected - and one missing from discovery still
    ends the review as EXPECTED_TARGET_MISSING, which is the whole shape of the live defect."""
    o = _patch_obligation(engine, _manifest(MANY))
    lost = "patch:body_147"
    found = {f"patch:body_{i}" for i in range(MANY)} - {lost}
    problems = missing_target_obligations((o,), {TargetKind.PATCH: found})
    assert any(lost in p for p in problems)
    assert missing_target_obligations((o,), {TargetKind.PATCH: found | {lost}}) == []


# #
# the inspection check, at both strengths
# #
def _ledger(kind: TargetKind, *ids: str) -> EvidenceLedger:
    led = EvidenceLedger()
    for tid in ids:
        led.add_target_inspection(InspectionTargetRef(kind, tid), "toggle_patch",
                                  image_ok=True, covered=True)
    return led


def test_exact_coverage_names_every_outstanding_id():
    o = _patch_obligation("cfmesh", _manifest(3))
    reasons = _check_obligation(o, _ledger(TargetKind.PATCH, "patch:body_0"))
    assert len(reasons) == 2
    assert {"patch:body_1", "patch:body_2"} == {r.split("'")[1] for r in reasons}
    assert all("has not been inspected" in r and "IS available" in r for r in reasons)
    assert any("toggle_patch on 'body_1'" in r for r in reasons)


def test_a_sampled_obligation_asks_for_a_number_and_not_a_roster():
    """Listing 1,990 outstanding ids would BE the correction message, and the reviewer would
    read it as an order to inspect all of them."""
    o = _patch_obligation("snapgrid", _manifest(MANY))
    reasons = _check_obligation(o, _ledger(TargetKind.PATCH, "patch:body_0", "patch:body_1"))
    assert len(reasons) == 1
    msg = reasons[0]
    assert f"2 of the {MANY} patch target(s)" in msg
    assert "10 are required" in msg
    assert "inspect 8 more" in msg
    assert "ARE available" in msg
    assert msg.count("body_") <= 8, "the roster leaked into the correction"


def test_a_sampled_obligation_is_discharged_at_the_floor():
    o = _patch_obligation("snapgrid", _manifest(MANY))
    led = _ledger(TargetKind.PATCH, *(f"patch:body_{i}" for i in range(PATCH_INSPECTION_FLOOR)))
    assert _check_obligation(o, led) == []


def test_one_short_of_the_floor_is_not_discharged():
    o = _patch_obligation("snapgrid", _manifest(MANY))
    led = _ledger(TargetKind.PATCH,
                  *(f"patch:body_{i}" for i in range(PATCH_INSPECTION_FLOOR - 1)))
    assert _check_obligation(o, led)


def test_inspections_outside_the_named_set_do_not_count_toward_the_floor():
    """A kind-only obligation counts any inspection of the kind. A named one must count only
    the named, or a patch inspected twice under two spellings would discharge two ids."""
    o = _patch_obligation("snapgrid", _manifest(MANY))
    led = _ledger(TargetKind.PATCH, *(f"patch:stranger_{i}" for i in range(40)))
    reasons = _check_obligation(o, led)
    assert reasons and f"0 of the {MANY}" in reasons[0]


# #
# the one rule, and what it reads
# #
def test_the_renderable_set_is_the_entities_the_review_surface_carries():
    """A name with an empty tag list is a boundary the mesh has and this engine's review surface
    does not draw - snappy's far field, which lives in the blockMesh boundary and never in the
    triSurface the review msh is built from. Obliging it could never be discharged."""
    assert renderable_entities(
        {"patches": {"body": [1], "farfield": [], "ground": []}}) == ["body"]
    assert renderable_entities({"patches": {}}) == []
    assert renderable_entities({}) == []
    assert renderable_entities(None) == []


def test_the_named_set_never_outruns_what_the_renderer_will_offer():
    """expand_patch_targets stops at MAX_PATCHES, so on a generated 2,000-part board discovery
    offers 512 and no more. Naming all 2,000 would end EVERY such review as
    EXPECTED_TARGET_MISSING on a mesh that is fine - the production check must name what can be
    produced. The renderer's patch_names() is sorted and expand_patch_targets takes them in
    order, so the same truncation lands on the same ids."""
    from meshpipeline.sandbox.render_adapter import MAX_PATCHES, expand_patch_targets

    names = {f"p{i:05d}": [i + 1] for i in range(MAX_PATCHES + 400)}
    named = renderable_entities({"patches": names})
    assert len(named) == MAX_PATCHES
    discovered = expand_patch_targets(sorted(names), required=True, purpose="p")
    assert [t.target_id for t in discovered] == [f"patch:{n}" for n in named]
    o = _patch_obligation("snapgrid", {"mesh_paths": {"volume": "v.vtu"}, "patches": names})
    assert missing_target_obligations(
        (o,), {TargetKind.PATCH: {t.target_id for t in discovered}}) == []


@pytest.mark.parametrize("engine", PATCH_ENGINES)
def test_a_manifest_naming_no_renderable_entity_falls_back_rather_than_inventing_one(engine):
    """Discovery finding no patch target at all already refuses the review, so the kind-only
    floor here never stands in for a roster it could not read."""
    o = _patch_obligation(engine, {"mesh_paths": {"volume": "v.vtu"}, "patches": {}})
    assert o.exact_ids == ()
    assert o.required_inspections() == 1
    assert "names none" in o.provenance or "no per-region rows" in o.provenance


def test_the_conjugate_engine_still_reads_its_own_independent_authority():
    """It names its regions off checkMesh's per-region rows rather than off `patches`, because
    the export is the thing that fails and asking it which regions exist is asking it to grade
    itself. The other four have no second reading, which is stated in renderable_entities."""
    m = _manifest(2)
    m["patches"] = {"solid": [1]}                 # the export lost the coolant
    o = _patch_obligation("cfmesh_conjugate", m)
    assert o.exact_ids == ("patch:body_0", "patch:body_1")
    assert missing_target_obligations((o,), {TargetKind.PATCH: {"patch:solid"}})


def test_every_patch_engine_reads_the_one_floor():
    """Five engines, one constant. A second quieter rule that happened to agree is how the
    conjugate obligation and the other four drifted apart in the first place."""
    for engine in PATCH_ENGINES:
        o = _patch_obligation(engine, _manifest(50))
        assert o.inspect_at_least == PATCH_INSPECTION_FLOOR, engine


def test_no_other_engine_was_given_a_patch_obligation_it_cannot_meet():
    """gmsh and vmtk draw GROUPs, OPENINGs and BRANCHes, not patches. Nothing here touches them
    and nothing should have started obliging them patches."""
    for name in engine_names():
        if name in PATCH_ENGINES:
            continue
        obs = get_spec(name).expected_target_obligations(_manifest(4), {}, "")
        assert not [o for o in obs if o.kind is TargetKind.PATCH], name
