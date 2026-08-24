# Responsibility: Verify intake pins the domain request as TYPED fields - the extents the user
# asked for, the ruler they are measured in, and whether requirements are strict - so the domain
# gate compares numbers the user approved instead of regex-parsing prose, and a re-plan can
# never change the measuring stick.
from __future__ import annotations


def _errs(**over) -> list[str]:
    from meshpipeline.agents.intake.validation import validate_submission
    base = {
        "domain": "external aero over a blunt body",
        "request_txt": ("A complete requirements summary covering the geometry, the simulation "
                        "type, every confirmed parameter and the mesh requirements. " * 2),
        "review_brief_txt": ("Acceptance criteria: a valid mesh, the correct regions, no fatal "
                             "defects, sizing at the builder's discretion. " * 2),
        "mesh_engine": "snappy", "mesh_fidelity": "standard", "engine_source": "user_direct",
        "purpose": "external_cfd", "input_kind": "body-surface", "engine_params": {},
        "dimensionality": "3D",
        "patches": [{"name": "body", "type": "wall"},
                    {"name": "farfield", "type": "farfield"}],
    }
    base.update(over)
    return validate_submission(base)


class TestTypedExtentCapture:
    def test_a_full_declaration_is_accepted(self):
        e = _errs(requested_extents={"upstream": 5, "downstream": 8,
                                     "lateral": 5, "vertical": 5},
                  reference_length_m=0.06)
        assert not any("extent" in x.lower() or "reference" in x.lower() for x in e), e

    def test_omitting_them_is_fine_the_user_never_stated_any(self):
        assert not any("extent" in x.lower() for x in _errs())

    def test_a_nonpositive_extent_is_refused(self):
        e = _errs(requested_extents={"downstream": -8}, reference_length_m=0.06)
        assert any("downstream" in x and "positive" in x.lower() for x in e), e

    def test_an_unknown_direction_is_refused(self):
        e = _errs(requested_extents={"sideways": 5}, reference_length_m=0.06)
        assert any("sideways" in x for x in e), e

    def test_extents_without_a_ruler_are_refused(self):
        # numbers in "body lengths" mean nothing without the length they multiply
        e = _errs(requested_extents={"downstream": 8})
        assert any("reference_length" in x for x in e), e

    def test_a_nonpositive_ruler_is_refused(self):
        e = _errs(reference_length_m=0)
        assert any("reference_length" in x and "positive" in x.lower() for x in e), e

    def test_requirements_strict_must_be_a_bool(self):
        e = _errs(requirements_strict="yes")
        assert any("requirements_strict" in x for x in e), e
        assert not any("requirements_strict" in x for x in _errs(requirements_strict=True))


class TestApprovedIntentBindsTheDeclaration:
    def _canonical(self, **kw):
        from meshpipeline.agents.intake.admission_token import approved_intent_canonical
        base = dict(engine="snappy", purpose="external_cfd", input_kind="body-surface",
                    dimensionality="3D",
                    patches=[{"name": "body", "type": "wall"}],
                    engine_params={}, request_txt="the request",
                    geometry={"sha256": "0" * 64, "bytes": 10,
                              "schema_version": 1, "revision_id": None})
        base.update(kw)
        return approved_intent_canonical(**base)

    def test_v5_carries_the_domain_declaration(self):
        c = self._canonical(requested_extents={"downstream": 8.0},
                            reference_length_m=0.06, requirements_strict=False)
        assert c["schema_version"] == 5
        assert c["requested_extents"] == {"downstream": 8.0}
        assert c["reference_length_m"] == 0.06
        assert c["requirements_strict"] is False

    def test_changing_an_extent_changes_the_fingerprint(self):
        from meshpipeline.agents.intake.admission_token import fingerprint
        a = self._canonical(requested_extents={"downstream": 8.0}, reference_length_m=0.06)
        b = self._canonical(requested_extents={"downstream": 6.0}, reference_length_m=0.06)
        assert fingerprint(a) != fingerprint(b)

    def test_absent_declaration_canonicalizes_to_none_and_strict_false(self):
        c = self._canonical()
        assert c["requested_extents"] is None
        assert c["reference_length_m"] is None
        assert c["requirements_strict"] is False
