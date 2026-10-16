# Responsibility: Verify an entity's fingerprint is stable under a repair's renumbering, says nothing when there is nothing to measure, and admits when two entities share one box.
from __future__ import annotations

from meshpipeline.cad.repair.identity import (
    GONE,
    MIN_QUANTUM_MM,
    EntityMapping,
    fingerprint_of,
    quantum_for,
    refs_for,
)

# Entity indices are only stable within ONE shape: ShapeFix renumbers everything it touches, so a
# report that named an edge by index could never say whether THAT edge's defect went away. The
# fingerprint is the name that survives, and these tests pin what it is made of. No kernel needed -
# the boxes here are exactly what `localize._centroid_and_box` hands back.


def _box(xmin, ymin, zmin, xmax, ymax, zmax) -> dict:
    return {"unit": "mm",
            "centroid": [(xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2],
            "bbox_min": [xmin, ymin, zmin], "bbox_max": [xmax, ymax, zmax]}


# THE QUANTUM


def test_the_quantum_scales_with_the_part_so_a_big_part_is_not_fingerprinted_to_nanometres():
    assert quantum_for(1000.0) > quantum_for(10.0)


def test_a_shape_with_no_extent_gets_the_floor_instead_of_a_zero_quantum():
    # A zero quantum divides by zero. An empty compound or a single-vertex shape reaches here, and
    # the honest answer is the finest fixed grid rather than an exception.
    assert quantum_for(0.0) == MIN_QUANTUM_MM
    assert quantum_for(-1.0) == MIN_QUANTUM_MM


# THE FINGERPRINT


def test_the_same_box_fingerprints_the_same_way_every_time():
    q = quantum_for(100.0)
    assert fingerprint_of("face", _box(0, 0, 0, 10, 10, 0), quantum=q) == \
           fingerprint_of("face", _box(0, 0, 0, 10, 10, 0), quantum=q)


def test_a_box_that_moved_less_than_the_quantum_keeps_its_name():
    # WHY THIS MATTERS. A repair legitimately nudges a vertex by a tolerance. If that renamed the
    # entity, every successful op would look like it deleted its target and invented a new one.
    q = quantum_for(100.0)
    nudge = q / 4
    assert fingerprint_of("face", _box(0, 0, 0, 10, 10, 0), quantum=q) == \
           fingerprint_of("face", _box(nudge, 0, 0, 10 + nudge, 10, 0), quantum=q)


def test_a_box_that_moved_far_more_than_the_quantum_gets_a_different_name():
    q = quantum_for(100.0)
    assert fingerprint_of("face", _box(0, 0, 0, 10, 10, 0), quantum=q) != \
           fingerprint_of("face", _box(50, 0, 0, 60, 10, 0), quantum=q)


def test_the_entity_type_is_part_of_the_name():
    # An edge and a face can share a box - a planar face's own boundary does. They are not the
    # same entity and no operation may aim at one thinking it is the other.
    q = quantum_for(100.0)
    assert fingerprint_of("edge", _box(0, 0, 0, 10, 0, 0), quantum=q) != \
           fingerprint_of("face", _box(0, 0, 0, 10, 0, 0), quantum=q)


def test_an_entity_with_nothing_to_measure_has_no_name_rather_than_a_made_up_one():
    # `_centroid_and_box` returns {} for a degenerate entity. A fingerprint invented from that
    # would match every other unmeasurable entity, so an operation could "fix" the wrong one.
    assert fingerprint_of("edge", {}, quantum=0.1) == ""
    assert fingerprint_of("edge", {"unit": "mm"}, quantum=0.1) == ""


# THE REF TABLE


def test_each_entity_gets_a_ref_carrying_its_index_for_kernel_lookups():
    refs = refs_for([("face", 3, _box(0, 0, 0, 10, 10, 0))], diagonal_mm=100.0)
    assert len(refs) == 1
    assert refs[0].entity_type == "face" and refs[0].entity_index == 3
    assert refs[0].fingerprint and not refs[0].ambiguous


def test_two_entities_sharing_one_box_are_both_marked_ambiguous():
    # A duplicated face, or two coincident edges a bad export left behind. The fingerprint cannot
    # tell them apart, so it SAYS so - a verdict about an ambiguous target may not claim the
    # kernel confirmed anything about it.
    refs = refs_for([("face", 1, _box(0, 0, 0, 10, 10, 0)),
                     ("face", 2, _box(0, 0, 0, 10, 10, 0))], diagonal_mm=100.0)
    assert len(refs) == 2
    assert all(r.ambiguous for r in refs)
    assert refs[0].fingerprint != refs[1].fingerprint, "an ambiguous ref still needs its own key"


def test_an_unmeasurable_entity_gets_a_ref_with_an_empty_fingerprint_not_no_ref():
    # The planner has to be able to SEE an untargetable defect to report skipping it.
    refs = refs_for([("edge", 7, {})], diagonal_mm=100.0)
    assert len(refs) == 1 and refs[0].fingerprint == "" and refs[0].entity_index == 7


# THE MAPPING


def test_the_mapping_reports_an_entity_the_kernel_deleted():
    mapping = EntityMapping(moved={}, gone=frozenset({"face:aa"}), basis="kernel")
    assert mapping.fate_of("face:aa") == (GONE, "")


def test_the_mapping_reports_what_an_entity_became():
    mapping = EntityMapping(moved={"face:aa": "face:bb"}, gone=frozenset(), basis="kernel")
    assert mapping.fate_of("face:aa") == ("moved", "face:bb")


def test_an_entity_the_mapping_never_mentions_is_unknown_and_keeps_its_own_name():
    # Most sub-shapes of a repaired shape are untouched, and an op's history only records what it
    # changed. "Unknown" is the common, correct answer - not a gap to be filled with a guess.
    mapping = EntityMapping(moved={}, gone=frozenset(), basis="kernel")
    assert mapping.fate_of("face:aa") == ("unknown", "face:aa")


def test_an_empty_mapping_declares_itself_geometric_because_no_kernel_said_anything():
    assert EntityMapping.empty().basis == "geometric"
    assert EntityMapping.empty().fate_of("face:aa") == ("unknown", "face:aa")
