# Responsibility: Read an ANSYS Fluent mesh (.msh) as its boundary surface, each face named by the zone Fluent gave it.
# Owns: the Fluent section grammar (ASCII and binary nodes and faces, zone names) and the one-sided-face rule for the boundary.
# Boundaries: boundary faces only, in the file's own unit; cells, face trees and periodic shadows are not read.
# Collaborates with: cad/ingest/readers.py (dispatches .msh files that start with a Fluent section here).
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from meshpipeline.cad.ingest import limits
from meshpipeline.cad.ingest.surface import SurfaceError, SurfaceMesh

#: "(13 (zone first last bc-type face-type)" - every number in these headers is hexadecimal.
_HEADER = re.compile(rb"\(\s*(\d+)\s*\(([^()]*)\)")
#: "(45 (zone-id bc-type name)())" and the older "(39 ...)": the zone's name as the user set it
_ZONE_NAME = re.compile(rb"\(\s*(?:45|39)\s*\(\s*(\d+)\s+(\S+)\s+(\S+?)\s*\)")
_END_BINARY = b"End of Binary Section"
#: Fluent's face-type codes: nodes per face, 0 = mixed (each face states its count), 5 = polygon
_FACE_NODES = {2: 2, 3: 3, 4: 4}


class FluentFormatError(SurfaceError):
    pass


class FluentNoFaces(FluentFormatError):
    """No face sections at all: not Fluent's own face-based layout (some tools write cells)."""


def is_fluent(head: bytes) -> bool:
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"(")


def read_fluent(path: Path) -> SurfaceMesh:
    """The faces that only one cell owns (c0 or c1 = 0) - the boundary of the fluid - with the name
    of the zone each lies in (inlet, wall, outlet ...). Interior zones fall away by that same rule."""
    data = Path(path).read_bytes()
    node_blocks: list[tuple[int, np.ndarray]] = []
    face_zones: list[tuple[int, np.ndarray, np.ndarray]] = []   # (zone, per-face node lists, c0c1)
    names: dict[int, str] = {}
    pos = 0
    n = len(data)
    while pos < n:
        i = data.find(b"(", pos)
        if i < 0:
            break
        m = re.match(rb"\(\s*(\d+)", data[i:i + 16])
        if not m:
            pos = i + 1
            continue
        index = int(m.group(1))
        if index in (0, 1):                                 # comment / header: a quoted string
            q1 = data.find(b'"', i)
            q2 = data.find(b'"', q1 + 1) if q1 >= 0 else -1
            pos = (data.find(b")", q2) + 1) if q2 >= 0 else i + 1
            continue
        if index in (39, 45):
            zm = _ZONE_NAME.match(data, i)
            if zm:
                names[int(zm.group(1))] = zm.group(3).decode("latin-1")
            pos = _skip_section(data, i, index)
            continue
        hm = _HEADER.match(data, i)
        if index in (10, 2010, 3010) and hm:
            pos = _read_nodes(data, hm, index, node_blocks)
            continue
        if index in (13, 2013, 3013) and hm:
            pos = _read_faces(data, hm, index, face_zones)
            continue
        pos = _skip_section(data, i, index)
    if not node_blocks:
        raise FluentFormatError("the Fluent mesh holds no nodes")
    if not face_zones:
        raise FluentNoFaces("the Fluent mesh holds no faces")
    return _boundary(node_blocks, face_zones, names)


def _skip_section(data: bytes, start: int, index: int) -> int:
    """The position after the section opened at `start`: a binary section ends at its marker, an
    ASCII one where its parentheses close."""
    if index >= 2000:
        end = data.find(_END_BINARY, start)
        if end >= 0:
            close = data.find(b")", end)
            return close + 1 if close >= 0 else len(data)
    depth = 0
    j = start
    n = len(data)
    while j < n:
        c = data[j]
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _fields(hm) -> list[int]:
    return [int(t, 16) for t in hm.group(2).split()]


def _body_start(data: bytes, hm) -> int:
    """Where a section's body starts ("(" after the header), or -1 when it has none."""
    j = hm.end()
    while j < len(data) and data[j:j + 1] in (b" ", b"\t", b"\r", b"\n"):
        j += 1
    return j + 1 if data[j:j + 1] == b"(" else -1


def _read_nodes(data: bytes, hm, index: int, out: list) -> int:
    f = _fields(hm)
    zone, first, last = f[0], f[1], f[2]
    nd = f[4] if len(f) > 4 else 3
    body = _body_start(data, hm)
    if zone == 0 or body < 0:                              # a declaration: no coordinates
        return _skip_section(data, hm.start(), index) if body >= 0 else hm.end() + 1
    count = last - first + 1
    if index == 10:
        close = data.find(b")", body)
        vals = np.array(data[body:close].split(), dtype=np.float64)
        end = data.find(b")", close + 1) + 1
    else:
        dt = np.dtype("<f8") if index == 3010 else np.dtype("<f4")
        nbytes = count * nd * dt.itemsize
        vals = np.frombuffer(data, dtype=dt, count=count * nd, offset=body).astype(np.float64)
        end = _skip_section(data, body + nbytes, index)
    if vals.size != count * nd:
        raise FluentFormatError("a Fluent node section is truncated")
    xyz = vals.reshape(count, nd)
    if nd == 2:
        xyz = np.c_[xyz, np.zeros(count)]
    out.append((first, xyz))
    return end


def _read_faces(data: bytes, hm, index: int, out: list) -> int:
    f = _fields(hm)
    zone, first, last, ftype = f[0], f[1], f[2], f[4] if len(f) > 4 else 0
    body = _body_start(data, hm)
    if zone == 0 or body < 0:
        return _skip_section(data, hm.start(), index) if body >= 0 else hm.end() + 1
    count = last - first + 1
    if count > limits.MAX_TRIANGLES:
        raise FluentFormatError(limits.too_many_triangles())
    if index == 13:
        close = data.find(b")", body)
        ints = np.array([int(t, 16) for t in data[body:close].split()], dtype=np.int64)
        end = data.find(b")", close + 1) + 1
    else:
        dt = np.dtype("<i8") if index == 3013 else np.dtype("<i4")
        ints, consumed = _binary_face_ints(data, body, dt, count, ftype)
        end = _skip_section(data, body + consumed, index)
    faces, cells = _split_faces(ints, count, ftype)
    out.append((zone, faces, cells))
    return end


def _binary_face_ints(data: bytes, body: int, dt: np.dtype, count: int, ftype: int):
    fixed = _FACE_NODES.get(ftype)
    if fixed:
        k = count * (fixed + 2)
        return np.frombuffer(data, dtype=dt, count=k, offset=body).astype(np.int64), \
            k * dt.itemsize
    # mixed or polygonal: every face states its own node count first
    vals: list[int] = []
    off = body
    for _ in range(count):
        nn = int(np.frombuffer(data, dtype=dt, count=1, offset=off)[0])
        row = np.frombuffer(data, dtype=dt, count=nn + 3, offset=off)
        vals.extend(row.tolist())
        off += (nn + 3) * dt.itemsize
    return np.asarray(vals, dtype=np.int64), off - body


def _split_faces(ints: np.ndarray, count: int, ftype: int):
    fixed = _FACE_NODES.get(ftype)
    if fixed:
        if ints.size != count * (fixed + 2):
            raise FluentFormatError("a Fluent face section is truncated")
        rows = ints.reshape(count, fixed + 2)
        return list(rows[:, :fixed]), rows[:, fixed:]
    faces, cells = [], []
    j = 0
    for _ in range(count):
        nn = int(ints[j])
        faces.append(ints[j + 1:j + 1 + nn])
        cells.append(ints[j + 1 + nn:j + 3 + nn])
        j += nn + 3
    if j != ints.size:
        raise FluentFormatError("a Fluent face section does not add up")
    return faces, np.asarray(cells, dtype=np.int64).reshape(-1, 2)


def _boundary(node_blocks, face_zones, names: dict[int, str]) -> SurfaceMesh:
    last = max(first + len(xyz) - 1 for first, xyz in node_blocks)
    pts = np.full((last + 1, 3), np.nan)
    for first, xyz in node_blocks:
        pts[first:first + len(xyz)] = xyz
    tris: list = []
    group: list[int] = []
    zone_names: list[str] = []
    for zone, faces, cells in face_zones:
        one_sided = (cells[:, 0] == 0) | (cells[:, 1] == 0)
        if not one_sided.any():
            continue                                         # an interior zone
        name = names.get(zone, "")
        gi = -1
        if name:
            if name not in zone_names:
                zone_names.append(name)
            gi = zone_names.index(name)
        for k in np.flatnonzero(one_sided):
            nodes: np.ndarray = np.asarray(faces[k], dtype=np.int64)
            # Fluent orders a face so its right-hand normal points at c0; a boundary face whose
            # only cell is c1 is turned round, so every face's normal leaves the fluid
            if cells[k, 0] != 0:
                nodes = nodes[::-1]
            for j in range(1, len(nodes) - 1):
                tris.append((nodes[0], nodes[j], nodes[j + 1]))
                group.append(gi)
    if not tris:
        raise FluentFormatError("the Fluent mesh has no boundary faces")
    t = np.asarray(tris, dtype=np.int64)
    if not np.isfinite(pts[np.unique(t)]).all():
        raise FluentFormatError("a Fluent face refers to a node that is not defined")
    used = np.unique(t)
    mesh = SurfaceMesh(pts[used].copy(), np.asarray(np.searchsorted(used, t), dtype=np.int64),
                       np.asarray(group, dtype=np.int64), zone_names)
    return _outward(mesh)


def _outward(mesh: SurfaceMesh) -> SurfaceMesh:
    """Whatever Fluent's orientation convention was in the file at hand, a closed boundary must
    enclose a positive volume; one that comes out negative is flipped as a whole."""
    c = mesh.corners()
    signed = float(np.einsum("ij,ij->i", c[:, 0], np.cross(c[:, 1], c[:, 2])).sum())
    if signed < 0:
        mesh.triangles = mesh.triangles[:, ::-1].copy()
    return mesh
