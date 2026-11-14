"""The step that names the openings is told what the file calls its own parts.

WHY THIS EXISTS. The naming step is a vision model looking at rendered pictures with numbered
stickers, and its system prompt says in its own words that "the numbers are the only names you may
use". The assembly's part names were read from the STEP (cad/fea_assembly.read_body_names, through
cad/solid_parts.part_skins), drawn on the stage and listed to the customer - and never put in
front of the one step that needs them.

MEASURED on ColdPlateAssembly_CLEAN.step, job ca246a07, 2026-11-13. The file names its six solids
Microfluidics_Core, Top_Lid, Outlet, Splits, Board_cooling, Inlet. Sticker 2 sits on Inlet's +x end
face and sticker 3 on Outlet's. What came back was inlet, outlet, outlet - with the inlet on
sticker 1, an 11.8 mm junction sealed inside the assembly that reaches no boundary at all, and
both real mouths called outlets. Correcting it by hand is what the stage is for; proposing it from
the file's own words is what this adds.

These tests are about the SEAM - that the names are collected and that they reach the text the
model reads. What the model then does with them is not something a unit test can pin.
"""
from meshpipeline.application.geometry_check import _facts_text

#: The six solids of the customer file, in the file's own order.
REAL = ["Microfluidics_Core", "Top_Lid", "Outlet", "Splits", "Board_cooling", "Inlet"]


class _Shot:
    name = "iso"
    facing = (1, 2, 3)
    direction = (0, 0, -1)
    path = "iso.png"


def _facts(**over):
    f = {"size_mm": [178, 42, 101], "input_kind": "solid-assembly", "body_kind": "single_solid",
         "flow": "internal", "openings": [], "confidence": {}}
    f.update(over)
    return f


def test_the_files_own_part_names_reach_the_model():
    said = _facts_text(_facts(part_names=REAL), [_Shot()], "")
    for name in REAL:
        assert name in said, f"{name} never reaches the step that names the openings"


def test_they_are_offered_as_evidence_and_not_as_an_instruction():
    """A part name can be generic, stale or wrong, and the pictures are what is being read."""
    said = _facts_text(_facts(part_names=REAL), [_Shot()], "")
    assert "the pictures decide" in said
    assert "generic, stale or wrong" in said


def test_a_file_that_names_nothing_adds_no_line():
    said = _facts_text(_facts(), [_Shot()], "")
    assert "names its own solids" not in said


def test_the_names_are_collected_from_the_drawing_the_stage_already_makes():
    """`Part_k` is what cad/solid_parts gives a solid the file does NOT name, so it carries no
    information; a real name beside it still does."""
    patches = [{"name": "Part_1"}, {"name": "Inlet"}, {"name": "Part_12"}, {"name": ""},
               {"name": "Outlet"}]
    named = [n for n in (str(q.get("name") or "").strip() for q in patches)
             if n and not (n.startswith("Part_") and n[5:].isdigit())]
    assert named == ["Inlet", "Outlet"]


def test_the_collector_in_the_check_is_the_same_rule():
    """A source check, because the collection happens inside a long function with a workspace and
    an object store around it. If the rule moves, this says so rather than silently diverging."""
    import inspect

    from meshpipeline.application import geometry_check as GC

    src = inspect.getsource(GC)
    assert 'facts["part_names"] = _named[:32]' in src
    assert 'n.startswith("Part_") and n[5:].isdigit()' in src
    # taken BEFORE the drawing is discarded for a solid it could not tessellate
    assert src.index('facts["part_names"]') < src.index("drawn_parts = None\n")
