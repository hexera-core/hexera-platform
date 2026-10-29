"""A cut port carries the name declared at intake, matched to its mouth by position.

WHY THIS EXISTS. The coupon plate went all the way through the console on 2026-10-29: the stage
read it, the intake proposed the conjugate engine, the ports were cut out of the lid, the mesh was
built on the Cloud Run mesh job, it came home - and then:

    failed_gate     patch_contract
    failure_detail  The mesh has no boundary called 'inlet' or 'outlet' (it has 'coolant_in' and
                    'coolant_out'); the mesh has 'coolant_in' and 'coolant_out', which you did not
                    approve, so the mesh does not match what you approved. This is our mistake,
                    not your geometry's.

Nothing was wrong with the mesh. `port_rows` names the first mouth it measured `coolant_in` and
says out loud that which end is fed is a guess - and its own docstring has always said "A declared
pair of names from intake overrides it". Nothing passed them.

BOUND BY POSITION, not by the order either list happens to be in. The declaration is measured off
the B-rep faces at intake and the stubs off the coolant's own mouths, by different code on
different surfaces, so nothing promises they come out in the same order - and an inlet and an
outlet swapped is a case that solves and runs backwards.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import mouths as MO
from meshpipeline.engines.cfmesh_conjugate.conjugate import render_port_patch_dicts

BLOCK_LO, BLOCK_HI = [0.0, 0.0, 0.0], [0.008, 0.006, 0.002]

#: The coupon's own two mouths and its own port_declaration.json, verbatim from the run above.
MOUTH_A, MOUTH_B = (0.006, 0.005, 0.002), (0.002, 0.001, 0.002)
DECLARED = [{"name": "inlet", "type": "inlet", "near_mm": [6, 5, 2], "diameter_mm": 0.9},
            {"name": "outlet", "type": "outlet", "near_mm": [2, 1, 2], "diameter_mm": 0.9}]


def _mouth(centre):
    return {"passage": 0, "centroid_m": list(centre), "normal": [0.0, 0.0, 1.0],
            "area_m2": 0.64e-6, "diameter_m": 0.0009, "clear_ahead": True}


@pytest.fixture
def stubs():
    got, _said = MO.stubs_for([_mouth(MOUTH_A), _mouth(MOUTH_B)], BLOCK_LO, BLOCK_HI)
    assert len(got) == 2
    return got


#   -- the binding -------------------------------------------------------------------------------

def test_the_declared_names_reach_the_cut(stubs):
    named, _why = MO.declared_for(stubs, DECLARED)
    assert named == [("inlet", "inlet"), ("outlet", "outlet")]
    rows, _said = MO.port_rows(stubs, named=named)
    assert [r["name"] for r in rows] == ["inlet", "outlet"]
    _topo, patch = render_port_patch_dicts(rows)
    assert "name inlet;" in patch and "name outlet;" in patch
    assert "coolant_in" not in patch, "the engine's default is what the contract refused"


def test_position_decides_and_not_the_order_of_either_list(stubs):
    """The two lists are measured by different code on different surfaces."""
    forwards, _ = MO.declared_for(stubs, DECLARED)
    backwards, _ = MO.declared_for(stubs, list(reversed(DECLARED)))
    assert forwards == backwards


def test_the_roles_are_the_declaration_s_too(stubs):
    """Which end is fed is the one thing the geometry cannot say, and the user has said it."""
    swapped = [{**DECLARED[0], "name": "coolant_return", "type": "outlet"},
               {**DECLARED[1], "name": "coolant_feed", "type": "inlet"}]
    named, _ = MO.declared_for(stubs, swapped)
    assert named == [("coolant_return", "outlet"), ("coolant_feed", "inlet")]
    rows, _ = MO.port_rows(stubs, named=named)
    assert [(r["name"], r["type"]) for r in rows] == [
        ("coolant_return", "outlet"), ("coolant_feed", "inlet")]


def test_what_it_says_names_the_ports_it_bound(stubs):
    named, why = MO.declared_for(stubs, DECLARED)
    assert "declared at intake" in why and "by position" in why
    assert "inlet (inlet)" in why and "outlet (outlet)" in why
    _rows, said = MO.port_rows(stubs, named=named)
    assert said.startswith("inlet and outlet are cut out of zmax")


#   -- when it cannot bind, it says so and keeps the honest default -------------------------------

def test_a_declaration_nowhere_near_the_mouths_is_not_used(stubs):
    far = [{"name": "inlet", "type": "inlet", "near_mm": [100, 100, 100]},
           {"name": "outlet", "type": "outlet", "near_mm": [200, 200, 200]}]
    named, why = MO.declared_for(stubs, far)
    assert named is None
    assert "could not be matched" in why
    rows, _ = MO.port_rows(stubs, named=named)
    assert [r["name"] for r in rows] == ["coolant_in", "coolant_out"]


def test_no_declaration_at_all_keeps_the_default_quietly(stubs):
    """Most parts declare nothing, and there is nothing to say about it."""
    named, why = MO.declared_for(stubs, [])
    assert named is None and why == ""
    rows, _ = MO.port_rows(stubs, named=named)
    assert [r["name"] for r in rows] == ["coolant_in", "coolant_out"]


def test_a_partly_matchable_declaration_is_refused_whole(stubs):
    """Half the patches under the user's names and half under ours is worse than all of ours."""
    half = [DECLARED[0], {"name": "outlet", "type": "outlet", "near_mm": [500, 500, 500]}]
    named, why = MO.declared_for(stubs, half)
    assert named is None, "one of the two could be placed; that is not enough"
    assert "could not be matched" in why


def test_a_declared_port_with_no_name_is_not_a_name(stubs):
    nameless = [{"name": "", "type": "inlet", "near_mm": [6, 5, 2]},
                {"name": "", "type": "outlet", "near_mm": [2, 1, 2]}]
    named, _why = MO.declared_for(stubs, nameless)
    assert named is None


@pytest.mark.parametrize("bad", [
    {"near_mm": []},            # the key is there and says nothing
    {"near_mm": [1, 2]},        # two axes is not a position
    {"near_mm": None},
    "drop",                     # ...and the key missing altogether
])
def test_a_declaration_with_no_usable_position_is_not_used(stubs, bad):
    """A declaration that cannot be PLACED cannot be matched, and matching by order instead is
    the thing this exists to avoid."""
    if bad == "drop":
        decl = [{k: v for k, v in d.items() if k != "near_mm"} for d in DECLARED]
    else:
        decl = [{**DECLARED[0], **bad}, {**DECLARED[1], **bad}]
    named, _why = MO.declared_for(stubs, decl)
    assert named is None


def test_the_tolerance_is_the_mouth_s_own_size(stubs):
    """The two measurements agree closely - the stage confirmed an opening the scout read off the
    same part - so the slack is a couple of mouth diameters, not a free-for-all."""
    assert MO.DECLARED_WITHIN == 2.0
    just_inside = [{**DECLARED[0], "near_mm": [6 + 1.7, 5, 2]},
                   {**DECLARED[1], "near_mm": [2, 1, 2]}]
    assert MO.declared_for(stubs, just_inside)[0] is not None
    way_off = [{**DECLARED[0], "near_mm": [6 + 5.0, 5, 2]},
               {**DECLARED[1], "near_mm": [2, 1, 2]}]
    assert MO.declared_for(stubs, way_off)[0] is None


#   -- and the driver passes them ---------------------------------------------------------------

def test_the_driver_reads_the_declaration_and_hands_it_over():
    """THE GAP THAT COST THE RUN: port_rows has taken these names since it was written, and the
    one caller never passed them."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as D

    src = inspect.getsource(D.drive)
    assert "declared_for" in src
    assert "port_declaration" in src
    assert "named=_named" in src, "and actually hands the result to port_rows"
