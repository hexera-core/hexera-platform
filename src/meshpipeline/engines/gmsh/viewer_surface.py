# Responsibility: Expose the surface the browser viewer draws for a Gmsh result, and the volume
# behind it so the viewer can colour that surface by cell quality.
# Boundaries: presentation of a delivered mesh; it judges nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_msh(workspace, *, by_face: bool = False, face_names: dict[int, str] | None = None) -> dict | None:
    """mesh.msh as the viewer needs it: points (P,3), the volume cells by kind (corner nodes
    only - a second-order element lists its corners first, and gmsh says how many), and the
    named boundary groups as polygons of point indices, in the order the surface is drawn.
    None when there is no mesh yet; {} when the file could not be read (logged). `by_face`: one
    patch per CAD face, named Face_k as the FEA deck names it (an FEA mesh, whose groups are not
    the user's faces), instead of one per physical group. `face_names`: an assembly's surface tag
    -> its name (Part_k_Face_j); the surfaces of one name are one patch."""
    msh = Path(workspace) / "mesh.msh"
    if not msh.exists() or msh.stat().st_size == 0:
        # A BIG FEA MESH STAYED IN THE BUCKET (engines/bucket_files, RUN-7): the mesher wrote its
        # surface, as this reads it, beside the record - read that, never the volume
        return load_viewer_mesh(Path(workspace)) if by_face else None
    import gmsh
    import numpy as np
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(msh))
        node_tags, coords, _ = gmsh.model.mesh.getNodes()
        node_tags = np.asarray(node_tags, dtype=np.int64)
        points = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
        index_of = np.full(int(node_tags.max()) + 1 if len(node_tags) else 1, -1, dtype=np.int64)
        index_of[node_tags] = np.arange(len(node_tags))

        def corners(et, nodes):
            _n, _d, _o, n_nodes, _c, n_primary = gmsh.model.mesh.getElementProperties(int(et))
            rows = np.asarray(nodes, dtype=np.int64).reshape(-1, int(n_nodes))[:, :int(n_primary)]
            return index_of[rows]

        cells: list = []
        etypes, _tags, enodes = gmsh.model.mesh.getElements(3)
        for et, nodes in zip(etypes, enodes):
            block = corners(et, nodes)
            if block.shape[1] in (4, 5, 6, 8) and len(block):
                cells.append(block)

        patches: list = []
        if by_face:
            seen: set = set()
            named: dict[str, list] = {}
            for dim, ptag in gmsh.model.getPhysicalGroups(2):
                for ent in gmsh.model.getEntitiesForPhysicalGroup(dim, ptag):
                    if int(ent) in seen:
                        continue
                    seen.add(int(ent))
                    face_polys: list = []
                    etypes, _etags, enodes = gmsh.model.mesh.getElements(2, ent)
                    for et, nodes in zip(etypes, enodes):
                        block = corners(et, nodes)
                        if block.shape[1] in (3, 4):
                            face_polys.extend(block.tolist())
                    if face_polys and face_names:
                        # AN ASSEMBLY: the face's own name, its pieces one patch
                        named.setdefault(face_names.get(int(ent)) or f"Face_{int(ent)}", []).extend(face_polys)
                    elif face_polys:
                        patches.append((f"Face_{int(ent)}", face_polys))
            if named:
                import re

                def key(name: str) -> tuple:
                    return tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name))

                patches = sorted(named.items(), key=lambda np_: key(np_[0]))
            else:
                patches.sort(key=lambda np_: int(np_[0].split("_")[1]))
            gmsh.model.remove()
            return {"points": points, "cells": cells, "patches": patches}
        for dim, ptag in gmsh.model.getPhysicalGroups(2):
            name = gmsh.model.getPhysicalName(dim, ptag) or f"group_{ptag}"
            polys: list = []
            for ent in gmsh.model.getEntitiesForPhysicalGroup(dim, ptag):
                etypes, _etags, enodes = gmsh.model.mesh.getElements(dim, ent)
                for et, nodes in zip(etypes, enodes):
                    block = corners(et, nodes)
                    if block.shape[1] in (3, 4):
                        polys.extend(block.tolist())
            if polys:
                patches.append((name, polys))
        gmsh.model.remove()
        return {"points": points, "cells": cells, "patches": patches}
    except Exception as exc:  # noqa: BLE001 - the viewer degrades, it never fails a job
        logger.warning("gmsh viewer surface parse failed: %s", exc)
        return {}
    finally:
        if _mine:
            gmsh.finalize()


def surface_patches_of(mesh: dict | None) -> dict[str, list]:
    """The drawn surface: patch name -> triangles as coordinate triples. A quad is drawn as two
    fan triangles, in the order render/volume_quality counts them."""
    if not mesh:
        return {}
    points = mesh["points"]
    out: dict[str, list] = {}
    for name, polys in mesh["patches"]:
        tris: list = []
        for poly in polys:
            p = [tuple(float(c) for c in points[i]) for i in poly]
            for k in range(1, len(p) - 1):
                tris.append([p[0], p[k], p[k + 1]])
        if tris:
            out[name] = tris
    return out


def surface_patches(workspace) -> dict[str, list]:
    return surface_patches_of(read_msh(workspace))


#: The most triangles the stand-in view of a big mesh draws: past it each face is thinned on its own
#: (its border kept exactly, so faces still meet), for a view a browser opens and a worker holds.
VIEW_MAX_TRIANGLES = 1_500_000


def downsample(mesh: dict, max_triangles: int = VIEW_MAX_TRIANGLES) -> dict:
    """`mesh` (read_msh's by-face form) with its surface thinned to about `max_triangles`: each patch
    decimated on its own (VTK's DecimatePro, topology kept, no border vertex removed) - the shape
    and every face's outline as before, fewer triangles inside. Unchanged when already under."""
    import numpy as np
    total = sum(max(0, len(poly) - 2) for _n, polys in mesh["patches"] for poly in polys)
    if total <= max_triangles:
        return mesh
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy
    keep = max_triangles / float(total)
    points = np.asarray(mesh["points"], dtype=np.float64)
    out_points: list = []
    out_patches: list = []
    at = 0
    for name, polys in mesh["patches"]:
        tris = np.asarray([[poly[0], poly[k], poly[k + 1]] for poly in polys for k in range(1, len(poly) - 1)],
                          dtype=np.int64)
        if not len(tris):
            continue
        used, local = np.unique(tris, return_inverse=True)
        local = local.reshape(-1, 3)
        pd = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        pts.SetData(numpy_to_vtk(points[used], deep=True))
        pd.SetPoints(pts)
        cells = vtk.vtkCellArray()
        cells.ImportLegacyFormat(numpy_to_vtkIdTypeArray(
            np.hstack([np.full((len(local), 1), 3, dtype=np.int64), local]).ravel(), deep=True))
        pd.SetPolys(cells)
        dec = vtk.vtkDecimatePro()
        dec.SetInputData(pd)
        dec.SetTargetReduction(max(0.0, min(0.99, 1.0 - keep)))
        dec.PreserveTopologyOn()
        dec.BoundaryVertexDeletionOff()
        dec.SplittingOff()
        dec.Update()
        res = dec.GetOutput()
        if res.GetNumberOfPolys():
            p = vtk_to_numpy(res.GetPoints().GetData())
            conn = vtk_to_numpy(res.GetPolys().GetConnectivityArray()).reshape(-1, 3)
        else:
            p, conn = points[used], local
        out_points.append(np.asarray(p, dtype=np.float64))
        out_patches.append((name, (conn + at).tolist()))
        at += len(p)
    return {"points": np.vstack(out_points) if out_points else points[:0], "cells": [], "patches": out_patches,
            "downsampled": {"triangles": total, "shown": sum(len(p) for _n, p in out_patches)}}


def save_viewer_mesh(path: Path, mesh: dict) -> None:
    """`mesh` (read_msh's by-face form) as the viewer needs it once the volume is gone: the points
    the surface uses and every patch's polygons - no volume cells (an FEA view draws none) - thinned
    to VIEW_MAX_TRIANGLES when the surface is bigger (downsample)."""
    import numpy as np
    mesh = downsample(mesh, VIEW_MAX_TRIANGLES)
    sizes: list[int] = []
    nodes: list[int] = []
    names: list[str] = []
    counts: list[int] = []
    for name, polys in mesh["patches"]:
        names.append(str(name))
        counts.append(len(polys))
        for poly in polys:
            sizes.append(len(poly))
            nodes.extend(int(i) for i in poly)
    flat = np.asarray(nodes, dtype=np.int64)
    used, inverse = np.unique(flat, return_inverse=True) if len(flat) else (flat, flat)
    with open(path, "wb") as fh:
        np.savez_compressed(fh, points=np.asarray(mesh["points"], dtype=np.float64)[used],
                            names=np.asarray(names, dtype=str), counts=np.asarray(counts, dtype=np.int64),
                            sizes=np.asarray(sizes, dtype=np.int64),
                            nodes=np.asarray(inverse, dtype=np.int64).reshape(-1),
                            full_triangles=np.asarray(
                                [int((mesh.get("downsampled") or {}).get("triangles") or 0)], dtype=np.int64))


def load_viewer_mesh(workspace: Path) -> dict | None:
    """The stand-in save_viewer_mesh wrote (engines/bucket_files.VIEWER_MESH_FILE), in read_msh's
    by-face form; None when there is none, {} when it cannot be read (logged)."""
    from meshpipeline.engines.bucket_files import VIEWER_MESH_FILE
    p = Path(workspace) / VIEWER_MESH_FILE
    if not p.is_file():
        return None
    import numpy as np
    try:
        with np.load(p, allow_pickle=False) as d:
            points, names, counts = d["points"], [str(n) for n in d["names"]], d["counts"]
            sizes, nodes = d["sizes"], d["nodes"]
        patches: list = []
        at = node_at = 0
        for name, count in zip(names, counts):
            polys = []
            for s in sizes[at:at + int(count)]:
                polys.append(nodes[node_at:node_at + int(s)].tolist())
                node_at += int(s)
            at += int(count)
            patches.append((name, polys))
        return {"points": points, "cells": [], "patches": patches}
    except Exception as exc:  # noqa: BLE001 - the viewer degrades, it never fails a job
        logger.warning("gmsh viewer stand-in unreadable: %s", exc)
        return {}


def write_surface_only(src: Path, dest: Path) -> bool:
    """`src` without its volume elements: the boundary and its named groups, for the reviewer's
    pictures of a mesh whose volume stayed in the bucket. True when written."""
    import gmsh
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(src))
        gmsh.model.removePhysicalGroups(gmsh.model.getPhysicalGroups(3))
        for _d, tag in gmsh.model.getEntities(3):
            gmsh.model.mesh.removeElements(3, tag)
        gmsh.model.mesh.reclassifyNodes()
        gmsh.option.setNumber("Mesh.SaveAll", 0)
        gmsh.write(str(dest))
        gmsh.model.remove()
        return dest.is_file() and dest.stat().st_size > 0
    finally:
        if _mine:
            gmsh.finalize()
