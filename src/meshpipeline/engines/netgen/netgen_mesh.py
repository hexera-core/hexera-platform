# Responsibility: Mesh one staged fluid boundary with Netgen in the mesh image: tetrahedra with prism layers on the wall, every patch under its staged name, written as a gmsh 2.2 volume file.
# Owns: reading the staged named-solid STL, Netgen's surface and volume meshing, naming each Netgen face from the staged patch it lies on, the node order gmsh expects, and the run's report.
# Boundaries: a STANDALONE SCRIPT run by the netgen interpreter (/opt/netgen-env, NETGEN_PYTHON) as a subprocess - it imports only netgen, numpy, scipy and the standard library, never meshpipeline. It writes files into the workspace and decides nothing about the plan: netgen_spec.json says what to build.
# Collaborates with: engines/netgen/runner.py (runs it, converts its output with gmshToFoam), engines/netgen/plan.py (writes the spec it reads).
"""usage: netgen-python netgen_mesh.py <workspace>

Reads   <ws>/fluid_boundary.stl   the staged fluid boundary: one named ASCII solid per patch, metres
        <ws>/netgen_spec.json     the plan (engines/netgen/plan.py): sizes, layers, angles
        <ws>/netgen_size.txt      optional: local cell sizes at wall points (Netgen's meshsize file)
Writes  <ws>/mesh_volume.msh      gmsh 2.2 ASCII: the tetrahedra and prisms (physical "fluid") and
                                  every boundary face under the physical name of its staged patch
        <ws>/netgen_report.json   what was built: cells by type, patches, layer settings, seconds
Exit 0 when a volume was written, 2 when Netgen produced no volume, 3 when a face could not be named.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

#: gmsh 2.2 element type of each Netgen volume element, by its node count
GMSH_VOLUME = {4: 4, 5: 7, 6: 6, 8: 5}
#: the physical tag of the fluid volume (patches count from 1)
FLUID_TAG = 1000
#: a Netgen face is named by the staged patch most of its triangles lie on; a face whose share of
#: its majority patch is below this is split element by element instead
FACE_PURITY = 0.98


def read_solids(path: Path) -> dict[str, np.ndarray]:
    """Every named solid of an ASCII STL as an (n, 3, 3) array; a name written as several solids
    (a wall in two pieces) is one entry."""
    solids: dict[str, list] = {}
    name, cur = "wall", []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("endsolid"):
                if cur:
                    solids.setdefault(name, []).append(np.asarray(cur, float).reshape(-1, 3, 3))
                cur = []
            elif s.startswith("solid"):
                name, cur = (s[5:].strip() or "wall"), []
            elif s.startswith("vertex"):
                cur.append([float(v) for v in s.split()[1:4]])
    return {k: np.concatenate(v) for k, v in solids.items()}


def write_plain_stl(path: Path, tris: np.ndarray) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("solid fluid\n")
        for t in tris:
            n = np.cross(t[1] - t[0], t[2] - t[0])
            n = n / (np.linalg.norm(n) or 1.0)
            fh.write(f"facet normal {n[0]:.9e} {n[1]:.9e} {n[2]:.9e}\nouter loop\n")
            for v in t:
                fh.write(f"vertex {v[0]:.12e} {v[1]:.12e} {v[2]:.12e}\n")
            fh.write("endloop\nendfacet\n")
        fh.write("endsolid fluid\n")


def oriented(cells: np.ndarray, pts: np.ndarray, nverts: int) -> tuple[np.ndarray, int]:
    """Volume elements in gmsh's node order (positive volume). Netgen numbers its elements the
    other way round; each element is checked on its own, so a mixed mesh comes out right too."""
    a = np.asarray(cells, dtype=np.int64)
    if not len(a):
        return a, 0
    X = pts[a]
    if nverts == 4:
        s = np.einsum("ij,ij->i", np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0]), X[:, 3] - X[:, 0])
        swap = [0, 2, 1, 3]
    elif nverts == 6:
        n = np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0])
        s = np.einsum("ij,ij->i", n, X[:, 3:6].mean(1) - X[:, 0:3].mean(1))
        swap = [0, 2, 1, 3, 5, 4]
    elif nverts == 5:
        n = np.cross(X[:, 2] - X[:, 0], X[:, 3] - X[:, 1])
        s = np.einsum("ij,ij->i", n, X[:, 4] - X[:, 0:4].mean(1))
        swap = [0, 3, 2, 1, 4]
    else:
        n = np.cross(X[:, 2] - X[:, 0], X[:, 3] - X[:, 1])
        s = np.einsum("ij,ij->i", n, X[:, 4:8].mean(1) - X[:, 0:4].mean(1))
        swap = [0, 3, 2, 1, 4, 7, 6, 5]
    bad = s < 0
    a[bad] = a[bad][:, swap]
    return a, int(bad.sum())


def point_triangle_distance(p: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Distance from each point p[i] to the triangle tri[i] (Ericson's closest-point regions)."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v = np.where(denom != 0, vb / denom, 0.0)
        w = np.where(denom != 0, vc / denom, 0.0)
        q = a + ab * v[:, None] + ac * w[:, None]                    # inside the face
        t_ab = np.clip(np.where(d1 - d3 != 0, d1 / (d1 - d3), 0), 0, 1)
        t_ac = np.clip(np.where(d2 - d6 != 0, d2 / (d2 - d6), 0), 0, 1)
        t_bc = np.clip(np.where((d4 - d3) + (d5 - d6) != 0, (d4 - d3) / ((d4 - d3) + (d5 - d6)), 0), 0, 1)
    e_ab = a + ab * t_ab[:, None]
    e_ac = a + ac * t_ac[:, None]
    e_bc = b + (c - b) * t_bc[:, None]
    inside = (va >= 0) & (vb >= 0) & (vc >= 0)
    cands = np.stack([np.where(inside[:, None], q, e_ab), e_ab, e_ac, e_bc], axis=1)
    return np.min(np.linalg.norm(cands - p[:, None, :], axis=2), axis=1)


#: the most sample points spread over the staged triangles for naming (the spacing widens to fit)
SAMPLES_MAX = 3_000_000


def sample_triangles(tris: np.ndarray, spacing: float) -> tuple[np.ndarray, np.ndarray]:
    """Points over every triangle no further apart than about `spacing` (each triangle's corners,
    centre and a barycentric grid as fine as its longest edge needs), and the triangle each point
    belongs to. The spacing widens until the count fits SAMPLES_MAX."""
    longest = np.max(np.stack([np.linalg.norm(tris[:, i] - tris[:, (i + 1) % 3], axis=1)
                               for i in range(3)]), axis=0)
    s = max(float(spacing), 1e-12)
    while True:
        n = np.clip(np.ceil(longest / s).astype(np.int64), 1, 400)
        if int(((n + 1) * (n + 2) // 2).sum()) <= SAMPLES_MAX or s > 1e6:
            break
        s *= 1.5
    pts, own = [tris.mean(axis=1)], [np.arange(len(tris))]
    for m in np.unique(n):
        sel = np.flatnonzero(n == m)
        ij = np.array([(i, j) for i in range(m + 1) for j in range(m + 1 - i)], float) / m
        w = np.column_stack([1.0 - ij.sum(axis=1), ij[:, 0], ij[:, 1]])        # (q, 3)
        p = np.einsum("qk,tkd->tqd", w, tris[sel]).reshape(-1, 3)
        pts.append(p)
        own.append(np.repeat(sel, len(w)))
    return np.concatenate(pts), np.concatenate(own)


def name_faces(mesh, tris: np.ndarray, labels: np.ndarray, names: list[str]) -> dict:
    """Give every Netgen surface face the name of the staged patch it lies on (nearest staged
    triangle of each surface element, majority per face). A face that straddles two patches - an
    opening whose rim Netgen did not keep as an edge - cannot carry one name: it is reported, and
    the run fails rather than deliver an opening of the wrong size."""
    from scipy.spatial import cKDTree
    pts = np.asarray(mesh.Coordinates(), float)
    a2 = mesh.Elements2D().NumPy()
    fi = np.asarray(a2["index"], dtype=np.int64)
    tri3 = np.asarray(a2["nodes"], dtype=np.int64).reshape(len(a2), -1)[:, :3] - 1
    cen = pts[tri3].mean(axis=1)
    # CANDIDATES FROM POINTS SPREAD OVER EVERY STAGED TRIANGLE, not from its centre: a CAD
    # tessellation's cylinder is slivers spanning the whole pipe, whose centres lie a pipe-length
    # from the rim - by centre, every wall element next to an opening found only the lid's triangles
    # (a bend's wall read 8% inlet/outlet). The exact distance then picks among the candidates.
    edge = np.linalg.norm(pts[tri3[:, 1]] - pts[tri3[:, 0]], axis=1)
    sp, owner = sample_triangles(tris, float(np.median(edge)) if len(edge) else 1.0)
    k = min(16, len(sp))
    _d, cand = cKDTree(sp).query(cen, k=k)
    cand = owner[np.asarray(cand).reshape(len(cen), k)]
    dist = np.stack([point_triangle_distance(cen, tris[cand[:, c]]) for c in range(k)], axis=1)
    lab = labels[cand[np.arange(len(cen)), np.argmin(dist, axis=1)]]
    out: dict = {"faces": {}, "mixed": []}
    for f in sorted(set(fi.tolist())):
        m = fi == f
        vals, cnt = np.unique(lab[m], return_counts=True)
        best = int(vals[np.argmax(cnt)])
        share = float(cnt.max() / cnt.sum())
        mesh.SetBCName(f - 1, names[best])
        out["faces"][int(f)] = {"name": names[best], "elements": int(cnt.sum()),
                                "share": round(share, 4)}
        if share < FACE_PURITY:
            out["mixed"].append({"face": int(f), "names": {names[int(v)]: int(c)
                                                           for v, c in zip(vals, cnt)}})
    return out


def export_gmsh(mesh, path: Path) -> dict:
    """The volume as a gmsh 2.2 ASCII file gmshToFoam reads: the boundary faces (triangles and the
    quads where a layer meets an opening) under their patch names, the volume as one physical."""
    pts = np.asarray(mesh.Coordinates(), float)
    interior = {i + 1 for i in range(mesh.GetNFaceDescriptors())
                if mesh.FaceDescriptor(i + 1).domin > 0 and mesh.FaceDescriptor(i + 1).domout > 0}
    # THE ELEMENT TABLES AS ARRAYS (Netgen's NumPy views: 1-based nodes, 0 where an element has
    # fewer than the table's width) - a Python object per element held an 8M-cell mesh's export
    # past a 20 GB slot
    vols: dict[int, np.ndarray] = {}
    a3 = mesh.Elements3D().NumPy()
    nodes3 = np.asarray(a3["nodes"], dtype=np.int64).reshape(len(a3), -1)
    n3 = (nodes3 > 0).sum(axis=1)
    for nv in np.unique(n3):
        vols[int(nv)] = nodes3[n3 == nv][:, :int(nv)] - 1
    a2 = mesh.Elements2D().NumPy()
    nodes2 = np.asarray(a2["nodes"], dtype=np.int64).reshape(len(a2), -1)
    idx2 = np.asarray(a2["index"], dtype=np.int64)
    keep = ~np.isin(idx2, list(interior)) if interior else np.ones(len(idx2), bool)
    nodes2, idx2 = nodes2[keep], idx2[keep]
    n2 = (nodes2 > 0).sum(axis=1)
    surf: dict[int, list] = {}
    for nv in np.unique(n2):
        m = n2 == nv
        surf[int(nv)] = list(zip((nodes2[m][:, :int(nv)] - 1).tolist(), idx2[m].tolist()))
    bc = {i + 1: mesh.GetBCName(i) for i in range(mesh.GetNFaceDescriptors())}
    names = sorted({bc[i] for lst in surf.values() for _v, i in lst})
    tag = {n: k + 1 for k, n in enumerate(names)}
    flipped = 0
    for k in list(vols):
        arr, n = oriented(np.asarray(vols[k]), pts, k)
        vols[k] = arr
        flipped += n
    n_el = sum(len(v) for v in surf.values()) + sum(len(v) for v in vols.values())
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$PhysicalNames\n{len(names) + 1}\n")
        for n in names:
            f.write(f'2 {tag[n]} "{n}"\n')
        f.write(f'3 {FLUID_TAG} "fluid"\n$EndPhysicalNames\n$Nodes\n{len(pts)}\n')
        np.savetxt(f, np.column_stack([np.arange(1, len(pts) + 1), pts]), fmt="%d %.14g %.14g %.14g")
        f.write(f"$EndNodes\n$Elements\n{n_el}\n")
        k = 0
        for nv in sorted(surf):
            lst = surf[nv]
            t = np.array([tag[bc[i]] for _v, i in lst], dtype=np.int64)
            conn = np.array([v for v, _i in lst], dtype=np.int64) + 1
            rows = np.column_stack([np.arange(k + 1, k + len(lst) + 1),
                                    np.full(len(lst), 2 if nv == 3 else 3), np.full(len(lst), 2),
                                    t, t, conn])
            np.savetxt(f, rows, fmt="%d")
            k += len(lst)
        for nv in sorted(vols):
            a = vols[nv]
            rows = np.column_stack([np.arange(k + 1, k + len(a) + 1), np.full(len(a), GMSH_VOLUME[nv]),
                                    np.full(len(a), 2), np.full(len(a), FLUID_TAG),
                                    np.full(len(a), FLUID_TAG), a + 1])
            np.savetxt(f, rows, fmt="%d")
            k += len(a)
        f.write("$EndElements\n")
    counts = {"tetrahedra": len(vols.get(4, ())), "prisms": len(vols.get(6, ())),
              "pyramids": len(vols.get(5, ())), "hexahedra": len(vols.get(8, ()))}
    faces = dict.fromkeys(names, 0)
    for lst in surf.values():
        for _v, i in lst:
            faces[bc[i]] += 1
    return {"cells": int(sum(counts.values())), "by_type": counts, "patch_faces": faces,
            "points": int(len(pts)), "reoriented": int(flipped)}


def build(ws: Path, spec: dict, rung: dict) -> tuple[object, dict]:
    import netgen.meshing as ngm
    from netgen.meshing import MeshingParameters, MeshingStep
    from netgen.stl import STLGeometry

    solids = read_solids(ws / "fluid_boundary.stl")
    names = list(solids)
    tris = np.concatenate([solids[n] for n in names])
    labels = np.concatenate([np.full(len(solids[n]), i) for i, n in enumerate(names)])
    plain = ws / "_netgen_surface.stl"
    write_plain_stl(plain, tris)
    geo = STLGeometry(str(plain))
    kw = {"maxh": float(rung["maxh"]), "grading": float(spec.get("grading", 0.3)),
          "curvaturesafety": float(spec.get("curvature_safety", 2.0)),
          "segmentsperedge": float(spec.get("segments_per_edge", 1.0)),
          "yangle": float(spec.get("feature_angle", 30.0)),
          "contyangle": float(spec.get("continue_angle", 20.0)),
          "edgecornerangle": float(spec.get("corner_angle", 60.0)),
          "closeedgefac": float(spec.get("close_edge_factor", 2.0)),
          "minedgelen": float(spec["min_edge"]), "optsteps3d": int(spec.get("opt_steps_3d", 3)),
          "perfstepsend": MeshingStep.MESHSURFACE}
    size_file = ws / "netgen_size.txt"
    if size_file.exists() and rung.get("size_file", True):
        kw["meshsizefilename"] = str(size_file)
    mp = MeshingParameters(**kw)
    mesh = geo.GenerateMesh(mp=mp)
    naming = name_faces(mesh, tris, labels, names)
    walls = [n for n in names if n in set(spec.get("walls") or ["wall"])]
    ports = [n for n in names if n not in walls]
    layers = []
    n_layers = int(rung.get("layers", 0) or 0)
    if n_layers > 0 and walls:
        first, growth = float(rung["first_layer"]), float(rung.get("growth", 1.2))
        thick = [first * growth ** k for k in range(n_layers)]
        layers = [ngm.BoundaryLayerParameters(
            boundary="|".join(walls), thickness=thick, domain=".*", outside=False,
            project_boundaries=("|".join(ports) or None),
            limit_growth_vectors=bool(rung.get("limit_growth", spec.get("limit_growth", True))))]
    mp3 = MeshingParameters(mp, perfstepsstart=int(spec.get("volume_start_step", 5)),
                            perfstepsend=int(spec.get("volume_end_step", 7)))
    geo.GenerateMesh(mp=mp3, mesh=mesh, boundary_layers=layers)
    return mesh, {"naming": naming, "walls": walls, "ports": ports}


def main(argv: list[str]) -> int:
    """Build ONE rung of the plan (argv[2], counted from 1); the runner walks the ladder, because
    only it can judge the result (gmshToFoam + checkMesh live outside this interpreter)."""
    ws = Path(argv[1])
    k = int(argv[2]) if len(argv) > 2 else 1
    t0 = time.time()
    spec = json.loads((ws / "netgen_spec.json").read_text())
    rung = (spec.get("rungs") or [])[k - 1]
    out = ws / f"netgen_rung{k}.json"
    entry: dict = {"rung": k, **rung}
    rc = 2
    (ws / "mesh_volume.msh").unlink(missing_ok=True)
    try:
        mesh, info = build(ws, spec, rung)
        entry.update(naming=info["naming"])
        if info["naming"]["mixed"] and not spec.get("allow_mixed_faces"):
            entry.update(ok=False, why="a surface face straddles two patches: an opening's rim "
                                       "was not kept as an edge")
            rc = 3
        elif len(mesh.Elements3D()) == 0:
            entry.update(ok=False, why="Netgen made no volume elements")
        else:
            entry.update(export_gmsh(mesh, ws / "mesh_volume.msh"), ok=True)
            rc = 0
    except Exception as exc:  # noqa: BLE001 - reported; the runner decides the next rung
        entry.update(ok=False, why=f"{type(exc).__name__}: {exc}"[:600])
    entry["seconds"] = round(time.time() - t0, 1)
    out.write_text(json.dumps(entry, indent=1))
    print(f"[netgen] rung {k}: " + json.dumps({x: entry.get(x) for x in ("ok", "why", "cells", "by_type", "seconds")}),
          flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
