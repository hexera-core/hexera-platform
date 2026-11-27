# Responsibility: Verify in a real browser that the SECOND served copy of the viewer -
# apps/console/public/static/js/viewer/viewer.js, which the console's own run page runs - draws the
# layer block the same way the api's copy does, and makes none of the claims the measurements
# refuted.
# Boundaries: the console's own js tree, served from a throwaway static root so nothing here
# touches the shared STATIC_DIR the rest of tests/ui is served from. The payload is built by the
# shipped backend producers. Nothing here opens a mesh, cuts an interior or renders a volume.
"""A SECOND COPY OF THE VIEWER IS SERVED AND THE REPOSITORY BELIEVED IT WAS NOT.

`finalize._near_wall` said in its own comment that `apps/console/public/static/js/viewer/viewer.js`
"is a second copy nothing serves", and `tests/unit/render/test_the_layer_count_reaches_the_reader`
scoped the one gate over the refuted layer claim to `ui/` on the strength of that. The chain was
traced end to end on 2026-11-17 instead of assumed:

    Dockerfile:682                 COPY --from=console-build /build/apps/console/public/ \\
                                       ./apps/console/public/
    runs/[jobId]/page.tsx          renders <Workbench bootJobId={jobId} ...>
    _components/workbench.tsx:85   <Script src="/static/js/main.js" type="module" />
    public/static/js/main.js:34    import { openViewer } from "./viewer/viewer.js";
    apps/console/next.config.ts    no rewrites, no redirects - Next serves public/ from the root

So /static/js/viewer/viewer.js on the console IS that file, and it had drifted to half the size of
the api's copy (1,836 lines against 3,554) while printing "none: a prism stack does not fit across
a passage this narrow without taking the cell quality to its limit" and "graded refinement
instead" - the claim this engine's own delivered mesh, with 2.25 layers in it, refutes. 8787 has
served a stale COPY of a viewer script before now, so which file is actually served is a known
trap in this repository.

WHY THIS FILE SERVES ITS OWN ROOT. tests/ui's session server is the api with STATIC_DIR at `ui/`,
and one env var decides it for the whole session - so pointing it at the console tree here would
serve the console's js to every other file in the suite. This stands up a plain static root of its
own instead, laid out exactly as the console serves it (/static/js, /static/vendor, /static/css),
and the module graph resolves natively: viewer.js's own `../core/format.js` and `./inspect.js`
come from the console's tree and not from the api's.

WHAT IS NOT CLAIMED HERE. This is the layer panel, which is text. VTK cannot render on this host
(DISPLAY is unset and there is no xvfb) and nothing in this file needs it to: no cut of a mesh
interior is drawn, and the one surface used is a 400-quad sheet.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socketserver
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

REPO = Path(__file__).parents[2]
#: THE TREE THAT GETS SERVED, and it is overridable for exactly one reason: to point this gate at
#: the console tree AS IT WAS, and watch it fail. A gate written after the fix, against the fixed
#: file, proves only that the fix is still there - so the before arm serves a snapshot of the
#: shipped copy from outside the checkout (`CONSOLE_STATIC_DIR`) rather than reverting anything in
#: it. Unset, which is every ordinary run, this is the console's own root.
CONSOLE_STATIC = Path(os.environ.get("CONSOLE_STATIC_DIR")
                      or (REPO / "apps/console/public/static"))
#: the two files this is about, in the order a reader should compare them
API_COPY = REPO / "ui/js/viewer/viewer.js"
CONSOLE_COPY = CONSOLE_STATIC / "js/viewer/viewer.js"

JOB = "console-layers-job"

# ONE SPELLING OF THE MEASUREMENT AND ONE OF THE PAYLOAD. The real plate's layer log and the chain
# that turns it into a viewer payload are in the sibling file; copying either here would give this
# repository's one real layer measurement a second spelling that could disagree.
from test_the_panel_says_which_layer_floors_the_run_missed import (  # noqa: E402
    REAL_LOG,
    TRUNCATED_LOG,
    ZERO_LAYER_LOG,
    _grid,
    _payload_quality,
)

from meshpipeline.render.viewer_pack import polymesh_response  # noqa: E402

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>console viewer</title></head>
<body><div id="stage"></div></body></html>"""


@pytest.fixture(scope="module")
def console_root(tmp_path_factory):
    """The console's own static tree, served the way the console image serves it."""
    root = tmp_path_factory.mktemp("console-static")
    (root / "index.html").write_text(_PAGE)
    for part in ("js", "vendor", "css", "assets"):
        src = CONSOLE_STATIC / part
        if src.is_dir():
            shutil.copytree(src, root / "static" / part)
    assert (root / "static/js/viewer/viewer.js").is_file(), "the console's viewer was not copied"
    return root


@pytest.fixture(scope="module")
def console_url(console_root):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):          # a static root is not the subject of this test
            pass

    handler = functools.partial(Quiet, directory=str(console_root))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _surface(q: dict) -> dict:
    surf = polymesh_response([_grid("fluid_to_solid", 20, 20)], {"p10": 0.001, "typ": 0.001},
                             {"fluid_to_solid": "wall"}, "m")
    assert surf is not None and surf["kind"] == "polymesh"
    surf.update(is_mesh=True, parts=[], quality=q)
    return surf


def _open_console(page, url: str, surf: dict) -> None:
    page.open(f"{url}/index.html", ready="!!document.getElementById('stage')")
    page.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__JOB__/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
    })()""".replace("__PAYLOAD__", json.dumps(surf)).replace("__JOB__", JOB), timeout=90)
    # THE PANEL, NOT THE ELEMENT THAT HOLDS IT. The viewer draws its result surface first and
    # fills the facts panel after, so waiting on the element alone returned an EMPTY string - and
    # every "this claim is not on the page" assertion below passes vacuously against an empty
    # string, which is how a gate over a refuted claim could have read as green while proving
    # nothing. The wait is on there being a panel to read.
    page.wait_for(f"((document.getElementById('v-facts-{JOB}')||{{}}).textContent||'').length > 0",
                  timeout=90, what="the console's viewer to draw the delivered-mesh panel")
    page.settle()


def _facts(page) -> str:
    """Every word the panel holds, lowercased.

    `textContent` AND NOT `innerText`, which is the one place this file differs from the sibling
    that drives the api's copy. `innerText` is what a reader SEES and so needs layout: the viewer
    draws this panel into its own result surface rather than into the host element (`#stage` is
    left empty), and that surface is laid out by `workbench.css`, which this static root
    deliberately does not load - the subject here is the js tree, not the stylesheet. Measured on
    this host: `innerText` came back empty while `textContent` held 1,083 characters of panel.
    Lower-cased for the same reason the sibling file lower-cases: `theme.css` capitalises a
    label's first letter, so case is never a fact about what the backend wrote.
    """
    return (page.evaluate(
        f"(document.getElementById('v-facts-{JOB}')||{{}}).textContent || ''") or "").lower()


# #
# the claim that must not keep being served
# #
def test_the_console_copy_no_longer_tells_a_customer_a_prism_stack_does_not_fit(page, console_url):
    """REAL PLATE (job 0ff6f9e5) figures, read from that run's own layer log by the shipped
    parser. SURFACE only. The row printed this whenever the count was 0, and the count was 0 on
    every mesh this engine delivered before 2026-11-16 - a claim about the customer's geometry
    taken from a stack 109% of its own background cell, which is a dictionary defect that
    `layers.fit_complaint` refuses, not a property of the part."""
    _open_console(page, console_url, _surface(_payload_quality(REAL_LOG)))
    text = _facts(page)
    # EVERY ASSERTION BELOW IS AN ABSENCE, and an empty panel satisfies all of them. This is the
    # one that says there was a panel.
    assert "delivered mesh" in text and "boundary layers" in text, (
        f"the console's panel did not draw the layer rows at all, so nothing below is tested:\n{text}")
    assert "does not fit" not in text, (
        "the copy the console's run page serves still tells this customer a prism stack does not "
        f"fit across their passages, on a mesh delivered with 2.25 layers in it:\n{text}")
    assert "graded refinement instead" not in text, (
        "and it still draws the graded cell at the wall as a substitute for a stack this mesh "
        f"has:\n{text}")
    assert "without its partner" not in text, (
        f"and that layers would break the conjugate interface, measured false on the coupon:\n{text}")


def test_the_console_copy_says_why_there_are_none_without_blaming_the_geometry(page, console_url):
    """THE PAYLOAD THAT ACTUALLY PRINTED IT. The refuted sentence was on the `near_wall_layers
    === 0` branch, so it never showed on the plate - it showed on every conjugate mesh delivered
    before 2026-11-16, each of which asked for none and grew none. A bare 0 still reads as a
    failure, so the row still has to say why there are none; what it must not say is that the
    customer's passages are the reason."""
    _open_console(page, console_url, _surface(_payload_quality(None, asked=0)))
    text = _facts(page)
    assert "boundary layers" in text, f"there is no layer row to read:\n{text}"
    assert "does not fit" not in text and "a passage this narrow" not in text, (
        "the copy the console serves tells a reader a prism stack does not fit across their "
        f"passages, which was measured on a dictionary defect and not on the part:\n{text}")
    assert "none were asked for" in text, (
        f"a bare 0 beside 'Boundary layers' with no reason on the row reads as a mesh that "
        f"failed to grow them:\n{text}")
    assert "cell at the wall" in text, (
        f"and the graded resolution that IS the near-wall answer here stopped being drawn:\n{text}")


def test_the_console_copy_draws_the_measured_count_against_the_ask(page, console_url):
    """It knew none of the layer keys the backend publishes, so 2.25 arrived with no ask beside
    it - and 2.25 with nothing beside it cannot be seen to have fallen short of anything."""
    _open_console(page, console_url, _surface(_payload_quality(REAL_LOG)))
    text = _facts(page)
    assert "boundary layers" in text and "2.25" in text, f"the count itself is missing:\n{text}"
    assert "3 asked for" in text, f"the ask is not beside it:\n{text}"
    assert "88.5826%" in text and "71.9%" in text and "239 per million" in text, (
        f"and the figures its three missed floors are drawn on are not on the page:\n{text}")
    assert "fell short" in text, (
        f"nothing on the console's panel says these sentences are shortfalls:\n{text}")
    for raw in ("layer coverage pct", "near wall layers measured", "layer short"):
        assert raw not in text, (
            f"{raw!r} is drawn under its own raw key name, which is what the Other group does to "
            f"a figure this build has no label for:\n{text}")


def test_the_console_copy_says_a_partly_judged_pass_is_partly_judged(page, console_url):
    """SYNTHETIC: a layer log holding only the Extruding and Added lines. One of the four floors
    had a figure, and an empty shortfall list drawn on its own reads as all four."""
    _open_console(page, console_url, _surface(_payload_quality(TRUNCATED_LOG)))
    text = _facts(page)
    assert "delivered mesh" in text, f"there is no panel to read:\n{text}"
    assert "no shortfall" not in text, (
        "the console's panel tells this reader there was no shortfall against this engine's layer "
        f"floors, on a run where three of the four were never judged:\n{text}")
    for floor in ("thickness", "layers kept", "faces in error"):
        assert floor in text, f"it does not name {floor!r} as a floor nothing judged:\n{text}"
    assert "not measured" in text, (
        f"and it no longer says that the count on the row is the ask:\n{text}")


def test_the_console_copy_loads_cleanly_as_the_console_serves_it(page, console_url):
    """IT IS A MODULE GRAPH, not a file. This imports it over HTTP from the console's own tree, so
    a sibling the port left behind - an export that moved, a label table out of step - fails here
    rather than on a customer's run page. The one failure this cannot see is a stylesheet: the
    console's css diverges from the api's too (theme, tokens and viewer all differ), which is part
    of why the two trees are held identical over the layer block and not file for file."""
    _open_console(page, console_url, _surface(_payload_quality(REAL_LOG)))
    page.settle()
    bad = list(page.page_errors) + list(page.console_errors)
    # the vendored vtk bundle cannot initialise a GL context on this host (DISPLAY is unset and
    # there is no xvfb) and says so; that is the renderer, not the panel this file is about.
    bad = [e for e in bad if "webgl" not in e.lower() and "gl context" not in e.lower()]
    assert not bad, "the console's viewer did not load cleanly:\n    " + "\n    ".join(bad)


# #
# the same two readings the api's copy is held to, on the copy the console serves
# #
def test_the_console_copy_counts_the_floors_it_judged_and_not_the_keys_it_got(page, console_url):
    """SYNTHETIC: a pass that grew nothing, with 90,000 faces in error over no cells added. The
    faces-in-error floor is a ratio over the cells the pass ADDED, so it could not be evaluated -
    and this copy drew the tally as "4 of the 4" over a verdict on three, exactly as the api's
    did. SURFACE only."""
    _open_console(page, console_url, _surface(_payload_quality(ZERO_LAYER_LOG)))
    text = _facts(page)
    assert "delivered mesh" in text, f"there is no panel to read:\n{text}"
        # TWO, not three. The floor that replaced the coverage one is drawn on
    # `layer_kept_of_offered_pct` (layers.KEPT_OF_OFFERED_FLOOR_PCT), which this
    # all-zero log cannot produce - so it joins "faces in error" as unjudged, and the
    # payload reports layer_floors_unjudged ["faces kept of those offered",
    # "faces in error"] against layer_floors_total 4. Computed from the shipped
    # producers, not guessed.
    assert ("2 of the 4 floors this engine judges a layer pass by were judged on what this "
            "pass measured") in text, (
        "the copy the console's run page serves tells a reader this pass was judged by all four "
        f"of this engine's layer floors, when two of the tests ran:\n{text}")
    assert "4 of the 4 floors" not in text, (
        f"and four of four is still claimed somewhere on the page:\n{text}")
    assert "not judged at all" in text, (
        f"and the floor whose test never ran is not said to be unjudged:\n{text}")
    assert "fell short" in text, (
        f"while the three floors that WERE missed stopped being a shortfall:\n{text}")


def test_the_console_copy_tells_an_unreadable_log_from_no_layer_pass_at_all(page, console_url):
    """BOTH PANELS, one after the other on the same served copy, because the claim is that they
    DIFFER - and they used to be identical, down to the row.

    THREE LAYERS ASKED FOR ON BOTH ARMS, which is what makes this a reading about the layer log
    rather than about the ask. SYNTHETIC: `read_layer_log("")` is the pass whose log could not be
    read and a report with no layers block at all is the run that never grew one, while the plan
    asks for 3 either way - so both panels say "Boundary layers 3 / not measured", and before this
    change everything else on them was the same too."""
    _open_console(page, console_url, _surface(_payload_quality("")))
    unreadable = _facts(page)
    _open_console(page, console_url, _surface(_payload_quality(None, asked=3)))
    none_at_all = _facts(page)
    assert unreadable != none_at_all, (
        "the console serves the same panel, word for word, for a layer pass whose log could not "
        f"be read and for a mesh that never grew a layer:\n{unreadable}")
    assert "a layer pass ran" in unreadable and "none of the 4 floors" in unreadable, (
        f"and it does not say which of the two this run was:\n{unreadable}")
    assert "no shortfall" not in unreadable and "fell short" not in unreadable, (
        f"nor may it deliver a verdict on a pass nothing judged:\n{unreadable}")
    assert "a layer pass ran" not in none_at_all, (
        f"a mesh that never grew a layer is drawn as one whose log failed to read:\n{none_at_all}")
    assert "not measured" in none_at_all, (
        f"and the half that was already right stopped being said:\n{none_at_all}")
