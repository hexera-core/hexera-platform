# Responsibility: Verify a customer can press Proceed on the role the geometry card offers for a
# mouth that is neither an inlet nor an outlet, and that confirming it declares no port pair.
# Boundaries: the contract and what it turns a role into. No CAD, no rendering, no database.
#
# MEASURED on session 11091006 (2026-11-27), the fourth run of the Corintis plate through the whole
# product as a customer. The geometry check offered, correctly:
#
#     1  mouth_1      role=opening          8.0 mm
#     2  mouth_2      role=opening          8.0 mm
#     3  capped bore  role=not_an_opening   11.8 mm
#
# `opening` because this plate's two hose mouths open into coolant spaces that do not connect, so
# neither is the inlet or the outlet OF anything (cad/fluid_volume_check._unname_a_severed_pair).
# Pressing Proceed on that card returned HTTP 422:
#
#     Input should be 'inlet', 'outlet' or 'not_an_opening'   [body, openings, 0, role]
#
# The product proposed a state its own API refused. Both words that were available say something
# false about such a mouth: inlet/outlet claim a flow path the surface does not have, which is what
# the engine refused job 44362085 for four and a half minutes after Proceed; `not_an_opening` is
# "a hole or a face the fluid does not pass", and the fluid passes through both of these.
#
# IT IS NOT A NEW WORD. `cfmesh_conjugate/mouths.port_rows` already types an undeclared mouth
# `opening`, `contracts/patch_names` maps it, and job 606af55e reached that path the only way that
# was possible before - every sticker `not_an_opening`, `patches: []` - and meshed 279,286,964 cells.
from __future__ import annotations

import pytest
from pydantic import ValidationError

from meshpipeline.api.v1.geometry import ConfirmedOpening
from meshpipeline.application.geometry_confirmation import patches_from


class _Body:
    """The confirm body's shape, as `patches_from` and `confirmation_message` read it."""

    def __init__(self, openings, flow="internal", input_kind="solid-assembly"):
        self.openings, self.flow, self.input_kind = openings, flow, input_kind
        self.part, self.openings_reason = "liquid-cooled chip cold plate", ""
        self.seed_point_mm, self.size_mm = [66.0, 30.0, 75.0], [177.5, 41.84, 101.0]


def _mouth(i: int, role: str, name: str | None = None) -> ConfirmedOpening:
    return ConfirmedOpening(id=i, name=name or f"mouth_{i}", role=role, diameter_mm=8.0,
                            centroid_mm=[99.88, 54.68, 43.31])


def test_the_role_the_card_offers_is_a_role_the_api_accepts():
    """THE DEFECT, as one assertion."""
    assert _mouth(1, "opening").role == "opening"


def test_the_three_words_that_already_worked_still_do():
    for role in ("inlet", "outlet", "not_an_opening"):
        assert _mouth(1, role).role == role


def test_a_word_the_card_never_offers_is_still_refused():
    """Widening the contract by one word is not opening it: a typo or an invented role has to stay
    a 422 rather than reach `patches_from` as a boundary type nothing downstream knows."""
    for bad in ("wall", "farfield", "Opening", "openings", "", "inlet_1"):
        with pytest.raises(ValidationError):
            _mouth(1, bad)


def test_confirming_unnamed_mouths_declares_no_port_pair():
    """WHAT IT MUST MEAN DOWNSTREAM. `plan.ports_from` returns a PortPlan only when a declaration
    carries an inlet or an outlet role; with neither it returns None and
    `mouths.port_rows` types every mouth `opening` as mouth_1, mouth_2 - the path job 606af55e
    delivered on. So the patches must carry the word through and name no port.
    """
    pats = patches_from(_Body([_mouth(1, "opening"), _mouth(2, "opening"),
                               _mouth(3, "not_an_opening", "capped bore")]))
    # `patches_from` adds the built-in wall beside the declared mouths, which is unchanged here.
    mouths = [p for p in pats if p["type"] != "wall"]
    assert [p["name"] for p in mouths] == ["mouth_1", "mouth_2"], pats
    assert {p["type"] for p in mouths} == {"opening"}, pats
    assert not [p for p in pats if p["type"] in ("inlet", "outlet")], (
        "a port pair was declared on a coolant that cannot flow between its mouths")
    assert [p["name"] for p in pats if p["type"] == "wall"] == ["wall"], pats


def test_the_capped_bore_is_still_left_out_of_the_patches():
    """`not_an_opening` keeps meaning what it meant: no patch is cut there at all."""
    pats = patches_from(_Body([_mouth(3, "not_an_opening", "capped bore")]))
    assert [p for p in pats if p["type"] != "wall"] == [], pats


def test_a_real_port_pair_still_declares_itself():
    """THE HALF THAT MUST NOT MOVE: every part whose two ends do connect."""
    pats = patches_from(_Body([_mouth(1, "inlet", "inlet"), _mouth(2, "outlet", "outlet")]))
    mouths = [(p["name"], p["type"]) for p in pats if p["type"] != "wall"]
    assert mouths == [("inlet", "inlet"), ("outlet", "outlet")], pats


def test_the_vision_model_is_still_not_offered_the_word():
    """THE ONE PLACE THAT IS DELIBERATELY NOT WIDENED. `opening` is the MEASUREMENT's word - it
    comes from a connectivity reading no render can make - so the naming model's own JSON schema
    keeps its three, and a model that answered `opening` would be answering a question it was not
    asked. See `geometry_check._merge`, which holds a marked mouth against the model anyway.
    """
    import inspect

    from meshpipeline.application import geometry_check as gc

    src = inspect.getsource(gc)
    assert '"enum": ["inlet", "outlet", "not_an_opening"]' in src, (
        "the naming model's enum changed shape; check it was not widened by accident")
    assert '"opening"' not in src.split('"enum": [')[1].split("]")[0]
