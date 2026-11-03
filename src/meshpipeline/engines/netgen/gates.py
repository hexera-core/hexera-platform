# Responsibility: The blocking gates a Netgen mesh must pass before review.
# Boundaries: Netgen delivers an OpenFOAM polyMesh measured exactly as cfMesh's is, so its gate set is the flow-engine one (engines/netgen/flow_gates.py, the engine-local copy), judging Netgen's own declared criteria.
from __future__ import annotations

from meshpipeline.engines.gates import GateSpec
from meshpipeline.engines.netgen.flow_gates import FLOW_GATES

NETGEN_GATES: tuple[GateSpec, ...] = FLOW_GATES
