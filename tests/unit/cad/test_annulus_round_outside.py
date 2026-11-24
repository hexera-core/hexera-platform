# Responsibility: Verify INS-20 - a part is read as the fluid of an annulus round an undrawn hub only when its ring
# ends are round outside as well as in: an annular passage's fluid (casing round a hub) is; a block drilled through
# with a side port, and a square bar bored through, are bodies with bores (batch 4: an intake block and a valve body
# were read as fluid volumes and their bores' ends as annulus openings).
# Boundaries: real OpenCASCADE solids written to STEP; the geometry check's own CAD scout; no model.
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_flat_holes import _write

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(r, h, base=(0.0, 0.0, 0.0), d=(0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*d)), r, h).Shape()


def _scout(shape, path: Path):
    from meshpipeline.application import geometry_check as gc
    facts, _ = gc._scout_exact(_write(shape, path), path.parent, None, _REF)
    return facts


def test_the_fluid_of_an_annular_passage_is_read_as_annular_fluid(tmp_path):
    # blade_row_passage_001's annulus, no blades: a casing of 226.65 mm round a hub of 134.44 mm, 232 mm long
    ring = BRepAlgoAPI_Cut(_cyl(226.65, 232.0), _cyl(134.44, 234.0, (0, 0, -1))).Shape()
    facts = _scout(ring, tmp_path / "annulus.step")
    assert facts.get("body_kind") == "annular_fluid" and facts.get("input_kind") == "fluid-domain"


def test_a_block_drilled_through_with_a_side_port_is_a_body(tmp_path):
    # the intake block's shape: 19 x 19 x 38 mm, a 12.7 mm bore through its length, a 12.7 mm port in one side
    block = BRepPrimAPI_MakeBox(gp_Pnt(-9.5, -9.5, 0.0), 19.0, 19.0, 38.0).Shape()
    block = BRepAlgoAPI_Cut(block, _cyl(6.35, 40.0, (0, 0, -1))).Shape()
    block = BRepAlgoAPI_Cut(block, _cyl(6.35, 12.0, (0, 0, 19.0), (1, 0, 0))).Shape()
    facts = _scout(block, tmp_path / "block.step")
    assert facts.get("body_kind") != "annular_fluid" and facts.get("input_kind") != "fluid-domain", facts.get("body_kind")


def test_a_square_bar_bored_through_is_a_body(tmp_path):
    # two coaxial ring ends, as an annulus has, but the outside is a square: thick at the corners, thin at the sides
    bar = BRepPrimAPI_MakeBox(gp_Pnt(-15.0, -15.0, 0.0), 30.0, 30.0, 60.0).Shape()
    bar = BRepAlgoAPI_Cut(bar, _cyl(10.0, 62.0, (0, 0, -1))).Shape()
    facts = _scout(bar, tmp_path / "bar.step")
    assert facts.get("body_kind") != "annular_fluid" and facts.get("input_kind") != "fluid-domain", facts.get("body_kind")
