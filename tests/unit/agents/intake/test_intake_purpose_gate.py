# Responsibility: Verify the purpose and input-kind gate admits only what an engine can actually mesh, roles included.
from __future__ import annotations


def _base(**over) -> dict:
    b = {
        "domain": "test part analysis",
        "request_txt": ("A complete requirements summary covering the geometry, the simulation "
                        "type, every confirmed parameter and the mesh requirements. " * 2),
        "review_brief_txt": ("Acceptance criteria: a valid mesh, the correct regions, no fatal "
                             "defects, sizing at the builder's discretion. " * 2),
        "dimensionality": "3D", "mesh_fidelity": "standard", "engine_source": "user_direct",
    }
    b.update(over)
    return b


def _errs(**over) -> list[str]:
    from meshpipeline.agents.intake.validation import validate_submission
    return validate_submission(_base(**over))


def test_cfmesh_plus_structural_is_rejected_as_impossible():
    from meshpipeline.engines.registry import engine_names
    e = _errs(mesh_engine="cfmesh", purpose="structural", input_kind="solid-body",
              engine_params={},
              patches=[{"name": "base", "type": "fixed"}])   # structural vocab
    assert any("cannot produce a structural mesh" in x for x in e), e
    _msg = " ".join(e)
    # the rejected engine may be named ("cfmesh cannot…"); no OTHER engine may be named unsolicited
    assert not any(n in _msg for n in engine_names() if n != "cfmesh"), \
        f"rejection named a replacement engine without being asked: {_msg!r}"
    assert "ask me to recommend" in _msg   # a recommendation is available ONLY on request


def test_gmsh_structural_from_solid_body_passes_the_gate():
    e = _errs(mesh_engine="gmsh", purpose="structural", input_kind="solid-body",
              engine_params={"element_order": "2"},
              patches=[{"name": "base", "type": "fixed"}, {"name": "rest", "type": "free"}])
    assert e == [], e   # a fully valid submission


def test_gmsh_cfd_from_fluid_domain_passes_the_gate():
    e = _errs(mesh_engine="gmsh", purpose="external_cfd", input_kind="fluid-domain",
              engine_params={"element_order": "2"},
              patches=[{"name": "body", "type": "wall"}, {"name": "ff", "type": "farfield"}])
    assert e == [], e   # fully valid: gmsh meshes the supplied fluid domain for CFD


def test_gmsh_cfd_from_solid_body_is_rejected_no_fluid_prep():
    e = _errs(mesh_engine="gmsh", purpose="external_cfd", input_kind="solid-body",
              engine_params={"element_order": "2"},
              patches=[{"name": "body", "type": "wall"}, {"name": "ff", "type": "farfield"}])
    assert any("from a 'solid-body' geometry" in x for x in e), e
    assert any("fluid-domain" in x for x in e)   # tells the user what input would work


# S3b: boundary vocabulary is PURPOSE-specific (not a shared flow bucket)
def test_external_cfd_vocab_accepts_symmetry_as_a_role_but_capability_gates_it():
    e = _errs(mesh_engine="cfmesh", purpose="external_cfd", input_kind="body-surface",
              engine_params={},
              patches=[{"name": "body", "type": "wall"}, {"name": "ff", "type": "farfield"},
                       {"name": "sym", "type": "symmetry"}])
    assert not any("'symmetry' is invalid" in x for x in e)   # a valid role, not a bad type


def test_external_cfd_rejects_inlet():
    bad = _errs(mesh_engine="cfmesh", purpose="external_cfd", input_kind="body-surface",
                engine_params={},
                patches=[{"name": "body", "type": "wall"},
                         {"name": "in", "type": "inlet", "diameter_mm": 40}])
    assert any("'inlet' is invalid" in x for x in bad), bad   # inlet is NOT external CFD


# engine CAPABILITY on patch types: only engines that mesh half-domains admit symmetry
def test_symmetry_rejected_for_engines_that_cannot_produce_it():
    for eng in ("cfmesh", "gmsh"):
        e = _errs(mesh_engine=eng, purpose="external_cfd", input_kind="body-surface",
                  engine_params=({"element_order": "2"} if eng == "gmsh" else {}),
                  patches=[{"name": "aircraft", "type": "wall"},
                           {"name": "farfield", "type": "farfield"},
                           {"name": "symmetry", "type": "symmetry"}])
        assert any("symmetry-plane" in x for x in e), (eng, e)
        assert any(eng in x for x in e if "symmetry-plane" in x), (eng, e)


def test_symmetry_admitted_at_intake_for_snappy_which_meshes_half_models():
    e = _errs(mesh_engine="snappy", purpose="external_cfd", input_kind="body-surface",
              engine_params={},
              patches=[{"name": "aircraft", "type": "wall"},
                       {"name": "farfield", "type": "farfield"},
                       {"name": "symmetry", "type": "symmetry"}])
    assert not any("symmetry-plane" in x for x in e), e


def test_dropping_symmetry_makes_the_same_run_valid():
    e = _errs(mesh_engine="snappy", purpose="external_cfd", input_kind="body-surface",
              engine_params={},
              patches=[{"name": "aircraft", "type": "wall"},
                       {"name": "farfield", "type": "farfield"}])
    assert e == [], e


def test_capability_flag_would_admit_symmetry_when_an_engine_implements_it(monkeypatch):
    from meshpipeline.engines import registry
    real = registry.get_spec

    class _Wrap:
        def __init__(self, s): self._s = s
        def __getattr__(self, k):
            return True if k == "supports_symmetry_plane" else getattr(self._s, k)

    # validate_submission does a function-local `from meshpipeline.engines.registry import get_spec`,
    # so the registry source is what a call resolves - patch it there.
    monkeypatch.setattr(registry, "get_spec", lambda n: _Wrap(real(n)))
    e = _errs(mesh_engine="snappy", purpose="external_cfd", input_kind="body-surface",
              engine_params={},
              patches=[{"name": "aircraft", "type": "wall"},
                       {"name": "farfield", "type": "farfield"},
                       {"name": "symmetry", "type": "symmetry"}])
    assert not any("symmetry-plane" in x for x in e), e


def test_internal_cfd_vocab_accepts_inlet_outlet_rejects_farfield():
    ok = _errs(mesh_engine="snappy", purpose="internal_cfd", input_kind="body-surface",
               engine_params={},
               patches=[{"name": "w", "type": "wall"},
                        {"name": "in", "type": "inlet", "diameter_mm": 40},
                        {"name": "out", "type": "outlet", "diameter_mm": 60}])
    assert ok == [], ok
    bad = _errs(mesh_engine="snappy", purpose="internal_cfd", input_kind="body-surface",
                engine_params={},
                patches=[{"name": "w", "type": "wall"}, {"name": "ff", "type": "farfield"}])
    assert any("'farfield' is invalid" in x for x in bad), bad   # farfield is NOT internal CFD


def test_purpose_and_input_kind_are_required_and_enum_checked():
    e = _errs(mesh_engine="gmsh", purpose="bogus", input_kind="nope",
              engine_params={"element_order": "2"},
              patches=[{"name": "base", "type": "fixed"}])
    assert any("purpose must be one of" in x for x in e)
    assert any("input_kind must be one of" in x for x in e)
