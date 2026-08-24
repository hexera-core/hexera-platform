# Responsibility: Verify intake captures internal-flow port declarations safely: names the
# engines can build verbatim, sizes/locations sufficient for unambiguous binding, and a refusal
# (a question to the user) whenever the declaration could only be bound by guessing.
from __future__ import annotations


def _errs(**over) -> list[str]:
    from meshpipeline.agents.intake.validation import validate_submission
    base = {
        "domain": "hydraulic manifold internal flow",
        "request_txt": ("A complete requirements summary covering the geometry, the simulation "
                        "type, every confirmed parameter and the mesh requirements. " * 2),
        "review_brief_txt": ("Acceptance criteria: a valid mesh, the correct regions, no fatal "
                             "defects, sizing at the builder's discretion. " * 2),
        "mesh_engine": "snappy", "mesh_fidelity": "standard", "engine_source": "user_direct",
        "purpose": "internal_cfd", "input_kind": "solid-body", "engine_params": {},
        "dimensionality": "3D",
    }
    base.update(over)
    return validate_submission(base)


def _port_errs(patches) -> list[str]:
    return [e for e in _errs(patches=patches) if "patches[" in e or "port" in e.lower()]


WYE = [
    {"name": "pipe_wall", "type": "wall"},
    {"name": "inlet_1", "type": "inlet", "diameter_mm": 40,
     "interchangeable_with": ["inlet_2"]},
    {"name": "inlet_2", "type": "inlet", "diameter_mm": 40,
     "interchangeable_with": ["inlet_1"]},
    {"name": "outlet", "type": "outlet", "diameter_mm": 60},
]


class TestAWellStatedDeclarationPassesWithNoQuestions:
    def test_the_wye_declaration_is_accepted(self):
        assert _port_errs(WYE) == []

    def test_locations_can_stand_in_for_sizes(self):
        patches = [
            {"name": "wall", "type": "wall"},
            {"name": "feed", "type": "inlet", "near_mm": [0, 0, 0]},
            {"name": "drain", "type": "outlet", "near_mm": [400, 0, 0]},
        ]
        assert _port_errs(patches) == []

    def test_external_flow_needs_none_of_this(self):
        e = _errs(purpose="external_cfd", input_kind="body-surface",
                  patches=[{"name": "body", "type": "wall"},
                           {"name": "farfield", "type": "farfield"}])
        assert not any("size" in x.lower() or "location" in x.lower() for x in e), e


class TestUnbuildableNamesAreRefusedAtIntake:
    def test_a_hyphenated_name_is_refused_with_the_legal_form(self):
        e = _port_errs([{"name": "pipe_wall", "type": "wall"},
                        {"name": "inlet-1", "type": "inlet", "diameter_mm": 40},
                        {"name": "outlet", "type": "outlet", "diameter_mm": 60}])
        assert any("inlet-1" in x and "letter" in x.lower() for x in e), e

    def test_a_digit_leading_name_is_refused(self):
        e = _port_errs([{"name": "pipe_wall", "type": "wall"},
                        {"name": "2in_feed", "type": "inlet", "diameter_mm": 40},
                        {"name": "outlet", "type": "outlet", "diameter_mm": 60}])
        assert any("2in_feed" in x for x in e), e

    def test_reserved_names_are_refused(self):
        e = _port_errs([{"name": "outer", "type": "wall"},
                        {"name": "inlet", "type": "inlet", "diameter_mm": 40},
                        {"name": "outlet", "type": "outlet", "diameter_mm": 60}])
        assert any("outer" in x and "reserved" in x.lower() for x in e), e


class TestAmbiguousDeclarationsBecomeQuestions:
    def test_a_port_with_no_size_and_no_location_asks_for_one(self):
        e = _port_errs([{"name": "wall", "type": "wall"},
                        {"name": "feed", "type": "inlet"},
                        {"name": "drain", "type": "outlet", "diameter_mm": 60}])
        assert any("feed" in x and ("size" in x.lower() or "location" in x.lower())
                   for x in e), e

    def test_samesize_twins_without_confirmation_ask_which_is_which(self):
        twins = [
            {"name": "pipe_wall", "type": "wall"},
            {"name": "inlet_1", "type": "inlet", "diameter_mm": 40},
            {"name": "inlet_2", "type": "inlet", "diameter_mm": 40},
            {"name": "outlet", "type": "outlet", "diameter_mm": 60},
        ]
        e = _port_errs(twins)
        assert any("inlet_1" in x and "inlet_2" in x for x in e), e
        assert any("interchangeable" in x.lower() or "location" in x.lower() for x in e), e

    def test_sizes_too_close_to_separate_ask_for_locations(self):
        # 40 vs 45 mm: area ratio 1.27, inside the binder's inseparable band - a counterbore
        # or nominal-vs-bore drift can swap them silently, so intake must collect locations
        close = [
            {"name": "pipe_wall", "type": "wall"},
            {"name": "feed", "type": "inlet", "diameter_mm": 40},
            {"name": "drain", "type": "outlet", "diameter_mm": 45},
        ]
        e = _port_errs(close)
        assert any("40" in x and "45" in x for x in e), e
        assert any("location" in x.lower() for x in e), e

    def test_well_separated_sizes_ask_nothing(self):
        ok = [
            {"name": "pipe_wall", "type": "wall"},
            {"name": "feed", "type": "inlet", "diameter_mm": 40},
            {"name": "drain", "type": "outlet", "diameter_mm": 60},
        ]
        assert _port_errs(ok) == []


class TestMalformedPortFieldsAreRefused:
    def test_two_sizing_forms_on_one_port(self):
        e = _port_errs([{"name": "wall", "type": "wall"},
                        {"name": "feed", "type": "inlet", "diameter_mm": 40, "area_mm2": 1257},
                        {"name": "drain", "type": "outlet", "diameter_mm": 60}])
        assert any("feed" in x and "one size" in x.lower() for x in e), e

    def test_near_mm_must_be_three_numbers(self):
        e = _port_errs([{"name": "wall", "type": "wall"},
                        {"name": "feed", "type": "inlet", "near_mm": [0, 0]},
                        {"name": "drain", "type": "outlet", "diameter_mm": 60}])
        assert any("near_mm" in x for x in e), e

    def test_interchangeable_with_must_name_a_declared_patch(self):
        e = _port_errs([{"name": "wall", "type": "wall"},
                        {"name": "feed", "type": "inlet", "diameter_mm": 40,
                         "interchangeable_with": ["ghost"]},
                        {"name": "drain", "type": "outlet", "diameter_mm": 60}])
        assert any("ghost" in x for x in e), e

    def test_a_nonpositive_size_is_refused(self):
        e = _port_errs([{"name": "wall", "type": "wall"},
                        {"name": "feed", "type": "inlet", "diameter_mm": -40},
                        {"name": "drain", "type": "outlet", "diameter_mm": 60}])
        assert any("feed" in x and "positive" in x.lower() for x in e), e
