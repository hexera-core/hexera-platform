# Responsibility: Read every accepted mesh format into one SurfaceMesh, keeping the named groups and bodies the file carries.
# Owns: one reader per format, the volume-mesh boundary extraction, and the group names each format stores in its own way.
# Boundaries: in-memory and bounded; it writes nothing (canonical.py does) and resolves no reference outside the file.
# Collaborates with: cad/ingest/surface.py (the value), cad/ingest/limits.py (the bounds), meshio, pyvista/VTK, trimesh, rhino3dm.
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree as ET  # noqa: S405 - entity declarations are refused before parsing

import numpy as np

from meshpipeline.cad.ingest import limits
from meshpipeline.cad.ingest.surface import SurfaceError, SurfaceMesh, concat, read_stl

#: VTK cell types by dimension. Quadratic and higher-order cells are included: VTK's own surface
#: filter turns their faces into triangles that keep the mid-side nodes.
_VTK_3D = {10, 11, 12, 13, 14, 15, 16, 24, 25, 26, 27, 29, 32, 33, 42, 71, 72, 73, 74}
_VTK_2D = {5, 6, 7, 8, 9, 22, 23, 28, 30, 31, 34, 36, 69, 70}
#: meshio cell-type names by dimension (corner-node prefixes: tetra10 is a tetra).
_MESHIO_3D = ("tetra", "hexahedron", "wedge", "pyramid", "polyhedron")
_MESHIO_2D = ("triangle", "quad", "polygon")


def _dim_of_meshio(cell_type: str) -> int:
    if cell_type.startswith(_MESHIO_3D):
        return 3
    if cell_type.startswith(_MESHIO_2D):
        return 2
    return 1 if cell_type.startswith("line") else 0


# VTK-native formats (.vtk, .vtu, .vtp, .ply) and the grid -> surface step every volume mesh shares

def read_vtk_family(path: Path) -> SurfaceMesh:
    import pyvista as pv

    data = pv.read(str(path))
    return dataset_to_surface(data)


def dataset_to_surface(data: Any, group_names: list[str] | None = None) -> SurfaceMesh:
    """Any VTK dataset as its surface: its 2D cells as they are, or - when it holds volume cells -
    the boundary of those cells, every face named by the 2D cell lying on it or else by the volume
    region it closes. A MultiBlock's blocks are separate bodies, named by their block names."""
    import pyvista as pv

    if isinstance(data, pv.MultiBlock):
        parts = []
        for i in range(data.n_blocks):
            block = data[i]
            if block is None or block.n_cells == 0:
                continue
            part = dataset_to_surface(block)
            name = data.get_block_name(i) or ""
            if name and not part.names:
                part.group[:] = 0
                part.names = [name]
            parts.append(part)
        if not parts:
            raise SurfaceError("the file holds no surface or volume cells")
        return concat(parts)

    if isinstance(data, pv.PolyData):
        return _polydata_to_surface(data, group_names)
    grid = data if isinstance(data, pv.UnstructuredGrid) else data.cast_to_unstructured_grid()
    return _grid_to_surface(grid, group_names or [])


def _polydata_to_surface(poly: Any, group_names: list[str] | None) -> SurfaceMesh:
    if poly.n_cells == 0:
        raise SurfaceError("the file holds no surface faces")
    has_group = "ingest_group" in poly.cell_data
    tri = poly.triangulate()
    faces = np.asarray(tri.faces).reshape(-1, 4)
    if len(faces) == 0 or not (faces[:, 0] == 3).all():
        raise SurfaceError("the file holds no surface faces")
    # triangulate() keeps verts and lines in their own arrays: `faces` is the polygons alone, and
    # their cell data sits after the verts' and lines' in the cell order.
    group = None
    if has_group:
        offset = tri.n_verts + tri.n_lines
        group = np.asarray(tri.cell_data["ingest_group"])[offset:offset + len(faces)]
    return SurfaceMesh(np.asarray(tri.points, dtype=np.float64), faces[:, 1:], group,
                       list(group_names or []))


def _grid_to_surface(grid: Any, group_names: list[str]) -> SurfaceMesh:

    types = np.asarray(grid.celltypes)
    if "ingest_group" not in grid.cell_data:
        grid.cell_data["ingest_group"] = np.full(grid.n_cells, -1, dtype=np.int64)
    vol_ids = np.flatnonzero(np.isin(types, list(_VTK_3D)))
    surf_ids = np.flatnonzero(np.isin(types, list(_VTK_2D)))
    if len(vol_ids) == 0:
        if len(surf_ids) == 0:
            raise SurfaceError("the file holds no surface or volume cells")
        skin = grid.extract_cells(surf_ids).extract_surface(pass_pointid=False, pass_cellid=False,
                                                             algorithm="dataset_surface")
        return _polydata_to_surface(skin, group_names)

    # THE BOUNDARY of the volume cells: faces that only one cell owns. VTK's surface filter does
    # this for every cell type (quadratic, polyhedral) and orients the faces outward.
    vol = grid.extract_cells(vol_ids)
    skin = vol.extract_surface(pass_pointid=True, pass_cellid=False, algorithm="dataset_surface")
    if skin.n_cells == 0:
        raise SurfaceError("the volume cells enclose no boundary")
    surface = _polydata_to_surface(skin, group_names)

    # A boundary face first takes the name of the volume region it closes - but only when the
    # file has more than one named region, because one region's name says nothing about any face.
    vol_groups = np.asarray(grid.cell_data["ingest_group"])[vol_ids]
    if len({g for g in vol_groups.tolist() if g >= 0}) < 2:
        surface.group[:] = -1
    # The 2D cells of a volume mesh are its named boundary groups (gmsh physical surfaces, an
    # Abaqus surface set, SU2 markers): a boundary face lying on one takes its name over that.
    if len(surf_ids):
        groups2d = np.asarray(grid.cell_data["ingest_group"])[surf_ids]
        if (groups2d >= 0).any():
            orig = np.asarray(skin.point_data["vtkOriginalPointIds"])
            # vol was extracted from grid, so its point ids map back through ITS original ids
            vol_orig = np.asarray(vol.point_data["vtkOriginalPointIds"])
            triples = _named_triples(grid, surf_ids, groups2d)
            if triples:
                tri_grid_pts = vol_orig[orig[surface.triangles]]
                for i, t in enumerate(np.sort(tri_grid_pts, axis=1).tolist()):
                    g = triples.get((t[0], t[1], t[2]))
                    if g is not None:
                        surface.group[i] = g
    return surface


#: A 2D cell with more nodes than this is not matched node-triple by node-triple (a polygon that
#: large is not a boundary group's face).
_MAX_MATCH_NODES = 12


def _named_triples(grid: Any, cell_ids: np.ndarray, groups: np.ndarray) -> dict[tuple, int]:
    """Every sorted triple of node ids inside each named 2D cell -> its group. A boundary triangle
    lies on a named face exactly when its three nodes are among that face's nodes: a triangle cut
    from a quad shares three of its four corners, and one cut from a quadratic face (VTK splits
    those through their mid-side nodes) shares three of its six or eight."""
    from itertools import combinations

    out: dict[tuple, int] = {}
    conn = np.asarray(grid.cell_connectivity)
    offs = np.asarray(grid.offset)
    for cid, g in zip(cell_ids.tolist(), groups.tolist()):
        if g < 0:
            continue
        ids = sorted(set(conn[offs[cid]:offs[cid + 1]].tolist()))
        if len(ids) > _MAX_MATCH_NODES:
            continue
        for tri in combinations(ids, 3):
            out[tri] = g
    return out


# meshio formats: Gmsh, Fluent, Nastran, Abaqus, Medit, SU2, OFF

def read_meshio(path: Path, file_format: str) -> SurfaceMesh:
    import meshio
    import pyvista as pv

    try:
        mesh = _meshio_read(path, file_format)
    except Exception as exc:  # noqa: BLE001 - every reader error is the same answer to the user
        raise SurfaceError(f"the file could not be read as {file_format} ({_short(exc)})") from exc
    if not mesh.cells:
        raise SurfaceError("the file holds no cells")
    names, per_block = _meshio_groups(mesh, file_format, path)
    keep = [i for i, b in enumerate(mesh.cells) if _dim_of_meshio(b.type) >= 2]
    if not keep:
        raise SurfaceError("the file holds no surface or volume cells (only lines or points)")
    blocks = [mesh.cells[i] for i in keep]
    sub = meshio.Mesh(mesh.points[:, :3] if mesh.points.shape[1] >= 3
                      else np.hstack([mesh.points, np.zeros((len(mesh.points), 3 - mesh.points.shape[1]))]),
                      blocks)
    grid = pv.from_meshio(sub)
    if grid.n_cells != sum(len(b.data) for b in blocks):
        raise SurfaceError("internal: cell order was not preserved reading the file")
    grid.cell_data["ingest_group"] = np.concatenate([per_block[i] for i in keep]).astype(np.int64)
    return _grid_to_surface(grid, names)


def _meshio_read(path: Path, file_format: str):
    import meshio

    if file_format == "nastran" and not _has_begin_bulk(path):
        # Bulk data on its own (an include file, a pre-processor export) has no executive or case
        # control, and so no BEGIN BULK line; meshio's reader starts at that line. The deck is
        # read as the bulk section it is, from a copy - the upload itself is never edited.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="ingest_bdf_") as td:
            framed = Path(td) / "framed.bdf"
            with framed.open("wb") as out, Path(path).open("rb") as src:
                out.write(b"BEGIN BULK\n")
                while chunk := src.read(1 << 20):
                    out.write(chunk)
                out.write(b"\nENDDATA\n")
            return meshio.read(str(framed), file_format=file_format)
    return meshio.read(str(path), file_format=file_format)


def _has_begin_bulk(path: Path) -> bool:
    with Path(path).open("rb") as fh:
        for raw in fh:
            if raw.lstrip().upper().startswith(b"BEGIN BULK") or raw.lstrip().upper().startswith(
                    b"BEGIN,BULK"):
                return True
            if raw[:1] not in (b"$", b" ", b"\t", b"\r", b"\n") and raw[:4].upper() in (
                    b"GRID", b"CTRI", b"CQUA", b"CTET", b"CHEX", b"CPEN"):
                return False
    return False


def _gmsh_tag_names(mesh: Any, _path: Path) -> dict[tuple[int, int], str]:
    """Gmsh physical groups: (dimension, tag) -> name, from the file's $PhysicalNames."""
    return {(int(v[1]), int(v[0])): str(k) for k, v in (mesh.field_data or {}).items()
            if len(v) >= 2}


def _su2_tag_names(_mesh: Any, path: Path) -> dict[tuple[int, int], str]:
    """SU2 boundary markers (always 2D): meshio numbers them 1.. in file order."""
    return {(2, tag): name for tag, name in _su2_marker_names(path).items()}


def _nastran_tag_names(_mesh: Any, path: Path) -> dict[tuple[int, int], str]:
    """Nastran property ids, named only where a pre-processor's comments name them."""
    return {(dim, pid): name for pid, name in _nastran_property_names(path).items()
            for dim in (2, 3)}


#: meshio format -> (the cell-data array holding each cell's tag, how the file names a tag).
#: Formats whose groups are tags; Abaqus names cells through element SETS instead (below).
_TAGGED_GROUPS = {
    "gmsh": ("gmsh:physical", _gmsh_tag_names),
    "su2": ("su2:tag", _su2_tag_names),
    "nastran": ("nastran:ref", _nastran_tag_names),
}
_ELEMENT_SET_FORMATS = frozenset({"abaqus"})


def _meshio_groups(mesh: Any, file_format: str, path: Path) -> tuple[list[str], list[np.ndarray]]:
    """The group names a file carries, and each cell's group, block by block (-1 = unnamed)."""
    blocks = mesh.cells
    per_block = [np.full(len(b.data), -1, dtype=np.int64) for b in blocks]
    names: list[str] = []

    def gid(name: str) -> int:
        if name not in names:
            names.append(name)
        return names.index(name)

    tagged = _TAGGED_GROUPS.get(file_format)
    if tagged is not None:
        key, namer = tagged
        tags = mesh.cell_data.get(key)
        by_tag = namer(mesh, path)
        if tags is not None and by_tag:
            for i, b in enumerate(blocks):
                dim = _dim_of_meshio(b.type)
                t = np.asarray(tags[i]).astype(np.int64).reshape(-1)
                for tag in np.unique(t):
                    name = by_tag.get((dim, int(tag)))
                    if name:
                        per_block[i][t == tag] = gid(name)
        return names, per_block

    if file_format in _ELEMENT_SET_FORMATS:
        # ELSETs: a cell takes the SMALLEST set that holds it (the most specific name); a set
        # holding every cell of its dimension says nothing that distinguishes one part from another,
        # and the sets a writer generates per entity (gmsh's Surface7, Abaqus/CAE's _PickedSet2)
        # name nothing the user drew
        sets = [(str(k), v) for k, v in (mesh.cell_sets or {}).items()
                if not _GENERATED_SET.match(str(k))]
        sized = sorted(sets, key=lambda kv: sum(len(np.asarray(a).reshape(-1)) for a in kv[1]))
        for name, members in reversed(sized):
            for i, a in enumerate(members):
                idx = np.asarray(a).astype(np.int64).reshape(-1)
                if len(idx) == 0 or i >= len(blocks):
                    continue
                if len(idx) == len(blocks[i].data) and len(sets) > 1 and _dim_of_meshio(
                        blocks[i].type) == 3 and _covers_all(sets, name, blocks):
                    continue
                per_block[i][idx] = gid(name)
    return names, per_block


_GENERATED_SET = re.compile(r"^(?:_|(?:Point|Line|Curve|Surface|Volume)\d+$)", re.I)


def _covers_all(sets, name, blocks) -> bool:
    members = dict(sets)[name]
    return all(len(np.asarray(a).reshape(-1)) == len(b.data) for a, b in zip(members, blocks))


def _su2_marker_names(path: Path) -> dict[int, str]:
    """SU2 MARKER_TAG names in file order; meshio numbers the markers 1.. in that same order."""
    out: dict[int, str] = {}
    with Path(path).open("r", encoding="latin-1") as fh:
        for line in fh:
            if line.lstrip().upper().startswith("MARKER_TAG"):
                out[len(out) + 1] = line.split("=", 1)[1].strip()
    return out


_HM_COMP = re.compile(r'^\$HMNAME\s+(?:COMP|PROP)S?\s+(\d+)\s*"([^"]+)"', re.I)
_ANSA_NAME = re.compile(r"^\$ANSA_NAME_COMMENT;(\d+);P\w+;([^;]+);", re.I)
_PSHELL_COMMENT = re.compile(r"^\$\s*(?:Shell|Solid|Property)[^:]*?(\d+)\s*:\s*(\S.*)$", re.I)


def _nastran_property_names(path: Path) -> dict[int, str]:
    """Component names the pre-processors write as comments (HyperMesh, ANSA); {} when none."""
    out: dict[int, str] = {}
    with Path(path).open("r", encoding="latin-1") as fh:
        for line in fh:
            if not line.startswith("$"):
                continue
            for rx in (_HM_COMP, _ANSA_NAME, _PSHELL_COMMENT):
                m = rx.match(line.strip())
                if m:
                    out.setdefault(int(m.group(1)), m.group(2).strip())
                    break
    return out


# Wavefront OBJ

def read_obj(path: Path) -> SurfaceMesh:
    """Vertices, polygon faces (fanned into triangles) and the o/g names they sit under."""
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    tgroup: list[int] = []
    names: list[str] = []
    current = -1
    with Path(path).open("r", encoding="latin-1") as fh:
        pending = ""
        for raw in fh:
            line = pending + raw.rstrip("\r\n")
            if line.endswith("\\"):
                pending = line[:-1] + " "
                continue
            pending = ""
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = []
                n = len(verts)
                for tok in line.split()[1:]:
                    i = int(tok.split("/", 1)[0])
                    idx.append(i - 1 if i > 0 else n + i)
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
                    tgroup.append(current)
                if len(tris) > limits.MAX_TRIANGLES:
                    raise SurfaceError(limits.too_many_triangles())
            elif line.startswith(("o ", "g ")):
                name = "_".join(line.split()[1:]) or ""
                if name and name != "default":
                    if name not in names:
                        names.append(name)
                    current = names.index(name)
                else:
                    current = -1
    if not tris:
        raise SurfaceError("the OBJ file holds no faces")
    return SurfaceMesh(np.asarray(verts, dtype=np.float64), np.asarray(tris, dtype=np.int64),
                       np.asarray(tgroup, dtype=np.int64), names)


# 3MF: a zip of XML parts, read in memory - nothing is ever extracted to disk

_3MF_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"


def read_3mf(path: Path) -> SurfaceMesh:
    with zipfile.ZipFile(path) as zf:
        refusal = limits.zip_refusal(zf)
        if refusal:
            raise SurfaceError(refusal)
        root_part = _3mf_root_part(zf)
        models: dict[str, Any] = {}

        def model(part: str):
            part = part.lstrip("/")
            if part not in models:
                models[part] = _parse_3mf_model(zf, part)
            return models[part]

        root = model(root_part)
        parts: list[SurfaceMesh] = []
        for item in root["build"]:
            m = _instance_3mf(model, root_part, item.get("path") or root_part, item["objectid"],
                              item["transform"], depth=0)
            if m is not None:
                name = root["objects"].get(item["objectid"], {}).get("name") \
                    if not item.get("path") else ""
                if name and not m.names:
                    m.group[:] = 0
                    m.names = [name]
                parts.append(m)
        if not parts:
            raise SurfaceError("the 3MF file builds no objects")
    out = concat(parts)
    unit = root.get("unit") or "millimeter"
    out.notes.append(f"3MF unit: {unit}")
    return out


def _3mf_root_part(zf: zipfile.ZipFile) -> str:
    try:
        rels = zf.read("_rels/.rels")
        for rel in ET.fromstring(_safe_xml(rels)):  # noqa: S314 - DTDs refused by _safe_xml
            if rel.get("Type") == _3MF_REL and rel.get("Target"):
                return rel.get("Target", "").lstrip("/")
    except KeyError:
        pass
    for name in zf.namelist():
        if name.lower().endswith("3dmodel.model"):
            return name
    raise SurfaceError("the 3MF file has no 3D model part")


def _safe_xml(data: bytes) -> bytes:
    head = data[:4096]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise SurfaceError("the 3MF file declares XML entities, which a 3MF model never needs")
    return data


def _parse_3mf_model(zf: zipfile.ZipFile, part: str) -> dict:
    """Objects (meshes or component lists), the build items, and the unit, from one model part.
    Streamed with iterparse, so a large model never sits in memory as text and as a tree at once."""
    try:
        info = zf.getinfo(part)
    except KeyError as exc:
        raise SurfaceError(f"the 3MF file refers to a missing part {part!r}") from exc
    with zf.open(info) as raw:
        head = raw.read(4096)
    _safe_xml(head)
    objects: dict[str, dict] = {}
    build: list[dict] = []
    unit = ""
    cur: dict | None = None
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    with zf.open(info) as fh:
        for event, el in ET.iterparse(limits.CappedReader(fh), events=("start", "end")):  # noqa: S314
            tag = el.tag.rsplit("}", 1)[-1]
            if event == "start":
                if tag == "model":
                    unit = el.get("unit") or ""
                elif tag == "object":
                    cur = {"id": el.get("id"), "name": el.get("name") or "", "mesh": None,
                           "components": []}
                    verts, tris = [], []
                continue
            if tag == "vertex":
                verts.append((float(el.get("x", 0)), float(el.get("y", 0)), float(el.get("z", 0))))
            elif tag == "triangle":
                tris.append((int(el.get("v1", 0)), int(el.get("v2", 0)), int(el.get("v3", 0))))
                if len(tris) > limits.MAX_TRIANGLES:
                    raise SurfaceError(limits.too_many_triangles())
            elif tag == "mesh" and cur is not None:
                cur["mesh"] = (np.asarray(verts, dtype=np.float64).reshape(-1, 3),
                               np.asarray(tris, dtype=np.int64).reshape(-1, 3))
            elif tag == "component" and cur is not None:
                cur["components"].append({"objectid": el.get("objectid"),
                                          "transform": _3mf_matrix(el.get("transform")),
                                          "path": _prod_path(el)})
            elif tag == "object" and cur is not None:
                objects[str(cur["id"])] = cur
                cur = None
            elif tag == "item":
                build.append({"objectid": el.get("objectid"),
                              "transform": _3mf_matrix(el.get("transform")),
                              "path": _prod_path(el)})
            if tag in ("vertex", "triangle", "component", "item", "object"):
                el.clear()
    return {"objects": objects, "build": build, "unit": unit}


def _prod_path(el) -> str:
    for k, v in el.attrib.items():
        if k.endswith("}path") or k == "path":
            return str(v).lstrip("/")
    return ""


def _3mf_matrix(text: str | None) -> np.ndarray:
    m = np.eye(4)
    if text:
        v = [float(x) for x in text.split()]
        if len(v) == 12:
            # 3MF transforms row vectors: [x y z 1] . M, with M's last row the translation
            m[:3, :3] = np.asarray(v[:9]).reshape(3, 3).T
            m[:3, 3] = v[9:12]
    return m


def _instance_3mf(model, root_part: str, part: str, objectid: str, xf: np.ndarray, *,
                  depth: int) -> SurfaceMesh | None:
    if depth > 32:
        raise SurfaceError("the 3MF file nests its components more than 32 deep")
    obj = model(part)["objects"].get(str(objectid))
    if obj is None:
        raise SurfaceError(f"the 3MF build refers to a missing object {objectid!r}")
    if obj["mesh"] is not None:
        v, t = obj["mesh"]
        if len(t) == 0:
            return None
        pts = (np.c_[v, np.ones(len(v))] @ xf.T)[:, :3]
        names = [obj["name"]] if obj["name"] else []
        return SurfaceMesh(pts, t, np.zeros(len(t), dtype=np.int64) if names else None, names)
    parts = []
    for comp in obj["components"]:
        sub = _instance_3mf(model, root_part, comp["path"] or part, comp["objectid"],
                            xf @ comp["transform"], depth=depth + 1)
        if sub is not None:
            parts.append(sub)
    if not parts:
        return None
    out = concat(parts)
    if obj["name"] and len(out.names) < 2:
        out.group[:] = 0
        out.names = [obj["name"]]
    return out


# glTF / GLB: one self-contained file; nothing outside it is ever opened

class _RefuseOutside:
    """A trimesh resolver that resolves nothing: a glTF that needs another file is refused at
    upload, and must not reach the disk of the machine reading it if it gets this far anyway."""

    def get(self, name: str):
        raise SurfaceError("the glTF file refers to a separate file; upload a single .glb instead")

    def __getitem__(self, name: str):
        return self.get(name)

    def namespaced(self, *_a, **_k):
        return self

    def write(self, *_a, **_k):
        raise SurfaceError("internal: nothing is written while reading a glTF")


def read_gltf(path: Path) -> SurfaceMesh:
    import trimesh

    data = Path(path).read_bytes()
    refusal = limits.gltf_refusal(data)
    if refusal:
        raise SurfaceError(refusal)
    kind = "glb" if data[:4] == b"glTF" else "gltf"
    import io

    try:
        scene: Any = trimesh.load(io.BytesIO(data), file_type=kind,
                                  resolver=cast(Any, _RefuseOutside()),
                                  force="scene", process=False)
    except SurfaceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SurfaceError(f"the glTF file could not be read ({_short(exc)})") from exc
    # Only the names the FILE gives: trimesh invents one for every unnamed node and mesh, and an
    # invented name would become a patch the user never drew.
    doc = limits.gltf_json(data)
    given = {str(x.get("name")) for key in ("nodes", "meshes") for x in (doc.get(key) or [])
             if isinstance(x, dict) and x.get("name")}
    parts: list[SurfaceMesh] = []
    for node in scene.graph.nodes_geometry:
        xf, geom_name = scene.graph[node]
        geom = scene.geometry.get(geom_name)
        if not isinstance(geom, trimesh.Trimesh) or len(geom.faces) == 0:
            continue
        pts = (np.c_[np.asarray(geom.vertices, dtype=np.float64), np.ones(len(geom.vertices))]
               @ np.asarray(xf, dtype=np.float64).T)[:, :3]
        name = next((str(n) for n in (node, geom_name) if str(n) in given), "")
        faces = np.asarray(geom.faces)
        parts.append(SurfaceMesh(pts, faces, np.zeros(len(faces)) if name else None,
                                 [name] if name else []))
    if not parts:
        raise SurfaceError("the glTF file holds no triangle meshes")
    return concat(parts)


# Rhino 3DM: what Rhino stores as triangles. Its NURBS cannot be rebuilt from what openNURBS
# exposes (rhino3dm gives no trimming loops), so only meshes are read, never guessed.

def read_3dm(path: Path) -> SurfaceMesh:
    import rhino3dm

    from meshpipeline.contracts.intake_formats import RHINO_EXPORT

    model: Any = rhino3dm.File3dm.Read(str(path))
    if model is None:
        raise SurfaceError("the Rhino file could not be read")
    layers = {i: model.Layers[i].Name for i in range(len(model.Layers))}
    parts: list[SurfaceMesh] = []
    unmeshed = 0
    for obj in model.Objects:
        geom = obj.Geometry
        meshes = _rhino_meshes(geom, rhino3dm)
        if meshes is None:
            continue                     # curves, points, text: not a surface
        if not meshes:
            unmeshed += 1
            continue
        name = obj.Attributes.Name or layers.get(obj.Attributes.LayerIndex, "") or ""
        for m in meshes:
            part = _rhino_mesh_to_surface(m)
            if part is not None:
                if name:
                    part.group[:] = 0
                    part.names = [name]
                parts.append(part)
    if unmeshed:
        raise SurfaceError(
            f"the Rhino file has {unmeshed} surface(s) or polysurface(s) saved without the render "
            f"meshes Hexera reads ({RHINO_EXPORT}, then upload the STEP file)")
    if not parts:
        raise SurfaceError(f"the Rhino file holds no surfaces or meshes ({RHINO_EXPORT}, then "
                           f"upload the STEP file)")
    return concat(parts)


def _rhino_meshes(geom, rhino3dm) -> list | None:
    """The triangles Rhino stored for one object: a mesh is itself; a Brep or extrusion keeps one
    render mesh per face. [] = a surface with none stored; None = not a surface at all."""
    if isinstance(geom, rhino3dm.Mesh):
        return [geom]
    if isinstance(geom, rhino3dm.Extrusion):
        m = geom.GetMesh(rhino3dm.MeshType.Any)
        return [m] if m is not None else []
    if isinstance(geom, rhino3dm.Brep):
        out = []
        for i in range(len(geom.Faces)):
            m = geom.Faces[i].GetMesh(rhino3dm.MeshType.Any)
            if m is None:
                return []
            out.append(m)
        return out
    if isinstance(geom, rhino3dm.SubD):
        return []
    return None


def _rhino_mesh_to_surface(m) -> SurfaceMesh | None:
    nv, nf = len(m.Vertices), len(m.Faces)
    if nv == 0 or nf == 0:
        return None
    pts = np.array([(p.X, p.Y, p.Z) for p in (m.Vertices[i] for i in range(nv))], dtype=np.float64)
    tris: list[tuple[int, int, int]] = []
    for i in range(nf):
        a, b, c, d = m.Faces[i]
        tris.append((a, b, c))
        if d != c:
            tris.append((a, c, d))
    return SurfaceMesh(pts, np.asarray(tris, dtype=np.int64))


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:160]


#: format key -> reader. meshio's own names for its formats sit beside the key they read.
_MESHIO = {"nastran": "nastran", "abaqus": "abaqus", "medit": "medit", "su2": "su2", "off": "off"}


def read_surface(path: Path, key: str) -> SurfaceMesh:
    """The surface of a file already sniffed as format `key` (contracts/intake_formats)."""
    p = Path(path)
    if key == "stl":
        return read_stl(p)
    if key == "obj":
        return read_obj(p)
    if key == "3mf":
        return read_3mf(p)
    if key in ("gltf", "glb"):
        return read_gltf(p)
    if key == "3dm":
        return read_3dm(p)
    if key in ("vtk", "vtu", "vtp", "ply"):
        return read_vtk_family(p)
    if key == "msh":
        if _msh_flavour(p) == "ansys":
            from meshpipeline.cad.ingest.fluent import FluentNoFaces, read_fluent

            try:
                # Fluent stores faces, not cells: the boundary is the faces one cell owns, named
                # by their zones. meshio would return every interior face as surface too.
                return read_fluent(p)
            except FluentNoFaces:
                return read_meshio(p, "ansys")       # a cell-based file some tools write
        return read_meshio(p, "gmsh")
    if key in _MESHIO:
        return read_meshio(p, _MESHIO[key])
    raise SurfaceError(f"no surface reader for {key!r}")


def _msh_flavour(path: Path) -> str:
    """Gmsh and ANSYS Fluent both name their meshes .msh; the first bytes say which this is."""
    with Path(path).open("rb") as fh:
        head = fh.read(256).lstrip(b"\xef\xbb\xbf \t\r\n")
    return "ansys" if head.startswith(b"(") else "gmsh"


