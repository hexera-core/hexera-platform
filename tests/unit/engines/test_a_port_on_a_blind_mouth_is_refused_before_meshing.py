"""A port declared on a mouth that opens into another part, refused before the mesher starts
(cfmesh_conjugate: mouths.shell_stubs, mouths.sealed_declared, driver).

THE RUN THIS IS WRITTEN AGAINST. ColdPlateAssembly_CLEAN.step has three mouths. The two 8 mm ones
open to the outside; the 11.8 mm one in the lid opens into 0.500 mm of flat metal under a detached
tube, so no coolant ever reaches it. The surface extraction MEASURES that and writes it down: in
`fluid_volume_answer.json` the 11.8 mm port carries `"clear_ahead": false` and both 8 mm ports carry
`true`, and the same record marks that rim `"sealed": true`.

Two separate jobs then paid for the flag nothing read:

  job 6ddb58aa  the engine INVENTED coolant_in on that mouth, because no declaration existed.
                topoSet put 0 of the coolant's 2,640 wall_outer faces in its box, createPatch
                logged "Removed zero-sized patch coolant_in type patch at position 2", and the
                mesh was refused for "the inlet was to be coolant_in, and the fluid region has no
                patch of that name". 283,410,955 cells, 11,217.3 s of mesher time.
  job e8256476  the USER declared that same mouth as outlet_2 at intake, under a
                patches_contract.txt headed "USER-CONFIRMED AT INTAKE - ENFORCED BY THE PIPELINE".
                Identical topoSet box coordinates, identical 0 faces, the same createPatch removal,
                and the mesh was refused for "the outlet was to be outlet_2, and the fluid region
                has no patch of that name". 14,910.8 s, on a second 64-core machine.

Commit 504fcc8f closed the first case: an absent declaration now means "name none". This closes the
second. The flag belongs in the ROLE decision and NOT in whether the patch is cut - cutting it is
free (job 6ddb58aa's own manifest has `empty_patches` empty, because createPatch deletes zero-sized
patches) and a face count of zero on that mouth is the only evidence left that the throat is closed.
[[project-corintis-port-roles]] [[project-coldplate-severed-circuit]]
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import mouths as MO

#: The three mouths as `fluid_volume_answer.json` records them for this part, in the order the
#: extraction measured them. Only the first has nothing clear ahead of it.
PORTS = (
    {"centroid_m": (0.065923414495, 0.033446178113, 0.07540532726), "normal": (0.0, 1.0, 0.0),
     "diameter_m": 0.0118, "area_m2": 1.093588e-04, "passage": 0, "clear_ahead": False},
    {"centroid_m": (0.099881949608, 0.054681178113, 0.043305688999), "normal": (1.0, 0.0, 0.0),
     "diameter_m": 0.008, "area_m2": 5.0265e-05, "passage": 0, "clear_ahead": True},
    {"centroid_m": (0.089339605589, 0.054681178113, 0.077388253039), "normal": (1.0, 0.0, 0.0),
     "diameter_m": 0.008, "area_m2": 5.0265e-05, "passage": 1, "clear_ahead": True},
)

#: Job e8256476's own port_declaration.json, which named the blind mouth an outlet.
DECLARED_B = (
    {"name": "outlet_2", "type": "outlet", "near_mm": [65.923414495, 33.446178113, 75.40532726]},
    {"name": "inlet", "type": "inlet", "near_mm": [99.881949608, 54.681178113, 43.305688999]},
    {"name": "outlet_1", "type": "outlet", "near_mm": [89.339605589, 54.681178113, 77.388253039]},
)
#: The same declaration with the two roles put on the two mouths that DO open to the outside.
DECLARED_OK = (
    {"name": "inlet", "type": "inlet", "near_mm": [99.881949608, 54.681178113, 43.305688999]},
    {"name": "outlet_1", "type": "outlet", "near_mm": [89.339605589, 54.681178113, 77.388253039]},
)


# # # the flag reaches the shell path # # #

def test_the_shell_path_carries_what_the_surface_measured_about_each_mouth():
    """`stubs_for`, the BOX path, has refused a mouth with nothing clear ahead of it since it was
    written. The SHELL path - which every real customer part takes - read the flag at neither end,
    so a measurement the extraction had already made reached nothing that could act on it."""
    stubs = MO.shell_stubs(list(PORTS))
    assert [s.reaches_air for s in stubs] == [False, True, True]


def test_a_mouth_nobody_measured_is_neither_promoted_nor_demoted():
    """True when the key is absent, so an older record or another extractor cannot start refusing
    parts that were fine. The flag has to be explicitly False to mean anything."""
    bare = [{k: v for k, v in p.items() if k != "clear_ahead"} for p in PORTS]
    assert all(s.reaches_air for s in MO.shell_stubs(bare))


def test_the_blind_mouth_is_still_cut():
    """NOT SKIPPED, which is what the box path does for its own good reason: there a stub would be
    a passage bored through metal that is not in the CAD, and here there is nothing to bore. Three
    grounds for cutting it anyway. Cutting is free: job 6ddb58aa cut it, topoSet selected 0 of
    wall_outer's 2,640 faces, createPatch removed the zero-sized patch and `empty_patches` came out
    empty, so no gate can fire on it. A face COUNT of zero is the measurement that says the throat
    is closed, and an absent patch cannot be told from a choice not to look. And the sibling job
    DECLARES a port on this mouth, so dropping it from the list would make `declared_for` fail to
    match and throw away all three of that customer's names."""
    assert len(MO.shell_stubs(list(PORTS))) == 3
    rows, _said = MO.port_rows(MO.shell_stubs(list(PORTS)), named=None, roles=False)
    assert [r["name"] for r in rows] == ["mouth_1", "mouth_2", "mouth_3"]
    blind = next(r for r in rows if r["diameter_m"] == pytest.approx(0.0118))
    assert blind["name"] == "mouth_1"


# # # the refusal # # #

def test_a_declared_outlet_on_the_blind_mouth_is_refused_with_its_own_numbers():
    stubs = MO.shell_stubs(list(PORTS))
    named, why = MO.declared_for(stubs, list(DECLARED_B))
    assert named is not None, why
    said = MO.sealed_declared(stubs, named)
    assert said, "job e8256476's declaration has to be refused before the mesher starts"
    # The mouth, the role and the name the customer used, so they can act on it without reading
    # a log. 11.800 mm at (65.9, 33.4, 75.4) mm.
    assert "outlet_2" in said
    assert "outlet" in said
    assert "11.800 mm" in said
    assert "65.9" in said and "33.4" in said and "75.4" in said
    # ...and what it means, in the words the surface record justifies and no others.
    assert "opens into another part" in said
    assert "zero faces" in said


def test_a_declaration_that_keeps_off_the_blind_mouth_is_not_refused():
    """The part is still meshable - job 6ddb58aa's own customer asked for exactly this and said so
    in writing. What is refused is a port on a mouth no coolant reaches, not the part."""
    stubs = MO.shell_stubs(list(PORTS))
    named, why = MO.declared_for(stubs, list(DECLARED_OK))
    assert named is not None, why
    assert MO.sealed_declared(stubs, named) == ""


def test_nothing_declared_is_nothing_refused():
    """With no declaration no end is named at all (504fcc8f), so there is no claim to refuse. The
    mouths are delivered as unassigned openings and the blind one's zero face count is the
    evidence."""
    stubs = MO.shell_stubs(list(PORTS))
    assert MO.sealed_declared(stubs, None) == ""
    assert MO.sealed_declared(stubs, []) == ""


def test_a_wall_on_the_blind_mouth_is_not_a_port_and_is_not_refused():
    """`declared_for` gives every mouth the declaration did not claim the row ("mouth_N", "wall").
    A wall is not a flow boundary, so a wall on a blind mouth is exactly right and must not refuse
    the run - which is what job 6ddb58aa's own plan did with its third mouth."""
    stubs = MO.shell_stubs(list(PORTS))
    assert MO.sealed_declared(stubs, [("mouth_1", "wall"), ("inlet", "inlet"),
                                      ("outlet_1", "outlet")]) == ""


def test_both_ends_on_blind_mouths_are_both_named():
    """A part whose inlet AND outlet were declared on sealed mouths gets one sentence naming both,
    not the first one only: a customer told to move one port and then told to move another has
    been through two runs to learn one thing."""
    ports = [dict(p, clear_ahead=False) for p in PORTS]
    stubs = MO.shell_stubs(ports)
    said = MO.sealed_declared(stubs, [("outlet_2", "outlet"), ("inlet", "inlet"),
                                      ("outlet_1", "outlet")])
    assert "outlet_2" in said and "inlet" in said and "outlet_1" in said
    assert said.count("mm mouth at") == 3


def test_a_mismatched_declaration_reaches_this_check_as_nothing_to_judge():
    """`declared_for` returns None when a declared port cannot be bound to a mouth by position,
    and a None is not a claim about any mouth. Passing it through must not raise."""
    stubs = MO.shell_stubs(list(PORTS))
    named, why = MO.declared_for(stubs, [
        {"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0]}])
    assert named is None and why
    assert MO.sealed_declared(stubs, named) == ""


def test_an_empty_mouth_list_is_not_a_refusal():
    assert MO.sealed_declared([], [("inlet", "inlet")]) == ""
    assert MO.sealed_declared([], None) == ""


# # # the driver acts on it before the mesher starts # # #

def test_the_driver_refuses_on_geometry_rather_than_on_price():
    """The refusal rides the path `ports_on_one_passage` already uses: `_say_why_before_meshing`
    with priced_out=False and geometry=True. Nothing about a budget, a resolution or a machine is
    true of this, and the four size refusals' sentences would all be false here."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    src = inspect.getsource(DR)
    assert "MO.sealed_declared(" in src, "the driver has to ask the question"
    # The REFUSAL block, not the first mention of the name: the marker's own docstring names it
    # too, and slicing from there measured a distance rather than the thing.
    block = src[src.index("if sealed_said:"):][:900]
    assert "_say_why_before_meshing" in block
    assert "priced_out=False" in block, "nothing about a budget is true of this"
    assert "geometry=True" in block
    assert "SEALED_PORT" in block, "and it carries its own marker, not the severed one"
    assert "SEVERED_COOLANT" not in block


def test_the_check_runs_before_the_mesher_and_not_after_it():
    """A refusal after the chain is the defect, not the fix: both runs it is written against were
    refused at the END, having already spent 11,217.3 s and 14,910.8 s of a 64-core machine."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    src = inspect.getsource(DR)
    # The first thing done FOR the mesher is staging the surface it fills.
    staging = src.index("(ws / PL.BLOCK_STL).write_bytes")
    assert src.index("MO.sealed_declared(") < staging
    assert src.index("if sealed_said:") < staging
    # ...and it is asked where the names were just bound to the mouths, which is inside the port
    # block and therefore before the connectivity check that already refuses before meshing.
    assert src.index("MO.sealed_declared(") < src.index("ports_on_one_passage(")
