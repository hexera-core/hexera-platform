"""A port about to be cut on a mouth the surface reading closed off, refused before the mesher
starts (cfmesh_conjugate: mouths.shell_stubs, mouths.sealed_rows, driver).

THE RUNS THIS IS WRITTEN AGAINST. ColdPlateAssembly_CLEAN.step has three mouths. The two 8 mm ones
open to the outside; the 11.8 mm one in the lid opens into 0.500 mm of flat metal under a detached
tube, so no coolant ever reaches it. The surface extraction measures that twice and writes both
down: the 11.8 mm port carries `"clear_ahead": false` where both 8 mm ports carry `true`, and
`open_rims` holds a rim of 63 edges, 35.941 mm long, centred 0.4998 mm along that mouth's own
normal and marked `"sealed": true`.

Two jobs paid for the flag nothing read, both at the END of a mesh:

  job 6ddb58aa  no declaration existed, so the engine INVENTED coolant_in on it. topoSet put 0 of
                the coolant's 2,640 wall_outer faces in its box, createPatch logged "Removed
                zero-sized patch coolant_in type patch at position 2", and the mesh was refused
                for "the inlet was to be coolant_in, and the fluid region has no patch of that
                name". 283,410,955 cells, 11,217.3 s of mesher time.
  job e8256476  the USER declared it outlet_2 under a patches_contract.txt headed
                "USER-CONFIRMED AT INTAKE - ENFORCED BY THE PIPELINE". Identical box, identical 0
                faces, same removal, refused for "the outlet was to be outlet_2". 14,910.8 s.

TWO SIGNALS AND NOT ONE. `clear_ahead` on its own is a single ray down the mouth's normal bounded
at the assembly's bounding box, so a mouth opening to real air with a bracket or a cross-over tube
beyond it reads False - and that mouth meshes, carries faces and delivers. The repo says so in
three places: `fluid_volume_check` prints it as a question for the picture and scores it 0.5, and
`cad/scout._name_openings` was deliberately moved OFF it for role decisions. So the refusal needs
the sealed rim beside it, which is what the part this is written against actually has.

ASKED OF THE ROWS ABOUT TO BE CUT. The first version of this asked `declared_for`'s output instead,
which left the same 283M-cell loss reachable: a declaration with roles but no bindable position
returns None with an empty reason, and `port_rows` then falls through to its own defaults and names
mouth 0 the inlet.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import mouths as MO

#: The three mouths as `fluid_volume_answer.json` records them for this part, in the order the
#: extraction measured them. Only the first has anything ahead of it.
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

#: Job e8256476's own port_declaration.json, which named the blind mouth an outlet.
DECLARED_B = (
    {"name": "outlet_2", "type": "outlet", "near_mm": [65.923414495, 33.446178113, 75.40532726]},
    {"name": "inlet", "type": "inlet", "near_mm": [99.881949608, 54.681178113, 43.305688999]},
    {"name": "outlet_1", "type": "outlet", "near_mm": [89.339605589, 54.681178113, 77.388253039]},
)
#: The same declaration with the roles on the two mouths that do open outward.
DECLARED_OK = (
    {"name": "inlet", "type": "inlet", "near_mm": [99.881949608, 54.681178113, 43.305688999]},
    {"name": "outlet_1", "type": "outlet", "near_mm": [89.339605589, 54.681178113, 77.388253039]},
)


def _stubs(ports=PORTS, rims=RIMS):
    return MO.shell_stubs(list(ports), list(rims) if rims is not None else None)


def _rows(stubs, declared=None, roles=True):
    named, _why = MO.declared_for(stubs, list(declared)) if declared else (None, "")
    rows, _said = MO.port_rows(stubs, named=named, roles=roles)
    return rows


# # # the two signals reach the shell path # # #

def test_the_shell_path_carries_both_of_what_the_surface_measured():
    """`stubs_for`, the BOX path, has read `clear_ahead` since it was written. The SHELL path -
    which every real customer part takes - read neither signal at either end."""
    stubs = _stubs()
    assert [s.ahead_is_clear for s in stubs] == [False, True, True]
    assert [s.sealed_rim for s in stubs] == [True, False, False]
    assert [s.is_sealed for s in stubs] == [True, False, False]


def test_one_signal_is_not_enough_to_kill_a_run():
    """A mouth opening to real air with a bracket beyond it reads clear_ahead False and has no
    sealed rim of its own. Refusing it would be worse than the defect this closes, because that
    mouth meshes and carries faces."""
    stubs = _stubs(rims=())
    assert [s.ahead_is_clear for s in stubs] == [False, True, True]
    assert not any(s.is_sealed for s in stubs)
    assert MO.sealed_rows(stubs, _rows(stubs, DECLARED_B)) == ""


def test_a_rim_belonging_to_another_mouth_does_not_seal_this_one():
    """The match is by position within one diameter. This part's other mouths are 23 mm and 34 mm
    away from the blind one, so a sealed rim on one cannot be read onto another."""
    far = ({"edges": 9, "length": 0.01, "centroid": [0.0999, 0.0547, 0.0433], "sealed": True},)
    stubs = _stubs(rims=far)
    assert [s.sealed_rim for s in stubs] == [False, True, False]


@pytest.mark.parametrize("value", [None, True])
def test_a_mouth_nobody_measured_is_neither_promoted_nor_demoted(value):
    """True when the key is absent AND when it is an explicit null, which `.get(key, True)` alone
    would not give: `{"clear_ahead": None}` would otherwise read as a mouth with metal in front."""
    ports = [dict(p, clear_ahead=value) for p in PORTS]
    if value is None:
        ports[0] = {k: v for k, v in ports[0].items() if k != "clear_ahead"}
        ports[1]["clear_ahead"] = None
    assert all(s.ahead_is_clear for s in MO.shell_stubs(ports, list(RIMS)))


def test_the_blind_mouth_is_still_cut():
    """NOT SKIPPED, which is what the box path does for its own good reason: there a stub would be
    a passage bored through metal that is not in the CAD, and here there is nothing to bore.
    Cutting is free - job 6ddb58aa cut it and `empty_patches` came out empty, because createPatch
    deletes a zero-sized patch - and the sibling job DECLARES a port on this mouth, so dropping it
    would make `declared_for` fail to match and throw away all three of that customer's names."""
    stubs = _stubs()
    assert len(stubs) == 3
    rows = _rows(stubs, roles=False)
    assert [r["name"] for r in rows] == ["mouth_1", "mouth_2", "mouth_3"]
    assert next(r for r in rows if r["diameter_m"] == pytest.approx(0.0118))["name"] == "mouth_1"


# # # the refusal # # #

def test_a_declared_outlet_on_the_blind_mouth_is_refused_with_its_own_numbers():
    stubs = _stubs()
    said = MO.sealed_rows(stubs, _rows(stubs, DECLARED_B))
    assert said, "job e8256476's declaration has to be refused before the mesher starts"
    assert "outlet_2" in said and "outlet" in said
    assert "11.800 mm" in said
    assert "65.9" in said and "33.4" in said and "75.4" in said


def test_the_engines_own_default_is_refused_too_and_that_is_the_hole_that_cost_a_run():
    """THE CASE THE FIRST VERSION MISSED. A declaration that carries roles but no bindable
    position makes `declared_for` return None with an empty reason, `port_rows(roles=True)` falls
    through to its own defaults, and mouth 0 - the blind one - is named coolant_in. That rebuilds
    job 6ddb58aa's recorded plan field for field. Asking the question of the ROWS catches it."""
    stubs = _stubs()
    named, why = MO.declared_for(stubs, [{"name": "inlet", "type": "inlet"}])
    assert named is None and why == "", "the silent path is the one being covered"
    rows, _ = MO.port_rows(stubs, named=None, roles=True)
    blind = rows[0]
    assert blind["name"] == "coolant_in" and blind["type"] == "inlet"
    assert blind["diameter_m"] == pytest.approx(0.0118)
    assert MO.sealed_rows(stubs, rows), "the invented inlet must be refused, not meshed"


def test_a_declaration_that_keeps_off_the_blind_mouth_is_not_refused():
    stubs = _stubs()
    assert MO.sealed_rows(stubs, _rows(stubs, DECLARED_OK)) == ""


def test_nothing_named_is_nothing_refused():
    """With no declaration no end is named at all, so there is no claim to refuse: every mouth is
    delivered as an unassigned opening and the blind one's face count is the evidence."""
    stubs = _stubs()
    assert MO.sealed_rows(stubs, _rows(stubs, roles=False)) == ""
    assert MO.sealed_rows(stubs, None) == ""
    assert MO.sealed_rows(stubs, []) == ""
    assert MO.sealed_rows([], [{"name": "x", "type": "inlet"}]) == ""


def test_a_wall_on_the_blind_mouth_is_not_a_port_and_is_not_refused():
    """A wall is not a flow boundary, and job 6ddb58aa's own plan put one on its third mouth."""
    stubs = _stubs()
    rows = [{"name": "mouth_1", "type": "wall"}, {"name": "inlet", "type": "inlet"},
            {"name": "outlet_1", "type": "outlet"}]
    assert MO.sealed_rows(stubs, rows) == ""


def test_the_remedy_is_the_one_this_part_can_actually_take():
    """"Declare it on a mouth that opens outward" is impossible here and the data says so: the
    stubs carry passages [0, 0, 1], and the only other mouth on passage 0 is mouth 2, which is
    already the other end. Offering it blind sent the customer into `ports_on_one_passage`'s
    refusal instead - the check that knows the answer and that never runs, because this one
    refuses first."""
    stubs = _stubs()
    said = MO.sealed_rows(stubs, _rows(stubs, DECLARED_B))
    # mouth 2 (8 mm, passage 0) does open outward, so it is named as the alternative.
    assert "On the same passage" in said
    assert "8.000 mm" in said
    # ...and when nothing on the passage opens outward, the CAD is the only way out.
    only = ({**PORTS[0]}, {**PORTS[1], "clear_ahead": False})
    rims = (*RIMS, {"edges": 4, "length": 0.02,
                    "centroid": [0.099881, 0.054681, 0.043305], "sealed": True})
    alone = MO.shell_stubs(list(only), list(rims))
    rows = [{"name": "coolant_in", "type": "inlet"}, {"name": "coolant_out", "type": "outlet"}]
    text = MO.sealed_rows(alone, rows)
    assert "nowhere on this part to move the role to" in text
    assert "opened in the CAD" in text


def test_the_sentence_claims_only_what_was_measured():
    """A wrong claim in a refusal is what this whole line of work is correcting. Two in particular
    must not come back: a face count on a mesh that was never built, and a hardcoded count of how
    many times this shape has failed."""
    stubs = _stubs()
    said = MO.sealed_rows(stubs, _rows(stubs, DECLARED_B))
    assert "no faces at all" in said, "it says what a cut there would produce"
    assert "stays in the delivered boundary" not in said, "there is no delivered boundary here"
    assert "sealed" in said and "bounding box" in said, "both signals are named"
    assert "which has happened twice on this shape" not in said


# # # the driver acts on it before the mesher starts # # #

def test_the_driver_asks_the_rows_and_not_the_declaration():
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    src = inspect.getsource(DR)
    assert "MO.sealed_rows(stubs, port_patches)" in src, "asked of the rows about to be cut"
    assert "sealed_declared" not in src, "the declaration-only form is gone"
    assert src.index("MO.port_rows(") < src.index("MO.sealed_rows("), "after the roles are decided"


def test_the_driver_refuses_on_geometry_rather_than_on_price():
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    src = inspect.getsource(DR)
    block = src[src.index("if sealed_said:"):][:900]
    assert "_say_why_before_meshing" in block
    assert "priced_out=False" in block, "nothing about a budget is true of this"
    assert "geometry=True" in block
    assert "SEALED_PORT" in block and "SEVERED_COOLANT" not in block


def test_the_check_runs_before_the_mesher_and_not_after_it():
    """Both runs it is written against were refused at the END, having already spent 11,217.3 s
    and 14,910.8 s of a 64-core machine."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    src = inspect.getsource(DR)
    staging = src.index("(ws / PL.BLOCK_STL).write_bytes")
    assert src.index("MO.sealed_rows(") < staging
    assert src.index("if sealed_said:") < staging


def test_the_driver_hands_the_rims_through():
    """Without `open_rims` every mouth reads unsealed and nothing can ever be refused - the safe
    direction, and the useless one."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    assert 'MO.shell_stubs(rec["ports"], rec.get("open_rims"))' in inspect.getsource(DR)


def test_the_what_next_follows_the_refusal_and_not_the_other_one():
    """ONE SENTENCE CANNOT SERVE BOTH SHAPES. "Change the file so it is one connected passage" is
    right for a SEVERED coolant, whose two ends are real and do not meet. It is the wrong lever for
    a SEALED port, where one end is not an end and the role can be moved at intake for nothing.
    The two were split into their own markers for exactly that reason and the advice was still
    shared, so half of these customers were being sent after the wrong change."""
    from meshpipeline.contracts.failure_cause import FailureCause, describe

    base = {"reason": "the outlet outlet_2 is to be cut on the 11.800 mm mouth",
            "priced_out": False, "geometry_refusal": True, "meshers_started": 0}
    _what, sealed = describe(FailureCause.NOT_BUILT, {**base, "geometry_fix": "roles"})
    _what2, severed = describe(FailureCause.NOT_BUILT, base)
    assert "which openings should carry the inlet and the outlet" in sealed
    assert "one connected passage" not in sealed, "that is the severed lever"
    # ...and an absent key keeps the wording every other shape refusal already had.
    assert "one connected passage" in severed
    assert "which openings should carry" not in severed


def test_the_driver_tells_the_record_which_lever_it_is():
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as DR

    src = inspect.getsource(DR)
    block = src[src.index("if sealed_said:"):][:900]
    assert 'fix="roles"' in block
    assert '"geometry_fix"' in src, "and the fact is written into the refusal record"
