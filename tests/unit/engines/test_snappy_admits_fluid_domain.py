# Responsibility: Verify snappyHexMesh admits a prepared fluid domain for internal CFD, and only there.
# The internal carve meshes a solid that IS the fluid (rod semantics; the hollow wall shell is its
# fallback), yet the capability catalog declared body-surface only. Every fluid-volume shape in the
# corpus - fluid twins, blade-row passages, the gen3 families - was therefore routed to gmsh's FEA
# lane, where knife-edge trailing edges make sliver tets no setting removes (blade_row_passage 0/6)
# and the intake refused a snappy request outright (blade_row_passage_001_snappy: intake_stalled).
from __future__ import annotations

import meshpipeline.engines.registry as ec
from meshpipeline.engines.purposes import is_compatible


def test_snappy_admits_a_prepared_fluid_domain_for_internal_cfd():
    snappy = ec.get_spec("snappy")
    assert is_compatible(snappy, "internal_cfd", input_kind="fluid-domain")
    assert is_compatible(snappy, "internal_cfd", input_kind="body-surface")


def test_snappy_still_refuses_a_fluid_domain_for_external_cfd():
    # the carve wants declared inlet/outlet mouths, not a far-field box around a body
    snappy = ec.get_spec("snappy")
    assert not is_compatible(snappy, "external_cfd", input_kind="fluid-domain")
    assert is_compatible(snappy, "external_cfd", input_kind="body-surface")


def test_the_declared_admission_no_longer_rejects_the_input_kind():
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary

    ev = AdmissionEvidence(engine="snappy", purpose="internal_cfd", input_kind="fluid-domain",
                          dimensionality="3D",
                          patches=(PatchSummary("inlet", "inlet"), PatchSummary("outlet", "outlet"),
                                   PatchSummary("wall", "wall")),
                          engine_params={})
    codes = {r.code for r in ec.get_spec("snappy").admit(ev)}
    assert "input_kind_incompatible" not in codes and "purpose_incompatible" not in codes


def test_cfmesh_is_unchanged_body_surface_only():
    cfmesh = ec.get_spec("cfmesh")
    assert not is_compatible(cfmesh, "internal_cfd", input_kind="fluid-domain")
