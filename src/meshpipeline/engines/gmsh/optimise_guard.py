# Responsibility: Run gmsh's volume optimiser so that no volume comes out of it worse than it went in (MESH-10): a volume the optimiser left with more invalid elements than it had keeps the mesh it was given, and the run says which.
# Boundaries: the current gmsh model only, in-process inside the FEA adapter and the gmsh driver; reads every volume's mesh before the optimiser, and puts back only the volumes it made invalid. Nothing else is touched; when no volume is put back the model is exactly what the optimiser left.
# Collaborates with: engines/gmsh/fea_adapter.py (the FEA optimise step), engines/gmsh/driver.py (the CFD optimise step), engines/gmsh/fea_adapter.give_back_volumes (how a mesh goes back in), docs/RULES.md MESH-10.
"""NO OPTIMISER LEAVES A VOLUME WORSE THAN IT GOT IT (MESH-10).

gmsh's Netgen optimiser treats an element whose four corners all sit on the boundary as illegal and
splits it - on a plate one or two elements thick, a large share of the elements (a quarter of ox_mc_lid's
2 mm plates', lab 27 Oct) - and nothing checked what it handed back. A guard, whatever the optimiser does:
every volume's mesh is read before the optimiser runs. Afterwards, a volume with more invalid elements
(SICN at or below zero, straight sides) than it came in with gets its input mesh back - its own nodes
and elements, the boundary untouched, since the optimiser never moves a boundary node - and is named.
Every other volume keeps what the optimiser made of it, trades included (a valid volume's worst element
may be a little lower after it; the optimiser's work on the whole volume stands). A run where the
optimiser made nothing invalid is left exactly as the optimiser left it.
"""
from __future__ import annotations

import sys

import numpy as np

RULE = "MESH-10"


def _invalid(gmsh, volume: int) -> int:
    """How many of the volume's elements have SICN at or below zero (as straight elements)."""
    _types, tags, _nodes = gmsh.model.mesh.getElements(3, int(volume))
    t = [int(x) for arr in tags for x in arr]
    if not t:
        return 0
    q = np.asarray(gmsh.model.mesh.getElementQualities(t, "minSICN"), dtype=float)
    return int((q <= 0.0).sum())


def _read(gmsh, volume: int) -> tuple:
    """The volume's own mesh, in give_back_volumes' form: its inside nodes and its elements."""
    tags, xyz, _par = gmsh.model.mesh.getNodes(3, int(volume), includeBoundary=False)
    types, etags, enodes = gmsh.model.mesh.getElements(3, int(volume))
    return (3, int(volume), tags, xyz, [], list(zip(types, etags, enodes, strict=True)))


def optimise(gmsh, method: str = "Netgen", passes: int = 1, *, log_prefix: str = "[GMSH]") -> dict:
    """Run gmsh's optimiser `passes` times over every volume; put back the input mesh of each volume it left
    with more invalid elements than it had. Returns {} when none was put back, else the record."""
    from meshpipeline.engines.gmsh.fea_adapter import give_back_volumes

    if passes <= 0:
        return {}
    volumes = [int(v) for _d, v in gmsh.model.getEntities(3)]
    given = {v: _read(gmsh, v) for v in volumes}
    invalid_in = {v: _invalid(gmsh, v) for v in volumes}
    for _ in range(int(passes)):
        gmsh.model.mesh.optimize(method)
    made = {v: n for v in volumes if (n := _invalid(gmsh, v)) > invalid_in[v]}
    if not made:
        return {}
    gmsh.model.mesh.clear([(3, v) for v in sorted(made)])
    give_back_volumes(gmsh, [given[v] for v in sorted(made)])
    gmsh.model.mesh.renumberNodes()
    gmsh.model.mesh.renumberElements()
    record = {"rule": RULE, "optimiser": method,
              "volumes": {str(v): {"invalid_after": made[v], "invalid_before": invalid_in[v],
                                   "invalid_now": _invalid(gmsh, v)} for v in sorted(made)}}
    print(f"{log_prefix} the {method} optimiser left volume(s) {', '.join(str(v) for v in sorted(made))} with "
          f"invalid elements they did not have ({', '.join(str(made[v]) for v in sorted(made))}): each keeps the mesh "
          f"it went in with ({RULE})", file=sys.stderr)
    return record
