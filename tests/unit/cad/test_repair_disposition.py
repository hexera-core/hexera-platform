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


def test_every_list_is_sorted_and_the_buckets_partition():
    before = [_d("z", a=1), _d("m", a=1), _d("gone2", a=1), _d("gone1", a=1)]
    after = [_d("z", a=1), _d("m", a=1), _d("new2", a=1), _d("new1", a=1)]
    out = ledger(before, after)
    assert out["inherent"] == ["m", "z"]
    assert out["introduced"] == ["new1", "new2"]
    assert out["resolved"] == ["gone1", "gone2"]
    assert out["reclassified"] == []


def test_reclassified_is_driven_by_the_caller_and_sorted():
    out = ledger([], [], reclassified=["b", "a"])
    assert out["reclassified"] == ["a", "b"]
