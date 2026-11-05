# Responsibility: Verify the scout reads a FILE OF LOOSE SURFACES for what the surfaces describe -
#                 a set that closes a box is a body's skin in a flow, a long skin with two small
#                 flat ends is still an open passage.
# Boundaries: the native tier, because only here does the CAD kernel exist. Real STEP files, no
#             model, no rendering; nothing is asserted about meshing or staging.
from __future__ import annotations

from pathlib import Path

import pytest
from tests.cad_fixtures import write_loose_faces_step

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")


def _scout(path: Path):
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )

    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=1e-3,
        basis=ResolutionBasis.file_declared, evidence="test")
    return scout_cad(path, prepared=from_occ_transfer(interp, parser_applied_unit(path)))


def _loose_faces_of(shape, path: Path) -> Path:
    """The same unjoined export `write_loose_faces_step` makes, for any shape: every face added to
    a compound on its own, so the file carries no shell and no solid."""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from tests.cad_fixtures import _write_step

    builder = BRep_Builder()
    loose = TopoDS_Compound()
    builder.MakeCompound(loose)
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        builder.Add(loose, TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return _write_step(loose, path, "MM")


def _loose_pipe_skin_step(path: Path) -> Path:
    """A GENUINE loose-surface passage: the skin of a 80 mm x 300 mm cylinder, its curved side and
    its two flat ends unjoined. Its flats cover about a tenth of its box, the signature of a duct."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    cylinder = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(1.0, 0.0, 0.0)), 40.0, 300.0).Shape()
    return _loose_faces_of(cylinder, path)


def test_loose_faces_that_close_a_box_are_a_body_in_a_flow_with_no_openings(tmp_path):
    """Six unjoined faces of a cube are a cube, not a six-mouthed passage. Before this rule the
    scout read them as a multi-outlet internal flow and proposed EVERY face as an opening, which
    left the staged wall with no faces at all and killed the mesh."""
    r = _scout(write_loose_faces_step(tmp_path / "loose.step"))
    assert r.solids == 0, "the fixture must still arrive with no solid"
    assert (r.body_kind, r.flow) == ("surface", "external")
    assert r.input_kind == "solid-body"
    assert r.openings == [], f"a closed box proposes no openings; got {[o.name for o in r.openings]}"
    assert any("close a box" in n for n in r.notes), r.notes


def test_a_loose_pipe_skin_is_still_an_open_passage_with_its_two_flat_ends(tmp_path):
    """The guard keys on flat share alone, so it must not swallow a real duct skin: a long
    cylinder's two ends cover about a tenth of its box and stay the passage's mouths."""
    r = _scout(_loose_pipe_skin_step(tmp_path / "skin.step"))
    assert r.solids == 0
    assert (r.body_kind, r.input_kind, r.flow) == ("surface", "body-surface", "internal")
    assert [o.role for o in r.openings] == ["inlet", "outlet"]
    assert r.confidence["input_kind"] == 0.4
    assert any("not a closed solid" in n for n in r.notes), r.notes
