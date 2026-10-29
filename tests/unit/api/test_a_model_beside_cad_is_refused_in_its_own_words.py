"""A thermal model uploaded beside CAD is told what to do, not that it "is not a CAD solid".

WHY THIS EXISTS. The first real customer's input is two files: `ColdPlateAssembly_CLEAN.step` and
`chip.ecxml` (Corintis, 2026-10-29). Uploaded together they hit the assembly path, which joins
every file into one STEP, and an ECXML earned:

    Several files are joined into one assembly only when each is a CAD solid (STEP, IGES or BREP):
    chip.ecxml is not. Upload it on its own, or export the whole assembly as one STEP file.

Every word of that is true and it is the wrong thing to say. An ECXML is not a part that failed to
be CAD: it is a MODEL - parts, their materials, their powers, fans and vents - and "export the
whole assembly as one STEP file" is advice to throw the physics away. The refusal now says which
of the two files to upload, and what each one gets you.

CARRYING THE TWO TOGETHER IS THE FEATURE THIS STANDS IN FOR - the STEP meshed, the ECXML's powers
and materials filling in the solver case's blanks - and the refusal deliberately does not promise
it, because the upload route cannot keep that promise today.
"""
import pytest

from meshpipeline.cad.ingest.assembly_files import CAD_SUFFIXES, NOT_CAD, NOT_CAD_MODEL
from meshpipeline.contracts.intake_formats import format_for_suffix


def test_a_model_format_is_the_one_told_apart():
    """The discriminator is the format's OWN check mode, not a list of suffixes kept here: a
    format that describes a model has one, and a file of triangles or faces does not."""
    assert format_for_suffix(".ecxml").check_mode == "thermal"
    assert format_for_suffix(".xml").check_mode == "thermal", (
        "an Icepak export is <name>_ec.xml and is the same thermal model")
    for suffix in (".step", ".stl", ".obj", ".iges"):
        assert not format_for_suffix(suffix).check_mode, suffix


def test_the_model_refusal_says_which_file_to_upload():
    said = NOT_CAD_MODEL.format(name="chip.ecxml")
    assert "chip.ecxml" in said
    assert "on its own" in said
    assert "thermal model" in said


def test_it_does_not_tell_them_to_export_the_physics_away():
    """What the old sentence said, and the reason this one exists."""
    said = NOT_CAD_MODEL.format(name="chip.ecxml")
    assert "one STEP file" not in said
    assert "is not" not in said, "a model is not a part that failed a test"


def test_it_promises_nothing_the_upload_cannot_do():
    """Carrying the pair together is not built. A refusal that hinted it was would be worse than
    one that refuses."""
    said = NOT_CAD_MODEL.format(name="chip.ecxml").lower()
    assert "cannot be uploaded together yet" in said
    for promise in ("will", "soon", "support", "coming"):
        assert promise not in said


def test_a_file_that_really_is_not_cad_keeps_the_old_words():
    """An STL among STEPs is a part in the wrong format, which is what NOT_CAD is about."""
    said = NOT_CAD.format(name="bracket.stl")
    assert "CAD solid" in said and "bracket.stl" in said


def test_the_cad_list_is_unchanged():
    assert ".step" in CAD_SUFFIXES and ".ecxml" not in CAD_SUFFIXES


#   -- the route picks between them --------------------------------------------------------------

def test_the_upload_route_chooses_by_the_format_s_check_mode():
    import inspect

    from meshpipeline.api.v1 import upload

    src = inspect.getsource(upload._joined_assembly)
    assert "NOT_CAD_MODEL" in src
    assert "_refusal(" in src
    assert "check_mode" in src, "the discriminator is the format's own, not a second suffix list"
    assert src.count("_refusal(") >= 4, (
        "every place a file is refused for not being CAD has to choose between the two: the first "
        "file, each later one by its name, and each again after checked_upload renames it")


@pytest.mark.parametrize("name, suffix, model", [
    ("chip.ecxml", ".ecxml", True),
    # An Icepak export is `<name>_ec.xml`, and the registry knows .xml as the same thermal model
    # (see the upload route's own format list). So it earns the model sentence too, which is the
    # right answer and not what this test first asserted.
    ("chip_ec.xml", ".xml", True),
    ("bracket.stl", ".stl", False),
    ("body.step", ".step", False),
    ("", "", False),
])
def test_which_sentence_each_file_earns(name, suffix, model):
    """The route's own helper, exercised through the same registry it reads."""
    fmt = format_for_suffix(suffix)
    chose_model = fmt is not None and bool(getattr(fmt, "check_mode", ""))
    assert chose_model is model, f"{name or '(unnamed)'} {suffix}"
