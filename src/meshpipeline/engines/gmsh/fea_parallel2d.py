# Responsibility: Mesh the surfaces of a gmsh model on every core the job has, REPEATABLY (MESH-23): the curves meshed once on one thread, then the surfaces cut into fixed blocks, each block meshed by one gmsh call on one thread in a forked worker process against the same curve mesh, and the results put back into the parent model in surface order - the same mesh for any number of workers, one included.
# Boundaries: runs inside the FEA adapter's own mesher process (never the planner's, never an event loop's): it forks that process. It plans nothing and changes no size: the model, its fields and its options are the parent's, copied by the fork. A model it cannot split exactly (a compound, a periodic face) is meshed the old way by the caller.
# Collaborates with: engines/gmsh/fea_adapter.surface_mesh (calls it in place of gmsh.model.mesh.generate(2)), docs/RULES.md MESH-23.
"""MESH-23: THE SURFACE STAGE ON EVERY CORE, THE SAME MESH.

gmsh's own threaded surface stage (Mesh.MaxNumThreads2D > 1) is not repeatable: 0 of 17 lab meshes came out
the same twice at 8 threads (24 Oct), so the adapter kept it on one thread - and a slow surface stage ran on
one core of an 8-core machine. This does the parallel part outside gmsh:

1. the curves are meshed once, in the parent, on one thread (gmsh.model.mesh.generate(1));
2. the surfaces are cut into BLOCKS - runs of consecutive surface numbers of about equal work, read from the
   curve mesh alone, so the same on every run and for any number of workers;
3. the parent forks W workers; each holds the same model (fields, options, curve mesh) and meshes its share
   of the blocks, each block by ONE gmsh call (only that block visible), on one thread, from gmsh's fixed
   seed (Mesh.RandomSeed), and writes each surface's nodes (with their parameters on the surface) and
   triangles to a file;
4. the parent adds them to its model in surface-number order, each worker's curve-node numbers mapped to the
   parent's curve by curve, and numbers nodes and elements as gmsh does after its own surface stage.

WHY BLOCKS, AND WHY NOT gmsh's OWN ONE-CALL MESH, TO THE BYTE. gmsh's surface stage draws from ONE random
stream for a whole call, seeded from Mesh.RandomSeed when the call starts: a surface's triangles depend on
how many numbers every surface meshed before it in that call drew (lab, 1 Nov: meshed in one call or one
surface per call, the first 15 surfaces of a 1,478-face plate came out the same and 1,439 of its 1,478
did not). No process can draw the numbers a surface it did not mesh would have drawn, so the one-call
mesh cannot be split. What can be is a fixed cut: each block starts its own call from the same seed, so a
block's mesh depends on that block alone - not on which worker meshed it, how many workers there were, or
in what order they finished. One process meshes the same blocks the same way: the same mesh, to the byte,
on 1 or N workers (lab: docs/RULES.md MESH-23). One call per block, not per surface: a gmsh call costs
tens of milliseconds whatever it meshes (one surface per call made a 1,478-face plate 7 times slower on
one core).
"""
from __future__ import annotations

import os
import pickle
import sys
import tempfile
from pathlib import Path

import numpy as np

#: MESH-23: fewer surfaces than this are meshed by one process. MESH-25: two - even a part with a handful of
#: faces shares them among the job's cores (gmsh's own threaded surface stage is not safe, determinism.pin_gmsh)
MIN_SURFACES = 2
#: MESH-23: the surfaces are cut into at most this many blocks (fixed: never the number of workers)
BLOCKS = 64
#: the rule id the adapter's log line cites
RULE = "MESH-23"


def workers_for(threads: int, n_surfaces: int) -> int:
    """How many worker processes: the job's cores (the adapter's thread count), never more than there are
    blocks to share; 1 means one process (the same mesh, meshed here)."""
    if n_surfaces < MIN_SURFACES or not hasattr(os, "fork"):
        return 1
    return max(1, min(int(threads), BLOCKS, n_surfaces))


def can_split(gmsh) -> bool:
    """Whether the model's surfaces can be meshed apart exactly: no periodic face (it copies its master's
    mesh, so the two must be meshed in one call)."""
    for _d, s in gmsh.model.getEntities(2):
        try:
            master = gmsh.model.mesh.getPeriodic(2, [s])
        except Exception:  # noqa: BLE001 - an API without it: no periodic face can be told apart, so no split
            return False
        if len(master) and int(master[0]) != int(s):
            return False
    return True


def _cost(gmsh, s: int) -> float:
    """A surface's share of the work, from the curve mesh alone (so the blocks are the same on every run):
    its boundary nodes squared (a surface's triangles grow as the square of the nodes round it)."""
    n = 0
    for _d, c in gmsh.model.getBoundary([(2, s)], combined=False, oriented=False):
        tags, _x, _p = gmsh.model.mesh.getNodes(1, abs(int(c)), includeBoundary=True)
        n += len(tags)
    return float(max(n, 3)) ** 2


def blocks(gmsh, surfaces: list[int]) -> list[list[int]]:
    """The fixed cut: consecutive surfaces (by number) in at most BLOCKS runs of about equal work. Decided by
    the curve mesh alone - never by the number of workers - so every run cuts the same blocks."""
    surfaces = sorted(surfaces)
    cost = [_cost(gmsh, s) for s in surfaces]
    total = float(sum(cost)) or 1.0
    n = min(BLOCKS, len(surfaces))
    out: list[list[int]] = [[]]
    done = 0.0
    for s, c in zip(surfaces, cost, strict=True):
        # a new block once this one holds its share of the work (the k-th block ends past k/n of the total)
        if out[-1] and len(out) < n and done >= total * len(out) / n:
            out.append([])
        out[-1].append(s)
        done += c
    return out


def shares(cut: list[list[int]], cost_of, workers: int) -> list[list[int]]:
    """Which worker meshes which blocks (by index): the costliest block first onto the least-loaded worker,
    ties by number. Only the speed depends on this - each block's mesh is its own."""
    load = [0.0] * workers
    out: list[list[int]] = [[] for _ in range(workers)]
    for k in sorted(range(len(cut)), key=lambda i: (-cost_of(i), i)):
        w = min(range(workers), key=lambda i: (load[i], i))
        out[w].append(k)
        load[w] += cost_of(k)
    return [sorted(x) for x in out if x]


def _low_nodes(gmsh) -> dict[tuple[int, int], np.ndarray]:
    """Every point's and curve's own node numbers, in gmsh's order (the curve mesh both sides share)."""
    out = {}
    for d, t in gmsh.model.getEntities(1) + gmsh.model.getEntities(0):
        tags, _x, _p = gmsh.model.mesh.getNodes(d, t, includeBoundary=False)
        out[(d, t)] = np.asarray(tags, dtype=np.int64)
    return out


def mesh_blocks(gmsh, cut: list[list[int]]) -> dict[int, str]:
    """Each block of `cut` meshed by its own gmsh call, in order (only it visible): its mesh starts from gmsh's
    fixed seed whatever was meshed before it. Returns {first surface of the block gmsh stopped on: its
    message} (at most one: meshing stops there, as generate(2) does). Visibility and numbering options are
    left as they were."""
    everything = gmsh.model.getEntities()
    seen = [e for e in everything if gmsh.model.getVisibility(*e)]
    only = gmsh.option.getNumber("Mesh.MeshOnlyVisible")
    renumber = gmsh.option.getNumber("Mesh.Renumber")
    threads = gmsh.option.getNumber("General.NumThreads")
    per_step = {k: gmsh.option.getNumber(f"Mesh.MaxNumThreads{k}") for k in ("1D", "2D")}
    gmsh.option.setNumber("Mesh.MeshOnlyVisible", 1)
    # NO OpenMP TEAM WHILE THE BLOCKS ARE MESHED: GNU libgomp's thread pool does not survive fork(), so a forked
    # worker whose parent had already run a parallel region (a 3D fill, the optimiser - a pass meshed again in
    # the same process) hung in its first team of more than one thread (lab, 1 Nov). Each block is meshed on one
    # thread - the cores are used by the worker processes, one block each (MESH-25 threads only gmsh's one-call
    # stage); one process does the same, so the mesh is the same. gmsh reads the step's own maximum
    # (Mesh.MaxNumThreads2D) before General.NumThreads: both to one, or a worker hangs in its first surface (lab,
    # 13 Nov: every worker of a 12-face beam asleep at 0% CPU once the step maxima were the job's cores).
    gmsh.option.setNumber("General.NumThreads", 1)
    for k in per_step:
        gmsh.option.setNumber(f"Mesh.MaxNumThreads{k}", 1)
    # no renumbering of the whole model after every block: numbers are set once, after the last (_put_back or
    # the caller) - the mesh of each entity, in its order, is all that is kept
    gmsh.option.setNumber("Mesh.Renumber", 0)
    errors: dict[int, str] = {}
    gmsh.model.setVisibility(everything, 0)
    try:
        for block in cut:
            dim_tags = [(2, s) for s in block]
            gmsh.model.setVisibility(dim_tags, 1, recursive=True)
            try:
                gmsh.model.mesh.generate(2)
            except Exception as exc:  # noqa: BLE001 - told to the caller, which raises it as the stage's own error
                errors[block[0]] = str(exc)
                break
            finally:
                gmsh.model.setVisibility(dim_tags, 0, recursive=True)
    finally:
        gmsh.model.setVisibility(everything, 0)
        gmsh.model.setVisibility(seen, 1)
        gmsh.option.setNumber("Mesh.MeshOnlyVisible", only)
        gmsh.option.setNumber("Mesh.Renumber", renumber)
        gmsh.option.setNumber("General.NumThreads", threads)
        for k, v in per_step.items():
            gmsh.option.setNumber(f"Mesh.MaxNumThreads{k}", v)
    return errors


def _worker(gmsh, cut: list[list[int]], out: Path) -> None:
    """In the forked child: mesh its blocks, one gmsh call each, and write what each surface got."""
    errors = mesh_blocks(gmsh, cut)
    faces = {}
    for block in cut:
        if errors and block[0] >= min(errors):
            break
        for s in block:
            tags, xyz, par = gmsh.model.mesh.getNodes(2, s, includeBoundary=False, returnParametricCoord=True)
            types, _etags, enodes = gmsh.model.mesh.getElements(2, s)
            faces[s] = {"tags": np.asarray(tags, dtype=np.int64), "xyz": np.asarray(xyz, dtype=float),
                        "par": np.asarray(par, dtype=float),
                        "elements": [(int(t), np.asarray(n, dtype=np.int64))
                                     for t, n in zip(types, enodes, strict=True)]}
    with open(out, "wb") as fh:
        pickle.dump({"low": _low_nodes(gmsh), "faces": faces, "errors": errors}, fh,
                    protocol=pickle.HIGHEST_PROTOCOL)


def mesh_surfaces(gmsh, workers: int) -> dict:
    """MESH-23: the whole surface stage on `workers` processes (the curves first, here, on one thread).
    Raises RuntimeError with gmsh's own message when a surface could not be meshed, as generate(2) does.
    Returns what it did, for the log."""
    gmsh.model.mesh.generate(1)
    surfaces = sorted(int(t) for _d, t in gmsh.model.getEntities(2))
    cut = blocks(gmsh, surfaces)
    if workers <= 1:
        # ONE PROCESS: the same blocks, meshed here - the mesh any number of workers builds
        errors = mesh_blocks(gmsh, cut)
        gmsh.model.mesh.renumberNodes()
        gmsh.model.mesh.renumberElements()
        if errors:
            raise RuntimeError(errors[min(errors)])
        return {"workers": 1, "surfaces": len(surfaces), "blocks": len(cut)}
    cost = {s: _cost(gmsh, s) for s in surfaces}
    parts = shares(cut, lambda k: sum(cost[s] for s in cut[k]), workers)
    low_parent = _low_nodes(gmsh)
    tmp = Path(tempfile.mkdtemp(prefix="fea2d_"))
    sys.stdout.flush()
    sys.stderr.flush()
    pids = []
    for w, mine in enumerate(parts):
        pid = os.fork()
        if pid == 0:  # the child: mesh, write, leave at once (no Python clean-up of the parent's state)
            code = 0
            try:
                _worker(gmsh, [cut[k] for k in mine], tmp / f"w{w}.pkl")
            except BaseException:  # noqa: BLE001 - any failure is the missing file the parent reports
                code = 1
            finally:
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                finally:
                    os._exit(code)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)
    results = []
    for w in range(len(parts)):
        f = tmp / f"w{w}.pkl"
        if not f.exists():
            raise RuntimeError(f"{RULE}: a surface worker died meshing surfaces {cut[parts[w][0]][:12]}")
        with open(f, "rb") as fh:
            results.append(pickle.load(fh))  # noqa: S301 - our own child's file
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    errors = {s: e for r in results for s, e in r["errors"].items()}
    _put_back(gmsh, surfaces, results, low_parent)
    if errors:
        raise RuntimeError(errors[min(errors)])
    return {"workers": len(parts), "surfaces": len(surfaces), "blocks": len(cut)}


def _put_back(gmsh, surfaces: list[int], results: list[dict], low_parent: dict) -> None:
    """Every surface's nodes and triangles into the parent model, in surface order, the workers' curve-node
    numbers mapped to the parent's; then nodes and elements numbered as gmsh numbers them after a stage."""
    owner = {s: r for r in results for s in r["faces"]}
    maps = []
    for r in results:
        child = np.concatenate([r["low"][k] for k in sorted(low_parent)] or [np.zeros(0, np.int64)])
        parent = np.concatenate([low_parent[k] for k in sorted(low_parent)] or [np.zeros(0, np.int64)])
        if len(child) != len(parent):
            raise RuntimeError(f"{RULE}: a surface worker's curve mesh is not the parent's")
        order = np.argsort(child, kind="stable")
        maps.append((child[order], parent[order]))
    index = {id(r): i for i, r in enumerate(results)}
    next_node = int(gmsh.model.mesh.getMaxNodeTag()) + 1
    next_el = int(gmsh.model.mesh.getMaxElementTag()) + 1
    for s in surfaces:
        found = owner.get(s)
        if found is None:
            continue
        r = found
        f = r["faces"][s]
        ck, pv = maps[index[id(r)]]
        n = len(f["tags"])
        new = np.arange(next_node, next_node + n, dtype=np.int64)
        next_node += n
        if n:
            gmsh.model.mesh.addNodes(2, s, new.tolist(), f["xyz"].tolist(), f["par"].tolist())
        own_sorted = np.argsort(f["tags"], kind="stable")
        own_keys = f["tags"][own_sorted]
        for etype, nodes in f["elements"]:
            mapped = np.empty_like(nodes)
            pos = np.searchsorted(own_keys, nodes)
            pos_c = np.clip(pos, 0, max(len(own_keys) - 1, 0))
            is_own = (len(own_keys) > 0) & (own_keys[pos_c] == nodes) if len(own_keys) else np.zeros(len(nodes), bool)
            mapped[is_own] = new[own_sorted[pos_c[is_own]]]
            rest = ~is_own
            if rest.any():
                q = np.searchsorted(ck, nodes[rest])
                q_c = np.clip(q, 0, len(ck) - 1)
                if not np.all(ck[q_c] == nodes[rest]):
                    raise RuntimeError(f"{RULE}: surface {s} uses a node no curve of the parent has")
                mapped[rest] = pv[q_c]
            per = gmsh.model.mesh.getElementProperties(etype)[3]
            count = len(nodes) // int(per)
            tags = list(range(next_el, next_el + count))
            next_el += count
            gmsh.model.mesh.addElementsByType(s, etype, tags, mapped.tolist())
    gmsh.model.mesh.renumberNodes()
    gmsh.model.mesh.renumberElements()


__all__ = ["BLOCKS", "MIN_SURFACES", "RULE", "blocks", "can_split", "mesh_blocks", "mesh_surfaces", "shares", "workers_for"]
