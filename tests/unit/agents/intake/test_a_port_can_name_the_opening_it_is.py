# Responsibility: A declared port may say WHICH measured opening it is, and that is its location.
# Boundaries: the binding and the payload gate; nothing here decides a role.
from __future__ import annotations

from meshpipeline.agents.intake.geometry_brief import bind_patches
from meshpipeline.agents.intake.validation import _validate_internal_ports

#: Two mouths of the SAME bore, which is the case no size can separate and the case a real customer
#: was interrogated about - twice - before being told to re-export a file that was never wrong.
_DOC = {
    "status": "ok",
    "bbox": {"diagonal_m": 1.2},
    "openings": [
        {"id": "o1", "centroid_m": [0.0, 0.0, -0.14], "bore_diameter_m": 0.551},
        {"id": "o2", "centroid_m": [0.0, -0.19, 0.0], "bore_diameter_m": 0.439},
        {"id": "o3", "centroid_m": [0.0, 0.19, 0.0], "bore_diameter_m": 0.439},
        {"id": "o4", "centroid_m": [0.52, 0.0, 0.0], "bore_diameter_m": 0.259},
    ],
}

_PORTS = [
    {"name": "inlet", "type": "inlet", "opening_id": "o1"},
    {"name": "outlet_o2", "type": "outlet", "opening_id": "o2"},
    {"name": "outlet_o3", "type": "outlet", "opening_id": "o3"},
    {"name": "outlet_o4", "type": "outlet", "opening_id": "o4"},
    {"name": "wall", "type": "wall"},
]


def test_a_port_that_names_its_opening_needs_no_size_and_no_coordinate():
    # The gate demanded a size or a point, so a model that knew perfectly well which mouth each port
    # was could not say so, and the submission was refused for a fact the measurement already held.
    assert _validate_internal_ports(_PORTS) == []


def test_two_mouths_of_the_same_bore_bind_to_themselves_and_not_to_each_other():
    # o2 and o3 are both 439 mm. Matching by size cannot separate them - naming them can.
    bound = bind_patches(_DOC, _PORTS)
    assert bound["checked"] is True, bound
    got = {b["name"]: b["opening_id"] for b in bound["bound"]}
    assert got == {"inlet": "o1", "outlet_o2": "o2", "outlet_o3": "o3", "outlet_o4": "o4"}


def test_an_opening_id_the_measurement_does_not_have_binds_to_nothing():
    # A named mouth that was never measured is a claim, not a location, and must not bind.
    ports = [{"name": "inlet", "type": "inlet", "opening_id": "o9"},
             {"name": "outlet", "type": "outlet", "opening_id": "o1"}]
    bound = bind_patches(_DOC, ports)
    assert bound["checked"] is False
    assert [b["name"] for b in bound["unbound"]] == ["inlet"]


def test_a_port_with_neither_a_size_nor_a_location_nor_an_id_is_still_refused():
    # The gate still has to fail when nothing identifies the mouth, or it is not a gate.
    errs = _validate_internal_ports([{"name": "inlet", "type": "inlet"},
                                     {"name": "outlet", "type": "outlet", "opening_id": "o2"},
                                     {"name": "wall", "type": "wall"}])
    assert len(errs) == 1 and "inlet" in errs[0]
