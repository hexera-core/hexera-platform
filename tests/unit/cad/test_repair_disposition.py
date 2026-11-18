from types import SimpleNamespace

from meshpipeline.cad.repair.disposition import ledger


def _d(fp, **measurements):
    return SimpleNamespace(fingerprint=fp, measurements=measurements)


def test_unchanged_defect_is_inherent():
    out = ledger(before=[_d("edge:a", length=0.000134865)],
                 after=[_d("edge:a", length=0.000134865)])
    assert out["inherent"] == ["edge:a"] and out["introduced"] == []
    assert out["resolved"] == []


def test_new_fingerprint_is_introduced():
    out = ledger(before=[], after=[_d("edge:b", length=0.5)])
    assert out["introduced"] == ["edge:b"] and out["inherent"] == []


def test_cleared_defect_is_resolved():
    out = ledger(before=[_d("edge:a", length=0.5)], after=[])
    assert out["resolved"] == ["edge:a"] and out["introduced"] == []


def test_same_fingerprint_with_a_moved_measurement_is_not_inherent():
    out = ledger(before=[_d("edge:a", length=0.5)], after=[_d("edge:a", length=0.9)])
    assert out["inherent"] == [] and out["introduced"] == ["edge:a"]
    assert out["resolved"] == []


def test_a_tiny_measurement_move_is_still_a_move():
    out = ledger(before=[_d("edge:a", length=0.5)], after=[_d("edge:a", length=0.5 + 1e-12)])
    assert out["inherent"] == []


def test_output_lists_are_sorted_and_each_fingerprint_is_in_exactly_one_bucket():
    before = [_d("z", a=1), _d("m", a=1), _d("gone2", a=1), _d("gone1", a=1)]
    after = [_d("z", a=1), _d("m", a=1), _d("new2", a=1), _d("new1", a=1)]
    out = ledger(before, after)
    assert out["inherent"] == ["m", "z"]
    assert out["introduced"] == ["new1", "new2"]
    assert out["resolved"] == ["gone1", "gone2"]
    assert out["reclassified"] == []
    buckets = out["inherent"] + out["introduced"] + out["resolved"] + out["ambiguous"]
    assert sorted(buckets) == sorted(set(buckets)) == sorted({"m", "z", "new1", "new2",
                                                              "gone1", "gone2"})


def test_mixed_scan_sorts_each_defect_into_its_own_bucket():
    before = [_d("keep", length=1.0), _d("moved", length=1.0), _d("gone", length=1.0)]
    after = [_d("keep", length=1.0), _d("moved", length=2.0), _d("new", length=1.0)]
    out = ledger(before, after)
    assert out["inherent"] == ["keep"]
    assert out["introduced"] == ["moved", "new"]
    assert out["resolved"] == ["gone"]


def test_renumbered_edge_indices_do_not_make_an_untouched_defect_introduced():
    before = [_d("shell:a", boundary_edges=[3, 4], boundary_length=2.5, relative_size=0.01)]
    after = [_d("shell:a", boundary_edges=[1, 2], boundary_length=2.5, relative_size=0.02)]
    out = ledger(before, after)
    assert out["inherent"] == ["shell:a"] and out["introduced"] == []
    after = [_d("shell:a", crossing_face_indices=[9], boundary_length=2.5)]
    before = [_d("shell:a", crossing_face_indices=[2], boundary_length=2.5)]
    assert ledger(before, after)["inherent"] == ["shell:a"]


def test_a_moved_intrinsic_measure_is_still_introduced_beside_renumbered_indices():
    before = [_d("shell:a", boundary_edges=[3, 4], boundary_length=2.5)]
    after = [_d("shell:a", boundary_edges=[1, 2], boundary_length=2.6)]
    assert ledger(before, after)["introduced"] == ["shell:a"]


def test_an_unknown_measurement_key_is_compared():
    out = ledger([_d("a", novel=1)], [_d("a", novel=2)])
    assert out["introduced"] == ["a"] and out["inherent"] == []


def test_empty_fingerprints_are_counted_not_listed():
    """`unlocated` IS THE AFTER SCAN ALONE: "how many survivors cannot be attributed".

    Counting both scans counted a surviving unlocated defect twice and went on counting one the
    repair had cleared, so the number answered no question. The upload's own unlocated defects
    are in its report; what this ledger is asked is what is left.
    """
    out = ledger([_d("", a=1), _d("x", a=1)], [_d("", a=1), _d("", a=2), _d("x", a=1)])
    assert out["unlocated"] == 2
    # An unlocated defect the repair CLEARED is not a survivor, so it is not counted at all.
    assert ledger([_d("", a=1), _d("x", a=1)], [_d("x", a=1)])["unlocated"] == 0
    assert out["inherent"] == ["x"]
    assert "" not in out["inherent"] + out["introduced"] + out["resolved"] + out["ambiguous"]


def test_a_shared_fingerprint_is_ambiguous_whatever_the_input_order():
    twins = [_d("t", a=1), _d("t", a=1)]
    for before, after in ((twins, twins), (twins, [_d("t", a=1)]), ([_d("t", a=1)], twins),
                          (twins, []), ([], twins)):
        out = ledger(before, after)
        assert out["ambiguous"] == ["t"]
        assert out["inherent"] == out["introduced"] == out["resolved"] == []
    a = ledger([_d("t", a=1), _d("t", a=2)], [_d("t", a=1)])
    b = ledger([_d("t", a=2), _d("t", a=1)], [_d("t", a=1)])
    assert a == b


def test_reclassified_is_driven_by_the_caller_and_sorted():
    out = ledger([], [], reclassified=["b", "a"])
    assert out["reclassified"] == ["a", "b"]
