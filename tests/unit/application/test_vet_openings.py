"""The check's proposal held to the staging the mesher will use: an opening that opens into no
passage (a bolt hole through a flange, a slot through a plate) would come back as a patch with
no faces, so it is proposed as not an opening, with the reason said."""
from __future__ import annotations

import meshpipeline.cad.fluid_passages as fp
from meshpipeline.application.geometry_check import vet_openings


def _facts():
    def o(i, name, role, area):
        return {"id": i, "name": name, "role": role, "kind": "ring", "shape": "circle", "area_mm2": area,
                "diameter_mm": 10.0, "centroid_mm": [float(i), 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}
    return {"input_kind": "body-surface", "notes": [],
            "openings": [o(1, "inlet", "inlet", 300.0), o(2, "outlet_1", "outlet", 200.0),
                         o(3, "outlet_2", "outlet", 10.0), o(4, "outlet_3", "outlet", 150.0)]}


def test_a_hole_into_no_passage_is_proposed_as_not_an_opening_with_its_reason(monkeypatch, tmp_path):
    seen = {}

    def _stranded(skin, patches):
        seen["names"] = [p["name"] for p in patches if p["type"] != "wall"]
        return {"outlet_2": "its lid stands on its own, joined to no wall: it closes no passage"}

    monkeypatch.setattr(fp, "stranded_openings", _stranded)
    facts = _facts()
    vet_openings(facts, tmp_path / "skin.stl")
    assert seen["names"] == ["inlet", "outlet_1", "outlet_2", "outlet_3"]
    roles = {o["name"]: o["role"] for o in facts["openings"]}
    assert roles == {"inlet": "inlet", "outlet_1": "outlet", "outlet_2": "not_an_opening", "outlet_3": "outlet"}
    assert any("opening 3 (outlet_2) is proposed as not an opening" in n for n in facts["notes"])


def test_a_stranded_inlet_hands_the_role_to_the_largest_opening_left(monkeypatch, tmp_path):
    monkeypatch.setattr(fp, "stranded_openings", lambda skin, patches: {"inlet": "pocket"})
    facts = _facts()
    vet_openings(facts, tmp_path / "skin.stl")
    roles = {o["name"]: o["role"] for o in facts["openings"]}
    assert roles["inlet"] == "not_an_opening" and roles["outlet_1"] == "inlet"


def test_a_vetting_never_leaves_a_part_with_fewer_than_two_openings(monkeypatch, tmp_path):
    monkeypatch.setattr(fp, "stranded_openings",
                        lambda skin, patches: {"inlet": "x", "outlet_1": "x", "outlet_3": "x"})
    facts = _facts()
    vet_openings(facts, tmp_path / "skin.stl")
    assert all(o["role"] != "not_an_opening" for o in facts["openings"]) and not facts["notes"]
