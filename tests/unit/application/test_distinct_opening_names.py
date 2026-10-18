# Responsibility: Verify the stage's confirmation names every real opening apart instead of
# refusing, and never compares stickers the user marked as not an opening.
from __future__ import annotations

import random

import pytest

from meshpipeline.api.v1.geometry import ConfirmedOpening, ConfirmIn
from meshpipeline.application.geometry_confirmation import distinct_opening_names, patches_from
from meshpipeline.contracts.patch_names import same_patch_name

_NAMES = ["inlet", "Inlet", "outlet", "outlet 1", "outlet_1", "OUTLET-1", "not_an_opening", "port",
          "x" * 40, "x" * 39 + "y", "wall", "a b", "a_b"]
_ROLES = ["inlet", "outlet", "not_an_opening"]


def _fuzz():
    rng = random.Random(20260929)
    return [[ConfirmedOpening(id=i + 1, name=rng.choice(_NAMES), role=rng.choice(_ROLES))
             for i in range(rng.randint(1, 9))] for _ in range(400)]


@pytest.mark.parametrize("openings", _fuzz())
def test_every_real_opening_ends_with_its_own_name_and_nothing_else_moves(openings):
    out = distinct_opening_names(openings)
    assert [o.id for o in out] == [o.id for o in openings] and [o.role for o in out] == [o.role for o in openings]
    real = [o.name for o in out if o.role != "not_an_opening"]
    for i, a in enumerate(real):
        assert all(not same_patch_name(a, b) for b in real[i + 1:]), real
    for before, after in zip(openings, out):
        assert len(after.name) <= 40
        if before.role == "not_an_opening":
            assert after.name == before.name                 # a discarded sticker is left as it is
    # the first of each name keeps it
    seen: list[str] = []
    for before, after in zip(openings, out):
        if before.role == "not_an_opening":
            continue
        if not any(same_patch_name(before.name, s) for s in seen):
            assert after.name == before.name
        seen.append(after.name)
    # and the patches the intake reads carry exactly those names
    body = ConfirmIn(input_kind="body-surface", flow="internal", openings=out)
    assert [p["name"] for p in patches_from(body) if p["type"] != "wall"] == real


def test_a_renamed_opening_is_said_never_silent():
    # review on #92: the declaration the intake reads names every opening that was renamed
    from meshpipeline.application.geometry_confirmation import renamed_openings_sentence
    before = [ConfirmedOpening(id=6, name="outlet", role="inlet"), ConfirmedOpening(id=7, name="Outlet", role="outlet")]
    after = distinct_opening_names(before)
    said = renamed_openings_sentence(before, after)
    assert "opening 7 is called Outlet_2" in said and "renamed on the picture" in said
    # the chat shows the confirmation to the user: it speaks to them, never to the model
    assert "tell the user" not in said.lower() and "next reply" not in said
    assert renamed_openings_sentence(before[:1], distinct_opening_names(before[:1])) == ""
