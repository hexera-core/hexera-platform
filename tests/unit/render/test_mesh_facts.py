# Responsibility: Verify rendered mesh facts keep numbers numeric, drop what is unusable, and refuse an unstated unit.
from __future__ import annotations

import pytest

from meshpipeline.render.mesh_facts import (
    BOUNDARY,
    CELL_GROUP,
    HIDDEN,
    INSPECTION,
    KINDS,
    REGION,
    VISIBLE,
    parts,
    quality,
)

# fixtures


def external_cfd() -> dict:
    return {
        "mesh_mode": "cfmesh", "mesh_units": "m", "cell_count": 3907590,
        "patch_types": {"aircraft": "wall", "farfield": "farfield"},
        "patch_face_counts": {"aircraft": 234878, "farfield": 38400},
        "quality": {"cells": 3907590, "faces": 12230050, "hexahedra": 3432035,
                    "polyhedra": 449847, "regions": 1, "max_non_ortho": 65.1943,
                    "max_skewness": 7.80077, "skew_faces": 37},
        "inspection_regions": [
            {"name": "slice_x_centre", "kind": "slice", "normal": [1, 0, 0],
             "origin": [0.93, 0.0, 0.11]},
            {"name": "nearwall_z", "kind": "nearwall", "normal": [0, 1, 0],
             "origin": [0.93, 0.0, 0.11]},
        ],
    }


def internal_cfd() -> dict:
    return {
        "mesh_mode": "snappy", "mesh_units": "m", "cell_count": 412_000,
        "patch_types": {"pipe_wall": "wall", "inlet": "inlet", "outlet": "outlet"},
        "patch_face_counts": {"pipe_wall": 88_120, "inlet": 1_240, "outlet": 1_240},
        "quality": {"cells": 412_000, "faces": 1_260_400, "hexahedra": 402_000,
                    "polyhedra": 10_000, "regions": 1, "max_non_ortho": 41.2},
    }


def multi_region() -> dict:
    return {
        "mesh_mode": "snappy_multiregion", "mesh_units": "m", "cell_count": 900_000,
        "patch_types": {"solid": "wall", "fluid_inlet": "inlet",
                        "fluid_outlet": "outlet", "interface": "wall"},
        "patch_face_counts": {"solid": 40_000, "fluid_inlet": 900,
                              "fluid_outlet": 900, "interface": 12_000},
        "quality": {"cells": 900_000, "faces": 2_700_000, "regions": 3,
                    "hexahedra": 880_000, "tetrahedra": 20_000},
        "inspection_regions": [{"name": "solid", "kind": "slice"}],   # same name as a patch
    }


def _ids(ps: list[dict]) -> list[str]:
    return [p["id"] for p in ps]


# quality


def test_quality_keeps_numbers_numeric():
    q = quality(external_cfd())
    assert q["cells"] == 3907590 and isinstance(q["cells"], int)
    assert q["max_skewness"] == pytest.approx(7.80077)
    assert isinstance(q["max_non_ortho"], float)


def test_a_metric_the_engine_did_not_compute_stays_absent():
    q = quality(internal_cfd())
    assert "max_skewness" not in q, "a missing metric was invented"
    assert "skew_faces" not in q
    assert q["max_non_ortho"] == pytest.approx(41.2)


def test_a_missing_metric_never_becomes_zero():
    man = external_cfd()
    del man["quality"]["hexahedra"]
    q = quality(man)
    assert "hexahedra" not in q, "absent was reported as zero"


@pytest.mark.parametrize("bad", ["", "n/a", None, [], {}, float("nan"), float("inf")])
def test_an_unusable_value_is_dropped_not_rendered(bad):
    man = external_cfd()
    man["quality"]["max_skewness"] = bad
    q = quality(man)
    assert "max_skewness" not in q
    assert q["cells"] == 3907590, "one bad field cost the user every other figure"


def test_a_manifest_that_cannot_state_its_unit_is_refused_not_described():
    from meshpipeline.contracts.mesh_units import MeshUnitsError
    for man in (None, {}, {"quality": "not a dict"}, {"quality": None}):
        with pytest.raises(MeshUnitsError):
            quality(man)


def test_a_malformed_quality_block_still_costs_only_that_block():
    man = external_cfd() | {"quality": "not a dict"}
    assert quality(man)["units"] == "m"


def test_the_part_roster_is_still_tolerant_of_a_thin_manifest():
    for man in (None, {}, {"quality": "not a dict"}, {"quality": None}):
        assert isinstance(parts(man), list)


def test_the_authoritative_cell_count_wins_over_a_disagreeing_quality_block():
    man = external_cfd()
    man["quality"]["cells"] = 12   # stale
    assert quality(man)["cells"] == 3907590


def test_quality_carries_its_units_and_engine():
    q = quality(internal_cfd())
    assert q["units"] == "m" and q["engine"] == "snappy"


# parts


def test_boundary_patches_become_parts():
    ps = parts(external_cfd(), rendered=("aircraft", "farfield"))
    b = [p for p in ps if p["kind"] == BOUNDARY]
    assert [p["label"] for p in b] == ["aircraft", "farfield"]
    assert b[0]["metadata"] == {"name": "aircraft", "role": "wall", "faces": 234878}
    assert b[0]["renderable"] is True


def test_inspection_regions_become_parts():
    ps = parts(external_cfd())
    ins = [p for p in ps if p["kind"] == INSPECTION]
    assert [p["label"] for p in ins] == ["slice_x_centre", "nearwall_z"]
    assert ins[0]["metadata"]["structure"] == "slice"
    assert ins[0]["metadata"]["normal"] == [1.0, 0.0, 0.0]


def test_cell_type_groups_are_represented_when_the_engine_counts_them():
    ps = parts(external_cfd())
    cg = {p["label"]: p["metadata"]["cells"] for p in ps if p["kind"] == CELL_GROUP}
    assert cg == {"hexahedra": 3432035, "polyhedra": 449847}
    # an engine that counts tetrahedra gets tetrahedra, not a fixed roster
    assert "tetrahedra" in {p["label"] for p in parts(multi_region())
                            if p["kind"] == CELL_GROUP}


def test_farfield_is_present_and_opens_out_of_the_way():
    ps = parts(external_cfd(), rendered=("aircraft", "farfield"))
    ff = next(p for p in ps if p["id"] == f"{BOUNDARY}:farfield")
    assert ff["selectable"] is True and ff["renderable"] is True
    assert ff["default_visibility"] == HIDDEN
    assert ff["metadata"]["encloses_domain"] is True


def test_no_farfield_is_invented_for_an_internal_case():
    ps = parts(internal_cfd(), rendered=("pipe_wall", "inlet", "outlet"))
    assert not [p for p in ps if "farfield" in p["id"]]
    assert all(p["default_visibility"] == VISIBLE
               for p in ps if p["kind"] == BOUNDARY), \
        "an internal case had a boundary hidden as if it enclosed the domain"


def test_internal_flow_inlet_and_outlet_are_selectable_parts():
    ps = parts(internal_cfd(), rendered=("pipe_wall", "inlet", "outlet"))
    b = {p["label"]: p["metadata"]["role"] for p in ps if p["kind"] == BOUNDARY}
    assert b == {"pipe_wall": "wall", "inlet": "inlet", "outlet": "outlet"}


def test_a_name_shared_across_categories_does_not_collide():
    ps = parts(multi_region())
    ids = _ids(ps)
    assert len(ids) == len(set(ids)), f"colliding part identities: {ids}"
    assert f"{BOUNDARY}:solid" in ids and f"{INSPECTION}:solid" in ids


def test_multi_region_declares_one_part_per_region():
    ps = parts(multi_region())
    assert [p["id"] for p in ps if p["kind"] == REGION] == \
        [f"{REGION}:0", f"{REGION}:1", f"{REGION}:2"]


def test_ordering_is_deterministic():
    man = external_cfd()
    assert _ids(parts(man)) == _ids(parts(man))
    # …and grouped: boundaries, then regions, then cell groups, then inspection
    kinds = [p["kind"] for p in parts(man)]
    assert kinds == sorted(kinds, key=[BOUNDARY, REGION, CELL_GROUP, INSPECTION].index)


def test_a_part_without_shipped_geometry_says_so_rather_than_pretending():
    ps = parts(external_cfd(), rendered=("aircraft",))   # farfield geometry not shipped
    ff = next(p for p in ps if p["id"] == f"{BOUNDARY}:farfield")
    assert ff["renderable"] is False and ff["selectable"] is True
    assert all(p["renderable"] is False for p in ps if p["kind"] != BOUNDARY)


def test_every_part_kind_is_in_the_closed_set():
    for man in (external_cfd(), internal_cfd(), multi_region()):
        assert all(p["kind"] in KINDS for p in parts(man))


def test_a_case_with_no_declared_parts_gets_an_empty_list_not_an_invented_one():
    assert parts({"mesh_mode": "gmsh"}) == []


def test_the_part_shape_is_stable():
    p = parts(external_cfd())[0]
    assert set(p) == {"id", "label", "kind", "source", "default_visibility",
                      "selectable", "renderable", "metadata"}
    assert p["source"] == "patch_types", "a part cannot say where it came from"
