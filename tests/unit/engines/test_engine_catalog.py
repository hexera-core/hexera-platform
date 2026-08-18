# Responsibility: Verify the catalogue roster, and that every implemented engine declares its full contract.
from __future__ import annotations

from meshpipeline.engines import registry as ec  # noqa: E402


def test_implemented_roster():
    import pytest
    assert set(ec.engine_names()) == {"cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"}
    assert ec.default_engine() == "cfmesh"
    assert ec.get_spec("").name == "cfmesh"              # UNSET → default
    with pytest.raises(ec.UnknownEngineError):           # UNKNOWN → raises, never substituted
        ec.get_spec("bogus")
    # TWO-STATE CONTRACT: the registry contains ONLY implemented (supported) rows -
    # planned/experimental rows are forbidden (future work lives in backlog prose)
    assert set(ec.all_engine_names()) == set(ec.engine_names())
    assert all(sp.implemented for sp in ec.ENGINE_CATALOG.values())


def test_every_engine_declares_export_formats():
    for spec in ec.ENGINE_CATALOG.values():
        assert spec.export_formats, f"{spec.name} has no export formats"
    assert ec.ENGINE_CATALOG["cfmesh"].export_formats[0] == "openfoam_polymesh"
    assert ec.ENGINE_CATALOG["snappy"].export_formats[0] == "openfoam_polymesh"
    assert "abaqus_inp" in ec.ENGINE_CATALOG["gmsh"].export_formats


def test_implemented_rows_declare_the_full_contract():
    for name in ec.engine_names():
        sp = ec.get_spec(name)
        # intake_params is OPTIONAL: an engine declares one only for a genuine either/or
        # its capability cannot already imply. topology used to be declared here and was
        # redundant with the purpose; the flow engines now declare none.
        assert sp.gates, f"{name} declares no executor gates"
        assert sp.intake_guidance, f"{name} has no native intake guidance"
        # stage-C seams: the audit-proven variant points are DECLARED
        assert sp.briefing is not None, f"{name} declares no builder briefing"
        assert sp.deliverable is not None, f"{name} declares no deliverable recipe"
        assert sp.run_policy is not None, f"{name} declares no run policy"
        assert sp.run_policy.run_timeout() > 0
        # VALIDATED_STANDARD pillar (2): the review modality must be JUSTIFIED, so
        # visual-vs-metric is never a silent asymmetry in rigor.
        assert sp.review_rationale, (
            f"{name} declares no review-modality rationale - every implemented "
            f"engine must justify its visual_review choice")
        # VALIDATED_STANDARD pillar (1): the mesh is validated to the FULLEST extent
        # no matter the engine - every ValidationAxis must be covered (a concrete
        # check, or a justified applicable=False). No axis may be silently absent.
        from meshpipeline.engines.base import ValidationAxis
        assert sp.validation_axes == set(ValidationAxis), (
            f"{name} does not cover all validation axes: "
            f"missing {set(ValidationAxis) - sp.validation_axes}")
        for cov in sp.validation_coverage:
            assert cov.check and cov.rationale, (
                f"{name} axis {cov.axis.value} declares no check/rationale")
        # INPUT CONTRACT - the geometry the engine can CONSUME (mirror of the output
        # validation): every implemented engine declares what it accepts.
        from meshpipeline.pipeline.enums import Dimensionality
        ic = sp.input_contract
        assert ic is not None, f"{name} declares no input_contract"
        assert ic.dimensionalities, f"{name} input_contract lists no dimensionalities"
        assert set(ic.dimensionalities) <= {d.value for d in Dimensionality}, (
            f"{name} input_contract has an unknown dimensionality value")
        assert ic.input_kind in ("solid", "surface"), (
            f"{name} input_kind must be 'solid' or 'surface'")
        assert ic.min_thickness_ratio >= 0.0
        assert ic.rationale, f"{name} input_contract has no rationale"
        # DOWNSTREAM TARGET - FACTUAL solver compatibility (not a domain assertion)
        dt = sp.downstream
        assert dt is not None, f"{name} declares no downstream target"
        assert dt.solvers, f"{name} downstream target lists no compatible solvers"
        # PHYSICAL CAPABILITY - the (input geometry → output mesh) records the engine
        # supports (basis of the engine×purpose compatibility gate)
        from meshpipeline.engines.purposes import INPUT_KINDS, MESH_KINDS
        assert sp.capabilities, f"{name} declares no mesh capabilities"
        for cap in sp.capabilities:
            assert cap.input_kind in INPUT_KINDS, f"{name} bad capability input_kind"
            assert cap.output_kind in MESH_KINDS, f"{name} bad capability output_kind"
            # a fluid-volume capability must say WHICH region(s) it produces - this is
            # what stops `vmtk + external_cfd` (a lumen mesher asked for a far field)
            if cap.output_kind == "fluid-volume":
                assert cap.topologies, (
                    f"{name} fluid-volume capability declares no topologies")
                assert set(cap.topologies) <= {"internal", "external"}, (
                    f"{name} bad capability topology")


def test_input_contract_rejects_unsuitable_dimensionality_at_intake():
    from meshpipeline.agents.intake.validation import validate_submission

    def _sub(engine):
        return {
            "domain": "airfoil external aero",
            "request_txt": ("External aerodynamics mesh around a thin 2D airfoil section "
                            "in a far-field box, wall + farfield + empty front/back faces. " * 2),
            "review_brief_txt": ("Accept a valid 2D external mesh: wall airfoil, farfield "
                                 "outer boundary, single empty front/back patch, no fatal defects. "),
            "mesh_engine": engine, "mesh_fidelity": "standard", "engine_source": "user_direct", "dimensionality": "2D",
            "purpose": "external_cfd", "input_kind": "body-surface",
            "patches": [{"name": "airfoil", "type": "wall"},
                        {"name": "farfield", "type": "farfield"},
                        {"name": "frontBack", "type": "empty"}],
            "engine_params": {},
        }
    sn = validate_submission(_sub("snappy"))
    assert any("cannot mesh 2D" in e for e in sn), sn
    # section 7: reject clearly WITHOUT naming a replacement engine; recommend only on request
    from meshpipeline.engines.registry import engine_names
    _m = " ".join(sn)
    assert not any(n in _m for n in engine_names() if not n.startswith("snappy")), \
        f"the rejection named a replacement engine unsolicited: {_m!r}"
    assert "ask me to recommend" in _m
    # cfmesh declares native 2D support (cartesian2DMesh) → no suitability rejection
    cf = validate_submission(_sub("cfmesh"))
    assert not any("cannot mesh" in e for e in cf), cf


def test_every_engine_has_a_solvability_check_specialized_to_it():
    from meshpipeline.engines.base import ValidationAxis
    for name in ec.engine_names():
        cov = {c.axis: c for c in ec.get_spec(name).validation_coverage}
        assert ValidationAxis.SOLVABILITY in cov, f"{name} declares no solvability check"
        assert cov[ValidationAxis.SOLVABILITY].applicable, (
            f"{name} solvability must be a real check, not N/A")
    # the two mesh families cover it differently, and say so
    assert "solve" in {c.axis: c for c in ec.get_spec("cfmesh").validation_coverage}[
        ValidationAxis.SOLVABILITY].check.lower()
    assert "element" in {c.axis: c for c in ec.get_spec("gmsh").validation_coverage}[
        ValidationAxis.SOLVABILITY].check.lower()


def test_validated_standard_is_written_once_and_names_all_three_pillars():
    from meshpipeline.engines.base import VALIDATED_STANDARD
    s = VALIDATED_STANDARD.lower()
    assert "executor_success" in s          # pillar 1: gates
    assert "reviewer" in s and "pass" in s   # pillar 2: same reviewer LLM + PASS bar
    assert "deliverable" in s or "shipped" in s  # pillar 3: delivery
    assert "identical" in s                  # the bar does NOT vary by engine


def test_every_engine_finalize_accepts_the_executors_exact_call_shape():
    import inspect

    from meshpipeline.engines.registry import engine_names
    from meshpipeline.engines.runtime import get_engine

    for name in engine_names():
        fn = get_engine(name).finalize
        sig = inspect.signature(fn)
        try:
            # the executor's exact positional shape (values irrelevant - bind only)
            sig.bind("ws", [], name, "domain", False, {}, "internal")
        except TypeError as exc:
            raise AssertionError(
                f"{name}.finalize signature lags the executor call: {exc}") from exc


def test_every_identifier_a_user_can_see_has_a_declared_name():
    from meshpipeline.contracts.display_names import VOCABULARIES
    from meshpipeline.engines.purposes import INPUT_KINDS, purpose_keys
    from meshpipeline.engines.registry import all_engine_names

    declared = {"mesh_engine": set(all_engine_names()),
                "purpose": set(purpose_keys()),
                "input_kind": set(INPUT_KINDS)}
    assert set(VOCABULARIES) == set(declared), "a vocabulary is missing from the word list"
    for name, keys in declared.items():
        assert set(VOCABULARIES[name]) == keys, (
            f"{name}: named {sorted(set(VOCABULARIES[name]) - keys)} that do not exist; "
            f"missing names for {sorted(keys - set(VOCABULARIES[name]))}")
