# Responsibility: Hold viewer.js's _EDGE_MAX_POLYS to the measurement that chose it - that it
# admits the largest actor the product actually ships, and that the most this page can put on
# screen with edges on stays under the one configuration measured taking the WebGL context away.
# Boundaries: arithmetic over recorded measurements, plus reading one constant out of the two
# served copies of viewer.js. It opens no mesh and starts no browser.
"""THE EDGE CAP IS A MEASUREMENT TOO, AND IT IS THE ONE THAT HID THE MESH.

`_EDGE_MAX_POLYS` was 750,000, chosen on 2026-11-08 to sit just above the largest actor that
shipped at the time (bl3's fluid_to_solid, 590,990 polygons). On 2026-11-19 `DRAWN_MAX_TRIANGLES`
went to 3,000,000, the delivered cold plate started being served losslessly, and its one big
surface became 851,763 polygons in a single actor - 13.6% past a cap nobody had looked at.
`setEdges` took its edges off, silently and correctly by its own rule, and that surface is 99.15%
of every polygon the page opens with. The plate arrived looking like CAD.

Nothing failed. No test could see it: the cap is a constant in a JS file, the decision is per
actor, and the only assertions that existed were on totals. This file is the one that can see it.
Every number here was measured, and the measurements are written beside the constant itself.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: The two copies of the viewer that are served. `test_console_ui_copy_parity` holds them byte
#: identical; this file reads the constant out of both anyway, because the failure it guards
#: against is a number drifting, and a number that drifts in one copy first is still the failure.
SERVED = ("ui/js/viewer/viewer.js",
          "apps/console/public/static/js/viewer/viewer.js")

#: THE LARGEST ACTOR THE PRODUCT SHIPS, counted the way `_countPolys` does, off the payload the
#: API served for job 6dd6703c-c8d2-43cd-882c-a01c7375d014 on 2026-11-19: fluid/fluid_to_solid,
#: the coolant side of the conformal interface, drawn losslessly.
PLATE_LARGEST_ACTOR = 851_763

#: ...and what it is a share of. The page opens with 859,052 polygons visible, so this one
#: surface carrying no edges is a page carrying none.
PLATE_VISIBLE_AT_OPEN = 859_052

#: THE MOST THIS PAYLOAD CAN EVER DRAW WITH EDGES ON: every surface it carries, including the
#: four that open hidden, switched on by hand in a real browser. Measured 2026-11-19 on Intel UHD
#: via ANGLE/D3D11 in headless Chrome, 491x754: 145.5 ms/frame, 6.9 fps, gl.getError() 0,
#: gl.isContextLost() false, no webglcontextlost event, 252.6 MB of VBO summed off gl.bufferData.
PLATE_EDGED_WORST_POLYGONS = 1_482_770
PLATE_EDGED_WORST_TRIANGLES = 3_023_513
PLATE_EDGED_WORST_VBO_BYTES = 252.6e6

#: THE ONE CONFIGURATION MEASURED TAKING THE CONTEXT AWAY. 2026-11-08, solid_to_air at 1:1 on the
#: same GPU: 1,904,636 polygons / 3,810,902 triangles with edges on was not a slow frame, it was
#: gl.getError() 37442, CONTEXT_LOST_WEBGL, and a loss that outlived the page.
CONTEXT_LOST_POLYGONS = 1_904_636
CONTEXT_LOST_SEGMENTS = 7_620_174
CONTEXT_LOST_VBO_BYTES = 348.9e6


def segments(polygons: int, triangles: int) -> int:
    """Line segments an edge pass draws: one per polygon corner, and a k-gon fans to k - 2
    triangles, so corners = triangles + 2 x polygons. This is the quantity that broke, not the
    triangle count: 3.8 M triangles drew fine and 7.6 M line segments did not."""
    return triangles + 2 * polygons


def cap_of(rel: str) -> int:
    text = (REPO / rel).read_text(encoding="utf-8")
    found = re.search(r"^const _EDGE_MAX_POLYS=(\d+);", text, re.MULTILINE)
    assert found, f"{rel} no longer declares _EDGE_MAX_POLYS at the top level"
    return int(found.group(1))


def test_both_served_copies_declare_the_same_cap():
    caps = {rel: cap_of(rel) for rel in SERVED}
    assert len(set(caps.values())) == 1, (
        f"the two served viewers disagree about where edges stop: {caps}")


def test_the_cap_admits_the_largest_actor_the_product_ships():
    """The defect this guards. Below 851,763 the delivered plate draws no cell edges on 99.15% of
    what a reader sees, and the page becomes a picture of a part instead of a picture of a mesh."""
    cap = cap_of(SERVED[0])
    assert cap >= PLATE_LARGEST_ACTOR, (
        f"the cap is {cap:,}, so the delivered plate's fluid/fluid_to_solid at "
        f"{PLATE_LARGEST_ACTOR:,} polygons draws without cell edges - and it is "
        f"{100 * PLATE_LARGEST_ACTOR / PLATE_VISIBLE_AT_OPEN:.2f}% of the polygons the page opens "
        "with, so that is the whole mesh, not one surface of it")


def test_the_worst_this_page_can_draw_stays_under_what_took_the_context():
    """THE OVERLAYS CAN ALL BE SWITCHED ON, so the cap has to hold for the page a reader can
    reach, not the page it opens with. The quantity that binds is line segments."""
    worst = segments(PLATE_EDGED_WORST_POLYGONS, PLATE_EDGED_WORST_TRIANGLES)
    assert worst == 5_989_053, worst
    assert worst < CONTEXT_LOST_SEGMENTS, (
        f"this payload can reach {worst:,} line segments with everything on, against the "
        f"{CONTEXT_LOST_SEGMENTS:,} that took the context away")
    assert PLATE_EDGED_WORST_VBO_BYTES < CONTEXT_LOST_VBO_BYTES, (
        "and the same page in bytes must stay under the 348.9 MB that did")


def test_the_cap_is_not_quietly_enormous():
    """The other direction, and the reason this is a cap at all: WebGL exposes no way to ask how
    much video memory is left, so a context loss cannot be detected and has to be bounded. A cap
    at or past the measured loss is not a bound."""
    cap = cap_of(SERVED[0])
    assert cap < CONTEXT_LOST_POLYGONS, (
        f"a cap of {cap:,} admits an actor at least as large as the {CONTEXT_LOST_POLYGONS:,} "
        "that was measured losing the context, which is the one thing it exists to prevent")


def test_the_cap_sits_just_above_what_ships_and_not_far_above():
    """The rule the 2026-11-08 note set and this change kept: bound the case that actually broke,
    do not pretend to know where the true boundary is. A cap far above the largest actor that
    ships is a cap chosen from a number nobody measured."""
    cap = cap_of(SERVED[0])
    assert cap <= 2 * PLATE_LARGEST_ACTOR, (
        f"the cap is {cap:,} against a largest shipped actor of {PLATE_LARGEST_ACTOR:,}. Raising "
        "it further needs a new measurement on a payload that actually has an actor that size")
