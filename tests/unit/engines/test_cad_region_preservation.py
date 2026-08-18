# Responsibility: Verify a CAD file's named components survive tessellation, or that nothing changes.
# Boundaries: the surface handed to an engine; what a mesher then does with the regions is its own.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.cad.cad_tessellate import tessellate_regions_to_stl, tessellate_to_stl
from meshpipeline.cad.regions import regions_of
from meshpipeline.cad.stl_io import read_stl_solids, read_stl_triangles
from meshpipeline.contracts.coordinate_state import (
    OCC_OUTPUT_UNIT,
    CoordinateOrigin,
    PreparedCoordinates,
)
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)

REPO = Path(__file__).resolve().parents[3]
#: Real exports: one names its parts, one does not. The whole question is what a file carries.
NAMED = REPO / "BETA" / "cht_concentric_pipe.step"
UNNAMED = REPO / "BETA" / "Naca0012.STEP"

pytestmark = pytest.mark.skipif(not NAMED.is_file() or not UNNAMED.is_file(),
                                reason="licensed CAD fixtures are local-only")


def _prepared() -> PreparedCoordinates:
    return PreparedCoordinates(
        origin=CoordinateOrigin.occ_transfer, current_unit=OCC_OUTPUT_UNIT,
        interpretation=GeometryInterpretation(
            interpretation_id="i", owner_id="o", geometry_source_id="g",
            unit=LengthUnit.metre, scale_to_metres=1.0,
            basis=ResolutionBasis.file_declared, evidence="fixture"))


def test_named_components_survive_as_separate_stl_solids(tmp_path):
    dest = tmp_path / "regions.stl"
    written = tessellate_regions_to_stl(NAMED, dest, prepared=_prepared())
    assert len(written) >= 2, written
    solids = read_stl_solids(dest)
    assert set(solids) == set(written), "the file does not hold the regions that were reported"
    assert all(solids[name] for name in solids), "a region was written with no triangles"
    assert set(written) <= set(regions_of(NAMED).names), \
        "a region was invented that the source file does not name"


def test_a_structureless_file_is_written_exactly_as_the_flat_path_would(tmp_path):
    # The guarantee that makes this safe to adopt anywhere: a file with nothing to preserve takes
    # the path it always took, byte for byte, so no engine's input changes until its geometry says
    # something new.
    flat, region = tmp_path / "flat.stl", tmp_path / "region.stl"
    tessellate_to_stl(UNNAMED, flat, prepared=_prepared())
    written = tessellate_regions_to_stl(UNNAMED, region, prepared=_prepared())
    assert written == []
    assert region.read_bytes() == flat.read_bytes()
    assert len(read_stl_triangles(region)) > 0, "the fallback produced no surface"


def test_the_unit_state_is_still_required(tmp_path):
    # Region-aware or not, tessellation never guesses the unit.
    with pytest.raises(ValueError, match="coordinate state"):
        tessellate_regions_to_stl(NAMED, tmp_path / "x.stl", prepared=None)
