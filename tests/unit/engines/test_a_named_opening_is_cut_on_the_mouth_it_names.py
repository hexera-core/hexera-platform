# Responsibility: an opening the customer named is cut on the mouth they pointed at.
# Boundaries: `mouths.declared_for` / `port_rows` / `sealed_rows`, and the detail capture/restore.
# Collaborates with: application/geometry_confirmation, which writes the declaration the carve reads.
"""Job a9fa0aba meshed for 161 minutes and was refused for a boundary name.

    failed_gate patch_contract   review_execution not_reached
    "The mesh has no boundary called 'mouth_1' (it has 'mouth_2' and 'wall')"

TWO RULES NUMBERING ONE NAME SPACE. The card numbers by sticker, in proposal order, over a set
that still contains the demoted one - so it offered mouth_1 at the 8 mm bore (99.9, 54.7, 43.3),
mouth_2 at the 8 mm bore (89.3, 54.7, 77.4), and "sticker 3: not an opening" for the 11.8 mm lid
bore. The carve numbers by `enumerate(stubs)` over `rec["ports"]`, sorted largest passage first
and then widest hole first - so ITS index 0 is the 11.8 mm bore the card had just thrown out, and
both real mouths shift by one. Card 1 becomes engine 2, card 2 becomes engine 3, and engine 1 is
a hole with no number on the card at all.

IT FELL THROUGH TO THAT RULE OVER ONE WORD. `declared_for` kept only declarations whose type was
"inlet" or "outlet", and both of this part's mouths are `opening` - the role a severed pair is
delivered under, and one a customer has only been able to send since 2026-11-27. With every
declaration dropped it returned (None, "") and `port_rows` named the mouths itself.

AND THE REFUSAL WAS LUCK. A name on the wrong bore still satisfies the contract, which compares
names. That bore is capped, so topoSet found no faces and createPatch deleted the patch - the only
reason the mismatch surfaced. With an open lid port the same run delivers a mesh whose mouth_1 is
the wrong hole and the gate passes it. These tests pin the binding, not the refusal.
"""
from __future__ import annotations

import json

import pytest

from meshpipeline.application import geometry_confirmation as G
from meshpipeline.engines.cfmesh_conjugate import mouths as MO

#: The three mouths as `fluid_volume_answer.json` records them for this part, in the order the
#: extraction measured them - the same fixture
#: `test_a_port_on_a_blind_mouth_is_refused_before_meshing` uses. Only the first is capped.
PORTS = (
    {"centroid_m": (0.065923414495, 0.033446178113, 0.07540532726), "normal": (0.0, 1.0, 0.0),
     "diameter_m": 0.0118, "area_m2": 1.093588e-04, "passage": 0, "clear_ahead": False},
    {"centroid_m": (0.099881949608, 0.054681178113, 0.043305688999),
     "normal": (0.998233779, 0.0, 0.059408106),
     "diameter_m": 0.008, "area_m2": 5.0265e-05, "passage": 0, "clear_ahead": True},
    {"centroid_m": (0.089339605589, 0.054681178113, 0.077388253039),
     "normal": (0.996433664, 0.0, 0.08437982),
     "diameter_m": 0.008, "area_m2": 5.0265e-05, "passage": 1, "clear_ahead": True},
)
#: That part's `open_rims`, verbatim. One rim, on the blind mouth, marked sealed.
RIMS = ({"edges": 63, "length": 0.035941, "centroid": [0.065923, 0.033946, 0.075405],
         "sealed": True},)

LID_MM = [65.923414495, 33.446178113, 75.40532726]
A_MM = [99.881949608, 54.681178113, 43.305688999]
B_MM = [89.339605589, 54.681178113, 77.388253039]

#: What job a9fa0aba's customer approved, pressing Proceed on the card unedited.
APPROVED = (
    {"name": "mouth_1", "type": "opening", "near_mm": A_MM},
    {"name": "mouth_2", "type": "opening", "near_mm": B_MM},
    {"name": "wall", "type": "wall"},
)


def stubs(ports=PORTS, rims=RIMS):
    return MO.shell_stubs(list(ports), list(rims) if rims is not None else None)


def rows_for(st, declared, *, roles=False):
    named, _why = MO.declared_for(st, list(declared)) if declared else (None, "")
    rows, _said = MO.port_rows(st, named=named, roles=roles)
    return rows


def named_at(rows):
    return [(r.get("name"), r.get("type")) for r in rows]


# ------------------------------------------------------------------ the binding

def test_a_named_opening_binds_to_the_mouth_it_was_named_at():
    """THE REGRESSION. Before the fix this returned (None, "") and the carve named the mouths
    itself, minting the customer's mouth_1 onto the 11.8 mm bore they had excluded."""
    got, said = MO.declared_for(stubs(), list(APPROVED))
    assert got is not None, f"the declaration was dropped: {said!r}"
    # the extraction's order is lid bore, mouth A, mouth B
    assert got[1][0] == "mouth_1", "the customer's mouth_1 is not on the bore they pointed at"
    assert got[2][0] == "mouth_2"
    assert got[1][1] == "opening" and got[2][1] == "opening"


def test_the_bore_the_customer_excluded_gets_no_customer_name():
    got, _said = MO.declared_for(stubs(), list(APPROVED))
    assert got[0][0] not in ("mouth_1", "mouth_2"), \
        "a name the customer gave was minted onto the hole they called not_an_opening"


def test_the_excluded_bore_is_a_wall_because_its_shape_says_so():
    """`is_sealed` is the two-signal measurement, not a guess."""
    st = stubs()
    assert [s.is_sealed for s in st] == [True, False, False]
    got, _ = MO.declared_for(st, list(APPROVED))
    assert got[0][1] == "wall"


def test_an_undeclared_mouth_is_still_cut_as_a_wall():
    """DELIBERATELY UNCHANGED. It was argued the leftover should take the role its shape earns,
    so an unsealed one would be an `opening`. That argument is about an openings-only card; here
    the ports are named and a spare mouth is a hole the customer did not ask for, so the delivery
    carries exactly the ports promised. See the note above the loop in `declared_for`."""
    st = stubs()
    got, _ = MO.declared_for(st, [dict(APPROVED[0]), dict(APPROVED[2])])
    assert got is not None
    assert st[2].is_sealed is False
    assert got[2][1] == "wall"


def test_the_names_reach_the_rows_that_are_cut():
    rows = rows_for(stubs(), APPROVED)
    assert ("mouth_1", "opening") in named_at(rows)
    assert ("mouth_2", "opening") in named_at(rows)


@pytest.mark.parametrize("role", ["inlet", "outlet"])
def test_inlet_and_outlet_still_bind_exactly_as_before(role):
    decl = [{**dict(APPROVED[0]), "type": role}, dict(APPROVED[2])]
    got, _said = MO.declared_for(stubs(), decl)
    assert got is not None
    assert got[1] == ("mouth_1", role)


def test_a_declaration_with_no_position_is_still_refused():
    """`near_mm` is the only thing that ties a name to a hole; without it there is nothing to
    bind and the carve must say so rather than guess."""
    got, said = MO.declared_for(stubs(), [{"name": "mouth_1", "type": "opening"},
                                          {"name": "wall", "type": "wall"}])
    assert got is None and said == ""


# ------------------------------------------------------------- priced before the machine

def test_an_opening_cut_on_a_capped_mouth_is_refused_before_meshing():
    """Once an `opening` CAN be bound, it can be bound to a capped bore - the 161-minute refusal
    one role word over. `sealed_rows` prices that before the machine is paid for."""
    st = stubs()
    rows = rows_for(st, [{"name": "mouth_1", "type": "opening", "near_mm": LID_MM},
                         {"name": "wall", "type": "wall"}])
    said = MO.sealed_rows(st, rows)
    assert said, "a declared opening on a capped mouth was not priced"
    assert "mouth_1" in said


def test_a_declared_opening_on_an_open_mouth_is_not_refused():
    st = stubs()
    assert MO.sealed_rows(st, rows_for(st, APPROVED)) == ""


def test_an_undeclared_capped_mouth_is_still_not_refused():
    """It is cut as a wall, which is what a capped bore is; only a declared PORT on one is a
    refusal, because that is a promise the mesh cannot keep."""
    st = stubs()
    assert MO.sealed_rows(st, rows_for(st, [dict(APPROVED[1]), dict(APPROVED[2])])) == ""


# ------------------------------------------------------------- the numbers survive the model

class _Store:
    def __init__(self, body):
        self._body = body

    def get_bytes(self, *, object_key):      # noqa: ARG002 - the key is not the subject
        return json.dumps(self._body).encode()


def test_an_openings_position_is_captured_for_restoring(monkeypatch):
    """The engine reads `port_declaration.json`, written verbatim from the list a MODEL submitted
    - and this repo measured a run where the model dropped the numbers and the mesh was refused
    after 29 minutes. The capture has to cover the role the mouths actually carry."""
    import meshpipeline.contracts.object_storage as OS

    monkeypatch.setattr(OS, "get_object_store",
                        lambda: _Store({"patches": [dict(p) for p in APPROVED]}))
    got = G.confirmed_port_details("sid")
    assert set(got) == {"mouth_1", "mouth_2"}, "an opening's measured numbers were not kept"
    assert got["mouth_1"]["near_mm"] == A_MM
    assert "wall" not in got, "a wall carries no position and names no mouth"


def test_an_openings_position_is_restored_when_the_model_drops_it():
    stripped = [{"name": "mouth_1", "type": "opening"},
                {"name": "mouth_2", "type": "opening"},
                {"name": "wall", "type": "wall"}]
    confirmed = {"mouth_1": {"near_mm": A_MM}, "mouth_2": {"near_mm": B_MM}}
    rows, restored = G.restore_port_details(stripped, confirmed)
    assert sorted(restored) == ["mouth_1", "mouth_2"]
    back = {r["name"]: r for r in rows}
    assert back["mouth_1"]["near_mm"] == A_MM
    # and with the numbers back, the carve binds to the right mouth
    bound, _ = MO.declared_for(stubs(), rows)
    assert bound is not None and bound[1][0] == "mouth_1"


def test_a_wall_is_never_given_port_details():
    rows, restored = G.restore_port_details(
        [{"name": "wall", "type": "wall"}], {"wall": {"near_mm": A_MM}})
    assert restored == []
    assert "near_mm" not in rows[0]
