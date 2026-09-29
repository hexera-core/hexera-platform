# Responsibility: Verify the STEP length unit is read from the file's own text - every way the real
# corpus spells it (plain SI metres, prefixed SI, an inch defined against millimetres or metres, a
# "METRE" or "MILLIMETRE" defined as one of itself) - and that an entity that does not parse is
# reported, never read as OpenCASCADE's metre fallback.
# Boundaries: small hand-written STEP texts; no OpenCASCADE.
from __future__ import annotations

import pytest

from meshpipeline.cad.step_units import declared_lengths
from meshpipeline.cad.unit_evidence import read_declared_unit, unit_of_scale
from meshpipeline.contracts.geometry_units import LengthUnit

_HEAD = "ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
_TAIL = "ENDSEC;\nEND-ISO-10303-21;\n"


def _step(tmp_path, body: str, name: str = "part.step"):
    p = tmp_path / name
    p.write_text(_HEAD + body + _TAIL)
    return p


def _context(unit_ref: str, ctx: int = 12) -> str:
    return (f"#{ctx}=(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#11))"
            f"GLOBAL_UNIT_ASSIGNED_CONTEXT(({unit_ref},#7,#10))REPRESENTATION_CONTEXT('ID2','3'));\n"
            "#7=(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.));\n"
            "#10=(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT());\n")


# the spellings the corpus uses, each with the file it came from
CASES = [
    # ANSA's DrivAer export and the T106 cascade: plain metres
    ("#6=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));\n", LengthUnit.metre, "metre"),
    # nearly everything else: prefixed SI millimetres
    ("#6=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n", LengthUnit.millimetre, "millimetre"),
    # the mounting bracket
    ("#6=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.CENTI.,.METRE.) );\n", LengthUnit.centimetre, "centimetre"),
    # the CRM and DLR-F6: an inch defined as 25.4 mm
    ("#41=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
     "#44=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#41);\n"
     "#6=(CONVERSION_BASED_UNIT('INCH',#44)LENGTH_UNIT()NAMED_UNIT(#45));\n", LengthUnit.inch, "inch"),
    # OpenCASCADE's own writer: the measure bare, not typed
    ("#349=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) );\n"
     "#348 = LENGTH_MEASURE_WITH_UNIT(25.4,#349);\n"
     "#6 = ( CONVERSION_BASED_UNIT('INCH',#348) LENGTH_UNIT() NAMED_UNIT(\n#347) );\n", LengthUnit.inch, "inch"),
    # the rocket nozzle: an inch defined as 0.0254 m
    ("#41=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));\n"
     "#44=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(0.0254),#41);\n"
     "#6=(CONVERSION_BASED_UNIT('INCH',#44)LENGTH_UNIT()NAMED_UNIT(#45));\n", LengthUnit.inch, "inch"),
    # the gmsh wing-fuselage: a "METRE" defined as 1 x m - read as millimetres before
    ("#41=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));\n"
     "#44=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.),#41);\n"
     "#6=(CONVERSION_BASED_UNIT('METRE',#44)LENGTH_UNIT()NAMED_UNIT(#45));\n", LengthUnit.metre, "metre"),
    # the FDA pump: spaces everywhere, a "MILLIMETRE" defined as 1 x mm
    ("#6 =  ( CONVERSION_BASED_UNIT( 'MILLIMETRE', #59 )LENGTH_UNIT(  )NAMED_UNIT( #62 ) );\n"
     "#59 = LENGTH_MEASURE_WITH_UNIT( LENGTH_MEASURE( 1.00000000000000 ), #74 );\n"
     "#74 =  ( NAMED_UNIT( #62 )LENGTH_UNIT(  )SI_UNIT( .MILLI., .METRE. ) );\n", LengthUnit.millimetre, "millimetre"),
]


@pytest.mark.parametrize("units,expected,name", CASES)
def test_every_spelling_the_corpus_uses_is_read(tmp_path, units, expected, name):
    p = _step(tmp_path, units + _context("#6"))
    ev = read_declared_unit(p)
    assert ev.resolved and ev.unit is expected
    assert ev.detail == f"declared in the file as {name}"


@pytest.mark.parametrize("entity,problem", [
    ("SI_UNIT(.NOPE.,.METRE.)", "prefix"),        # what OCC reads as its metre fallback
    ("SI_UNIT($,$)", "not the metre"),
    ("SI_UNIT(.MILLI.,.GRAM.)", "not the metre"),
])
def test_an_entity_that_does_not_parse_is_reported_never_read_as_metres(tmp_path, entity, problem):
    p = _step(tmp_path, f"#6=(LENGTH_UNIT()NAMED_UNIT(*){entity});\n" + _context("#6"))
    found = declared_lengths(p)
    assert len(found) == 1 and found[0].metres is None and problem in found[0].problem
    ev = read_declared_unit(p)
    assert not ev.resolved and "malformed" in ev.detail


def test_a_unit_we_do_not_carry_is_asked_not_rounded(tmp_path):
    feet = ("#41=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));\n"
            "#44=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(0.3048),#41);\n"
            "#6=(CONVERSION_BASED_UNIT('FOOT',#44)LENGTH_UNIT()NAMED_UNIT(#45));\n")
    ev = read_declared_unit(_step(tmp_path, feet + _context("#6")))
    assert not ev.resolved and "unsupported length unit 'foot'" in ev.detail
    assert unit_of_scale(0.3048) is None and unit_of_scale(0.0254) is LengthUnit.inch


def test_an_instance_is_found_by_its_own_number_not_a_longer_one_or_a_reference(tmp_path):
    # #6 is referenced before it is defined, and #61 (a different unit) starts with "#6"
    body = ("#61=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.CENTI.,.METRE.));\n"
            "#99=SOMETHING('a name; with a semicolon',#6);\n"
            "#6=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));\n")
    found = declared_lengths(_step(tmp_path, body + _context("#6")))
    assert [(f.metres, f.name) for f in found] == [(1.0, "metre")]


def test_contexts_that_agree_are_one_unit_and_a_file_with_none_says_so(tmp_path):
    same = ("#6=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n" + _context("#6")
            + _context("#6", ctx=13))
    assert [f.metres for f in declared_lengths(_step(tmp_path, same))] == [0.001]
    none = _step(tmp_path, "#1=CARTESIAN_POINT('',(0.,0.,0.));\n", name="bare.step")
    assert declared_lengths(none) == []
    assert "declares no length unit" in read_declared_unit(none).detail
