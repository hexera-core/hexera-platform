# Responsibility: Verify an entity's fingerprint is stable under a repair's renumbering, says nothing when there is nothing to measure, and admits when two entities share one box.
from __future__ import annotations

from meshpipeline.cad.repair.identity import (
    GONE,
    MIN_QUANTUM_MM,
    EntityMapping,
    EntityRef,
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


# THE LIMIT OF THE NAME (documented, not a bug)


def test_a_sub_quantum_move_across_a_cell_edge_does_change_the_name_which_is_why_the_mapping_is_authoritative():
    # A known limit, pinned so it cannot be forgotten. The grid rounds each coordinate, so a box
    # just below a cell edge and a box just above it get different names even though they moved
    # by only a fraction of a quantum. Here xmin sits at 0.49q (cell 0) and is nudged by 0.02q to
    # 0.51q (cell 1). This is ACCEPTABLE: identity across a repair is carried by the kernel mapping,
    # and a consumer treats a vanished fingerprint with no `gone` entry as unknown, not fixed.
    q = quantum_for(100.0)
    before = _box(0.49 * q, 0, 0, 10, 10, 0)
    after = _box(0.51 * q, 0, 0, 10, 10, 0)
    assert 0 < (0.51 - 0.49) * q < q
    assert fingerprint_of("face", before, quantum=q) != fingerprint_of("face", after, quantum=q)


# NON-FINITE COORDINATES: NEVER RAISE, NEVER INVENT A NAME


def test_a_non_finite_diagonal_falls_back_to_the_floor_quantum():
    assert quantum_for(float("nan")) == MIN_QUANTUM_MM
    assert quantum_for(float("inf")) == MIN_QUANTUM_MM


def test_a_box_with_a_non_finite_coordinate_has_no_name_and_does_not_raise():
    q = quantum_for(100.0)
    assert fingerprint_of("face", _box(float("nan"), 0, 0, 10, 10, 0), quantum=q) == ""
    assert fingerprint_of("face", _box(0, 0, 0, float("inf"), 10, 0), quantum=q) == ""


# THE REF TABLE, REMAINING SURFACE


def test_targetable_is_true_only_for_an_entity_with_a_name():
    assert EntityRef(entity_type="face", fingerprint="face:aa").targetable is True
    assert EntityRef(entity_type="face", fingerprint="").targetable is False


def test_three_entities_sharing_one_box_get_distinct_numbered_keys_and_all_are_ambiguous():
    q = quantum_for(100.0)
    base = fingerprint_of("face", _box(0, 0, 0, 10, 10, 0), quantum=q)
    refs = refs_for([("face", 1, _box(0, 0, 0, 10, 10, 0)),
                     ("face", 2, _box(0, 0, 0, 10, 10, 0)),
                     ("face", 3, _box(0, 0, 0, 10, 10, 0))], diagonal_mm=100.0)
    assert [r.fingerprint for r in refs] == [f"{base}#0", f"{base}#1", f"{base}#2"]
    assert all(r.ambiguous for r in refs)


def test_two_entities_with_empty_boxes_are_not_marked_ambiguous_with_each_other():
    # Two unmeasurable entities share the empty fingerprint, but that is not a collision: neither
    # has a name to be confused about, and both stay untargetable.
    refs = refs_for([("edge", 1, {}), ("edge", 2, {})], diagonal_mm=100.0)
    assert [r.fingerprint for r in refs] == ["", ""]
    assert not any(r.ambiguous for r in refs)


def test_refs_for_returns_one_ref_per_input_in_input_order_across_a_mixed_list():
    # Later tasks zip this output against the input by position, so order and count are contract.
    a = _box(0, 0, 0, 10, 10, 0)
    b = _box(50, 0, 0, 60, 10, 0)
    boxes = [("face", 0, a), ("edge", 1, {}), ("face", 2, a), ("face", 3, b), ("face", 4, a)]
    refs = refs_for(boxes, diagonal_mm=100.0)
    assert len(refs) == len(boxes)
    assert [(r.entity_type, r.entity_index) for r in refs] == [(k, i) for k, i, _ in boxes]
    assert [r.ambiguous for r in refs] == [True, False, True, False, True]
    assert refs[0].fingerprint.endswith("#0") and refs[2].fingerprint.endswith("#1")
    assert refs[4].fingerprint.endswith("#2")
    assert refs[1].fingerprint == "" and refs[3].fingerprint and not refs[3].ambiguous


# A SHARED BOX IS ONLY A COLLISION BETWEEN DIFFERENT ENTITIES


def test_one_entity_listed_twice_is_not_a_collision_and_keeps_its_plain_name():
    # The localiser reports one defect per CODE, so one edge carrying two statuses appears twice
    # in this table with the same index and the same box. That is one entity reported twice, not
    # two entities - and suffixing it `#0`/`#1` made the name unfindable in the next scan, where
    # the survivor is stamped without a suffix.
    base = fingerprint_of("edge", _box(0, 0, 0, 10, 0, 0), quantum=quantum_for(100.0))

    refs = refs_for([("edge", 7, _box(0, 0, 0, 10, 0, 0)),
                     ("edge", 7, _box(0, 0, 0, 10, 0, 0))], diagonal_mm=100.0)

    assert [r.fingerprint for r in refs] == [base, base]
    assert not any(r.ambiguous for r in refs), "one entity cannot be ambiguous with itself"


def test_a_shared_box_across_different_indices_is_still_a_collision():
    # The case the marking exists for is untouched: a duplicated face really is two entities the
    # fingerprint cannot tell apart.
    refs = refs_for([("face", 1, _box(0, 0, 0, 10, 10, 0)),
                     ("face", 2, _box(0, 0, 0, 10, 10, 0))], diagonal_mm=100.0)

    assert all(r.ambiguous for r in refs)
    assert refs[0].fingerprint != refs[1].fingerprint


def test_a_repeated_entity_beside_a_genuine_twin_marks_only_the_twins():
    # Both situations at once: edge 7 reported twice, and edge 9 genuinely sharing its box.
    refs = refs_for([("edge", 7, _box(0, 0, 0, 10, 0, 0)),
                     ("edge", 7, _box(0, 0, 0, 10, 0, 0)),
                     ("edge", 9, _box(0, 0, 0, 10, 0, 0))], diagonal_mm=100.0)

    assert [r.ambiguous for r in refs] == [True, True, True]
    assert refs[0].fingerprint == refs[1].fingerprint
    assert refs[2].fingerprint != refs[0].fingerprint


def test_a_collision_survives_stamping_and_reaches_every_operations_targets():
    """The collision check lives in the session's basis rule, which reads `ref.ambiguous` on the
    OPERATION's targets. `_stamped` used to keep only the fingerprint and each `targets_for` built a
    fresh ref, so the real planner path always handed the session `ambiguous=False`."""
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect, _stamped
    from meshpipeline.cad.repair.operations import OPERATIONS
    from meshpipeline.cad.repair.triage import defect_from_entity

    box = _box(0, 0, 0, 10, 10, 0)
    found = tuple(EntityDefect(code=DefectCode.curve_inconsistency, severity=DefectSeverity.error,
                               entity_type="face", entity_index=i, message="m", location=box)
                  for i in (1, 2))
    stamped = _stamped(found, diagonal_mm=100.0)
    assert all(d.ambiguous for d in stamped)
    # and it survives the report round trip the node and the minting read
    assert all(defect_from_entity(d.to_dict()).ambiguous for d in stamped)

    aimed = [ref for op in OPERATIONS for ref in op.targets_for(stamped[0])]
    assert aimed, "some operation must claim a curve_inconsistency on a face"
    assert all(ref.ambiguous for ref in aimed)
