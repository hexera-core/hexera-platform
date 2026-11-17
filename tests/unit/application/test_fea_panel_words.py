# Responsibility: Verify the few words the FEA stage's sleek panel shows in one line: the clean check ("Clean", a part
# that may have self-crossing faces "- we'll still try", "Not a closed solid") with its full words kept for the
# fold under it, the panel's title (an assembly is named after its file, never after its first body), and what
# stays solid in a shell in a few words (the full sentence kept for the tooltip).
# Boundaries: pure over facts and summaries built here; nothing is stored, no CAD is read.
from __future__ import annotations

from meshpipeline.application import fea_check as F
from meshpipeline.application import shell_check as SC
from meshpipeline.cad.fea_assembly import FACT_KEY as ASM_KEY
from meshpipeline.cad.fea_measure import FACT_KEY

CLEAN = {"clean": True, "summary": "The geometry is clean."}


def _asm(n: int = 3, suspects: list | None = None, **extra) -> dict:
    parts = [{"id": f"Part_{k}", "name": f"Bracket {k}"} for k in range(1, n + 1)]
    return {"solids": n, "source_format": "step", "validity": dict(CLEAN),
            FACT_KEY: {ASM_KEY: {"parts": parts, "interfaces": []}, "suspects": suspects or []}, **extra}


def test_a_clean_part_is_one_word_with_nothing_to_open():
    v = F.fea_view({"solids": 1, "source_format": "step", "validity": dict(CLEAN)})
    assert v["clean_short"] == "Clean" and v["clean_details"] == []


def test_a_suspect_part_is_named_in_one_line_and_its_words_open_under_it():
    v = F.fea_view(_asm(3, [{"kind": "self_overlap", "body": 2, "faces": ["Face_4", "Face_9"], "confirmed": True}]))
    assert v["clean_short"] == "Bracket 2 may have self-crossing faces - we'll still try"
    assert any("Face_4" in d for d in v["clean_details"])
    assert F.TRY_ANYWAY not in v["clean_details"]           # the line already says we'll still try
    assert v["clean_summary"]                                # the long words are still served whole


def test_several_suspect_parts_are_counted_and_a_fold_is_said_as_one():
    sus = [{"kind": "self_overlap", "body": 1, "faces": ["Face_1"]}, {"kind": "self_overlap", "body": 3, "faces": ["Face_2"]}]
    assert F.fea_view(_asm(3, sus))["clean_short"] == "2 parts may have self-crossing faces - we'll still try"
    one = F.fea_view({"solids": 1, "source_format": "step", "validity": dict(CLEAN),
                      FACT_KEY: {"suspects": [{"kind": "fold", "body": 1, "faces": ["Face_1", "Face_2"]}]}})
    assert one["clean_short"] == "The part may have a paper-thin fold - we'll still try"


def test_a_part_the_check_could_not_judge_says_so():
    v = F.fea_view({"solids": 1, "source_format": "step", "validity": {"kind": "unchecked"}})
    assert v["clean_short"] == "Not checked" and v["clean_details"]


def test_an_assembly_is_titled_by_its_file_and_a_single_part_leaves_the_title_to_its_name():
    assert F.fea_view(_asm(11, file_name="frame assembly"))["title"] == "frame assembly"
    assert F.fea_view(_asm(11))["title"] == ""               # no file name: the stage says "Your part"
    single = F.fea_view({"solids": 1, "source_format": "step", "validity": dict(CLEAN), "file_name": "bracket v2"})
    assert single["title"] == ""                            # a single part keeps its own name


def test_what_stays_solid_is_said_in_a_few_words():
    why = ("no wall is thin against its size: its most plate-like wall is 4 mm thick and spans about 38.9 mm, only "
           "9.7 times its thickness; a shell needs at least 10 times")
    summary = {"bodies": [{"body": "Part_1", "verdict": "solid"}, {"body": "Part_2", "verdict": "shell"}],
               "solid_regions": [{"body": "Part_1", "whole": True, "why": why},
                                 {"body": "Part_2", "whole": False, "faces": ["Face_3"], "n_faces": 3, "thick_mm": 30.0}],
               "open_edges": 1, "open_places": [{"at_mm": [0, 0, 0]}]}
    rows = SC.not_shelled(summary)
    assert rows[0]["short"] == "Part_1 · too thick (4 mm wall, 9.7× span)"
    assert rows[0]["words"].startswith("Part_1 stays solid")      # the full sentence is kept
    assert rows[1]["short"] == "Part_2 · 30 mm+ thick, 3 faces"
    assert [r["short"] for r in rows[2:]] == ["Plates don't meet at 1 place (red)", "plates don't meet"]
    assert SC.solid_short("its most plate-like wall is 6 mm thick but only 12 mm wide, 2 times its thickness") \
        == "too narrow (6 mm wall, 2× width)"
    assert SC.solid_short("something else") == ""
