# Responsibility: Verify a purpose owns the boundary-role vocabulary and required mesh kind, and an engine does not.
from __future__ import annotations


def test_purposes_declare_expected_role_vocabularies():
    from meshpipeline.engines.purposes import PURPOSES, get_purpose, roles_for_purposes
    # REQUIRED MEMBERSHIP, not a closed world: these four purposes must exist and must declare the
    # vocabularies below. A fifth supported purpose is a product decision, not a failure here.
    assert {"structural", "external_cfd", "internal_cfd",
            "conjugate_heat_transfer"} <= set(PURPOSES)
    assert get_purpose("structural").boundary_roles == ("fixed", "load", "contact", "free")
    assert set(get_purpose("external_cfd").boundary_roles) == {
        "wall", "farfield", "symmetry", "empty"}
    assert set(get_purpose("internal_cfd").boundary_roles) == {
        "wall", "inlet", "outlet", "symmetry", "empty"}
    # CHT declares the external contract; the fluid-solid interfaces are DERIVED, not asked
    assert set(get_purpose("conjugate_heat_transfer").boundary_roles) == {
        "inlet", "outlet", "wall", "external", "symmetry", "empty"}
    # the ordered union dedups the roles shared across purposes (wall/symmetry/empty)
    u = roles_for_purposes(("external_cfd", "internal_cfd"))
    assert set(u) == {"wall", "farfield", "symmetry", "empty", "inlet", "outlet"}
    assert len(u) == len(set(u))


def test_engine_has_no_boundary_roles_field_vocab_is_purpose_only():
    from meshpipeline.engines import registry as ec
    assert not hasattr(ec.get_spec("gmsh"), "boundary_roles")
    assert not hasattr(ec.get_spec("cfmesh"), "boundary_roles")


def test_purposes_declare_required_mesh_kind():
    from meshpipeline.engines.purposes import MESH_KINDS, PURPOSES
    # structural accepts BOTH: a 3D solid-volume mesh, or the surface mesh that IS
    # the solid discretization of a PLANAR body (2D plane-stress/strain elements)
    assert PURPOSES["structural"].requires_mesh_kind == ("solid-volume", "surface-mesh")
    assert PURPOSES["external_cfd"].requires_mesh_kind == "fluid-volume"
    assert PURPOSES["internal_cfd"].requires_mesh_kind == "fluid-volume"
    for p in PURPOSES.values():
        _req = (p.requires_mesh_kind,) if isinstance(p.requires_mesh_kind, str) \
            else p.requires_mesh_kind
        assert all(k in MESH_KINDS for k in _req)


def test_engine_purpose_compatibility_is_physical_capability():
    from meshpipeline.engines import registry as ec
    from meshpipeline.engines.purposes import compatible_purposes, is_compatible
    # gmsh is a general tool - every purpose is physically possible
    assert set(compatible_purposes(ec.get_spec("gmsh"))) == {
        "structural", "external_cfd", "internal_cfd"}
    assert set(compatible_purposes(ec.get_spec("cfmesh"))) == {"external_cfd", "internal_cfd"}
    assert set(compatible_purposes(ec.get_spec("snappy"))) == {"external_cfd", "internal_cfd"}
    # gmsh + CFD is now VALID (the caveat is gone) …
    assert is_compatible(ec.get_spec("gmsh"), "external_cfd")
    # … but a fluid mesher can NEVER produce a solid-body mesh - the true impossibility
    assert not is_compatible(ec.get_spec("cfmesh"), "structural")
    assert not is_compatible(ec.get_spec("snappy"), "structural")
    # snappy_multiregion is the ONLY engine that produces a multiregion-volume, so it alone serves
    # conjugate_heat_transfer - and it serves nothing else (no single-region capability)
    assert set(compatible_purposes(ec.get_spec("snappy_multiregion"))) == {"conjugate_heat_transfer"}
    assert is_compatible(ec.get_spec("snappy_multiregion"), "conjugate_heat_transfer",
                         input_kind="solid-assembly")
    for other in ("gmsh", "cfmesh", "snappy"):
        assert not is_compatible(ec.get_spec(other), "conjugate_heat_transfer")


def test_compatibility_can_refine_on_submitted_geometry_kind():
    from meshpipeline.engines import registry as ec
    from meshpipeline.engines.purposes import is_compatible
    gmsh = ec.get_spec("gmsh")
    assert is_compatible(gmsh, "structural", input_kind="solid-body")
    assert is_compatible(gmsh, "external_cfd", input_kind="fluid-domain")
    # a solid body submitted for a CFD run: gmsh would mesh the body, not the fluid
    assert not is_compatible(gmsh, "external_cfd", input_kind="solid-body")
