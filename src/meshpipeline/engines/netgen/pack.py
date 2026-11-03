# Responsibility: Carry the Netgen builder's system prompt and tool set - kept for the engine seam, though a deterministic driver builds every Netgen attempt.
# Boundaries: text and names only.
from __future__ import annotations

NETGEN_SYSTEM = """Netgen attempts are built by the engine's deterministic driver: the staged fluid boundary (fluid_boundary.stl, one named solid per patch) is sized from the measured local passage (about 14 cells across every passage), prism layers are grown on the wall, and the native run walks a ladder (planned layers, a thinner stack, no layers) until a valid polyMesh comes out. There is nothing for a model to author; submit_mesh is called by the driver."""

NETGEN_TOOL_NAMES = {"read_file", "list_directory", "submit_mesh"}
