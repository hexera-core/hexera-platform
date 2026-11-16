# Responsibility: Read a DXF (.dxf) file into one SurfaceMesh: every mesh face its model space holds, grouped by the layer it is drawn on.
# Owns: the DXF vocabulary this product reads (polyface meshes, polygon meshes, 3DFACE, MESH), its bounds, and the refusals for the DXF content that carries no surface.
# Boundaries: in-memory and bounded; it writes nothing (canonical.py does), resolves no block reference and reads no unit (a DXF rarely states one).
# Collaborates with: cad/ingest/readers.py (dispatches here), cad/ingest/surface.py (the value), cad/ingest/limits.py (the bounds), cad/ingest/sniff.py (looks_like_dxf).
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from meshpipeline.cad.ingest import limits
from meshpipeline.cad.ingest.surface import SurfaceError, SurfaceMesh

#: The sentinel AutoCAD writes at the head of a binary DXF. Its group codes are packed bytes, not
#: the two lines a tagged DXF writes, so it is refused with the way on rather than mis-read.
BINARY_SENTINEL = b"AutoCAD Binary DXF\r\n\x1a\x00"

#: A tagged (ASCII) DXF opens with group code 0, the word SECTION, group code 2 and a section name.
#: Codes are right-aligned in three columns by most writers and bare in some, so the space is loose.
_ASCII_HEAD = re.compile(rb"^\s*0\s*\r?\n\s*SECTION\s*\r?\n\s*2\s*\r?\n\s*"
                         rb"(?:HEADER|CLASSES|TABLES|BLOCKS|ENTITIES|OBJECTS|THUMBNAILIMAGE)\s*\r?\n")
#: Group code 999 is a comment, and a DXF may open with any number of them (Deswik and Surpac both
#: stamp the exporter there). They are skipped before the head is matched - bounded, because a file
#: that is nothing but comments must not be read to its end to be turned away.
_COMMENT_PAIR = re.compile(rb"^\s*999\s*\r?\n[^\n]*\n")
MAX_LEADING_COMMENTS = 64

#: The byte-order mark a Windows text editor leaves at the head of a DXF it has re-saved. It is not
#: part of the first group code, and `int()` would choke on it.
_BOM_TEXT = "﻿\xef\xbb\xbf"

#: POLYLINE flags (group code 70) that say what the vertices after it mean.
_POLYFACE = 64          # a polyface mesh: free vertices, then one record per face
_POLYGON_MESH = 16      # an M x N polygon mesh: a grid of vertices, faces implied by the grid
#: VERTEX flags (group code 70). A face record is marked 128 and is NOT itself a mesh vertex (64).
_VERTEX_FACE_RECORD = 128
_VERTEX_OF_MESH = 64

#: The layer a DXF entity carries when its own layer is the drawing default. Kept out of the group
#: names: SurfaceMesh treats "0" as a name like any other and it tells a reader nothing.
_DEFAULT_LAYER = "0"


def looks_like_dxf(head: bytes) -> bool:
    """True when the bytes open as a DXF - tagged or binary - whatever the file is named."""
    if head.startswith(BINARY_SENTINEL):
        return True
    text = head.lstrip(b"\xef\xbb\xbf")
    for _ in range(MAX_LEADING_COMMENTS):
        comment = _COMMENT_PAIR.match(text)
        if comment is None:
            break
        text = text[comment.end():]
    return _ASCII_HEAD.match(text) is not None


def _tags(path: Path) -> Iterator[tuple[int, str]]:
    """The (group code, value) pairs of a tagged DXF, in file order.

    A line where a group code should be, that is not one, is the end of what can be trusted: a
    file damaged there holds entities after it that will not be read, and writing the ones before
    it as the model would quietly deliver half a drawing. It is refused instead."""
    with path.open("r", encoding="latin-1", errors="replace") as fh:
        first = True
        while True:
            code_line = fh.readline()
            if not code_line:
                return
            if first:
                code_line = code_line.lstrip(_BOM_TEXT)
                first = False
            if not code_line.strip():
                # a blank line is not a group code; writers leave one after the final EOF
                continue
            value_line = fh.readline()
            if not value_line:
                raise SurfaceError(_damaged(code_line, "it ends in the middle of one"))
            try:
                code = int(code_line.strip())
            except ValueError:
                raise SurfaceError(_damaged(code_line, "this is not a group code")) from None
            yield code, value_line.rstrip("\r\n")


def _damaged(line: str, said: str) -> str:
    shown = line.strip()[:40]
    return (f"this DXF file is damaged: every value in it is preceded by a group code on its own "
            f"line, and {said} ({shown!r}). Export it again from the program that made it.")


class _Builder:
    """Triangles and the layers they were drawn on, accumulated across entities."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float, float]] = []
        self.tris: list[tuple[int, int, int]] = []
        self.group: list[int] = []
        #: layer name -> group index, in the order the layers first carried a face. A drawing can
        #: hold tens of thousands of layers, so this is a lookup and not a scan of a growing list,
        #: and a layer is only given an index when a face is actually drawn on it - the layers
        #: holding a drawing's text, dimensions and centrelines are not regions of the model.
        self._index: dict[str, int] = {}

    def layer(self, name: str) -> int:
        if not name or name == _DEFAULT_LAYER:
            return -1
        at = self._index.get(name)
        if at is None:
            at = self._index[name] = len(self._index)
        return at

    def face(self, corners: list[tuple[float, float, float]], layer: str) -> None:
        """One flat face of any number of corners, fanned into triangles."""
        at = self.layer(layer)
        base = len(self.points)
        self.points.extend(corners)
        for k in range(1, len(corners) - 1):
            self.tris.append((base, base + k, base + k + 1))
            self.group.append(at)
        self._check()

    def indexed(self, verts: list[tuple[float, float, float]],
                faces: list[list[int]], layer: str) -> None:
        """Faces given as 0-based indices into `verts`, which is added whole: a polyface mesh
        shares its vertices between faces and splitting them would lose that."""
        if not faces:
            return
        at = self.layer(layer)
        base = len(self.points)
        self.points.extend(verts)
        for idx in faces:
            for k in range(1, len(idx) - 1):
                self.tris.append((base + idx[0], base + idx[k], base + idx[k + 1]))
                self.group.append(at)
        self._check()

    def _check(self) -> None:
        if len(self.tris) > limits.MAX_TRIANGLES:
            raise SurfaceError(limits.too_many_triangles())

    def mesh(self) -> SurfaceMesh:
        return SurfaceMesh(np.asarray(self.points, dtype=np.float64).reshape(-1, 3),
                           np.asarray(self.tris, dtype=np.int64).reshape(-1, 3),
                           np.asarray(self.group, dtype=np.int64), list(self._index))


def _corners(d: dict[int, list[str]], codes: tuple[int, ...]) -> list[tuple[float, float, float]]:
    """The points a 3DFACE-style entity states, one per (10, 20, 30)-style code triple. A corner
    repeated from the one before it is a triangle written in the four slots the format gives."""
    out: list[tuple[float, float, float]] = []
    for c in codes:
        if c not in d:
            break
        try:
            p = (float(d[c][0]), float(d[c + 10][0]), float(d[c + 20][0]))
        except (KeyError, IndexError, ValueError):
            break
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 2 and out[0] == out[-1]:
        out.pop()
    return out


def _polyface_indices(d: dict[int, list[str]]) -> list[int]:
    """The 0-based corner indices of one polyface face record. DXF numbers them from 1 and marks an
    edge it would not draw by negating the index; a slot it does not use holds 0, and a corner
    repeated in a slot is how a triangle is written in a record with four of them."""
    out: list[int] = []
    for code in (71, 72, 73, 74):
        if code not in d:
            continue
        try:
            i = abs(int(float(d[code][0])))
        except ValueError:
            continue
        if i and (i - 1) not in out:
            out.append(i - 1)
    return out


def _polygon_mesh_faces(m: int, n: int, closed_m: bool, closed_n: bool) -> list[list[int]]:
    """The quads of an M x N polygon mesh, whose vertices arrive row by row and whose faces are
    implied by the grid. A mesh closed in a direction wraps back onto its first row or column."""
    faces: list[list[int]] = []
    rows = m if closed_m else m - 1
    cols = n if closed_n else n - 1
    for i in range(max(rows, 0)):
        for j in range(max(cols, 0)):
            i2, j2 = (i + 1) % m, (j + 1) % n
            faces.append([i * n + j, i2 * n + j, i2 * n + j2, i * n + j2])
    return faces


def _subdmesh(d: dict[int, list[str]]) -> tuple[list[tuple[float, float, float]], list[list[int]]]:
    """The vertices and faces of a MESH (AcDbSubDMesh) entity: code 92 counts the vertices that
    follow as 10/20/30 triples, and code 93 counts a flat face list of 90s, each face written as
    its corner count followed by that many 0-based indices."""
    verts = [(float(x), float(y), float(z))
             for x, y, z in zip(d.get(10, []), d.get(20, []), d.get(30, []))]
    flat = [int(float(v)) for v in d.get(90, [])]
    faces: list[list[int]] = []
    at = 0
    while at < len(flat):
        n = flat[at]
        if n < 3 or at + n >= len(flat):
            break
        faces.append(flat[at + 1:at + 1 + n])
        at += n + 1
    return verts, faces


def read_dxf(path: Path) -> SurfaceMesh:
    """Every mesh face a DXF's model space holds, as one surface named by the layers it was drawn
    on. Only the entities that carry a surface are read: polyface meshes, polygon meshes, 3DFACE
    and MESH. Curves, text, dimensions and hatches hold no surface and are passed over."""
    p = Path(path)
    with p.open("rb") as fh:
        if fh.read(len(BINARY_SENTINEL)) == BINARY_SENTINEL:
            raise SurfaceError(
                "this is a binary DXF, which stores its geometry in packed bytes. Re-save it as "
                "an ASCII DXF (in AutoCAD, SAVEAS with 'AutoCAD DXF' and the ASCII option), or "
                "export the model as STL or STEP instead")

    build = _Builder()
    section = ""
    kind = ""                                   # the entity being read
    d: dict[int, list[str]] = {}                # its group codes, each in the order it appeared
    # a POLYLINE owns the VERTEX entities that follow it, up to its SEQEND
    poly: dict | None = None
    saw_insert = False

    def flush() -> None:
        """The entity whose codes have just been read, turned into triangles."""
        nonlocal poly, saw_insert
        if d.get(67) and d[67][0].strip() in ("1", "1.0"):
            # Paper space: a layout's title block, border and detail views live in the ENTITIES
            # section beside the model, at the sheet's position and scale. They are drawing
            # furniture, not geometry, and adding them would move and dwarf the real model.
            return
        layer = d.get(8, [""])[0]
        if kind == "POLYLINE":
            flags = int(float(d.get(70, ["0"])[0]))
            poly = {"layer": layer, "flags": flags, "verts": [], "faces": [],
                    "m": int(float(d.get(71, ["0"])[0])), "n": int(float(d.get(72, ["0"])[0]))}
        elif kind == "VERTEX" and poly is not None:
            flags = int(float(d.get(70, ["0"])[0]))
            if flags & _VERTEX_FACE_RECORD and not flags & _VERTEX_OF_MESH:
                idx = _polyface_indices(d)
                if len(idx) >= 3:
                    poly["faces"].append(idx)
            else:
                try:
                    poly["verts"].append((float(d[10][0]), float(d[20][0]), float(d[30][0])))
                except (KeyError, IndexError, ValueError):
                    pass
        elif kind == "SEQEND" and poly is not None:
            if poly["flags"] & _POLYFACE:
                faces = [i for i in poly["faces"] if max(i) < len(poly["verts"])]
                build.indexed(poly["verts"], faces, poly["layer"])
            elif poly["flags"] & _POLYGON_MESH and poly["m"] > 1 and poly["n"] > 1 \
                    and len(poly["verts"]) >= poly["m"] * poly["n"]:
                grid = _polygon_mesh_faces(poly["m"], poly["n"],
                                           bool(poly["flags"] & 1), bool(poly["flags"] & 32))
                build.indexed(poly["verts"][:poly["m"] * poly["n"]], grid, poly["layer"])
            poly = None
        elif kind in ("3DFACE", "SOLID"):
            # a 3DFACE states four corners; a SOLID states them in the order 1, 2, 4, 3
            pts = _corners(d, (10, 11, 12, 13))
            if kind == "SOLID" and len(pts) == 4:
                pts = [pts[0], pts[1], pts[3], pts[2]]
            if len(pts) >= 3:
                build.face(pts, layer)
        elif kind == "MESH":
            verts, faces = _subdmesh(d)
            faces = [i for i in faces if i and max(i) < len(verts)]
            if verts and faces:
                build.indexed(verts, faces, layer)
        elif kind == "INSERT":
            saw_insert = True

    for code, value in _tags(p):
        if code == 0:
            flush()
            d = {}
            kind = value.strip().upper()
            if kind == "SECTION":
                section = ""
            elif kind == "ENDSEC":
                section = ""
                poly = None
            continue
        if kind == "SECTION" and code == 2:
            section = value.strip().upper()
            continue
        if section != "ENTITIES":
            continue
        d.setdefault(code, []).append(value.strip())
    flush()

    if not build.tris:
        extra = (" Its model space places block references (INSERT), whose contents this reader "
                 "does not expand: explode them, or export the model as STL or STEP." if saw_insert
                 else " A DXF of lines, polylines, text or hatches holds no surface to mesh.")
        raise SurfaceError("the DXF file holds no mesh faces." + extra)
    return build.mesh()
