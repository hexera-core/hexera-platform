# Responsibility: The FEA quality post-pass (MESH-mmg-1): after gmsh has filled and optimised a part's straight tetrahedra, and before they are curved, the pinned MMG binary (mmg3d) improves the volumes that still hold an element the production limits would note - with the boundary frozen, so no node on a CAD face, curve or joint moves - and the result is kept only when it is measurably better and the boundary is exactly the one that went in.
# Boundaries: runs inside the gmsh FEA adapter's process on the current gmsh model, one volume at a time, between gmsh's optimiser and setOrder. It never touches a surface mesh, a named face, a joint, an embedded entity or an fTetWild part; a volume it cannot improve keeps its gmsh mesh to the byte. It decides no lever and no verdict.
# Collaborates with: engines/gmsh/fea_adapter.py (calls improve() and records what it did in quality.json), engines/fea_quality.py (the same element measures the verdict reads), Dockerfile (the pinned MMG build), docs/RULES.md MESH-mmg-1.
"""MMG, THE QUALITY POST-PASS (MESH-mmg-1).

gmsh's Delaunay fill and its Netgen optimiser leave a few poor elements on some parts: a sliver
between two nearly parallel faces, a needle along a short edge (Areen's C5 knuckle: scaled Jacobian
0.0029, aspect 153, angles 1.6 and 176.8 degrees - delivered "passed with notes"). MMG (Dapogny,
Dobrzynski, Frey; LGPL-3) is the open-source remesher built for exactly that: it swaps, collapses,
splits and moves inside a volume until its elements are well shaped, at the sizes the mesh already has.

WHAT IS FIXED, AND WHY (never guessed):
* ONLY THE INSIDE MOVES. mmg3d runs with -nosurf, and every boundary triangle and boundary node is
  also marked required: the surface mesh (every CAD face, every named group, every bonded or tied
  interface between parts) goes in and comes out the same triangles on the same nodes. The pass checks
  that, triangle by triangle, and keeps the gmsh mesh when it is not so.
* THE SIZES STAY. -optim keeps the local size the mesh has (the plan's zones, its through-wall count),
  with no gradation of its own (-hgrad -1).
* ONLY WHERE IT IS NEEDED. A volume is handed to MMG only when one of its straight elements would be
  noted on a delivered quadratic mesh: aspect over 20, a dihedral angle under 5 or over 170 degrees,
  or a scaled Jacobian under 0.01 (engines/fea_quality.LIMITS, limits_for(2)). A part with no such
  element is untouched, to the byte.
* NEVER WORSE. The volume's new elements are kept only when every one is valid, none of the worst
  figures (scaled Jacobian, aspect, smallest and largest dihedral) is worse, the count of elements
  over a limit is not higher, and at least one worst figure is better. Otherwise the gmsh mesh stays.
* REPEATABLE. mmg3d is single-threaded and deterministic: the same volume gives the same mesh.

THE ROOT CAUSE COMES FIRST. A sliver pinned by a CAD detail far below the element size (MESH-9, PLAN-fea-12)
is a geometry problem, fixed in the geometry; this pass is the safety net for what is left, and on a part
that has nothing left to fix it does nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from meshpipeline.settings.env import optional_env

RULE = "MESH-mmg-1"
BIN_NAME = "mmg3d_O3"
BIN_PATH = "/opt/mmg/bin/mmg3d_O3"
#: FEA_MMG=off: no post-pass (the gmsh mesh as it is filled and optimised)
ENABLED: bool = (optional_env("FEA_MMG", "on") or "on").strip().lower() not in ("off", "false", "0", "no")
#: the limits a delivered QUADRATIC mesh is noted on (engines/fea_quality.limits_for(2)), read on the
#: straight element: what MMG can still change before the elements are curved
TRIGGER_SJ = 0.01
TRIGGER_ASPECT = 20.0
TRIGGER_MIN_DIHEDRAL = 5.0
TRIGGER_MAX_DIHEDRAL = 170.0
#: the limits counted for "never more elements over a limit" (the straight production limits)
_COUNT_LIMITS = (("scaled_jacobian", "<", 0.2), ("aspect", ">", 20.0), ("min_dihedral", "<", 5.0),
                 ("max_dihedral", ">", 170.0))
#: mmg3d's options: frozen surface, local size kept, no gradation, quiet
OPTIONS = ("-nosurf", "-optim", "-hgrad", "-1", "-v", "-1")
#: the time one volume may take: a base plus this many seconds per element
TIME_BASE_S = 120.0
TIME_PER_ELEMENT_S = 5e-4
#: a boundary node read back is the node that went in when it lies this close (a share of the box's
#: diagonal: mmg's ASCII writes 15 significant digits)
MATCH_SHARE = 1e-9


def binary() -> str | None:
    """The mmg3d binary the mesh image ships, or None where it is not installed."""
    found = shutil.which(BIN_NAME)
    if found:
        return found
    return BIN_PATH if Path(BIN_PATH).exists() else None


# #
# the figures (pure)
# #

def worst(xyz: np.ndarray, tets: np.ndarray, on_boundary: np.ndarray | None = None) -> dict:
    """The worst straight-element figures of tets (E, 4) into xyz, and how many elements miss each
    straight production limit. With `on_boundary` (a per-node mask), also how many of the elements the
    pass is for have every corner on the boundary: those the frozen surface pins, which no move inside
    the volume can reshape."""
    from meshpipeline.engines.fea_quality import element_measures

    if len(tets) == 0:
        return {"elements": 0}
    m = element_measures(xyz[tets])
    over = 0
    for key, op, lim in _COUNT_LIMITS:
        over += int(np.count_nonzero(m[key] < lim if op == "<" else m[key] > lim))
    trig = ((m["scaled_jacobian"] < TRIGGER_SJ) | (m["aspect"] > TRIGGER_ASPECT)
            | (m["min_dihedral"] < TRIGGER_MIN_DIHEDRAL) | (m["max_dihedral"] > TRIGGER_MAX_DIHEDRAL))
    out = {"elements": int(len(tets)), "invalid": int(np.count_nonzero(m["min_detj"] <= 0)),
           "min_sj": float(np.min(m["scaled_jacobian"])), "max_aspect": float(np.max(m["aspect"])),
           "min_dihedral": float(np.min(m["min_dihedral"])), "max_dihedral": float(np.max(m["max_dihedral"])),
           "over_limits": over, "triggered": int(np.count_nonzero(trig))}
    if on_boundary is not None:
        corners = on_boundary[tets].sum(axis=1)
        out["triggered_pinned"] = int(np.count_nonzero(trig & (corners == 4)))
        # standing on a surface triangle: only the fourth corner can move
        out["triggered_on_face"] = int(np.count_nonzero(trig & (corners == 3)))
    return out


def better(before: dict, after: dict, *, tol: float = 1e-9) -> tuple[bool, str]:
    """(keep, why): the after figures are kept only when nothing is worse and something is better."""
    if not after.get("elements"):
        return False, "no elements came back"
    if after.get("invalid"):
        return False, f"{after['invalid']} invalid element(s)"
    worse = [k for k, low in (("min_sj", True), ("max_aspect", False), ("min_dihedral", True),
                              ("max_dihedral", False))
             if (after[k] < before[k] - tol if low else after[k] > before[k] + tol)]
    if worse:
        return False, "worse on " + ", ".join(worse)
    if after["over_limits"] > before["over_limits"]:
        return False, f"more elements over a limit ({after['over_limits']} > {before['over_limits']})"
    gained = [k for k, low in (("min_sj", True), ("max_aspect", False), ("min_dihedral", True),
                               ("max_dihedral", False))
              if (after[k] > before[k] + tol if low else after[k] < before[k] - tol)]
    if not gained and after["over_limits"] >= before["over_limits"]:
        return False, "no better"
    return True, "better on " + (", ".join(gained) or "elements over a limit")


# #
# Medit files (pure)
# #

def write_medit(path: Path, xyz: np.ndarray, tris: np.ndarray, tri_refs: np.ndarray, tets: np.ndarray,
                required_vertices: np.ndarray) -> None:
    """A Medit .mesh: vertices (0-based arrays in, 1-based in the file), triangles with their refs,
    tetrahedra (ref 1), every triangle and the given vertices required."""
    with open(path, "w", encoding="ascii") as fh:
        fh.write("MeshVersionFormatted 2\nDimension 3\n")
        fh.write(f"Vertices\n{len(xyz)}\n")
        fh.write("".join(f"{x:.17g} {y:.17g} {z:.17g} 0\n" for x, y, z in xyz))
        fh.write(f"Triangles\n{len(tris)}\n")
        fh.write("".join(f"{a + 1} {b + 1} {c + 1} {int(r)}\n" for (a, b, c), r in zip(tris, tri_refs, strict=True)))
        fh.write(f"Tetrahedra\n{len(tets)}\n")
        fh.write("".join(f"{a + 1} {b + 1} {c + 1} {d + 1} 1\n" for a, b, c, d in tets))
        fh.write(f"RequiredTriangles\n{len(tris)}\n")
        fh.write("".join(f"{i + 1}\n" for i in range(len(tris))))
        fh.write(f"RequiredVertices\n{len(required_vertices)}\n")
        fh.write("".join(f"{int(i) + 1}\n" for i in required_vertices))
        fh.write("End\n")


def read_medit(path: Path) -> dict[str, np.ndarray]:
    """{xyz, tris, tri_refs, tets} of a Medit .mesh (0-based)."""
    tok = Path(path).read_text(encoding="ascii", errors="replace").split()
    out: dict[str, np.ndarray] = {"xyz": np.zeros((0, 3)), "tris": np.zeros((0, 3), dtype=np.int64),
                                  "tri_refs": np.zeros(0, dtype=np.int64), "tets": np.zeros((0, 4), dtype=np.int64)}
    width = {"Vertices": 4, "Triangles": 4, "Tetrahedra": 5, "Edges": 3, "Corners": 1, "Ridges": 1,
             "RequiredVertices": 1, "RequiredTriangles": 1, "RequiredEdges": 1, "Normals": 3,
             "NormalAtVertices": 2, "Tangents": 3, "TangentAtVertices": 2, "Quadrilaterals": 5, "Prisms": 7,
             "RequiredTetrahedra": 1}
    i = 0
    while i < len(tok):
        key = tok[i]
        if key in ("MeshVersionFormatted", "Dimension"):
            i += 2
            continue
        if key == "End":
            break
        if key not in width:
            raise ValueError(f"unknown Medit keyword {key!r}")
        n = int(tok[i + 1])
        w = width[key]
        block = tok[i + 2:i + 2 + n * w]
        i += 2 + n * w
        if key == "Vertices":
            out["xyz"] = np.asarray(block, dtype=float).reshape(n, 4)[:, :3]
        elif key == "Triangles":
            a = np.asarray(block, dtype=np.int64).reshape(n, 4)
            out["tris"], out["tri_refs"] = a[:, :3] - 1, a[:, 3]
        elif key == "Tetrahedra":
            out["tets"] = np.asarray(block, dtype=np.int64).reshape(n, 5)[:, :4] - 1
    return out


def _oriented(xyz: np.ndarray, tets: np.ndarray) -> np.ndarray:
    """tets with a positive volume (gmsh's and Medit's convention), swapping two corners where not."""
    if len(tets) == 0:
        return tets
    p = xyz[tets]
    det = np.einsum("ij,ij->i", p[:, 3] - p[:, 0], np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]))
    t = tets.copy()
    neg = det < 0
    t[neg, 1], t[neg, 2] = tets[neg, 2], tets[neg, 1]
    return t


# #
# one volume through mmg3d
# #

def remesh(xyz: np.ndarray, tris: np.ndarray, tri_refs: np.ndarray, tets: np.ndarray, *, exe: str,
           timeout: float, workdir: Path) -> dict:
    """mmg3d on one volume's mesh (0-based arrays; the boundary = tris). Returns {ok, why, xyz, tets,
    boundary_map (new vertex -> input vertex or -1), seconds}."""
    t0 = time.monotonic()
    bnodes = np.unique(tris)
    src, dst = workdir / "in.mesh", workdir / "out.mesh"
    write_medit(src, xyz, tris, tri_refs, tets, bnodes)
    try:
        cp = subprocess.run([exe, "-in", str(src), "-out", str(dst), *OPTIONS], capture_output=True, text=True,
                            timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": f"mmg3d over its {timeout:.0f} s", "seconds": time.monotonic() - t0}
    if cp.returncode != 0 or not dst.exists():
        return {"ok": False, "why": f"mmg3d exit {cp.returncode}: {(cp.stderr or cp.stdout)[-300:].strip()}",
                "seconds": time.monotonic() - t0}
    got = read_medit(dst)
    from scipy.spatial import cKDTree

    diag = float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0))) or 1.0
    tree = cKDTree(xyz[bnodes])
    d, k = tree.query(got["xyz"])
    hit = d <= MATCH_SHARE * diag
    bmap = np.where(hit, bnodes[k], -1)
    # THE BOUNDARY CAME BACK AS IT WENT IN: the same triangles, on the same nodes
    if len(got["tris"]) != len(tris) or (bmap[got["tris"]] < 0).any():
        return {"ok": False, "why": "the boundary did not come back as it went in", "seconds": time.monotonic() - t0}
    want = {tuple(sorted(t)) for t in tris.tolist()}
    back = {tuple(sorted(t)) for t in bmap[got["tris"]].tolist()}
    if want != back or int(hit.sum()) != len(bnodes):
        return {"ok": False, "why": "the boundary did not come back as it went in", "seconds": time.monotonic() - t0}
    # the boundary nodes keep their exact coordinates; the inside ones are mmg's
    new_xyz = got["xyz"].copy()
    new_xyz[hit] = xyz[bmap[hit]]
    tets_new = _oriented(new_xyz, got["tets"])
    return {"ok": True, "why": "", "xyz": new_xyz, "tets": tets_new, "boundary_map": bmap,
            "seconds": time.monotonic() - t0}


# #
# the gmsh side
# #

def _volume_arrays(gmsh, v: int):
    """(node tags, xyz, tets as node indices, boundary tris as node indices, their surface refs) of
    volume v's straight tetrahedra, or None when the volume holds anything else."""
    types, _etags, _ = gmsh.model.mesh.getElements(3, v)
    if [int(t) for t in types] != [4]:
        return None
    _et, enodes = gmsh.model.mesh.getElementsByType(4, v)
    tet_tags = np.asarray(enodes, dtype=np.int64).reshape(-1, 4)
    surfs = [int(s) for _d, s in gmsh.model.getBoundary([(3, v)], combined=False, oriented=False, recursive=False)]
    tri_tags, refs = [], []
    for s in surfs:
        stypes, _st, _sn = gmsh.model.mesh.getElements(2, s)
        if [int(t) for t in stypes] != [2]:
            return None
        _t, tn = gmsh.model.mesh.getElementsByType(2, s)
        a = np.asarray(tn, dtype=np.int64).reshape(-1, 3)
        tri_tags.append(a)
        refs.append(np.full(len(a), s, dtype=np.int64))
    if not tri_tags:
        return None
    tris_t = np.concatenate(tri_tags)
    ntags, coords, _ = gmsh.model.mesh.getNodes(3, v, includeBoundary=True, returnParametricCoord=False)
    ntags = np.asarray(ntags, dtype=np.int64)
    xyz = np.asarray(coords, dtype=float).reshape(-1, 3)
    order = np.argsort(ntags)
    ntags, xyz = ntags[order], xyz[order]
    # boundary nodes the volume call may not list (a node on a curve or point of the boundary)
    need = np.setdiff1d(np.unique(np.concatenate([tris_t.ravel(), tet_tags.ravel()])), ntags)
    if len(need):
        extra = np.array([gmsh.model.mesh.getNode(int(n))[0] for n in need], dtype=float).reshape(-1, 3)
        ntags = np.concatenate([ntags, need])
        xyz = np.vstack([xyz, extra])
        order = np.argsort(ntags)
        ntags, xyz = ntags[order], xyz[order]
    idx = {int(t): i for i, t in enumerate(ntags.tolist())}
    to_i = np.vectorize(idx.__getitem__, otypes=[np.int64])
    return ntags, xyz, to_i(tet_tags), to_i(tris_t), np.concatenate(refs)


def _replace(gmsh, v: int, ntags: np.ndarray, out: dict) -> None:
    """Volume v's mesh replaced by mmg's: its inside nodes and its tetrahedra (the boundary's nodes,
    classified on its faces, curves and points, are left alone and reused)."""
    bmap = out["boundary_map"]
    inside = np.flatnonzero(bmap < 0)
    gmsh.model.mesh.clear([(3, v)])
    next_node = int(gmsh.model.mesh.getMaxNodeTag()) + 1
    tag_of = np.empty(len(bmap), dtype=np.int64)
    tag_of[bmap >= 0] = ntags[bmap[bmap >= 0]]
    tag_of[inside] = np.arange(next_node, next_node + len(inside))
    if len(inside):
        gmsh.model.mesh.addNodes(3, v, tag_of[inside].tolist(), out["xyz"][inside].ravel().tolist())
    next_el = int(gmsh.model.mesh.getMaxElementTag()) + 1
    tets = tag_of[out["tets"]]
    gmsh.model.mesh.addElementsByType(v, 4, list(range(next_el, next_el + len(tets))), tets.ravel().tolist())


def improve(gmsh, vols, *, enabled: bool | None = None, exe: str | None = None) -> dict:
    """MESH-mmg-1 on every listed volume of the current gmsh model (straight tetrahedra, before
    setOrder). Returns the record quality.json carries: {rule, ran, volumes: [...], kept, seconds} or
    {rule, ran: False, why}."""
    if not (ENABLED if enabled is None else enabled):
        return {"rule": RULE, "ran": False, "why": "off (FEA_MMG=off)"}
    exe = exe or binary()
    if not exe:
        return {"rule": RULE, "ran": False, "why": "mmg3d is not installed in this image"}
    t0 = time.monotonic()
    rows: list[dict] = []
    for v in sorted(int(x) for x in vols):
        try:
            if gmsh.model.mesh.getEmbedded(3, v):
                rows.append({"volume": v, "kept": False, "why": "has embedded entities"})
                continue
        except Exception:  # noqa: BLE001 - an older gmsh without the call: nothing is embedded by the FEA path
            pass
        arrays = _volume_arrays(gmsh, v)
        if arrays is None:
            rows.append({"volume": v, "kept": False, "why": "not straight tetrahedra"})
            continue
        ntags, xyz, tets, tris, refs = arrays
        on_b = np.zeros(len(xyz), dtype=bool)
        on_b[np.unique(tris)] = True
        before = worst(xyz, tets, on_b)
        if not before.get("triggered"):
            continue                                # nothing a delivered mesh would be noted on
        if before.get("triggered_pinned") == before["triggered"]:
            # every element it is for has all four corners on the frozen surface: nothing inside can move
            rows.append({"volume": v, "before": before, "kept": False, "seconds": 0.0,
                         "why": "every element it is for has all its corners on the surface, which stays as meshed"})
            continue
        with tempfile.TemporaryDirectory(prefix="fea_mmg_") as td:
            out = remesh(xyz, tris, refs, tets, exe=exe, workdir=Path(td),
                         timeout=TIME_BASE_S + TIME_PER_ELEMENT_S * len(tets))
        row: dict = {"volume": v, "mmg": True, "before": before, "seconds": round(float(out["seconds"]), 2)}
        if not out["ok"]:
            rows.append({**row, "kept": False, "why": out["why"]})
            continue
        after = worst(out["xyz"], out["tets"], out["boundary_map"] >= 0)
        keep, why = better(before, after)
        row.update(after=after, kept=keep, why=why)
        if keep:
            _replace(gmsh, v, ntags, out)
        rows.append(row)
    return {"rule": RULE, "ran": True, "volumes": rows, "kept": sum(1 for r in rows if r.get("kept")),
            "tried": sum(1 for r in rows if r.get("mmg")), "seconds": round(time.monotonic() - t0, 2)}


def words(record: dict) -> str:
    """One line for the log and the result: what the pass did."""
    if not record or not record.get("ran"):
        return ""
    kept = [r for r in record.get("volumes") or [] if r.get("kept")]
    if not kept:
        return ""
    b = min(r["before"]["min_sj"] for r in kept)
    a = min(r["after"]["min_sj"] for r in kept)
    return (f"MMG improved the elements inside {len(kept)} part volume(s), the surface left as meshed "
            f"(worst scaled Jacobian {b:.3g} -> {a:.3g}; {RULE})")


__all__ = ["BIN_PATH", "ENABLED", "OPTIONS", "RULE", "better", "binary", "improve", "read_medit", "remesh",
           "words", "worst", "write_medit"]
