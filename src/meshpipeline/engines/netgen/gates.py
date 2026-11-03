# Responsibility: The blocking gates a Netgen mesh must pass before review.
# Boundaries: Netgen delivers an OpenFOAM polyMesh measured exactly as cfMesh's is, so the flow-engine gate set is cfMesh's; only the quality floor reads this engine's own declared criteria.
from __future__ import annotations

from dataclasses import replace

from meshpipeline.engines.gates import GateCtx, GateSpec


def _gate_quality_floor(ctx: GateCtx) -> tuple[bool, str]:
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    return gate_declared_criteria("netgen", ctx.manifest_or_load())


def _gates() -> tuple[GateSpec, ...]:
    from meshpipeline.engines.cfmesh.flow_gates import FLOW_GATES
    return tuple(replace(g, check=_gate_quality_floor,
                         proves="The mesh clears every quality bar Netgen requires - no fatal "
                                "topology defects")
                 if g.key == "quality_floor" else g for g in FLOW_GATES)


NETGEN_GATES: tuple[GateSpec, ...] = _gates()
