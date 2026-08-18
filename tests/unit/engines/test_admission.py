# Responsibility: Verify the declared and measured admission phases each reject only what their evidence supports.
from __future__ import annotations

from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines import registry as ec  # noqa: E402
from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary, Rejection  # noqa: E402


def _ev(engine, purpose, input_kind=None, dim=None, patches=(), params=None, surface=None):
    return AdmissionEvidence(
        engine=engine, purpose=purpose, input_kind=input_kind, dimensionality=dim,
        patches=tuple(PatchSummary(n, t) for n, t in patches),
        engine_params=params or {}, surface_analysis=surface)


def _codes(engine, **kw):
    return {r.code for r in ec.get_spec(engine).admit(_ev(engine, **kw))}


# DECLARED phase: capability (engine × purpose × input)

def test_fluid_mesher_cannot_produce_a_structural_mesh():
    codes = _codes("cfmesh", purpose="structural", input_kind="solid-body",
                   dim="3D", patches=[("base", "fixed")])
    assert "purpose_incompatible" in codes


def test_gmsh_produces_structural_from_a_solid_body():
    codes = _codes("gmsh", purpose="structural", input_kind="solid-body", dim="3D",
                   patches=[("base", "fixed")], params={"element_order": "2"})
    assert "purpose_incompatible" not in codes and "input_kind_incompatible" not in codes


def test_cfd_from_a_bare_solid_is_rejected_no_fluid_prep():
    codes = _codes("gmsh", purpose="external_cfd", input_kind="solid-body", dim="3D",
                   patches=[("body", "wall"), ("ff", "farfield")], params={"element_order": "2"})
    assert "input_kind_incompatible" in codes and "purpose_incompatible" not in codes


# DECLARED phase: dimensionality, symmetry, structure, empty-patch, params

def test_dimensionality_the_engine_cannot_consume_is_rejected():
    # snappyHexMesh is 3D-only (2D and the former 2.5D value both reject)
    assert "dimensionality_unsupported" in _codes(
        "snappy", purpose="external_cfd", input_kind="body-surface", dim="2D",
        patches=[("b", "wall"), ("ff", "farfield")])
    # cfMesh declares native 2D (cartesian2DMesh) - same evidence admits on the
    # dimensionality rule; the 2D-contract rule still wants the single empty patch
    cf = _codes("cfmesh", purpose="external_cfd", input_kind="body-surface",
                dim="2D", patches=[("b", "wall"), ("ff", "farfield")])
    assert "dimensionality_unsupported" not in cf
    assert "missing_empty_patch_2d" in cf


def test_symmetry_patch_rejected_unless_the_engine_produces_it():
    sym = [("b", "wall"), ("ff", "farfield"), ("s", "symmetry")]
    assert "symmetry_unsupported" in _codes("cfmesh", purpose="external_cfd",
                                            input_kind="body-surface", dim="3D", patches=sym)
    # snappy declares supports_symmetry_plane
    assert "symmetry_unsupported" not in _codes("snappy", purpose="external_cfd",
                                                input_kind="body-surface", dim="3D", patches=sym)


def test_flow_structure_and_empty_patch_rules():
    # a flow mesh with no wall
    assert "missing_wall_patch" in _codes("snappy", purpose="external_cfd",
                                          input_kind="body-surface", dim="3D",
                                          patches=[("ff", "farfield")])
    # 2D case without the empty front/back patch
    assert "missing_empty_patch_2d" in _codes("snappy", purpose="external_cfd",
                                              input_kind="body-surface", dim="2D",
                                              patches=[("af", "wall"), ("ff", "farfield")])
    # 'empty' on a 3D case
    assert "empty_patch_in_3d" in _codes("snappy", purpose="external_cfd",
                                         input_kind="body-surface", dim="3D",
                                         patches=[("af", "wall"), ("ff", "farfield"), ("fb", "empty")])


def test_engine_params_validity_is_part_of_declared_admission():
    codes = _codes("gmsh", purpose="structural", input_kind="solid-body", dim="3D",
                   patches=[("base", "fixed")], params={"element_order": "nonsense"})
    assert "engine_param_invalid" in codes


def test_an_unknown_engine_param_is_rejected_not_silently_accepted():
    codes = _codes("vmtk", purpose="internal_cfd", input_kind="body-surface", dim="3D",
                   patches=[("w", "wall"), ("i", "inlet"), ("o", "outlet")],
                   params={"wall_layers": "on", "topology": "internal"})
    assert "engine_param_invalid" in codes


# evidence-phase skipping: a rule whose evidence is absent does not fire

def test_absent_evidence_skips_its_rule_never_fails_it():
    # no purpose → no capability verdict; no dimensionality → no dim verdict; no patches →
    # no structural verdict. (The engine_params rule DOES run - it must catch a missing
    # required param even from an empty set - so we assert the SKIP of the others, not an
    # empty result.)
    codes = _codes("gmsh", purpose="")
    assert "purpose_incompatible" not in codes and "input_kind_incompatible" not in codes
    assert "dimensionality_unsupported" not in codes
    assert "missing_wall_patch" not in codes and "missing_empty_patch_2d" not in codes
    assert "symmetry_unsupported" not in codes


def test_measured_rules_do_not_run_without_a_surface():
    # vmtk requires a non-self-intersecting surface, but with no surface_analysis the measured
    # phase is skipped entirely (declared-only call, e.g. intake).
    codes = _codes("vmtk", purpose="internal_cfd", input_kind="body-surface", dim="3D",
                   patches=[("w", "wall"), ("i", "inlet"), ("o", "outlet")])
    assert "geometry_unsuitable" not in codes


# MEASURED phase: same method, more evidence, same Rejection shape

def test_self_intersecting_surface_is_a_measured_rejection():
    rs = ec.get_spec("vmtk").admit(_ev(
        "vmtk", purpose="internal_cfd", input_kind="body-surface", dim="3D",
        patches=[("w", "wall"), ("i", "inlet"), ("o", "outlet")],
        params={"source_ids": [0], "target_ids": [1]},
        surface={"self_intersecting": True, "diag": 1.0}))
    measured = [r for r in rs if r.phase == "measured"]
    assert measured and measured[0].code == "geometry_unsuitable"
    assert measured[0].message.startswith("[GEOMETRY_UNSUITABLE]")
    assert isinstance(measured[0], Rejection)   # same type as declared rejections


def test_clean_surface_passes_the_measured_phase():
    rs = ec.get_spec("vmtk").admit(_ev(
        "vmtk", purpose="internal_cfd", input_kind="body-surface", dim="3D",
        patches=[("w", "wall"), ("i", "inlet"), ("o", "outlet")],
        params={"source_ids": [0], "target_ids": [1]},
        surface={"self_intersecting": False, "diag": 1.0}))
    assert not [r for r in rs if r.phase == "measured"]


def test_a_wrap_then_fill_engine_has_no_measured_rejection():
    rs = ec.get_spec("snappy").admit(_ev(
        "snappy", purpose="external_cfd", input_kind="body-surface", dim="3D",
        patches=[("b", "wall"), ("ff", "farfield")],
        surface={"self_intersecting": True, "diag": 1.0}))
    assert not [r for r in rs if r.phase == "measured"]


# SINGLE-ENTRY invariants: one path, no duplicated shapes

def test_a_new_engine_rule_is_picked_up_by_both_phases_without_editing_either_caller(monkeypatch):
    from meshpipeline.engines.base import EngineSpec
    real_admit = EngineSpec.admit
    sentinel = Rejection(code="brand_new_rule", phase="declared", message="nope, new rule")

    def _patched(self, evidence):
        return (*real_admit(self, evidence), sentinel)

    monkeypatch.setattr(EngineSpec, "admit", _patched)
    # intake surfaces it …
    from meshpipeline.agents.intake.validation import validate_submission
    errs = validate_submission({
        "domain": "x part", "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
        "purpose": "structural", "input_kind": "solid-body", "dimensionality": "3D",
        "engine_params": {"element_order": "2"}, "patches": [{"name": "b", "type": "fixed"}],
        "request_txt": "A complete requirements summary covering everything needed here. " * 3,
        "review_brief_txt": "Acceptance criteria covering mesh validity and regions here. " * 3,
    })
    assert any("nope, new rule" in e for e in errs)
    # … and the same declared rule object is what the node would see too (same method).
    assert any(r.code == "brand_new_rule" for r in ec.get_spec("gmsh").admit(
        _ev("gmsh", purpose="structural", input_kind="solid-body", dim="3D",
            patches=[("b", "fixed")], params={"element_order": "2"})))
