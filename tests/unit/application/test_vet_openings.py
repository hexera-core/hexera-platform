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


def test_a_body_read_in_a_flow_is_not_staged_at_check_time(monkeypatch, tmp_path):
    # the Camaro on dev (4 Nov): a car read as a body in a flow spent 4 of the check's 6 minutes
    # staging a dozen wheel-arch and grille openings that are never drawn on a body
    import meshpipeline.application.geometry_check as gc

    ran = []
    monkeypatch.setattr(gc, "keep_a_stageable_proposal", lambda f, *a: ran.append("keep") or f)
    monkeypatch.setattr(gc, "vet_openings", lambda f, skin: ran.append("vet"))
    car = {**_facts(), "flow": "external"}
    out = gc.hold_openings_to_the_staging(car, tmp_path / "car.stl", tmp_path / "skin.stl", 0.001)
    assert ran == [] and out["openings_vetted"] is False
    assert [o["role"] for o in out["openings"]] == ["inlet", "outlet", "outlet", "outlet"]   # kept for a flip
    pipe = {**_facts(), "flow": "internal"}
    out = gc.hold_openings_to_the_staging(pipe, tmp_path / "pipe.stl", tmp_path / "skin.stl", 0.001)
    assert ran == ["keep", "vet"] and out["openings_vetted"] is True


def test_a_flip_to_through_it_with_an_opening_into_nothing_is_stopped_before_any_mesher(tmp_path):
    # the openings a body kept unvetted reach the mesher's staging if the user says the fluid flows
    # through it: one that opens into nothing is said there, with the way out, before a mesher runs
    import numpy as np

    from meshpipeline.cad.fluid_passages import PassagePlan, PassageReading

    plan = PassagePlan(meshed=[], left_out=[], stranded={"outlet_2": "it closes no passage"},
                       reading=PassageReading(passages=[], labels=np.zeros(0), patch_of=[], triangles=np.zeros((0, 3, 3))))
    assert "outlet_2" in plan.refusal() and "Mark it as not an opening" in plan.refusal()
