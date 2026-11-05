# Responsibility: Verify a CAD file is read as what it is, not as what its staged name says.
# Staging copies every CAD upload to geometry.step; an IGES upload then met the STEP reader and every cfMesh internal
# IGES case of the lab test set stopped with "OpenCASCADE could not read CAD file: geometry.step" (2026-10-04).
from __future__ import annotations

from meshpipeline.cad.cad_tessellate import is_iges

_IGES_START = ("IGES file written by a CAD system".ljust(72) + "S      1\n"
               + "1H,,1H;,".ljust(72) + "G      1\n")
_STEP_START = "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('x'),'2;1');\n"


def test_an_iges_file_under_a_step_name_is_iges(tmp_path):
    p = tmp_path / "geometry.step"
    p.write_text(_IGES_START)
    assert is_iges(p)


def test_a_step_file_under_an_iges_name_is_step(tmp_path):
    p = tmp_path / "part.igs"
    p.write_text(_STEP_START)
    assert not is_iges(p)


def test_the_name_decides_only_when_the_content_says_neither(tmp_path):
    (tmp_path / "a.iges").write_text("?")
    (tmp_path / "a.step").write_text("?")
    assert is_iges(tmp_path / "a.iges") and not is_iges(tmp_path / "a.step")
    assert not is_iges(tmp_path / "missing.step")
