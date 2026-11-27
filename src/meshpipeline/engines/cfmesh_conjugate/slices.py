# Responsibility: Name the cuts a cooled solid is looked at through, where they are, HOW WIDE
# A WINDOW each one has to be drawn in for the thing it was cut to show to survive to a pixel, and
# WHAT A DRAWN FRAME HAS TO BE MEASURED TO CONTAIN before it is evidence of anything.
# Boundaries: numbers only. It renders nothing and reads no file. The one thing it judges is a
# pixel array it is handed: whether the periodic structure a cut exists to show is in it.
# Collaborates with: cfmesh_conjugate/native.py (which renders these windows on the mesh VM, hands
# each drawn PNG back to `ink_structure`, and publishes the figure) and
# cfmesh_conjugate/finalize.py (which publishes them as inspection_regions).
from __future__ import annotations

# THE PICTURE DETECTOR IS NOT HERE ANY MORE. "ink_structure" and the bars it measures against
# moved to sandbox/picture_structure.py, verbatim, so the side that DRAWS a frame and the side
# that READS it run the same code over the same pixels. It used to run only on the mesh VM and
# travel home as a boolean in the manifest, and the home side let that boolean discharge a
# region - see that module for the frame of solid metal it discharged. These names are
# re-exported because this is where every caller on this side of the boundary imports them from,
# and "native._measure_frame" is one of them.
from meshpipeline.sandbox.picture_structure import (  # noqa: F401 - re-exported, see __all__
    ACF_MIN,
    INK_MAX,
    MIN_PERIOD_PX,
    MIN_REPEATS,
    PERIOD_MATCH,
    SCAN_AXES,
    ink_structure,
    period_matches,
    worst_contrast,
)

#: Off the exact middle by this fraction of the span. A block meshed with an even number of cells
#: has a grid plane down the middle, and a cut along a face of cells shows no cells at all.
_OFF_CENTRE = 1e-3

_AXIS_OF = {"x": 0, "y": 1, "z": 2}

#: The screenshot the reviewer's evidence has always been produced at. Imported from the home
#: side rather than restated, so the two sides of the VM boundary cannot drift: a window computed
#: against 1280x960 and drawn into some other frame is a window whose published um/px is a lie.
#: Falls back to the same pair only when the import is unavailable (an engine unit test with no
#: sandbox on the path), never to a different one.
try:                                      # pragma: no cover - exercised by the import itself
    from meshpipeline.sandbox.render_inputs import (
        DEFAULT_SCREENSHOT_H as FRAME_H,
    )
    from meshpipeline.sandbox.render_inputs import (
        DEFAULT_SCREENSHOT_W as FRAME_W,
    )
except Exception:                         # noqa: BLE001
    FRAME_W, FRAME_H = 1280, 960

#: HOW MANY PIXELS ONE CELL MUST BE, and this is the number the whole aid turns on.
#:
#: The cuts exist to answer two questions and `review_slices` names them below: how many cells are
#: ACROSS each channel, and how many are THROUGH its height. Both are counts of cells, so the
#: thing that has to survive to the picture is a CELL, not a channel. A channel drawn four pixels
#: wide with four cells across it gives each cell one pixel, and two neighbouring cells then share
#: a boundary that falls inside a single pixel: the count cannot be read off at any contrast.
#:
#: MEASURED, AND WHAT IT MEASURES IS NOT WHAT THE LAST VERSION OF THIS COMMENT CLAIMED. It said
#: the wide window "drew 109 dark pixels in 1,228,800 ... the passages are gone from the picture".
#: Run again through the shipped renderer, on job 0ff6f9e5's own fluid/solid interface, on the one
#: plane and window that hold this plate's channel array (y = 22.713 mm, centred 22.921, 41.899 mm):
#:
#:     per cell   window      um/px     ink of 1,228,800 px   repeats   period read   periodic at
#:      1 px      32.202 mm   25.1578      531,822  43.280%      319     402.525 um      0.969
#:      2 px      16.101 mm   12.5789      304,400  24.772%      159     100.631 um      0.994
#:      4 px       8.050 mm    6.2895      150,920  12.282%       79     100.631 um      0.987
#:
#: NONE OF THAT TABLE CAN BE RE-CHECKED ON THIS BOX AND IT IS LEFT AS IT WAS. A reading of
#: 149,760 ink pixels and 12.188% was offered against the 4-px row; it is neither confirmed nor
#: refuted here, because the artifact every row was measured on is gone. Job 0ff6f9e5's fluid
#: interior was 10,366,611,525 bytes and was pruned off the workspace before anyone read it, and
#: the preserved copy of that run (`_verify/`) holds its logs, plan, report and manifest and no
#: VTK tree at all - 44 KiB under VTK/. There is no .vtu and no fluid_to_solid .vtp of the part
#: left to cut, so the only honest statement is that the figures stand unverified. Re-taking them
#: needs the part re-meshed, or the archive of that run fetched back. The ONE figure here that
#: does not depend on the artifact is the identity below, which is arithmetic on the plan.
#:
#: 531,822 is not 109, so that figure does not reproduce and the passages are NOT gone from the
#: wide frame: they are there and they are WRONG. At one pixel per cell the period reads 402.525
#: um, four times the 100.631 um the same plane shows at two and four pixels per cell, because a
#: wall every four pixels aliases into a beat every sixteen. A frame that passes a content check
#: with a figure four times out is worse than one that fails, and nothing in the picture says
#: which it is.
#:
#: SO THE TWO TESTS ARE NOT INTERCHANGEABLE, which is the whole lesson. The scale is what makes a
#: cell countable and the ink is what says a passage is there; the wide frame above has the ink
#: and not the scale, a port bore has the scale and not the ink. Both have to hold.
#:
#: AND WHY IT IS NOT SIZED FROM THE PLAN'S FEATURE THICKNESS, which is what it looks like it
#: should be. On this part `fine_surfaces[0].thickness_m` is 0.0001006312 and `fine_cell_pinned_m`
#: is 2.51578e-05, and the first divided by the second is 4.0 EXACTLY, because the planner pins
#: the cell AT the thickness over its four-cell floor. So "a window where the thickness is four
#: pixels" is identically "a window where the cell is one pixel", on this part and on every part
#: the planner sizes the same way. Sizing from the thickness cannot ever produce a countable cell;
#: it only looks as though it can.
CELL_PIXELS = 4

#: ...AND NONE OF THAT IS EVIDENCE ABOUT THE PICTURE, which is what the rest of this file is for.
#:
#: Everything above is arithmetic on the plan, and it is a PURE SCALE IDENTITY: the window is set
#: to `plan_cell / CELL_PIXELS` here and the reader divides by `quality.near_wall_cell_m`, which
#: finalize.py derives as `background_cell_m / 2**levels`. On job 0ff6f9e5 that is
#: 0.0004025248 / 2**4 = 2.51578e-05, the same `fine_cell_pinned_m` this window was computed from,
#: exactly. So every zoom frame declares itself countable, always, whatever is actually drawn in
#: it, and nothing ever looks at the ink.
#:
#: MEASURED ON THE REAL PLATE, job 0ff6f9e5, through the shipped renderer, on a cut of the
#: fluid/solid interface. All three zoom frames were declared countable and introduced to the
#: reviewer with the words "cells are countable in this frame":
#:
#:     frame                     what is in it                   ink      repeats   period
#:     along_the_flow_zoom       the channel array                3.258%       79   16.0 px
#:     across_the_flow_zoom      three plenum rectangles          0.592%        3   none
#:     through_the_plate_zoom    one circular port bore           0.414%        0   none
#:
#: Two of those three are a picture with no passage in it, handed back as countable evidence. A
#: frame that discharges a region on the strength of a scale identity is worse than no frame: the
#: run stops saying "evidence missing" and starts returning a confident verdict from a port bore.
#:
#: So a frame is evidence only when ITS OWN PIXELS have been measured to hold the periodic
#: structure the cut exists to show. `ink_structure` is that measurement and the constants beside
#: it are the bar; both live in `sandbox/picture_structure` now, imported at the top of this file,
#: so the reading side takes the same measurement on its own copy of the PNG instead of reading
#: this side's verdict out of the manifest. The cost of getting this wrong is the whole aid: a
#: reviewer cannot tell a dense healthy grid of plenum cells from a dense healthy grid of channel
#: cells, and neither can a scale.


def flow_axis(inlet: str, outlet: str) -> int | None:
    """Which axis the coolant runs along, from the block faces it enters and leaves by.

    The block faces are named xmin/xmax/ymin/ymax/zmin/zmax, so the port names carry the axis.
    None when the part is sealed, which is a legal plan: the branching test part has no ports at
    all and is still worth meshing.
    """
    for name in (inlet or "", outlet or ""):
        axis = _AXIS_OF.get(name[:1])
        if axis is not None and name[1:] in ("min", "max"):
            return axis
    return None


def plane_axes(normal_axis: int) -> tuple[int, int]:
    """The two world axes that lie in a cut, as (horizontal, vertical) on the screen.

    THE ORDER IS THE RENDERER'S, NOT A PREFERENCE. The lower-numbered axis goes across the frame's
    width and the higher one up its height, which is what the review renderer already does with
    an up vector on the later axis. Reproduced here rather than guessed, because the published
    um/px is computed from this mapping: get it the wrong way round and every scale published
    beside a cut is wrong by the frame's aspect ratio, silently, in the direction that flatters.
    """
    return tuple(k for k in range(3) if k != normal_axis)  # type: ignore[return-value]


def fitted_metres_per_pixel(extent_h: float, extent_v: float, *, width_px: int = FRAME_W,
                            height_px: int = FRAME_H) -> float:
    """Metres per pixel when a cut of these in-plane extents is fitted whole into the frame.

    The binding direction is whichever runs out of pixels first, so it is the LARGER ratio. This
    reproduces the scales the reviewer has actually been getting on the Corintis plate, whose part
    is 177.69 x 41.83 x 101.21 mm: 105.42 um/px across the flow (101.21 mm against the 960 height)
    and 138.82 um/px on the other two (177.69 mm against the 1280 width).
    """
    return max(float(extent_h) / max(1, int(width_px)),
               float(extent_v) / max(1, int(height_px)))


def drawn_cell_m(cell_m: float | None, *, layer_thickness_m: float | None = None,
                 layers_in_mesh: float | None = None) -> float:
    """The cell the picture is ACTUALLY drawn at, which is not the cell the plan pinned.

    THIS IS THE NUMBER JOB b8f09ae9 FAILED ITS WHOLE REVIEW OVER. The zoom window was sized from
    the plan cell here and the reader divided by the plan cell on the far side of the container
    boundary, so the claim "a cell is four pixels across" was a pure scale identity that could not
    be wrong - and the picture drew something else. A layer pass puts its stack INSIDE the
    interface cell, so what is left of that cell at the wall, and what the picture therefore shows
    as its repeating pitch, is the cell MINUS the stack:

        job b8f09ae9: 2.51578e-05 - 1.21e-05 = 1.30578e-05 m

    MEASURED, not derived. At that job's 6.289450e-06 m/px the plan cell claims 4.0000 px and the
    integer-pixel scan of the real PNG read a repeat pitch of 2.0 px. 1.30578e-05 is 2.076 px,
    which IS the measurement. 4.00 was outside CELL_PITCH_TOLERANCE 1.5 - band [2.667, 6.0] - and
    2.076 is inside it. Nothing about the tolerance changes; the claim stops being a lie.

    AND IT IS A ONE-PIXEL KNIFE EDGE, which is why this is a named function and not an inline
    subtraction. Jobs 22ac7054 and cba7ac69 are the same mesh as each other (279,281,786 cells)
    and read a pitch of 3.0 px against the same 4.00 claim - inside the band - and PASSED. Their
    PNG is 459,927 B against b8f09ae9's 452,410 B, and m_per_px, cell_px, ink_fraction, period_px,
    repeats, strength, extent and both bare runs agree to six decimals. Two nearly identical
    meshes fell on opposite sides of an integer because the yardstick was wrong for all three.

    NO LAYERS, NO CHANGE. Without a measured layer pass there is no stack inside the cell and the
    plan cell is the right answer, so this returns it untouched - which leaves every other engine
    and every unlayered run of this one byte-identical.
    """
    cell = float(cell_m or 0.0)
    if not (cell > 0.0):
        return 0.0
    grown = float(layers_in_mesh or 0.0)
    stack = float(layer_thickness_m or 0.0)
    if not (grown > 0.0 and stack > 0.0):
        return cell
    # A STACK THAT ATE ITS WHOLE CELL IS NOT A MEASUREMENT OF ONE. MIN_THICKNESS_OF_STACK keeps a
    # real stack well under its cell (0.8 of it at most), so a non-positive remainder means the
    # two figures came from different runs - and a zero window renders as a divide by zero at the
    # camera. The plan cell stands, exactly as it does when no pass ran.
    return cell - stack if stack < cell else cell


def zoom_window(feature_m: float, *, width_px: int = FRAME_W, height_px: int = FRAME_H,
                pixels: int = CELL_PIXELS) -> tuple[float, float, float]:
    """The window a cut must be drawn in for `feature_m` to be `pixels` pixels across.

    Returns (width_m, height_m, metres_per_pixel). `feature_m` is the part's own smallest meshed
    cell, which is the thing a count of cells is a count of. See CELL_PIXELS for the measurement
    that fixes `pixels` at four and for why the plan's feature THICKNESS is the wrong input here.

    Returns (0, 0, 0) for a feature that is not a positive length, so a plan that carries no fine
    cell publishes no zoom rather than a window computed from nothing. A window nobody can check
    is not evidence, and a window of zero metres would render as a divide by zero at the camera.
    """
    f = float(feature_m or 0.0)
    if not (f > 0.0) or pixels <= 0:
        return 0.0, 0.0, 0.0
    mpp = f / float(pixels)
    return mpp * float(width_px), mpp * float(height_px), mpp


def _box(lo, hi) -> tuple[list[float], list[float], list[float]] | None:
    """(lo, hi, span) as floats, or None when it is not a box with volume in it."""
    try:
        a = [float(v) for v in lo]
        b = [float(v) for v in hi]
    except (TypeError, ValueError):
        return None
    if len(a) != 3 or len(b) != 3:
        return None
    span = [b[k] - a[k] for k in range(3)]
    return (a, b, span) if min(span) > 0.0 else None


def review_slices(lo, hi, *, inlet: str = "", outlet: str = "", cell_m: float | None = None,
                  fine_lo=None, fine_hi=None, repeat_axis: int | None = None,
                  width_px: int = FRAME_W, height_px: int = FRAME_H,
                  drawn_m: float | None = None) -> list[dict]:
    """The three cuts, as engines/manifest publishes an inspection region.

    WHAT EACH ONE IS FOR, because a slice nobody can say the purpose of is a slice a reviewer
    skips. A cold plate is a flat block with coolant running through it, and the three questions
    about such a mesh are: how many cells are across each channel (cut square across the flow),
    where the coolant reaches and where the metal is (cut through the plate at mid-thickness), and
    how many cells are through the channel's height with metal above and below it (cut along the
    flow). One cut answers each.

    EACH CUT CARRIES TWO FRAMES, not one, and that is the fix for the defect that made every one
    of them useless. A cut has only ever been drawn once, fitted to the whole part, and on the
    Corintis plate that is 105.42 to 138.82 um/px against a 98.2422 um passage: the channel was
    under one pixel on all three cuts, so the reviewer was being shown three pictures of nothing
    and asked how many cells were across a channel. The `whole` frame keeps the context, which is
    worth having and is all that ever existed; the `zoom` frame is the one the question can be
    answered from. Both are published with the window they were drawn in and the um/px that came
    out, so a claim made about a cut can be checked against the picture it was made from. A cut
    whose window is not stated is not evidence.

    `cell_m` is the part's own finest meshed cell (`conjugate_plan.fine_cell_pinned_m`). Without
    it only the whole-part frame is published: a zoom computed from a guessed feature would be a
    stated scale that is wrong, which is worse than one frame fewer.

    THE PLANES ARE PLACED FROM THE PART'S OWN FINE ZONE when one is given (`fine_lo`/`fine_hi`,
    from `conjugate_plan.stretch_zones[0]`), because the middle of the block is not where the
    channels are. MEASURED on job 0ff6f9e5: `through_the_plate` is normal to y and the block's
    middle puts it at y = 41.070 mm, while every microchannel on that part lies between
    y = 21.51 mm and y = 33.55 mm. That plane could not cross a passage at any window, and the
    zoom drawn on it came back as one circular port bore and was handed to the reviewer as
    countable evidence. A plane in the wrong place is not fixed by a better camera.

    `repeat_axis` is MEASURED, not guessed: the world axis the passages repeat along, read out of
    a frame that was already drawn (see native.`_render_cuts`). With it, the coolant runs along
    the other in-plane axis and `across_the_flow` is placed normal to THAT one, so it crosses
    every passage. Without it the long axis stands in, which on this part is the repeat axis
    itself - the guess that put `across_the_flow` ALONG the passages - so the unmeasured cut set
    is a starting point for the renderer to correct, never a claim.

    `lo`/`hi` are the block in metres. Returns [] for a degenerate block rather than publishing a
    promise that renders to nothing.
    """
    block = _box(lo, hi)
    if block is None:
        return []
    lo, hi, span = block
    frame = [lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]
    block_mid = [(lo[k] + hi[k]) / 2.0 for k in range(3)]

    fine = _box(fine_lo, fine_hi) if fine_lo is not None and fine_hi is not None else None
    if fine is None:
        place_lo, place_span = lo, span
        placed_from = "the middle of the plan's block (no fine zone was given)"
    else:
        place_lo, _, place_span = fine
        placed_from = "the plan's own fine zone, which is where the channels are"
    mid = [place_lo[k] + place_span[k] / 2.0 for k in range(3)]

    flow = flow_axis(inlet, outlet)
    axis_source = "the plan's named inlet and outlet"
    thin = min(range(3), key=lambda k: place_span[k])
    if flow is None and repeat_axis is not None and int(repeat_axis) != thin:
        # MEASURED. The passages repeat along `repeat_axis`, so they RUN along the other in-plane
        # axis, and a plane normal to that one cuts square across every one of them.
        flow = next(k for k in range(3) if k not in (thin, int(repeat_axis)))
        axis_source = (f"measured from a drawn cut: the passages repeat along "
                       f"{'xyz'[int(repeat_axis)]}, so they run along {'xyz'[flow]}")
    elif flow is None:
        # SEALED and nothing drawn yet, so there is no flow direction to cut across. The long axis
        # stands in. It is a GUESS and it is wrong on the one part this has been measured against:
        # job 0ff6f9e5's longest span is x and x is the axis the passages repeat along, so this
        # puts `across_the_flow` along them. The renderer replaces it once a frame has been drawn.
        flow = max(range(3), key=lambda k: span[k])
        axis_source = ("a guess from the longest span, because the plan names no inlet or outlet "
                       "and nothing has been drawn yet")
    along = next((k for k in range(3) if k not in (flow, thin)), thin)

    # COERCED ONCE, HERE, and every number the zoom frame publishes comes from this one value.
    # `cell_m` is Optional because a plan need not carry a fine cell, and the three things the
    # frame states about itself - the window it was drawn in, the um/px that came out, and the
    # `feature_m` it was sized from - have to be the SAME length or the frame declares a scale it
    # was not drawn at, which is the one failure this file exists to prevent. Taking the
    # conversion out of the three call sites is also what makes the absence provable rather than
    # assumed: `zoom_window` returns a zero scale for anything that is not a positive length, so
    # a missing cell leaves `zmpp` at 0.0 and the gate below publishes no zoom at all.
    cell = float(cell_m or 0.0)
    # THE WINDOW IS SIZED FROM WHAT WILL BE DRAWN, AND `feature_m` STILL REPORTS THE PLAN.
    # `drawn_m` is `drawn_cell_m(...)` above - the plan cell less the layer stack sitting
    # inside it - and it is what makes the frame's own claim checkable. `feature_m` is left as
    # the plan cell on purpose: native._expected_pitch_m derives the part's repeat pitch as
    # `feature_m * CELL_PIXELS` and re-aims the planes off it, so moving that key would
    # silently move the yardstick the cuts are placed by.
    drawn = float(drawn_m or 0.0) or cell
    zw, zh, zmpp = zoom_window(drawn, width_px=width_px, height_px=height_px)

    def _cut(name: str, axis: int, label: str, why: str) -> dict:
        origin = list(mid)
        origin[axis] += _OFF_CENTRE * place_span[axis]
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        ah, av = plane_axes(axis)
        whole_mpp = fitted_metres_per_pixel(span[ah], span[av],
                                            width_px=width_px, height_px=height_px)
        frames = [{
            "scope": "whole",
            "why_frame": "the whole cut, for where in the part this is",
            "window_m": [round(whole_mpp * width_px, 12), round(whole_mpp * height_px, 12)],
            # THE WHOLE PART, so this one stays on the BLOCK's middle even when the plane itself
            # was placed from the fine zone: a context frame centred on the channels would push
            # the rest of the part out of a window that was sized to hold all of it.
            "centre_m": [block_mid[ah], block_mid[av]],
            "centre_source": "the middle of the plan's block",
            "metres_per_pixel": whole_mpp,
            "um_per_pixel": round(whole_mpp * 1e6, 4),
        }]
        # `zmpp` is above zero only when `cell` is a positive length - see `zoom_window`, which
        # returns a zero scale for everything else - so inside this branch `cell` is the real
        # planned cell and not the 0.0 a missing one collapses to.
        if zmpp > 0.0:
            frames.append({
                "scope": "zoom",
                # SAID AS WHAT IT IS. `cell_m` is the PLANNED cell; this mesh's own cells span
                # 7.787 um to 678.88 um, a factor of 87, so "the mesh's 25.16 um cell" would be a
                # claim about the mesh that the mesh does not support.
                "why_frame": (f"sized so a cell of {drawn * 1e6:.4f} um is {CELL_PIXELS} "
                              f"pixels across, which is what counting cells needs"
                              + ("" if drawn == cell else
                                 f" - that is the planned {cell * 1e6:.4f} um cell less the "
                                 f"layer stack grown inside it")),
                "window_m": [zw, zh],
                "centre_m": [mid[ah], mid[av]],
                "centre_source": placed_from,
                "metres_per_pixel": zmpp,
                "um_per_pixel": round(zmpp * 1e6, 4),
                "feature_m": cell,
                "feature_pixels": CELL_PIXELS,
                # WHAT THE READER MUST DIVIDE BY, published on the frame rather than left to
                # be recomputed from `quality` on the far side of the container boundary -
                # because that recomputation is precisely what made the claim an identity.
                "drawn_cell_m": drawn,
            })
        return {"name": name, "kind": "slice", "normal": normal, "origin": origin,
                "clip_box": list(frame), "label": label, "why": why,
                "plane_axes": [ah, av], "normal_axis": axis,
                # WHERE THE PLANE MAY BE MOVED TO, which is the band the features are in. The
                # renderer searches inside this and nowhere else: a plane that wandered out of
                # the fine zone to find something periodic would be showing a different part of
                # the part than the one it is named after.
                "search_m": [place_lo[axis], place_lo[axis] + place_span[axis]],
                "placed_from": placed_from, "axis_source": axis_source, "frames": frames}

    cuts = [
        _cut("across_the_flow", flow, "Square across the coolant's path",
             "the cells across each passage, and the metal between them"),
        _cut("through_the_plate", thin, "Through the part at mid-thickness",
             "where the coolant reaches and where the metal is"),
    ]
    if along not in (flow, thin):
        cuts.append(_cut("along_the_flow", along, "Along the coolant's path",
                         "the cells through the passage's height, with metal above and below"))
    return cuts


__all__ = ["ACF_MIN", "CELL_PIXELS", "FRAME_H", "FRAME_W", "INK_MAX", "MIN_PERIOD_PX",
           "MIN_REPEATS", "PERIOD_MATCH", "SCAN_AXES", "fitted_metres_per_pixel", "flow_axis",
           "ink_structure", "period_matches", "plane_axes", "review_slices", "worst_contrast",
           "zoom_window"]
