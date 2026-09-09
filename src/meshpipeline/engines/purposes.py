# Responsibility: Describe what each supported workflow needs of a mesh, and which engines can produce it.
# Owns: purpose lookup, flow topology, and engine/purpose compatibility.
# Boundaries: workflow knowledge, not engine mechanics.
# Collaborates with: engines/purpose_review_axes.py and engines/registry.py.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Purpose:
    key: str               # "structural" | "external_cfd" | "internal_cfd" | ...
    # Name AND explanation, for prose with room for both. The short name a user is shown lives in
    # contracts/display_names.py with every other user-facing word.
    label: str
    boundary_roles: tuple  # the region/patch vocabulary this purpose exposes
    # The mesh KIND this purpose needs an engine to PRODUCE - the basis of the
    # (engine × purpose) compatibility gate. "solid-volume" (a conformal volume mesh
    # of the body, for stress/FEA) | "fluid-volume" (a volume mesh of the fluid
    # region, for CFD). A purpose is possible on an engine iff SOME engine capability
    # produces this kind - physical capability, not an inferred domain.
    requires_mesh_kind: str | tuple = ""   # one kind, or a tuple of kinds that all serve it
    # WHICH fluid region this purpose needs. external_cfd IS flow around a body;
    # internal_cfd IS flow through a cavity. It is therefore NOT a separate question to
    # ask the user - asking twice is how the two answers came to disagree
    # (`internal_cfd` + `topology=external` was an accepted submission). Engines derive
    # it from the purpose; only the gate and the engine runners read it.
    # "" = the distinction does not apply (structural; CHT, which declares its own).
    flow_topology: str = ""
    # USE-CASE review axes (ReviewAxis tuple): what THIS workflow requires of any mesh,
    # regardless of engine (external CFD wants far-field clearance; structural wants the
    # restraint/load surfaces tagged). The reviewer runtime unions these with the
    # engine's mesh-class axes. Same no-setpoint invariant as the engine rubric.
    review_axes: tuple = ()


from meshpipeline.engines.purpose_review_axes import (
    _CHT_AXES,
    _EXTERNAL_CFD_AXES,
    _INTERNAL_CFD_AXES,
    _STRUCTURAL_AXES,
)

PURPOSES: dict[str, Purpose] = {
    "structural": Purpose(
        key="structural",
        label="Structural / FEA (static stress, modal)",
        boundary_roles=("fixed", "load", "contact", "free"),
        # stress needs the BODY discretized: a solid-volume mesh in 3D, or - for a
        # PLANAR body (2D plane-stress/strain) - the surface mesh that IS the solid
        # discretization (CPS/plane elements). Both kinds serve this purpose.
        requires_mesh_kind=("solid-volume", "surface-mesh"),
        review_axes=_STRUCTURAL_AXES,
    ),
    "external_cfd": Purpose(
        key="external_cfd",
        label="External CFD (flow around a body in a far-field)",
        boundary_roles=("wall", "farfield", "symmetry", "empty"),
        requires_mesh_kind="fluid-volume",   # needs the fluid AROUND the body meshed
        flow_topology="external",
        review_axes=_EXTERNAL_CFD_AXES,
    ),
    "internal_cfd": Purpose(
        key="internal_cfd",
        label="Internal CFD (flow through a cavity)",
        boundary_roles=("wall", "inlet", "outlet", "symmetry", "empty"),
        requires_mesh_kind="fluid-volume",   # needs the fluid INSIDE the cavity meshed
        flow_topology="internal",
        review_axes=_INTERNAL_CFD_AXES,
    ),
    "conjugate_heat_transfer": Purpose(
        key="conjugate_heat_transfer",
        label="Conjugate heat transfer (coupled fluid + solid regions)",
        # user-declared external contract; the fluid<->solid interfaces are DERIVED
        # (splitMeshRegions auto-creates the coupled mappedWall patches), not asked for.
        boundary_roles=("inlet", "outlet", "wall", "external", "symmetry", "empty"),
        requires_mesh_kind="multiregion-volume",  # coupled fluid + solid meshes in one case
        review_axes=_CHT_AXES,
    ),
}

# The OUTPUT mesh kinds an engine capability may produce, and the INPUT geometry
# kinds it may accept (MeshCapability). "multiregion-volume" = a coupled set of
# per-region volume meshes (fluid + solid) for conjugate heat transfer;
# "solid-assembly" = a multi-solid CAD assembly (one closed solid per region).
MESH_KINDS = ("solid-volume", "fluid-volume", "multiregion-volume", "surface-mesh")
INPUT_KINDS = ("solid-body", "fluid-domain", "body-surface", "solid-assembly",
               "planar-domain")


def get_purpose(key: str) -> Purpose:
    return PURPOSES[key]


def purpose_label(key: str) -> str:
    from meshpipeline.contracts.display_names import display_name
    return display_name("purpose", key)


def input_kind_label(key: str) -> str:
    from meshpipeline.contracts.display_names import display_name
    return display_name("input_kind", key)


def purpose_keys() -> tuple:
    return tuple(PURPOSES)


def roles_for_purposes(keys) -> tuple:
    seen: list[str] = []
    for k in keys:
        for r in PURPOSES[k].boundary_roles:
            if r not in seen:
                seen.append(r)
    return tuple(seen)


# #
# (Engine × Purpose) COMPATIBILITY - the true impossibility gate. A purpose is
# possible on an engine iff SOME engine capability PRODUCES the mesh kind the
# purpose REQUIRES. gmsh supports solid-body→solid-volume AND fluid-domain→
# fluid-volume, so it serves structural AND CFD (with a supplied fluid domain); the
# flow engines support body-surface→fluid-volume (snappy also fluid-domain→
# fluid-volume for INTERNAL flow, since its carve meshes a solid that is the fluid),
# so they serve CFD only and cfmesh+structural stays a genuine impossibility. When the SUBMITTED geometry's
# kind is known (intake, later stage), pass ``input_kind`` to also require that a
# capability accepts it. Declared PHYSICAL capability - never an inferred domain.
# `spec` is duck-typed (EngineSpec) so this module stays free of the engine
# registry (no import cycle).
# #
def flow_topology(purpose_key: str) -> str:
    return PURPOSES[purpose_key].flow_topology


def topology_of(purpose_key: str) -> str:
    return PURPOSES[purpose_key].flow_topology if purpose_key in PURPOSES else ""


def is_compatible(spec, purpose_key: str, input_kind: str | None = None) -> bool:
    p = PURPOSES[purpose_key]
    req, req_topo = p.requires_mesh_kind, p.flow_topology
    req = (req,) if isinstance(req, str) else tuple(req)
    return any(
        c.output_kind in req
        and (input_kind is None or c.input_kind == input_kind)
        and (not req_topo or req_topo in c.topologies)
        for c in spec.capabilities
    )


def engines_producing_topology(specs: dict, topo: str) -> list[str]:
    return sorted(
        name for name, sp in specs.items()
        if sp.implemented and any(topo in c.topologies for c in sp.capabilities)
    )


def compatible_purposes(spec, input_kind: str | None = None) -> tuple:
    return tuple(k for k in PURPOSES if is_compatible(spec, k, input_kind))
