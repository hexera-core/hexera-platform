# Responsibility: Verify the two things a real customer assembly broke - the cap probe builds its
# solid classifiers ONCE rather than per cap, and a split that runs out of time is told apart from a
# part with no coolant - and that the first changes no verdict.
# Boundaries: OpenCASCADE primitives built in the test (metres); no engine, no storage, no model.
#
# WHY THIS FILE EXISTS. A Corintis cold plate, six solids and 9226 faces, took 347.5 s to split
# against a 120 s budget, so production discarded it and the part read as having no coolant at all.
# 291.0 s of that was metal_on_both_sides rebuilding a BRepClass3d_SolidClassifier per candidate
# cap: 764 caps x 6 solids = 4584 constructions, where building all six once costs 0.21 s. Hoisted,
# the whole extraction is 141.5 s and every number it reports is the same.
import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopoDS import TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad import fluid_volume as fv
from meshpipeline.cad.fluid_volume_check import fold

R_OUT, R_BORE, L = 0.05, 0.04, 0.2          # m: the pipe, as test_fluid_volume draws it


def _cyl(x0, r, length, axis=(1, 0, 0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x0, 0.0, 0.0), gp_Dir(*axis)), r, length).Shape()


def _tube(x0, length):
    return BRepAlgoAPI_Cut(_cyl(x0, R_OUT, length), _cyl(x0 - 0.001, R_BORE, length + 0.002)).Shape()


def _comp(*shapes):
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _parts():
    shape = _comp(_tube(0.0, L / 2), _tube(L / 2, L / 2))
    lo, hi = fv._real_box(shape)
    diag = sum((hi[k] - lo[k]) ** 2 for k in range(3)) ** 0.5
    solids, loose = fv.parts_of(shape, diag)
    return solids, loose, diag


# the hoist

def test_sharing_the_classifiers_reaches_the_same_verdict_on_every_cap():
    """THE WHOLE POINT OF THE HOIST: it is 7x faster and it decides nothing differently.

    A classifier is only ever READ by metal_on_both_sides (Perform/State), so one set shared across
    every cap is the same question asked the same way. This asserts that rather than assuming it.
    """
    solids, loose, diag = _parts()
    caps = fv.candidate_caps(solids, loose, diag)
    assert caps, "the pipe's two ends give no caps to probe"
    shared = fv.solid_classifiers(solids)
    for cap in caps:
        own = fv.metal_on_both_sides(solids, cap)                # builds its own, the old way
        hoisted = fv.metal_on_both_sides(solids, cap, shared)    # the shared set
        assert own is hoisted, f"the shared classifiers changed the verdict on cap at {cap.centroid}"


def test_the_classifiers_are_built_once_for_the_whole_probe():
    """One construction per SOLID, not per cap. This is the 291 s.

    Counted by wrapping the constructor the module imports, which is how the cost was found: the
    call site at `kept = [...]` used to be inside the comprehension.
    """
    import meshpipeline.cad.fluid_volume as mod
    solids, loose, diag = _parts()
    built = []
    real = mod.solid_classifiers

    def counting(ss):
        built.append(len(ss))
        return real(ss)

    mod.solid_classifiers = counting
    try:
        caps = mod.candidate_caps(solids, loose, diag)
    finally:
        mod.solid_classifiers = real
    assert caps, "no caps were probed, so nothing was measured"
    assert len(built) == 1, (
        f"the classifiers were built {len(built)} times for one probe of {len(caps)} kept caps; "
        "they must be built once and shared")


def test_a_caller_that_passes_nothing_still_works():
    """The parameter is optional: the four positional call sites in the pin-top tests stay green."""
    solids, loose, diag = _parts()
    caps = fv.candidate_caps(solids, loose, diag)
    assert fv.metal_on_both_sides(solids, caps[0]) in (True, False)


# the budget, and what it is measured against

def test_the_check_budget_leaves_room_for_an_assembly_at_the_face_limit():
    """300 s, priced off the one bound the extraction actually has.

    `extract` refuses anything over MAX_FACES outright, so the worst case is a file at that limit.
    The customer assembly at 9226 faces measures 162.9 s of child wall; the budget has to clear
    that with room, and 120 s did not. This pins the relationship rather than the number, so
    raising MAX_FACES without looking at the budget fails here.
    """
    assert fv.MAX_FACES == 20000
    assert fv.CHECK_BUDGET_S >= 300.0, (
        "a six-solid customer assembly takes 162.9 s of child wall at 9226 faces, and the slowest "
        "part in the corpus takes 62.6 s: a budget under 300 s discards real parts")
    assert fv.STAGE_BUDGET_S >= fv.CHECK_BUDGET_S, (
        "the builder's own extraction writes a surface as well as splitting, so its budget cannot "
        "be the tighter of the two")


# a timeout is not an answer about the part

def test_a_split_that_ran_out_of_time_is_not_told_as_a_part_with_no_coolant():
    """THE TWO USED TO ARRIVE AS THE SAME THING, and that is how a slow function withheld an engine.

    run_bounded returns {"error"} on a timeout, `_summary` copies `found` only when the key is
    there, and both readers downstream take one boolean - so "we gave up" and "this part holds no
    fluid" were indistinguishable. `_kinds_on_offer` then withholds solid-assembly and the
    conjugate mesher is never offered, whatever the pictures say.
    """
    facts = {"flow": "external", "solids": 6, "openings": [{"id": 1}, {"id": 2}], "notes": []}
    fold(facts, {"error": "finding the fluid inside the parts took over 300 s"})

    assert facts["fluid_volume"].get("unfinished") is True, (
        "nothing downstream can tell a timeout from a measured absence")
    said = " ".join(facts["notes"]).lower()
    assert said, "the customer was told nothing at all"
    assert "limit of the check" in said, f"the note blames the part rather than us: {said}"


def test_a_part_that_really_holds_no_fluid_is_not_marked_unfinished():
    """The other half: a measured absence is the geometry's answer and must not read as our fault."""
    facts = {"flow": "external", "solids": 6, "openings": [{"id": 1}, {"id": 2}], "notes": []}
    fold(facts, {"found": False, "reason": "no space inside the parts", "parts": 6, "caps": 0})
    assert not facts["fluid_volume"].get("unfinished")
