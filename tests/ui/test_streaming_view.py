# Responsibility: Record that the STREAMING VIEW is no longer offered to a reader, and hold the
# UI to it - the control is absent from the inspector and nothing in the viewer can turn the mode
# on by itself.
# Boundaries: this does not test the dormant pass. It tests that the pass is unreachable.
"""STREAMING IS CUT. This file is what is left of it, and why.

It tiled the screen and read real delivered cells into every tile, to answer "show me the whole
mesh" without merging anything. Three measurements say it should not be a control a reader can
reach, and they are independent of each other:

    ITS OWN TILING WAS REFUTED FIRST. 71.4% of the tiles a pass asked for came back empty, and
    even spending the whole budget a pass reached full detail only to about 4 mm of a 170 mm part.
    The thing it was built to do, it did not do.

    IT DRAWS NOTHING ON THE DELIVERED MESH. Driven on the Corintis plate on 2026-11-11 with the
    mode on and a pass forced: 0 faces, 0 tiles, 0 reads, 0 left, 0 dense. Not "few" - none.

    DEEP DOES THE JOB AND IS MEASURED DOING IT. On the same mesh, the same afternoon: refused
    above 20 mm because the box held more than the route will send, then 818,905 real delivered
    cells at 16.3 mm across, in ONE request rather than dozens of tiles.

So the button is gone from `inspect.inspectorHtml`. `setStream` and its pass are still in
viewer.js, unreachable from the UI, because `drawCut` and the edge budget are shared with Deep and
Follow and untangling them is its own change. That is exactly the state this file guards: a
reader cannot reach it, and nobody re-adds the button without reading the three measurements
above.

The three browser tests that used to live here drove that button. They are not kept as skips:
a skipped test for a cut feature is a reminder, not a check, and the check that matters now is
that the control is absent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

UI = Path(__file__).resolve().parents[2] / "ui/js/viewer"
CONSOLE = (Path(__file__).resolve().parents[2]
           / "apps/console/public/static/js/viewer")


def _inspector_markup() -> str:
    """The template `inspectorHtml` returns, as source text."""
    src = (UI / "inspect.js").read_text()
    start = src.index("export function inspectorHtml")
    return src[start:src.index("export default", start)]


def test_the_inspector_offers_no_stream_control():
    """No id, and no label, in the markup a reader is handed."""
    markup = _inspector_markup()
    assert "id('stream')" not in markup, "the Stream button is back without the measurements"
    assert "id(\"stream\")" not in markup
    assert not re.search(r">\s*Stream\s*<", markup), "a Stream label is back in the inspector"


def test_the_two_copies_of_the_inspector_agree():
    """apps/console/public/static is a byte copy of ui/, so a control cut in one is cut in both."""
    if not (CONSOLE / "inspect.js").is_file():
        pytest.skip("this tree has no console copy of the inspector")
    assert (CONSOLE / "inspect.js").read_bytes() == (UI / "inspect.js").read_bytes()


def test_the_dormant_pass_is_unreachable_rather_than_merely_unbound():
    """The mode is off because its ELEMENT is never emitted, and that is the whole chain.

    The toggle is still written - `str.onclick = () => setStream(!streaming)` - and that is fine,
    and better than deleting it blind: it sits behind `if (str)`, `str` is
    getElementById('v-insp-stream-' + job), and the inspector no longer emits that id, so `str` is
    null and the handler is never bound. Removing the guard instead of the button would be a null
    dereference on every mesh, which is what this asserts against.

    What must NOT exist is an unconditional enable. Turning the mode OFF is called from several
    places, because the three cut modes are mutually exclusive, and all of that must keep working.
    """
    src = (UI / "viewer.js").read_text()

    # the element the whole mode hangs off, and the guard in front of the toggle
    assert "document.getElementById('v-insp-stream-'+job)" in src.replace(" ", ""), (
        "the stream element is no longer looked up by id, so the cut no longer works by absence")
    i = src.index("setStream(!streaming)")
    assert "if(str){" in src[max(0, i - 400):i].replace(" ", ""), (
        "the stream toggle is no longer guarded by the element existing: with the button cut "
        "this dereferences null")

    # ...and nobody switches it on without a reader
    forced = [m.group(0) for m in re.finditer(r"setStream\(\s*true\s*\)", src)]
    assert not forced, "something turns streaming on by itself: %s" % forced


def test_the_reasoning_is_recorded_where_the_button_was():
    """A cut with no reason beside it gets undone by the next person who misses the button."""
    markup = _inspector_markup()
    assert "STREAM IS CUT" in markup
    for figure in ("71.4%", "818,905", "16.3 mm"):
        assert figure in markup, "the measurement %r that justifies the cut is not recorded" % figure
