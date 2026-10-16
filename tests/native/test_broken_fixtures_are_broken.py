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
    assert small[0].entity_type == "edge" and small[0].entity_index > 0
    assert small[0].location.get("bbox_min"), "an unlocated defect cannot be targeted"


def test_the_loose_faces_fixture_has_no_solid_and_a_free_boundary(tmp_path):
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import measure

    path = write_loose_faces_step(tmp_path / "loose.step")
    assert measure(_read_shape(path)).solids == 0, "loose faces must not arrive as a solid"
    found, _ = _scan(path)
    assert [d for d in found if d.code is DefectCode.open_shell], (
        f"no open boundary found; the scan reported {[d.code.value for d in found]}")
