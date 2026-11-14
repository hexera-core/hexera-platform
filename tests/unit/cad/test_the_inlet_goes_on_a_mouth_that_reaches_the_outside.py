"""The first guess at which opening is the inlet, and which facts it is allowed to ignore.

`cad/scout._name_openings` makes sure every pin on the picture has a name before the vision step
and the user get to overrule it. With more than two openings the rule was "the widest is the feed",
and on the first real customer assembly the widest was a mouth the fluid cannot be fed through.

MEASURED on ColdPlateAssembly_CLEAN.step, sessions eb22296d and aad03e39, 2026-11-13 and -14.
Three mouths: an 11.8 mm junction sealed inside the assembly and two real 8 mm hose mouths. The
junction was proposed as the inlet, and `clear_ahead` had been false on it the whole time - the
scout measures it, scores that opening 0.5 for it, and `cad/fluid_volume_check` prints a sentence
about it on the same card.
"""
from __future__ import annotations

from meshpipeline.cad.scout import Opening, _name_openings


def _opening(area_mm2: float, centroid, *, clear_ahead: bool, measured: bool = True) -> Opening:
    """`measured` false is the triangle-mesh path, where nobody measured whether the mouth reaches
    the outside and `clear_ahead` is only an alias for on_extremity."""
    return Opening(face_index=-1, kind="ring", centroid=tuple(v * 1e-3 for v in centroid),
                   normal=(1.0, 0.0, 0.0), area=area_mm2 * 1e-6, wh=(0.0, 0.0),
                   clear_ahead=clear_ahead, on_extremity=True,
                   reaches_outside=clear_ahead if measured else None)

def test_the_inlet_is_never_proposed_on_a_mouth_sealed_inside_the_assembly():
    """MEASURED on this plate: three mouths, and the widest of them is an 11.8 mm junction sealed
    inside the assembly. "The widest is the feed" made it the inlet and called both real 8 mm hose
    mouths outlets - with the scout's own note printed above it saying opening 1 "opens into
    another part whose inside reaches the outside some other way"."""
    junction = _opening(109.36, [65.92, 33.45, 75.41], clear_ahead=False)
    mouth_a = _opening(50.27, [99.88, 54.68, 43.31], clear_ahead=True)
    mouth_b = _opening(50.27, [89.34, 54.68, 77.39], clear_ahead=True)
    named = _name_openings([junction, mouth_a, mouth_b])     # widest first, as the pool is sorted
    assert named[0] is not junction
    assert named[0].role == "inlet" and named[0].clear_ahead
    # AND SYMMETRIC: protecting only the feed left the junction as the OUTLET - an outlet in the
    # middle of the metal - and the two then sat in the SAME piece of coolant, so the
    # severed-circuit check that had correctly refused this part stopped firing. Measured on the
    # real plate through the whole product, 2026-11-14.
    assert named[-1] is junction, "a mouth that reaches nothing is ordered last, not named outlet"
    assert [o.role for o in named] == ["inlet", "outlet", "outlet"]
    assert named[1] is not junction and named[1].clear_ahead


def test_the_widest_is_still_the_feed_when_every_mouth_reaches_the_outside():
    wide = _opening(109.36, [0.0, 0.0, 0.0], clear_ahead=True)
    narrow_a = _opening(50.27, [10.0, 0.0, 0.0], clear_ahead=True)
    narrow_b = _opening(50.27, [20.0, 0.0, 0.0], clear_ahead=True)
    named = _name_openings([wide, narrow_a, narrow_b])
    assert named[0] is wide and wide.role == "inlet"
    assert [o.name for o in named] == ["inlet", "outlet_1", "outlet_2"]


def test_every_mouth_blind_leaves_the_order_exactly_as_it_was():
    """Nothing to prefer, so nothing is preferred: the widest keeps the guess it always had."""
    pool = [_opening(109.36, [0.0, 0.0, 0.0], clear_ahead=False),
            _opening(50.27, [10.0, 0.0, 0.0], clear_ahead=False)]
    assert [o.role for o in _name_openings(list(pool))] == ["inlet", "outlet"]
    three = pool + [_opening(10.0, [20.0, 0.0, 0.0], clear_ahead=False)]
    named = _name_openings(list(three))
    assert [o is q for o, q in zip(named, three)] == [True, True, True]
    assert [o.role for o in named] == ["inlet", "outlet", "outlet"]


def test_two_mouths_put_the_inlet_on_the_one_that_reaches_the_outside():
    """With two, the lower one along the axis that separates them is the inlet - unless it is the
    blind one."""
    blind_low = _opening(50.27, [0.0, 0.0, 0.0], clear_ahead=False)
    open_high = _opening(50.27, [100.0, 0.0, 0.0], clear_ahead=True)
    named = _name_openings([blind_low, open_high])
    assert named[0] is open_high and open_high.role == "inlet"
    assert blind_low.role == "outlet"
    # ...and with both reaching, the lower one is the inlet exactly as before
    low = _opening(50.27, [0.0, 0.0, 0.0], clear_ahead=True)
    high = _opening(50.27, [100.0, 0.0, 0.0], clear_ahead=True)
    assert _name_openings([high, low])[0] is low


# WHAT THE FIRST VERSION OF THE RULE GOT WRONG. It read `clear_ahead`, which does not mean the same
# thing at the three producers that set it, so it reordered the inlet on parts where nobody had
# measured whether a mouth reaches the outside.

def test_an_unmeasured_mouth_neither_sinks_nor_rises():
    """cad/scout_mesh._opening sets `clear_ahead=on_extremity` - literally, in the constructor -
    and on_extremity is "this mouth's own normal reaches the part's BOX within 3% of the
    diagonal". A header whose feed stub ends short of the box in its own axis, because a smaller
    branch reaches further, is false by that reading and is wide open in fact. The rule read it as
    sealed and moved the inlet off the feed onto a 10 mm branch."""
    feed = _opening(78.54, [90.0, 0.0, 0.0], clear_ahead=False, measured=False)
    branch_a = _opening(78.54, [0.0, 0.0, 60.0], clear_ahead=True, measured=False)
    branch_b = _opening(78.54, [0.0, 0.0, 59.0], clear_ahead=True, measured=False)
    named = _name_openings([feed, branch_a, branch_b])
    assert named[0] is feed, (
        "an unmeasured mouth was demoted, so the inlet moved off the feed and onto a branch")
    assert [o.name for o in named] == ["inlet", "outlet_1", "outlet_2"]


def test_two_unmeasured_mouths_are_not_swapped():
    """The two-mouth branch swaps inlet and outlet outright, and it swapped on the same unmeasured
    value. Which way that lands on a given part is luck, not measurement - and on a two-mouth cold
    plate the two directions are the whole answer, so the swap may only fire on a mouth somebody
    really did ray."""
    low = _opening(50.27, [0.0, 0.0, 0.0], clear_ahead=False, measured=False)
    high = _opening(50.27, [100.0, 0.0, 0.0], clear_ahead=True, measured=False)
    named = _name_openings([low, high])
    assert named[0] is low and low.role == "inlet", (
        "with nothing measured, the lower mouth stays the inlet - the rule may not swap on a "
        "value nobody took")


def test_a_measured_mouth_still_decides_it():
    """The fix must not cost the behaviour the plate was measured on: where the scout really did
    ray the mouth against the part, the sealed one still sinks."""
    sealed = _opening(109.36, [65.92, 33.45, 75.41], clear_ahead=False, measured=True)
    real_a = _opening(50.27, [99.88, 54.68, 43.31], clear_ahead=True, measured=True)
    real_b = _opening(50.27, [89.34, 54.68, 77.39], clear_ahead=True, measured=True)
    named = _name_openings([sealed, real_a, real_b])
    assert named[0] is not sealed and named[-1] is sealed
    assert [o.role for o in named] == ["inlet", "outlet", "outlet"]


def test_a_mixed_pool_sinks_only_the_one_that_was_measured_sealed():
    """Unknown must not be treated as sealed, and must not be treated as open either."""
    unknown = _opening(109.36, [0.0, 0.0, 0.0], clear_ahead=False, measured=False)
    sealed = _opening(80.0, [10.0, 0.0, 0.0], clear_ahead=False, measured=True)
    reaching = _opening(50.27, [20.0, 0.0, 0.0], clear_ahead=True, measured=True)
    named = _name_openings([unknown, sealed, reaching])
    assert named[-1] is sealed, "only the MEASURED sealed mouth sinks"
    assert named[0] is unknown, "and the unmeasured one keeps the order it came in with"


def test_the_bounded_ray_stops_at_the_assembly_box():
    """cad/fluid_volume._port rayed the mouth against a compound of EVERY solid and loose face in
    the file, unbounded, so a mouth open to atmosphere that points across the file at the bracket
    it bolts to read as sealed. Beyond the box the ray has left the assembly and what it meets
    cannot seal the mouth."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.fluid_volume import _meets

    # the far part: a 10 mm cube whose near face is 200 mm along +x from the mouth
    far = BRepPrimAPI_MakeBox(gp_Pnt(0.200, -0.005, -0.005), 0.010, 0.010, 0.010).Shape()
    mouth, normal = (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)

    assert _meets(far, mouth, normal, 1e-6), (
        "unbounded, the ray finds the far part - this is what the old call asked")
    assert not _meets(far, mouth, normal, 1e-6, limit=0.005), (
        "bounded at a box that ends 5 mm ahead, the mouth is open to atmosphere: a part 200 mm "
        "away, outside this one's own box, cannot seal it")
    assert _meets(far, mouth, normal, 1e-6, limit=0.5), (
        "and a limit that really does reach the far part still finds it")
