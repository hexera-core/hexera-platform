"""A structural request is never refused for the model calling a surface file a "body-surface".

dev-areen, 12 Nov 2026: on a surface STEP the intake model declared (gmsh, structural, body-surface) and
the gate answered "impossible", so the user was told the part "requires a Solid body or Planar domain".
"""
from meshpipeline.agents.intake.executor import fea_part_kind
from meshpipeline.agents.intake.validation import preview_admission


def test_structural_body_surface_becomes_the_part():
    out = fea_part_kind({"purpose": "structural", "input_kind": "body-surface", "x": 1})
    assert out == {"purpose": "structural", "input_kind": "solid-body", "x": 1}


def test_structural_solid_assembly_becomes_the_part():
    out = fea_part_kind({"purpose": "structural", "input_kind": "solid-assembly"})
    assert out == {"purpose": "structural", "input_kind": "solid-body"}
    prev = preview_admission("gmsh", out["purpose"], out["input_kind"], dimensionality="3D")
    assert prev["verdict"] != "impossible", prev


def test_any_other_word_for_a_structural_part_becomes_the_part():
    for kind in ("fluid-domain", "some-new-kind", "body-surface", "solid-assembly"):
        out = fea_part_kind({"purpose": "structural", "input_kind": kind})
        assert out["input_kind"] == "solid-body", kind
    assert fea_part_kind({"purpose": "structural", "input_kind": ""})["input_kind"] == ""


def test_a_thermal_assembly_is_left_alone():
    args = {"purpose": "conjugate_heat_transfer", "input_kind": "solid-assembly"}
    assert fea_part_kind(dict(args)) == args


def test_other_kinds_and_purposes_are_untouched():
    for args in ({"purpose": "structural", "input_kind": "planar-domain"},
                 {"purpose": "structural", "input_kind": "solid-body"},
                 {"purpose": "internal_cfd", "input_kind": "body-surface"},
                 {"purpose": "external_cfd", "input_kind": "body-surface"},
                 {"input_kind": "body-surface"}, {}):
        assert fea_part_kind(dict(args)) == args
    assert fea_part_kind(None) is None


def test_the_gate_refused_the_old_kind_and_admits_the_new_one():
    before = preview_admission("gmsh", "structural", "body-surface", dimensionality="3D")
    assert before["verdict"] == "impossible"
    args = fea_part_kind({"purpose": "structural", "input_kind": "body-surface"})
    after = preview_admission("gmsh", args["purpose"], args["input_kind"], dimensionality="3D")
    assert after["verdict"] != "impossible", after
