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
    far_side_interfaces,
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


# named regions (a multi-region engine lists its regions as rows)


def named_regions() -> dict:
    return {
        "mesh_mode": "snapgrid", "mesh_units": "m", "cell_count": 1000,
        "patch_types": {"domain_xmin": "patch", "domain_xmax": "patch"},
        "quality": {"cells": 1000, "regions": [
            {"name": "air", "type": "fluid", "cells": 800, "fatal": []},
            {"name": "PCB", "type": "solid", "cells": 150, "fatal": []},
            {"name": "SoC", "type": "solid", "cells": 50, "fatal": []}]},
    }


def test_named_regions_become_parts_under_the_files_names():
    ps = parts(named_regions(), rendered=("air", "PCB", "SoC"))
    regs = [p for p in ps if p["kind"] == REGION]
    assert [p["id"] for p in regs] == [f"{REGION}:air", f"{REGION}:PCB", f"{REGION}:SoC"]
    assert [p["label"] for p in regs] == ["air", "PCB", "SoC"]
    assert regs[1]["metadata"] == {"name": "PCB", "role": "solid", "cells": 150}
    assert all(p["renderable"] for p in regs)


def test_a_named_region_with_no_surface_is_listed_but_not_drawn():
    regs = [p for p in parts(named_regions(), rendered=("PCB",)) if p["kind"] == REGION]
    assert {p["label"]: p["renderable"] for p in regs} == {"air": False, "PCB": True,
                                                         "SoC": False}


def test_the_surfaces_the_engine_says_enclose_open_hidden():
    ps = parts(named_regions(), rendered=("air", "PCB", "domain_xmin", "domain_xmax"),
               enclosing=("air", "domain_xmin", "domain_xmax"))
    vis = {p["id"]: p["default_visibility"] for p in ps}
    assert vis[f"{REGION}:air"] == HIDDEN and vis[f"{REGION}:PCB"] == VISIBLE
    assert vis[f"{BOUNDARY}:domain_xmin"] == HIDDEN == vis[f"{BOUNDARY}:domain_xmax"]


def test_named_regions_are_counted():
    assert quality(named_regions())["regions"] == 3


def test_a_delivered_surface_nothing_declares_is_still_a_part():
    ps = parts(named_regions(), rendered=("PCB", "baffle"), enclosing=("baffle",),
               list_drawn=True)
    extra = next(p for p in ps if p["id"] == f"{BOUNDARY}:baffle")
    assert extra["renderable"] and extra["default_visibility"] == HIDDEN
    assert extra["source"] == "surface"
    # ...and nothing drawn is listed twice
    drawn = [p["metadata"]["name"] for p in ps if p["renderable"]]
    assert sorted(drawn) == ["PCB", "baffle"]


def test_an_undeclared_surface_is_not_listed_unless_asked():
    assert f"{BOUNDARY}:baffle" not in _ids(parts(named_regions(), rendered=("baffle",)))


# an interface drawn from BOTH sides


def conjugate_plate() -> dict:
    """The delivered cold plate: three regions and two interfaces, each one surface with a side
    in each of two regions - 16,619,777 faces on fluid_to_solid and the same 16,619,777 on
    solid_to_fluid, 270,816 each way on the chip contact."""
    return {
        "mesh_mode": "cfmesh_conjugate", "mesh_units": "m", "cell_count": 270_424_307,
        "patch_types": {"fluid_to_solid": "mappedWall", "port_a": "patch", "port_b": "patch",
                         "solid_to_air": "wall", "solid_to_fluid": "mappedWall",
                         "solid_to_chip": "mappedWall", "chip_to_air": "wall",
                         "chip_to_solid": "mappedWall"},
        "patch_face_counts": {"fluid_to_solid": 16_619_777, "port_a": 1_331, "port_b": 1_308,
                               "solid_to_air": 1_213_523, "solid_to_fluid": 16_619_777,
                               "solid_to_chip": 270_816, "chip_to_air": 1_492_860,
                               "chip_to_solid": 270_816},
        "quality": {"cells": 270_424_307, "regions": [
            {"name": "fluid", "type": "fluid", "cells": 55_303_441},
            {"name": "solid", "type": "solid", "cells": 199_394_482},
            {"name": "chip", "type": "solid", "cells": 15_726_384}]},
    }


BOTH_SIDES = ("fluid/fluid_to_solid", "fluid/port_a", "fluid/port_b",
              "solid/solid_to_air", "solid/solid_to_fluid", "solid/solid_to_chip",
              "chip/chip_to_air", "chip/chip_to_solid")
ONE_SIDE = tuple(s for s in BOTH_SIDES if s not in ("solid/solid_to_fluid",
                                                    "solid/solid_to_chip"))


def test_the_far_side_of_an_interface_is_the_outer_bodys_side():
    assert far_side_interfaces(BOTH_SIDES, ("solid/solid_to_air",)) == {
        "solid/solid_to_fluid": "fluid/fluid_to_solid",
        "solid/solid_to_chip": "chip/chip_to_solid"}


def test_a_patch_whose_partner_is_not_drawn_is_not_a_far_side():
    # the payload that ships: one sheet per interface, so there is nothing to hide
    assert far_side_interfaces(ONE_SIDE, ("solid/solid_to_air",)) == {}
    # and the ambient is not a region, so the outside of the plate is nobody's far side
    assert far_side_interfaces(("solid/solid_to_air",), ()) == {}


def test_exactly_one_side_is_picked_when_neither_region_encloses():
    pair = ("a/a_to_b", "b/b_to_a")
    assert far_side_interfaces(pair, ()) == {"b/b_to_a": "a/a_to_b"}
    assert far_side_interfaces(pair[::-1], ()) == {"a/a_to_b": "b/b_to_a"}


def test_the_far_side_opens_hidden_and_says_what_it_duplicates():
    ps = parts(conjugate_plate(), rendered=BOTH_SIDES, enclosing=("solid/solid_to_air",),
               coincident=far_side_interfaces(BOTH_SIDES, ("solid/solid_to_air",)),
               list_drawn=True)
    by = {p["id"]: p for p in ps}
    far = by[f"{BOUNDARY}:solid_to_fluid"]
    assert far["default_visibility"] == HIDDEN
    assert far["metadata"]["coincident_with"] == "fluid/fluid_to_solid"
    # the near side is untouched: it is the sheet that stays on screen
    near = by[f"{BOUNDARY}:fluid_to_solid"]
    assert near["default_visibility"] == VISIBLE and "coincident_with" not in near["metadata"]
    # and it is hidden, not dropped - the metal still names it, which is the only way a reader
    # who isolates the metal reaches the channel walls carved into it
    assert by[f"{REGION}:solid"]["metadata"]["drawn_as"] == [
        "solid/solid_to_air", "solid/solid_to_fluid", "solid/solid_to_chip"]


def test_the_shipped_one_sided_payload_hides_nothing_new():
    ps = parts(conjugate_plate(), rendered=ONE_SIDE, enclosing=("solid/solid_to_air",),
               coincident=far_side_interfaces(ONE_SIDE, ("solid/solid_to_air",)),
               list_drawn=True)
    hidden = {p["id"] for p in ps if p["default_visibility"] == HIDDEN}
    assert hidden == {f"{BOUNDARY}:solid_to_air"}, "a one-sided payload changed"


def test_a_far_side_nothing_declares_still_opens_hidden():
    man = conjugate_plate()
    del man["patch_types"]["solid_to_fluid"]
    del man["patch_face_counts"]["solid_to_fluid"]
    ps = parts(man, rendered=BOTH_SIDES, enclosing=("solid/solid_to_air",),
               coincident=far_side_interfaces(BOTH_SIDES, ("solid/solid_to_air",)),
               list_drawn=True)
    extra = next(p for p in ps if p["id"] == f"{BOUNDARY}:solid/solid_to_fluid")
    assert extra["renderable"] and extra["default_visibility"] == HIDDEN
    assert extra["metadata"]["coincident_with"] == "fluid/fluid_to_solid"
