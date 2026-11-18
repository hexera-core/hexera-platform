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


ASSEMBLY = """ISO-10303-21;
HEADER;
FILE_NAME('x.step','2026-11-16',(''),(''),'','','');
ENDSEC;
DATA;
#10=PRODUCT('Housing Mount','Housing Mount','',(#2));
#11=PRODUCT_DEFINITION_FORMATION('','',#10);
#12=PRODUCT_DEFINITION('design','',#11,#3);
#20=PRODUCT('Shaft','Shaft','',(#2));
#21=PRODUCT_DEFINITION_FORMATION('','',#20);
#22=PRODUCT_DEFINITION('design','',#21,#3);
#30=PRODUCT('{root}','{root}','',(#2));
#31=PRODUCT_DEFINITION_FORMATION('','',#30);
#32=PRODUCT_DEFINITION('design','',#31,#3);
#40=NEXT_ASSEMBLY_USAGE_OCCURRENCE('1','Housing Mount','',#32,#12,$);
#41=NEXT_ASSEMBLY_USAGE_OCCURRENCE('2','Shaft','',#32,#22,$);
ENDSEC;
END-ISO-10303-21;
"""


def test_an_assembly_is_named_by_its_root_never_its_first_part(tmp_path):
    # a 10-part assembly's run was labelled after "Housing Mount", the first PRODUCT in its file (16 Nov)
    f = tmp_path / "upload.step"
    f.write_text(ASSEMBLY.format(root="Gearbox Assembly"))
    assert pn.part_name(f, "gearbox.step") == "Gearbox Assembly"


def test_an_unnamed_assembly_root_takes_the_file_name_never_a_part(tmp_path):
    f = tmp_path / "upload.step"
    f.write_text(ASSEMBLY.format(root="Assembly1"))
    assert pn.part_name(f, "gearbox_rev2.step") == "gearbox rev2"
