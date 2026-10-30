# Responsibility: Hold the difference between a part with no coolant in it, a part whose coolant was
# found and measured but is too fine for the budget, and a part that was never converted to metres.
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


def test_a_part_that_is_not_in_metres_is_not_a_part_that_is_too_fine():
    """THE SAME DEFECT AS THIS FILE'S, ONE REFUSAL LATER.

    A fifth refusal declines a part whose geometry was never converted to metres. It is not a size
    refusal and must not borrow TOO_FINE: a part refused for its units is not one whose detail was
    too fine, and telling the next reader to ask for less detail sends them to the wrong place -
    which is exactly the mistake the four above were making with NO_COOLANT.

    It also cannot be detected as a size refusal. The cell estimate is a dimensionless ratio, so a
    job consistently in millimetres prices identically to one consistently in metres; the plate
    that was written unscaled estimated 334,340,370 cells, passed every gate, and built
    877,659,515 octree boxes before it was killed at 250 of 251 GiB.
    """
    assert D.NOT_METRES == "conjugate_geometry_not_metres"
    assert D.NOT_METRES not in (D.NO_COOLANT, D.TOO_FINE, D.NO_SOURCE, D.BUILD_FAILED)
    assert "NOT_METRES" in D.__all__
    src = _source()
    assert src.count("failure_marker=NOT_METRES") == 1, (
        "the units refusal is not filing itself as a units refusal")


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


def test_every_refusing_to_start_names_itself_truthfully():
    """Every `refusing to start` log line must file a marker that means what the line says.

    Five of them now: the four size refusals carry TOO_FINE, and the units refusal carries
    NOT_METRES. The rule the original defect broke is the one being held - a refusal's marker is
    what survives into the failure record, so it has to say the true thing - and not the count,
    which only ever happened to be four. None of them may blame the coolant.
    """
    src = _source()
    starts = [m.start() for m in re.finditer(r"refusing to start", src)]
    assert len(starts) == 5, f"expected five refusals, found {len(starts)}"
    # EACH REFUSAL'S OWN MARKER IS THE NEXT ONE IN THE SOURCE, not whatever appears before the
    # next log line. The window form this replaces only worked while the refusals happened to sit
    # together: the units refusal comes earlier in `drive` than the coolant one, so its window
    # swallowed `failure_marker=NO_COOLANT` and the test accused it of blaming the coolant.
    seen: list[str] = []
    for at in starts:
        nxt = re.search(r"failure_marker=(\w+)", src[at:])
        assert nxt is not None, f"the refusal at offset {at} logs a reason and files no marker"
        seen.append(nxt.group(1))
    assert seen.count("TOO_FINE") == 4, f"expected four size refusals, found {seen}"
    assert seen.count("NOT_METRES") == 1, f"expected one units refusal, found {seen}"
    assert "NO_COOLANT" not in seen, f"a refusal still blames the coolant: {seen}"


def test_the_marker_is_exported():
    assert "TOO_FINE" in D.__all__
