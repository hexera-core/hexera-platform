# Responsibility: Verify "first what the part is, then which opening is which" (application/
# part_understanding): the object read from the user's words and the measured openings, and every
# opening's role from that understanding - sure where engineering says (a pump's eye, a diffuser's
# narrow end, an exhaust manifold's runners), a marked guess where nothing can know the way the fluid
# goes (a straight pipe, a bend, a tee that may split or mix) - and that the stage and the pictures
# never show a sticker on a face that is not an opening.
# Boundaries: plain facts dicts; no CAD, no model, no storage.
from __future__ import annotations

from meshpipeline.application import geometry_check as gc
from meshpipeline.application import part_understanding as pu


def _o(i, centre, normal, d, role="outlet"):
    return {"id": i, "name": role, "role": role, "shape": "circle", "diameter_mm": d,
            "area_mm2": 3.14159 / 4 * d * d, "centroid_mm": list(centre), "normal": list(normal), "confidence": 0.85}


def _facts(openings, size=(1000.0, 400.0, 400.0), lo=(0.0, -200.0, -200.0), **over):
    f = {"body_kind": "pipe_wall", "input_kind": "body-surface", "flow": "internal", "solids": 1,
         "size_mm": list(size), "bbox_min_m": [v / 1000.0 for v in lo], "confidence": {"input_kind": 0.9},
         "inside": {"rule": "INS-1", "has_inside": True, "passages": 1, "mouths_per_passage": [len(openings)],
                    "enclosed_mm3": 1.0e6},
         "openings": openings, "notes": []}
    f.update(over)
    return f


def _roles(openings) -> dict:
    return {o["id"]: o["role"] for o in openings}


# ------------------------------------------------------------------ what the part is ----
def test_a_straight_pipe_is_named_and_its_direction_is_a_marked_guess():
    ops = [_o(1, (0, 0, 0), (-1, 0, 0), 100, "inlet"), _o(2, (1000, 0, 0), (1, 0, 0), 100)]
    f = _facts(ops)
    und = pu.understand(f, ops, flow="internal")
    assert und["class"] == "straight" and und["by"] == "shape" and "straight" in und["object"]
    out = pu.assign(ops, und, f)
    assert _roles(out) == {1: "inlet", 2: "outlet"}
    assert all(o["role_reason"]["guess"] and o["role_reason"]["reason"] for o in out)


def test_a_pump_casing_draws_in_at_its_eye_whatever_order_the_scout_gave():
    # the eye on the casing's axis, in its middle; the outlet square to it and off to the side
    ops = [_o(1, (150, 260, 0), (0, 1, 0), 120, "inlet"), _o(2, (0, 0, 110), (0, 0, 1), 90)]
    f = _facts(ops, size=(400.0, 520.0, 220.0), lo=(-200.0, -260.0, -110.0))
    und = pu.understand(f, ops, flow="internal")
    assert und["class"] == "volute"
    out = {o["id"]: o for o in pu.assign(ops, und, f)}
    assert (out[2]["role"], out[1]["role"]) == ("inlet", "outlet")
    assert out[2]["role_reason"]["guess"] is False and "eye" in out[2]["role_reason"]["reason"]


def _manifold():
    """Four equal runners into one larger collector."""
    return [_o(1, (0, 0, 0), (-1, 0, 0), 120, "inlet")] + [
        _o(k + 2, (300 + 150 * k, 0, 200), (0, 0, 1), 50) for k in range(4)]


def test_an_exhaust_manifold_takes_the_flow_in_at_its_runners_because_the_user_said_so():
    ops = _manifold()
    f = _facts(ops)
    und = pu.understand(f, ops, flow="internal", words="mesh this exhaust manifold for the CFD")
    assert und["class"] == "manifold" and und["mode"] == "collect" and und["by"] == "words"
    out = {o["id"]: o for o in pu.assign(ops, und, f)}
    assert out[1]["role"] == "outlet" and all(out[k]["role"] == "inlet" for k in (2, 3, 4, 5))
    assert not any(o["role_reason"]["guess"] for o in out.values())
    assert [out[k]["name"] for k in (2, 3, 4, 5)] == ["inlet_1", "inlet_2", "inlet_3", "inlet_4"]


def test_a_manifold_with_no_words_feeds_from_the_larger_opening_and_says_it_is_a_guess():
    ops = _manifold()
    f = _facts(ops)
    und = pu.understand(f, ops, flow="internal")
    assert und["class"] == "manifold" and "4" in und["object"]
    out = {o["id"]: o for o in pu.assign(ops, und, f)}
    assert out[1]["role"] == "inlet" and out[1]["role_reason"]["guess"] is True
    assert "exhaust" in out[1]["role_reason"]["reason"]


def _tee():
    return [_o(1, (0, 0, 0), (-1, 0, 0), 100, "inlet"), _o(2, (1000, 0, 0), (1, 0, 0), 100),
            _o(3, (500, 0, 300), (0, 0, 1), 60)]


def test_a_tee_may_split_or_mix_so_its_roles_are_a_guess_until_the_words_say():
    ops = _tee()
    f = _facts(ops)
    und = pu.understand(f, ops, flow="internal")
    assert und["class"] == "tee"
    out = pu.assign(ops, und, f)
    assert _roles(out) == {1: "inlet", 2: "outlet", 3: "outlet"} and all(o["role_reason"]["guess"] for o in out)
    mixed = pu.assign(ops, pu.understand(f, ops, flow="internal", words="a mixing tee"), f)
    assert _roles(mixed) == {1: "inlet", 2: "outlet", 3: "inlet"}


def test_a_wye_splits_from_its_stem_unless_the_words_say_it_joins():
    ops = [_o(1, (0, 0, 0), (-1, 0, 0), 120, "inlet"), _o(2, (500, -170, 0), (0.88, -0.47, 0), 90),
           _o(3, (500, 170, 0), (0.88, 0.47, 0), 90)]
    f = _facts(ops)
    und = pu.understand(f, ops, flow="internal")
    assert und["class"] == "wye" and und["measured"]["stem"] == 1
    assert _roles(pu.assign(ops, und, f)) == {1: "inlet", 2: "outlet", 3: "outlet"}
    joined = pu.assign(ops, pu.understand(f, ops, flow="internal", words="a mixing wye"), f)
    assert _roles(joined) == {1: "outlet", 2: "inlet", 3: "inlet"}


def test_the_users_words_name_the_object_before_the_pictures():
    ops = _tee()
    f = _facts(ops)
    und = pu.understand(f, ops, flow="internal", words="a valve body", vision={"part": "a pipe tee", "confidence": 0.9})
    assert und["by"] == "words" and und["class"] == "chamber"
    pic = pu.understand(f, ops, flow="internal", vision={"part": "a pipe tee", "confidence": 0.9})
    assert pic["class"] == "tee" and any("pictures agree" in r for r in pic["reasons"])


def test_a_body_in_a_flow_gets_no_roles():
    ops = _tee()
    f = _facts(ops, flow="external", body_kind="single_solid", input_kind="solid-body")
    und = pu.understand(f, ops, flow="external")
    assert und["class"] == "body"


# ------------------------------------------------------------- the stage and the pictures ----
def test_a_face_that_is_not_an_opening_wears_no_sticker():
    ops = _tee() + [dict(_o(4, (500, 0, -200), (0, 0, -1), 20), role="not_an_opening", name="side_1")]
    assert [o["id"] for o in gc.stickered(_facts(ops))] == [1, 2, 3]


def test_the_proposal_carries_the_understanding_and_each_roles_reason():
    ops = _manifold()
    f = _facts(ops)
    gc.settle_flow_kind(f)
    p = gc._proposal(f, None, words="exhaust manifold")
    assert p["understanding"]["class"] == "manifold"
    assert {o["id"]: o["role"] for o in p["openings"]}[1] == "outlet"
    assert all("role_reason" in o for o in p["openings"])


def test_a_pictures_role_over_a_guess_stays_a_guess_for_the_user_to_confirm():
    ops = [_o(1, (0, 0, 0), (-1, 0, 0), 100, "inlet"), _o(2, (1000, 0, 0), (1, 0, 0), 100)]
    f = _facts(ops)
    gc.settle_flow_kind(f)
    vision = {"part": "a pipe", "flow": "internal", "input_kind": "body-surface", "confidence": 0.9,
              "openings": [{"id": 1, "name": "outlet", "role": "outlet", "confidence": 0.8},
                           {"id": 2, "name": "inlet", "role": "inlet", "confidence": 0.8}]}
    p = gc._proposal(f, vision)
    by = {o["id"]: o for o in p["openings"]}
    assert (by[1]["role"], by[2]["role"]) == ("outlet", "inlet")
    assert by[1]["role_reason"]["guess"] is True and "pictures" in by[1]["role_reason"]["reason"]


def test_assign_never_raises_and_leaves_declared_openings_alone():
    declared = [dict(_o(1, (0, 0, 0), (-1, 0, 0), 100, "inlet"), declared_by="file"),
                dict(_o(2, (1000, 0, 0), (1, 0, 0), 100), declared_by="file")]
    out = pu.assign(declared, {"class": "straight"}, {})
    assert all("role_reason" not in o for o in out)
    assert pu.assign(None, None, None) == []  # type: ignore[arg-type]
