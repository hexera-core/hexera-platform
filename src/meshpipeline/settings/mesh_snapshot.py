# Responsibility: Declare the one switch that decides whether a running mesh publishes a partial
# surface of the mesh-so-far before it finishes.
# Boundaries: settings only. Read on the mesher's side (whether snappy is invoked so that it keeps
# its per-phase meshes, and whether the runner watches for one) and nowhere else. Nothing here
# decides what a partial contains or how it travels.
from __future__ import annotations

from meshpipeline.settings.env import bool_env

#: OFF unless a deployment turns it on, and OFF is today's behaviour exactly: snappyHexMesh keeps
#: being invoked with `-overwrite`, no time directory is ever written, nothing is extracted and no
#: pointer is published. When on, a SERIAL snappy run keeps its castellated phase mesh, one
#: decimated boundary surface of it is published mid-run, and the intermediate is deleted again.
#: The parallel path never snapshots: a snapshot there needs a `reconstructParMesh` per frame,
#: minutes of serial CPU charged against the mesh's own timeout, on the largest jobs.
MESH_PROGRESSIVE_SNAPSHOT_ENABLED: bool = bool_env("MESH_PROGRESSIVE_SNAPSHOT_ENABLED", "false")
