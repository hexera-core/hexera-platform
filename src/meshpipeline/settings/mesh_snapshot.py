# Responsibility: Declare the one switch that decides whether a running mesh publishes a partial
# surface of the mesh-so-far before it finishes, and answer it for a dispatch.
# Boundaries: settings only. Read on the mesher's side (whether snappy is invoked so that it keeps
# its per-phase meshes, and whether the runner watches for one) and by the two execution adapters
# at dispatch, which pass it to the mesh container. Nothing here decides what a partial contains
# or how it travels.
"""THE SWITCH ALONE PUBLISHES NOTHING, in any environment this repo deploys.

A frame is only ever read from a SERIAL snappyHexMesh run (engines/snappy/progressive: each rank
of a parallel run writes its own `processor*/1/polyMesh`, so a frame of the whole mesh would need
a `reconstructParMesh` per look). No deployed environment is serial: the Cloud Run mesh job is
8 vCPU, the big-machine VM is larger still and limits nothing, and `MESH_SNAPPY_RANKS` is blank,
so `engines/determinism.snappy_ranks` answers 8 on both routes.

So `MESH_PROGRESSIVE_SNAPSHOT_ENABLED=true` BY ITSELF CHANGES NOTHING. It has to be paired with
`MESH_SNAPPY_RANKS=1`, which makes the mesh serial and slower - that is the price of the frame
today. A run asked for frames that cannot publish them logs the reason once, at info
(`progressive.snapshots_enabled`), so the silence is never left to be read as a broken feature.
"""
from __future__ import annotations

from meshpipeline.settings.env import bool_env

#: OFF unless a deployment turns it on, and OFF is today's behaviour exactly: snappyHexMesh keeps
#: being invoked with `-overwrite`, no time directory is ever written, nothing is extracted and no
#: pointer is published. When on, a SERIAL snappy run keeps its castellated phase mesh, one
#: decimated boundary surface of it is published mid-run, and the intermediate is deleted again.
#: The parallel path never snapshots (see the module text: this needs MESH_SNAPPY_RANKS=1 beside
#: it, or it reaches nothing).
MESH_PROGRESSIVE_SNAPSHOT_ENABLED: bool = bool_env("MESH_PROGRESSIVE_SNAPSHOT_ENABLED", "false")


def snapshots_enabled_at_dispatch() -> bool:
    """THE one answer both execution routes hand to the mesh container.

    The mesh image carries no environment of its own - `deploy/gcp/cloud-run/mesh-job.yaml`
    declares none, and the big VM runs that same image - so this setting reaches the mesher only
    as something the dispatch states: a per-execution env override on the Cloud Run route, an
    instance metadata value on the VM route. Both read THIS, so neither owns the decision and the
    two cannot disagree about a run (which would publish frames on small meshes and not on large
    ones, and read as a bug in the feature).

    Read per dispatch, so turning it off takes effect on the next run rather than the next mesh
    deploy; total, so it is never the reason a mesh is not submitted.
    """
    return bool(MESH_PROGRESSIVE_SNAPSHOT_ENABLED)
