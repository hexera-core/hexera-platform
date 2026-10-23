# Responsibility: Hold the FEA geometry-check path to its contract: an FEA part opens its own stage
# (never the flow form), shows its size, unit, bodies and clean check, refuses several bodies with
# a plain message, declares itself as a structural solid, and is never named by the picture model.
# Boundaries: pure functions and the naming step with its store stood in for; no model, no network.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.application import fea_check
from meshpipeline.application import geometry_confirmation as gcf
from meshpipeline.application import purpose_map as pm


def _said(*texts):
    return [{"role": "assistant", "content": "What are you meshing this for?"}] + [
        {"role": "user", "content": t} for t in texts]


def test_the_stage_waits_for_the_purpose_then_opens_on_the_right_path():
    assert fea_check.stage_route({}, []) == ""                                  # nobody answered yet
    assert fea_check.stage_route({}, _said("thermal")) == ""                    # the question is open
    assert fea_check.stage_route({}, _said("stress testing")) == fea_check.STAGE_FEA
    assert fea_check.stage_route({}, _said("heat conduction through this solid")) == fea_check.STAGE_FEA
    assert fea_check.stage_route({}, _said("airflow over it")) == fea_check.STAGE_FLOW
    assert fea_check.stage_route({}, _said("electronics cooling")) == fea_check.STAGE_FLOW
    # an ECXML model is its file's, whatever is said
    assert fea_check.stage_route({"check_mode": "thermal"}, _said("stress")) == fea_check.STAGE_THERMAL


def test_the_view_says_the_bodies_and_the_clean_check_in_plain_words():
    clean = {"solids": 1, "validity": {"clean": True, "kind": "brep", "summary": "The geometry is clean."}}
    view = fea_check.fea_view(clean)
    assert view["bodies"] == 1 and view["clean"] is True and view["can_proceed"]
    assert view["question"] == "Is this your part and are the units right?"
    dirty = {"solids": 1, "validity": {"clean": False, "kind": "brep",
                                       "summary": "The geometry is not clean: 2 faces overlap"}}
    assert fea_check.fea_view(dirty)["clean_summary"].startswith("The geometry is not clean")
    assert fea_check.fea_view({"solids": 1})["clean"] is None


def test_several_bodies_get_a_clear_message_and_no_proceed():
    view = fea_check.fea_view({"solids": 3})
    assert not view["can_proceed"]
    assert view["single_part_only"].startswith("This file has 3 separate bodies. Version 1 meshes one part")
    assert fea_check.multi_body_message({"solids": 1}) == ""
    assert fea_check.multi_body_message({}) == ""


def test_the_declaration_is_a_structural_solid_with_no_fluid_words():
    body = SimpleNamespace(check_mode="fea", part="", bodies=1, clean_summary="The geometry is clean.",
                           size_mm=[200.0, 100.0, 5.0], unit="mm", unit_fixed=False)
    message = gcf.confirmation_message(body)
    assert message.startswith(f"{gcf.CONFIRMED_MARK} {gcf.FEA_LEAD}")
    assert "One body." in message and "Clean check: The geometry is clean." in message
    assert "Part size: 200 x 100 x 5 mm." in message and "millimetres" in message
    for word in ("fluid flows", "opening", "inlet", "outlet", "far field", "sticker"):
        assert word not in message.lower()
    msgs = [{"role": "assistant", "content": message}]
    assert gcf.declared_case(msgs) == ("structural", "solid-body")
    assert gcf.fea_declared(msgs) and not gcf.thermal_declared(msgs)


def test_the_naming_step_asks_no_model_for_an_fea_part(monkeypatch):
    from meshpipeline.application import geometry_check as gc

    stored = {"status": gc.STATUS_SCOUTED, "facts": {
        "input_kind": "solid-body", "flow": "external", "openings": [], "size_mm": [300.0, 20.0, 20.0],
        "notes": [], "solids": 1, "scale_to_m": 0.001, "unit_assumed": False}, "snapshots": []}
    landed: dict = {}
    monkeypatch.setattr(gc, "_wait_for_scout", lambda sid: stored)
    monkeypatch.setattr(gc, "store_status", lambda sid, status, step, **kw: landed.update(status=status, **kw) or landed)
    monkeypatch.setattr(gc, "_name_with_vision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    out = gc._name(session_id="s1", owner_id="o", purpose_text="stress testing", interpretation=None)
    assert out["status"] == gc.STATUS_READY and out["vision"] == {} and out["proposal"]["openings"] == []
    # the answer to the purpose question decides it, as the stage reads it
    assert gc._is_fea("simulation\n" + pm.DOMAIN_CHOICES[1][1])
    assert not gc._is_fea("simulation\n" + pm.DOMAIN_CHOICES[0][1])
    assert gc._is_fea("thermal\n" + pm.THERMAL_CHOICES[1][1])


def test_one_solid_of_overlapping_pieces_is_said_the_same_on_both_lines():
    """A turbine blade and its root drawn as two overlapping lumps of one solid: the stage counted
    1 body while the clean check said 1 pair of bodies pass into each other (22 Oct)."""
    facts = {"solids": 1, "validity": {"clean": False, "kind": "brep", "measured": {"bodies": 2},
                                       "summary": "1 pair of separate bodies pass into each other"}}
    view = fea_check.fea_view(facts)
    assert view["bodies"] == 1 and view["pieces"] == 2
    assert fea_check.piece_count({"solids": 1}) is None


def test_a_gentle_curve_is_not_a_fillet_hot_spot():
    import numpy as np

    from meshpipeline.cad import fea_features as ff

    blade = ff.Arc(kind="cylinder", radius=205.0, span_deg=30.0, concave=True, axis=np.array([0, 0, 1.0]),
                   centre=np.zeros(3))
    blend = ff.Arc(kind="cylinder", radius=4.0, span_deg=90.0, concave=True, axis=np.array([0, 0, 1.0]),
                   centre=np.zeros(3))
    assert ff.kind_of(blade, diag=320.0) is None                  # the blade's own camber
    assert ff.kind_of(blend, diag=320.0) == (ff.FILLET, "fillet")  # a real blend
    assert ff.kind_of(blade) == (ff.FILLET, "fillet")              # no size known: as before
