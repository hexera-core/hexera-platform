# Responsibility: Hold the difference between a part with no coolant in it and a part whose coolant
# was found and measured but is too fine for the budget.
# Boundaries: the failure markers cfmesh_conjugate's driver files; no meshing and no OpenCASCADE.
"""A PART REFUSED FOR ITS SIZE WAS FILED AS A PART WITH NO COOLANT.

Four refusals in the conjugate driver price the part against a budget and decline before anything
runs: two scales of passage that no pair of cell sizes resolves, an estimate over the deployment's
cell limit, cartesianMesh's own 32-bit octree ceiling, and an archive the caller would not take
back. Each logs a precise sentence and then returned `conjugate_no_coolant` - on parts whose
coolant had been found, measured and written to disk a few lines earlier.

MEASURED on the first customer assembly, 2026-10-30 (ColdPlateAssembly_CLEAN.step, job 7439e942,
through the page as a customer):

    fluid volume: wrote the coolant surface - 52334 triangles, 53661.786893 mm3 over 2 passage(s)
    conjugate: refusing to start - this part has two scales of passage - about 2.11 mm and
               0.0982 mm - and no pair of cell sizes resolves the thin ones at 4 cells across
               inside a limit of 8,000,000 cells

and the failure recorded against that job was "no coolant". The marker is what survives into the
failure record and into any later reading of why a part did not mesh, so it has to say the true
thing. The driver's own comment beside the archive check states the standard: "the reason names the
setting rather than blaming the part".

This test does not re-measure the geometry. It holds the shape of the refusals in the source,
because that is what the defect was: the right sentence in the log and the wrong name on the record.
"""
from __future__ import annotations

import inspect
import re

from meshpipeline.engines.cfmesh_conjugate import driver as D


def _source() -> str:
    return inspect.getsource(D)


def test_the_two_markers_exist_and_say_what_they_mean():
    assert D.NO_COOLANT == "conjugate_no_coolant"
    assert D.TOO_FINE == "conjugate_too_fine_for_budget"
    assert D.NO_COOLANT != D.TOO_FINE


def test_only_one_refusal_is_about_coolant_that_is_not_there():
    """The split finding nothing is one case. Everything else refused before meshing is about
    size, and must not borrow this name."""
    src = _source()
    assert src.count("failure_marker=NO_COOLANT") == 1, (
        "more than one refusal claims the part has no coolant; the size refusals must use TOO_FINE")


def test_all_four_size_refusals_say_so():
    """Two scales, over the cell limit, over the octree ceiling, and an archive too big to return."""
    src = _source()
    assert src.count("failure_marker=TOO_FINE") == 4, (
        "a size refusal is not filing itself as a size refusal")


def test_every_refusing_to_start_is_followed_by_the_size_marker():
    """The four `refusing to start` log lines are exactly the size refusals, so each one's return
    has to carry TOO_FINE. Anchored on the log line because that is what makes a refusal one of
    these four rather than a different kind of stop."""
    src = _source()
    starts = [m.start() for m in re.finditer(r'refusing to start', src)]
    assert len(starts) == 4, f"expected four size refusals, found {len(starts)}"
    for i, at in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else len(src)
        window = src[at:nxt]
        assert "failure_marker=TOO_FINE" in window, (
            f"the refusal at offset {at} logs a reason but does not file itself as TOO_FINE")
        assert "failure_marker=NO_COOLANT" not in window, (
            f"the refusal at offset {at} still blames the coolant")


def test_the_marker_is_exported():
    assert "TOO_FINE" in D.__all__
