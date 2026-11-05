# Responsibility: Verify named surface regions reach snappyHexMeshDict as separate wall patches.
# Boundaries: the dict this system authors; whether the mesher then emits them is the native tier's proof.
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from meshpipeline.cad.analysis import analyze_surface, recommend_refinement
from meshpipeline.cad.cad_tessellate import tessellate_regions_to_stl
from meshpipeline.cad.prepared_surface import PreparedSurface, SurfaceRepresentation
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
from meshpipeline.engines.snappy import snappy_runner as R

REPO = Path(__file__).resolve().parents[3]
NAMED = REPO / "BETA" / "cht_concentric_pipe.step"

pytestmark = pytest.mark.skipif(not NAMED.is_file(),
                                reason="licensed CAD fixture is local-only")


def _prepared() -> PreparedCoordinates:
    return PreparedCoordinates(
        origin=CoordinateOrigin.occ_transfer, current_unit=OCC_OUTPUT_UNIT,
        interpretation=GeometryInterpretation(
            interpretation_id="i", owner_id="o", geometry_source_id="g",
            unit=LengthUnit.metre, scale_to_metres=1.0,
            basis=ResolutionBasis.file_declared, evidence="fixture"))


def _workspace() -> Path:
    ws = Path(tempfile.mkdtemp())
    (ws / "system").mkdir(parents=True)
    (ws / "constant" / "triSurface").mkdir(parents=True)
    return ws


def _dict_for(regions) -> str:
    ws = _workspace()
    surface = tessellate_regions_to_stl(NAMED, ws / "input.stl", prepared=_prepared())
    assert surface, "the fixture must carry regions for this test to mean anything"
    R.prepare_surface(ws, geometry_file="input.stl", wall_patch="body",
                      domain_min=[-1] * 3, domain_max=[1] * 3)
    surf = PreparedSurface(path=ws / "constant" / "triSurface" / "body.stl", source_id="s",
                           interpretation_id="i", consumed=_prepared(),
                           representation=SurfaceRepresentation.stl)
    analysis = analyze_surface(surf)
    R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=analysis,
                         recommendation=recommend_refinement(analysis),
                         domain_min=[-5] * 3, domain_max=[5] * 3, surface_regions=regions)
    return (ws / "system" / "snappyHexMeshDict").read_text()


def test_prepare_surface_reports_the_regions_the_surface_carries():
    ws = _workspace()
    tessellate_regions_to_stl(NAMED, ws / "input.stl", prepared=_prepared())
    out = R.prepare_surface(ws, geometry_file="input.stl", wall_patch="body",
                            domain_min=[-1] * 3, domain_max=[1] * 3)
    assert len(out["surface_regions"]) >= 2, out
    written = (ws / "constant" / "triSurface" / "body.stl").read_text()
    for name in out["surface_regions"]:
        assert f"solid {name}" in written, f"{name} was reported but not written"


def test_each_region_becomes_its_own_named_wall_patch():
    text = _dict_for(["fluid", "wall"])
    assert "regions { fluid { name fluid; } wall { name wall; } }" in text, text[:400]
    assert text.count("patchInfo { type wall; }") == 2
    # PROVEN natively: snappyHexMesh names a region patch after the REGION, not <surface>_<region>.
    # A pattern built on the surface name matched nothing and OpenFOAM added no layers at all -
    # silently, since it still exits 0. One entry per real patch name is what attaches them.
    assert "layers { fluid { nSurfaceLayers" in text and "wall { nSurfaceLayers" in text
    assert "body_" not in text


def test_a_surface_without_regions_renders_the_dict_it_always_did():
    text = _dict_for(None)
    assert "regions {" not in text
    assert "layers { body {" in text
    assert "geometry { body.stl { type triSurfaceMesh; name body; } }" in text
