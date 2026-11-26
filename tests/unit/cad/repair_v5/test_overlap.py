"""v5 step 8: overlap_self_intersection - the tessellated loss measurement, its triggers, the overlap detectors and
the new ops' aim/parameter rules. No kernel: pure arithmetic and fakes."""

from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.loss import from_kernel
from meshpipeline.cad.repair_v4.loss_kernel import common_contradicts_volumes
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import overlap as O
from meshpipeline.cad.repair_v5 import tess_kernel as K


def _box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> list:
    """An outward-oriented closed triangle mesh of an axis-aligned box."""
    p = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]   # index = 4*ix + 2*iy + iz
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return [t for a, b, c, d in quads for t in ((p[a], p[b], p[c]), (p[a], p[c], p[d]))]


CUBE = _box(0, 0, 0, 10, 10, 10)


def test_a_closed_mesh_has_its_volume_and_winding_one_inside_zero_outside() -> None:
    assert K.mesh_volume(CUBE) == pytest.approx(1000.0)
    w_in, w_out = K.winding_numbers([(5, 5, 5), (15, 5, 5)], CUBE)
    assert w_in == pytest.approx(1.0) and w_out == pytest.approx(0.0, abs=1e-9)


def test_a_self_overlapping_solid_counts_its_doubly_covered_region_as_inside() -> None:
    """Two lobes of one self-intersecting solid: winding 2 where they overlap - still inside (a parity test says out)."""
    lobes = CUBE + _box(5, 0, 0, 15, 10, 10)
    assert K.winding_numbers([(7, 5, 5)], lobes)[0] == pytest.approx(2.0)
    assert K.inside(K.winding_numbers([(7, 5, 5)], lobes)[0])


def test_sampling_reads_a_pure_split_as_kept_and_a_deletion_as_lost() -> None:
    halves = _box(0, 0, 0, 5, 10, 10) + _box(5, 0, 0, 10, 10, 10)       # the body split in two, nothing lost
    kept = K.kept_by_sampling(CUBE, halves)
    assert kept["kept"] == pytest.approx(1.0, abs=0.01)
    assert kept["volume_mc_mm3"] == pytest.approx(1000.0, rel=0.05)
    gone = K.kept_by_sampling(CUBE, _box(20, 0, 0, 30, 10, 10))          # the body deleted, volume moved elsewhere
    assert gone["kept"] == pytest.approx(0.0, abs=1e-9)
    quarter = K.kept_by_sampling(CUBE, _box(0, 0, 0, 10, 2.5, 10))
    assert quarter["kept"] == pytest.approx(0.25, abs=0.03)


def test_common_is_second_guessed_only_when_it_contradicts_the_plain_volumes() -> None:
    # step 7's pure split: Common says 0.85% kept, the part's volume moved 5e-14
    assert common_contradicts_volumes(8.5, 1000.0, 1000.0, 1000.0 - 5e-11, 0.10)
    # b25999e0f8be: Common ~0 on a self-interfering body, volume change 1e-5 relative
    assert common_contradicts_volumes(0.0, 1000.0, 1000.0, 999.99, 0.10)
    # a real deletion: the part lost the body's volume - Common stands
    assert not common_contradicts_volumes(0.0, 1000.0, 3000.0, 2000.0, 0.10)
    # Common says it is kept: nothing to second-guess
    assert not common_contradicts_volumes(900.0, 1000.0, 1000.0, 1000.0, 0.10)


def test_the_tessellated_measurement_rides_on_the_report_and_the_rule_is_unchanged() -> None:
    payload = {"solidless": False, "bodies": {"S1": 0.9999}, "part_kept": 0.9999, "volume_ratio": 0.99999,
               "bodies_total": 1, "self_intersecting": ["S1"],
               "tessellated": {"S1": {"kept": 0.9999, "inside": 400, "sampled": 4000}}}
    report = from_kernel(payload)
    assert not report.catastrophic and report.detail["self_intersecting"] == ["S1"]
    assert report.detail["tessellated"]["S1"]["inside"] == 400
    lost = from_kernel({**payload, "bodies": {"S1": 0.05}, "part_kept": 0.05})
    assert lost.catastrophic and lost.reason == "part_volume_lost"


# --- detectors --------------------------------------------------------------------------------------------------

REV = {("B", 1): "B1", ("B", 2): "B2", ("B", 3): "B3", ("F", 4): "F4", ("F", 7): "F7", ("F", 9): "F9"}


def test_a_cluster_holding_a_duplicate_pair_gets_the_duplicate_row() -> None:
    d = Defect("S@1:overlap:B1", "validity", "body_overlap", ("B1", "B2", "B3"), pairs=(("B1", "B2"), ("B2", "B3")),
               cls="overlap_self_intersection", rule="layer 1 reported body_overlap")
    [out] = O.attach_overlap_facts([d], {"duplicates": [[2, 3]], "self_int": {}}, REV)
    assert out.cause["kind"] == "duplicate_body" and out.cause["duplicates"] == ["B2|B3"]
    assert loop.table_key(out) == "overlap_self_intersection:duplicate_body"
    assert loop.CLASS_REPAIRS[loop.table_key(out)][0] == ("drop_duplicate_body",)
    [plain] = O.attach_overlap_facts([d], {"duplicates": [], "self_int": {}}, REV)
    assert loop.table_key(plain) == "overlap_self_intersection"
    assert loop.CLASS_REPAIRS["overlap_self_intersection"][0] == ("boolean_by_volume",)


def test_a_self_intersection_is_aimed_at_its_faces_a_self_crossing_face_first() -> None:
    d = Defect("S@1", "validity", "self_intersection", ("B1",), cls="overlap_self_intersection", rule="r")
    [out] = O.attach_overlap_facts([d], {"duplicates": [], "self_int": {"1": {"faces": [4, 9], "wire_self_int": [9]}}},
                                   REV)
    assert out.cause["aim"] == ["F9", "F4"] and out.cause["kind"] == "self_intersecting_face"
    assert loop.table_key(out) == "overlap_self_intersection:self_intersecting_face"
    assert seq.params_for("rebuild_face", out) == (["F9"], {"continuity": "G0"})
    # the body ops still aim at the BODY although the aim names faces
    assert seq.params_for("fragment_drop_small", out) == (["B1"], {"max_fraction": 1e-3})
    [no_face] = O.attach_overlap_facts([d], {"self_int": {"1": {"faces": [4], "wire_self_int": []}}}, REV)
    assert loop.table_key(no_face) == "overlap_self_intersection:self_intersection"
    assert loop.CLASS_REPAIRS[loop.table_key(no_face)][0] == ("fragment_drop_small",)


def test_no_answer_leaves_the_defects_as_they_were() -> None:
    d = Defect("S@1", "validity", "self_intersection", ("B1",), cls="overlap_self_intersection", rule="r")
    assert O.attach_overlap_facts([d], None, REV) == [d]


def test_the_model_is_not_offered_rescale_or_the_class_rule_ops() -> None:
    from meshpipeline.cad.repair_v4 import proposer

    offered = proposer._offered()
    assert "rescale" not in offered and "boolean_by_volume" not in offered and "conformalise" in offered
    assert {"boolean_by_volume", "fragment_drop_small", "drop_duplicate_body"} <= set(seq.REGISTRY)


def test_a_pair_op_needs_exactly_two_bodies() -> None:
    d = Defect("S@1:overlap:B1", "validity", "body_overlap", ("B1", "B2", "B3"), pairs=(("B1", "B2"),),
               cls="overlap_self_intersection")
    assert seq.params_for("boolean_by_volume", d, pair=("B1", "B2")) == (["B1", "B2"], {})
    with pytest.raises(seq.NoParameters):
        seq.params_for("drop_duplicate_body", d)
