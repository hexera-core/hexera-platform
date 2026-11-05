# Responsibility: Verify each new fixture actually carries the defect it exists to carry, as the real localiser sees it.
# Boundaries: the native tier, because only here does the CAD kernel exist. It asserts nothing
#             about repair - a fixture that is not broken makes every repair test below it
#             meaningless, so the brokenness is proved on its own.
from __future__ import annotations

import pytest
from tests.cad_fixtures import (
    write_loose_faces_step,
    write_small_edge_step,
)

from meshpipeline.cad.repair.contracts import DefectCode

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


def _scan(path):
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan

    found, complete = localize_scan(_read_shape(path))
    return found, complete


def test_the_small_edge_fixture_carries_a_small_edge(tmp_path):
    found, complete = _scan(write_small_edge_step(tmp_path / "sliver.step"))
    assert complete
    small = [d for d in found if d.code is DefectCode.small_edge]
    assert small, f"no small edge found; the scan reported {[d.code.value for d in found]}"
    # LOCATED, not merely counted: an operation has to be able to aim at it.
    assert all(d.entity_type == "edge" and d.entity_index > 0 for d in small)
    assert all(d.location.get("bbox_min") for d in small), "an unlocated defect cannot be targeted"
    # WHICH edge, not just some edge. The notch's tip face is 0.002 mm wide at x = 20, so its top and
    # bottom edges are the sliver. Pinned from what the localiser REPORTS (its measurements and
    # location), because that is what a targeted operation acts on. At least one must match the tip;
    # the first reported is not assumed to be it.
    threshold = 1e-4 * 141.774  # SMALL_EDGE_LENGTH_FRACTION * the plate's diagonal
    tips = [
        d for d in small
        if d.measurements["length"] < threshold
        and abs(d.location["bbox_min"][0] - 20.0) < 0.01
        and abs(d.location["bbox_max"][0] - 20.0) < 0.01
    ]
    assert tips, (
        "no reported small edge is the wedge tip (length < threshold, at x = 20); "
        f"reported: {[(d.entity_index, d.measurements, d.location) for d in small]}")


def test_the_loose_faces_fixture_has_no_solid_and_a_free_boundary(tmp_path):
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import measure

    path = write_loose_faces_step(tmp_path / "loose.step")
    measured = measure(_read_shape(path))
    assert measured.solids == 0, "loose faces must not arrive as a solid"
    # The STEP reader wraps each loose face in a shell of its own, so "unjoined" reads as one shell
    # PER FACE, not as no shell. Pinned so a sewing test can assert the count FALLS from here.
    assert measured.shells == measured.faces == 6, f"expected six single-face shells, got {measured}"
    found, _ = _scan(path)
    assert [d for d in found if d.code is DefectCode.open_shell], (
        f"no open boundary found; the scan reported {[d.code.value for d in found]}")
