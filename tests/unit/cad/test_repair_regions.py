from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.cad.repair.regions import decompose


def _defect(fp, at, *, size=0.1, entity_type="face", index=1, located=True):
    x, y, z = at
    loc = ({"bbox_min": [x, y, z], "bbox_max": [x + size, y + size, z + size],
            "centroid": [x, y, z], "unit": "mm"} if located else {})
    return SimpleNamespace(fingerprint=fp, entity_type=entity_type, entity_index=index,
                           severity="error", location=loc)


def test_distant_defects_form_separate_regions():
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(100, 0, 0))],
                        diagonal_mm=200.0)
    assert len(regions) == 2


def test_near_defects_merge_into_one_region():
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(0.5, 0, 0))],
                        diagonal_mm=200.0)
    assert len(regions) == 1
    assert regions[0].fingerprints == frozenset({"a", "b"})


def test_merging_is_transitive_so_a_chain_is_one_region():
    # a--b and b--c close, a--c far: all three must land together. Listed c, a, b so a single
    # pairwise pass in input order would miss it.
    regions = decompose([_defect("c", at=(5.0, 0, 0)), _defect("a", at=(0, 0, 0)),
                         _defect("b", at=(2.5, 0, 0))], diagonal_mm=200.0)
    assert len(regions) == 1


def test_order_is_deterministic():
    defects = [_defect("b", at=(100, 0, 0)), _defect("a", at=(0, 0, 0))]
    assert [r.index for r in decompose(defects, diagonal_mm=200.0)] == [0, 1]
    assert decompose(defects, diagonal_mm=200.0)[0].fingerprints == frozenset({"a"})


def test_one_region_is_a_valid_decomposition():
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(0.2, 0, 0)),
                         _defect("c", at=(0.4, 0, 0))], diagonal_mm=200.0)
    assert len(regions) == 1 and len(regions[0].fingerprints) == 3


def test_no_defects_is_no_regions():
    assert decompose([], diagonal_mm=200.0) == ()


def test_region_box_spans_members_and_face_indices_exclude_edges():
    regions = decompose([_defect("a", at=(0, 0, 0), index=3),
                         _defect("b", at=(1, 0, 0), entity_type="edge", index=9)],
                        diagonal_mm=200.0)
    (region,) = regions
    assert region.bbox_min == (0.0, 0.0, 0.0)
    assert region.bbox_max == (1.1, 0.1, 0.1)
    assert region.face_indices == frozenset({3})


def test_unlocated_defects_form_their_own_last_region_and_do_not_merge():
    regions = decompose([_defect("u1", at=(0, 0, 0), located=False),
                         _defect("a", at=(0, 0, 0)), _defect("b", at=(100, 0, 0)),
                         _defect("u2", at=(0, 0, 0), located=False, index=7)],
                        diagonal_mm=200.0)
    assert [r.fingerprints for r in regions] == [frozenset({"a"}), frozenset({"b"}),
                                                 frozenset({"u1", "u2"})]
    assert [r.located for r in regions] == [True, True, False]
    assert regions[2].bbox_min is None and regions[2].bbox_max is None
    assert regions[0].bbox_min is not None
    assert [r.index for r in regions] == [0, 1, 2]


def test_no_unlocated_region_when_all_are_located():
    regions = decompose([_defect("a", at=(0, 0, 0))], diagonal_mm=200.0)
    assert all(r.located for r in regions)


def test_only_unlocated_defects_give_one_unlocated_region():
    (region,) = decompose([_defect("u", at=(0, 0, 0), located=False)], diagonal_mm=200.0)
    assert not region.located and region.bbox_min is None


def test_result_is_independent_of_input_order():
    defects = [_defect("c", at=(5.0, 0, 0)), _defect("a", at=(0, 0, 0)),
               _defect("b", at=(2.5, 0, 0)), _defect("d", at=(100, 0, 0)),
               _defect("u", at=(0, 0, 0), located=False)]
    assert decompose(defects, diagonal_mm=200.0) == decompose(list(reversed(defects)),
                                                              diagonal_mm=200.0)
