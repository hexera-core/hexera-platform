# Responsibility: Verify the part's name read from the file (cad/part_name): a STEP's own product name, else the upload's file name, never an exporter's placeholder - the name every stage shows when no model names the part (an FEA part).
# Boundaries: pure over small files written here; no model, no store.
from __future__ import annotations

from meshpipeline.cad import part_name as pn

STEP = """ISO-10303-21;
HEADER;
FILE_NAME('x.step','2026-10-22',(''),(''),'','','');
ENDSEC;
DATA;
#1=PRODUCT('{name}','{name}','',(#2));
ENDSEC;
END-ISO-10303-21;
"""


def test_a_step_is_named_by_its_own_product(tmp_path):
    f = tmp_path / "upload.step"
    f.write_text(STEP.format(name="Mounting Bracket"))
    assert pn.part_name(f, "bracket_v2.step") == "Mounting Bracket"


def test_a_placeholder_product_falls_back_to_the_file_name(tmp_path):
    f = tmp_path / "upload.stp"
    f.write_text(STEP.format(name="Part1"))
    assert pn.part_name(f, "C:\\cad\\gear_housing-rev3.STP") == "gear housing rev3"


def test_any_other_file_is_named_after_itself():
    assert pn.part_name(None, "pump volute.stl") == "pump volute"
    assert pn.part_name(None, "assembly.step.gz") == ""            # a placeholder name: nothing
    assert pn.part_name(None, "") == ""


def test_the_fea_proposal_carries_the_files_name():
    from meshpipeline.application.geometry_check import _proposal

    facts = {"input_kind": "solid-body", "flow": "external", "openings": [], "size_mm": [10, 10, 10],
             "part_name": "Mounting Bracket"}
    assert _proposal(facts, {})["part"] == "Mounting Bracket"            # no model asked (FEA)
    assert _proposal(facts, {"part": "a bracket"})["part"] == "a bracket"   # the pictures' name wins
