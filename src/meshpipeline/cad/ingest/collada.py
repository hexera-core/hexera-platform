# Responsibility: Read a COLLADA (.dae) file into one SurfaceMesh: every triangle its scene places, where it places it, in the unit and with the up axis the file states.
# Owns: the COLLADA vocabulary this product reads (geometries, node transforms, instancing, unit, up axis), its bounds, and the unit evidence a .dae file gives.
# Boundaries: in-memory and bounded; it writes nothing (canonical.py does) and resolves no reference outside the file.
# Collaborates with: cad/ingest/readers.py (dispatches here), cad/ingest/units.py (the unit evidence), cad/ingest/surface.py (the value), cad/ingest/limits.py (the bounds).
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET  # noqa: S405 - entity declarations are refused before parsing

import numpy as np

from meshpipeline.cad.ingest import limits
from meshpipeline.cad.ingest.surface import SurfaceError, SurfaceMesh, concat

#: How much of the file the prolog check and the asset read look at: COLLADA puts its <asset>
#: first, and the root element comes after at most an XML declaration and a comment or two.
HEAD_BYTES = 64 * 1024
#: Nodes placing nodes: deeper than this is a loop or a file built to make reading it cost.
MAX_DEPTH = 32
#: Node visits a scene may ask for. Triangles are paid for as they are placed (limits.MAX_TRIANGLES),
#: but a node can place empty nodes many times over; this bounds the walk itself.
MAX_PLACEMENTS = 200_000

_ROOT_TAG = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?COLLADA[\s>/]")
_DECLARATIONS = (b"<!DOCTYPE", b"<!ENTITY")


def looks_like_collada(head: bytes) -> bool:
    """True when the first element of the bytes is a COLLADA root - whatever the file is named."""
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not text.startswith(b"<"):
        return False
    at = _first_element_at(text)
    return at is not None and _ROOT_TAG.match(text, at) is not None


def _first_element_at(text: bytes) -> int | None:
    """Where the first element's start tag begins, past the XML declaration, comments and any
    processing instructions; None when the head holds none."""
    i = 0
    n = len(text)
    while i < n:
        j = text.find(b"<", i)
        if j < 0:
            return None
        if text.startswith(b"<?", j):
            k = text.find(b"?>", j)
        elif text.startswith(b"<!--", j):
            k = text.find(b"-->", j)
        elif text.startswith(b"<!", j):
            k = text.find(b">", j)
        else:
            return j
        if k < 0:
            return None
        i = k + 1
    return None


def prolog_refusal(head: bytes) -> str:
    """Why these bytes must not be parsed; "" when they may. A COLLADA document never needs a DTD,
    and an entity declaration is how an XML file is made to expand without end."""
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    at = _first_element_at(text)
    if at is None:
        return "the file is not a COLLADA document (no root element where one belongs)"
    if any(d in text[:at] for d in _DECLARATIONS):
        return "the COLLADA file declares XML entities, which a COLLADA model never needs"
    if _ROOT_TAG.match(text, at) is None:
        return "the file is not a COLLADA document (its root element is not <COLLADA>)"
    return ""


# the unit and the up axis: the file's <asset>, read before any geometry

#: COLLADA states its unit as metres per unit. These are the units this product names; a file in
#: another one is converted exactly into millimetres (see ColladaFrame).
_KNOWN_UNITS = (("m", 1.0), ("mm", 1e-3), ("cm", 1e-2), ("in", 0.0254))
_UNIT_WORDS = {"m": "metres", "mm": "millimetres", "cm": "centimetres", "in": "inches"}
#: The proper rotation that turns each up axis the file may state into +Z, the axis every engine
#: and the geometry check read as up: Y_UP -> (x, -z, y), X_UP -> (-z, y, x).
_UP_TO_Z = {
    "Z_UP": np.eye(3),
    "Y_UP": np.array([[1.0, 0, 0], [0, 0, -1.0], [0, 1.0, 0]]),
    "X_UP": np.array([[0, 0, -1.0], [0, 1.0, 0], [1.0, 0, 0]]),
}


@dataclass(frozen=True)
class ColladaFrame:
    """What the file's <asset> says about its coordinates.

    `meter` is the metres one unit stands for (None: the file states no <unit>). `unit` is the
    product's name for the unit the CONVERTED coordinates are in: the file's own when it is one
    the product names (m, mm, cm, in), "mm" when the file states another (1 unit = 0.3048 m is
    converted exactly by `scale`), "" when the file states none - the unit is then asked. COLLADA
    defaults to metres, but the mesh tools that leave <unit> out write whatever numbers they hold
    (MeshLab writes millimetre models that way), so the default is not taken as evidence."""
    meter: float | None
    up_axis: str            # Z_UP, Y_UP or X_UP, as the file states it (COLLADA's default: Y_UP)
    unit: str
    scale: float            # coordinates are multiplied by this (1.0 unless converted into mm)

    @property
    def rotation(self) -> np.ndarray:
        return _UP_TO_Z[self.up_axis]

    def notes(self) -> list[str]:
        out = []
        if self.meter is None:
            out.append("the COLLADA file does not state its unit")
        elif self.scale != 1.0:
            out.append(f"the COLLADA unit is {self.meter:g} m; the geometry was converted exactly "
                       "into millimetres")
        else:
            out.append(f"the COLLADA unit is {_UNIT_WORDS[self.unit]}")
        if self.up_axis != "Z_UP":
            out.append(f"the COLLADA file puts up along {self.up_axis[0]}; the geometry was turned "
                       "so up is +Z, as every engine here reads it")
        return out


def frame_of(meter: float | None, up_axis: str | None) -> ColladaFrame:
    up = (up_axis or "Y_UP").strip().upper()
    if up not in _UP_TO_Z:
        up = "Y_UP"
    if meter is None:
        return ColladaFrame(None, up, "", 1.0)
    for name, m in _KNOWN_UNITS:
        if abs(meter - m) <= 1e-9 * m:
            return ColladaFrame(meter, up, name, 1.0)
    return ColladaFrame(meter, up, "mm", meter / 1e-3)


def read_frame(path: Path) -> ColladaFrame:
    """The unit and up axis the file's top-level <asset> states, read without its geometry."""
    p = Path(path)
    with p.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
    refusal = prolog_refusal(head)
    if refusal:
        raise SurfaceError(refusal)
    meter: float | None = None
    up: str | None = None
    depth = 0
    try:
        with p.open("rb") as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):  # noqa: S314 - prolog checked
                tag = _local(el.tag)
                if event == "start":
                    depth += 1
                    if depth == 2 and tag != "asset":
                        break                       # <asset> comes first, or not at all
                    continue
                depth -= 1
                if depth == 2 and tag == "unit":
                    meter = _meter(el.get("meter"))
                elif depth == 2 and tag == "up_axis":
                    up = (el.text or "").strip()
                elif depth == 1 and tag == "asset":
                    break
    except ET.ParseError as exc:
        raise SurfaceError(f"the COLLADA file is not valid XML ({str(exc)[:120]})") from exc
    return frame_of(meter, up)


def _meter(text: str | None) -> float | None:
    if text is None:
        return 1.0                                  # <unit> with no meter attribute: the spec's 1.0
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def declared_unit(path: Path, UnitEvidence):
    """UnitEvidence for a COLLADA file: the unit its <asset> states, or unresolved (asked)."""
    from meshpipeline.contracts.geometry_units import LengthUnit

    try:
        frame = read_frame(path)
    except (SurfaceError, OSError) as exc:
        return UnitEvidence.unresolved(f"the COLLADA file could not be read ({str(exc)[:120]})")
    if not frame.unit:
        return UnitEvidence.unresolved("the COLLADA file does not state its unit")
    if frame.scale != 1.0:
        return UnitEvidence(True, LengthUnit.millimetre,
                            f"declared in the file as {frame.meter:g} m per unit, converted "
                            "exactly into millimetres")
    return UnitEvidence(True, LengthUnit(frame.unit),
                        f"declared in the file as {_UNIT_WORDS[frame.unit]}")


# the geometry

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el, name: str):
    return [c for c in el if _local(c.tag) == name]


def _child(el, name: str):
    return next((c for c in el if _local(c.tag) == name), None)


def _floats(text: str | None) -> np.ndarray:
    try:
        return np.asarray((text or "").split(), dtype=np.float64)
    except ValueError as exc:
        raise SurfaceError("the COLLADA file holds a number that is not a number") from exc


def _ints(text: str | None) -> np.ndarray:
    try:
        return np.asarray((text or "").split(), dtype=np.int64)
    except ValueError as exc:
        raise SurfaceError("the COLLADA file holds an index that is not a whole number") from exc


class _Doc:
    """The parsed document: every element with an id, and the geometry each id resolves to."""

    def __init__(self, root) -> None:
        self.root = root
        self.ids: dict[str, object] = {}
        for el in root.iter():
            i = el.get("id")
            if i and i not in self.ids:
                self.ids[i] = el
        self._geom: dict[str, tuple[np.ndarray, np.ndarray] | None] = {}
        self.outside: set[str] = set()
        self.skipped: dict[str, int] = {}

    def ref(self, url: str | None):
        """The element a url names inside this file; None for one outside it (never opened)."""
        u = (url or "").strip()
        if not u.startswith("#"):
            if u:
                self.outside.add(u[:80])
            return None
        return self.ids.get(u[1:])

    def geometry(self, el) -> tuple[np.ndarray, np.ndarray] | None:
        """(points, triangles) of a <geometry>, read once however often it is placed."""
        key = el.get("id") or str(id(el))
        if key not in self._geom:
            self._geom[key] = self._read_geometry(el)
        return self._geom[key]

    def _read_geometry(self, el):
        mesh = _child(el, "mesh")
        if mesh is None:
            kinds = {_local(c.tag) for c in el if _local(c.tag) != "asset" and _local(c.tag) != "extra"}
            for k in kinds:
                self.skipped[k] = self.skipped.get(k, 0) + 1
            return None
        verts = _child(mesh, "vertices")
        positions: dict[str, np.ndarray] = {}
        if verts is not None:
            for inp in _children(verts, "input"):
                if inp.get("semantic") == "POSITION":
                    positions[verts.get("id") or ""] = self._source(inp.get("source"))
        all_pts: list[np.ndarray] = []
        all_tris: list[np.ndarray] = []
        offset = 0
        for prim in mesh:
            kind = _local(prim.tag)
            if kind not in ("triangles", "polylist", "polygons", "trifans", "tristrips"):
                if kind in ("lines", "linestrips"):
                    self.skipped[kind] = self.skipped.get(kind, 0) + 1
                continue
            pts, tris = self._primitive(prim, kind, positions)
            if tris is None or len(tris) == 0:
                continue
            all_pts.append(pts)
            all_tris.append(tris + offset)
            offset += len(pts)
        if not all_tris:
            return None
        return np.vstack(all_pts), np.vstack(all_tris)

    def _source(self, url: str | None) -> np.ndarray:
        """The (n, 3) points a <source> holds, through its accessor's stride, offset and params."""
        src = self.ref(url)
        if src is None or _local(src.tag) != "source":
            raise SurfaceError(f"the COLLADA file refers to a missing vertex source {url!r}")
        arr = _child(src, "float_array")
        if arr is None:
            raise SurfaceError(f"the COLLADA vertex source {src.get('id')!r} holds no numbers")
        data = _floats(arr.text)
        acc = None
        tc = _child(src, "technique_common")
        if tc is not None:
            acc = _child(tc, "accessor")
        stride, start, cols = 3, 0, [0, 1, 2]
        if acc is not None:
            stride = int(acc.get("stride") or 1)
            start = int(acc.get("offset") or 0)
            params = _children(acc, "param")
            named = [i for i, p in enumerate(params) if p.get("name")]
            if params and len(named) >= 3:
                by = {(params[i].get("name") or "").upper(): i for i in named}
                cols = [by[a] for a in "XYZ"] if all(a in by for a in "XYZ") else named[:3]
        if stride < 3 or max(cols) >= stride:
            raise SurfaceError(f"the COLLADA vertex source {src.get('id')!r} is not 3D points")
        count = (len(data) - start) // stride
        if acc is not None and acc.get("count"):
            count = min(count, int(acc.get("count") or 0))
        block = data[start:start + count * stride].reshape(count, stride)
        return block[:, cols]

    def _primitive(self, prim, kind: str, positions: dict[str, np.ndarray]):
        inputs = _children(prim, "input")
        stride = 1 + max((int(i.get("offset") or 0) for i in inputs), default=0)
        vin = next((i for i in inputs if i.get("semantic") == "VERTEX"), None)
        if vin is None:
            return None, None
        key = (vin.get("source") or "").lstrip("#")
        pts = positions.get(key)
        if pts is None:
            # a few writers point VERTEX straight at the position source
            pts = self._source(vin.get("source"))
        voff = int(vin.get("offset") or 0)
        tris: np.ndarray | None

        def corners(p_el) -> np.ndarray:
            idx = _ints(p_el.text)
            n = len(idx) // stride
            return idx[:n * stride].reshape(n, stride)[:, voff]

        if kind == "triangles":
            p = _child(prim, "p")
            if p is None:
                return pts, None
            v = corners(p)
            tris = v[:len(v) // 3 * 3].reshape(-1, 3)
        elif kind == "polylist":
            p, vc = _child(prim, "p"), _child(prim, "vcount")
            if p is None:
                return pts, None
            v = corners(p)
            counts = _ints(vc.text) if vc is not None else np.full(len(v) // 3, 3, dtype=np.int64)
            tris = _polygons(v, counts, pts)
        elif kind == "polygons":
            if _children(prim, "ph"):
                # a polygon with holes is its outline AND the holes cut from it; cutting them is
                # not done here, and dropping either would hand on a different surface
                raise SurfaceError(
                    "the COLLADA file has polygons with holes in them (<ph>), which Hexera does not "
                    "cut - export it again with its faces triangulated, or as an STL")
            rings = [corners(c) for c in _children(prim, "p")]
            tris = (_polygons(np.concatenate(rings), np.asarray([len(r) for r in rings]), pts)
                    if rings else None)
        else:
            parts = [_strip_or_fan(corners(c), kind) for c in _children(prim, "p")]
            parts = [t for t in parts if len(t)]
            tris = np.vstack(parts) if parts else None
        if tris is None:
            return pts, None
        tris = tris.reshape(-1, 3)
        if len(tris) and (tris.min() < 0 or tris.max() >= len(pts)):
            raise SurfaceError("the COLLADA file indexes a vertex it does not hold")
        if len(tris) > limits.MAX_TRIANGLES:
            raise SurfaceError(limits.too_many_triangles())
        return pts, tris


def _polygons(v: np.ndarray, counts: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Polygons of `counts` corners each, laid end to end in `v` (indices into `pts`), split into
    triangles that cover each polygon exactly: a triangle as it is, a quad along the diagonal from
    its reflex corner when it has one, a larger polygon by fanning when it is convex and by ear
    clipping when it is not - a fan from a corner of a concave polygon lays triangles outside it."""
    counts = np.asarray(counts, dtype=np.int64).reshape(-1)
    if len(counts) and int(counts.min()) < 0:
        raise SurfaceError("the COLLADA file gives a polygon a negative number of corners")
    if int(counts.sum()) > len(v):
        raise SurfaceError("the COLLADA file's polygon list is shorter than its counts say")
    # every triangle is paid for before one is made: a few bytes of counts can ask for billions
    if int(np.maximum(counts - 2, 0).sum()) > limits.MAX_TRIANGLES:
        raise SurfaceError(limits.too_many_triangles())
    v = np.asarray(v, dtype=np.int64)[:int(counts.sum())]
    if len(v) and (int(v.min()) < 0 or int(v.max()) >= len(pts)):
        raise SurfaceError("the COLLADA file indexes a vertex it does not hold")
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]]).astype(np.int64) if len(counts) \
        else np.zeros(0, np.int64)
    parts = [v[starts[counts == 3][:, None] + np.arange(3)]]
    quads = v[starts[counts == 4][:, None] + np.arange(4)]
    if len(quads):
        parts.append(_quads(quads, pts))
    for at, n in zip(starts[counts > 4].tolist(), counts[counts > 4].tolist()):
        ring = v[at:at + n]
        parts.append(ring[_ear_clip(pts[ring])])
    parts = [t.reshape(-1, 3) for t in parts if len(t)]
    return np.vstack(parts) if parts else np.zeros((0, 3), dtype=np.int64)


def _turns(P: np.ndarray) -> np.ndarray:
    """How each corner of each polygon (k, n, 3) turns about the polygon's own normal (Newell):
    positive at a convex corner, negative at a reflex one."""
    q = np.roll(P, -1, axis=1)
    normal = np.stack([((P[..., 1] - q[..., 1]) * (P[..., 2] + q[..., 2])).sum(axis=1),
                       ((P[..., 2] - q[..., 2]) * (P[..., 0] + q[..., 0])).sum(axis=1),
                       ((P[..., 0] - q[..., 0]) * (P[..., 1] + q[..., 1])).sum(axis=1)], axis=1)
    e_in, e_out = P - np.roll(P, 1, axis=1), q - P
    return np.einsum("knj,kj->kn", np.cross(e_in, e_out), normal)


def _quads(quads: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Quads (k, 4) split along the diagonal from their reflex corner (corner 0 for a convex one),
    so neither triangle leaves the quad."""
    turn = _turns(pts[quads])
    at = np.where((turn < 0).any(axis=1), np.argmin(turn, axis=1), 0)
    order = (at[:, None] + np.arange(4)) % 4
    r = np.take_along_axis(quads, order, axis=1)
    return np.concatenate([r[:, [0, 1, 2]], r[:, [0, 2, 3]]])


def _ear_clip(P: np.ndarray) -> np.ndarray:
    """Triangles (as corner positions in the polygon) covering one planar polygon of n > 4
    corners: a fan when it is convex, else ears clipped one by one."""
    n = len(P)
    turn = _turns(P[None])[0]
    if (turn >= 0).all() or (turn <= 0).all():
        return np.stack([np.zeros(n - 2, dtype=np.int64), np.arange(1, n - 1), np.arange(2, n)], axis=1)
    # in the polygon's own plane, counter-clockwise
    q = np.roll(P, -1, axis=0)
    normal = np.array([((P[:, 1] - q[:, 1]) * (P[:, 2] + q[:, 2])).sum(),
                       ((P[:, 2] - q[:, 2]) * (P[:, 0] + q[:, 0])).sum(),
                       ((P[:, 0] - q[:, 0]) * (P[:, 1] + q[:, 1])).sum()])
    normal = normal / (np.linalg.norm(normal) or 1.0)
    u = np.cross(normal, [0.0, 0.0, 1.0] if abs(normal[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    w = np.cross(normal, u)
    xy = np.c_[P @ u, P @ w]

    def cross(o, a, b) -> float:
        return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    idx = list(range(n))
    out: list[tuple[int, int, int]] = []
    while len(idx) > 3:
        for j in range(len(idx)):
            a, b, c = idx[j - 1], idx[j], idx[(j + 1) % len(idx)]
            if cross(xy[a], xy[b], xy[c]) <= 0:
                continue                                   # a reflex (or flat) corner is no ear
            if any(cross(xy[a], xy[b], xy[o]) >= 0 and cross(xy[b], xy[c], xy[o]) >= 0
                   and cross(xy[c], xy[a], xy[o]) >= 0 for o in idx if o not in (a, b, c)):
                continue                                   # another corner lies in it
            out.append((a, b, c))
            idx.pop(j)
            break
        else:
            # no ear: a polygon that crosses itself - the rest is fanned, as a reader must
            out.extend((idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1))
            idx = []
            break
    if len(idx) == 3:
        out.append((idx[0], idx[1], idx[2]))
    return np.asarray(out, dtype=np.int64).reshape(-1, 3)


def _strip_or_fan(v: np.ndarray, kind: str) -> np.ndarray:
    if len(v) < 3:
        return np.zeros((0, 3), dtype=np.int64)
    i = np.arange(len(v) - 2)
    if kind == "trifans":
        return np.stack([np.full(len(i), v[0]), v[i + 1], v[i + 2]], axis=1)
    tris = np.stack([v[i], v[i + 1], v[i + 2]], axis=1)
    odd = (i % 2) == 1                       # a strip alternates its winding
    tris[odd] = tris[odd][:, [1, 0, 2]]
    return tris


# node transforms

def _node_matrix(node) -> np.ndarray:
    """A node's own transform: its transform elements composed in the order the file lists them."""
    m = np.eye(4)
    for c in node:
        t = _local(c.tag)
        if t == "matrix":
            v = _floats(c.text)
            if len(v) == 16:
                m = m @ v.reshape(4, 4)          # COLLADA writes matrices row by row
        elif t == "translate":
            v = _floats(c.text)
            if len(v) == 3:
                x = np.eye(4)
                x[:3, 3] = v
                m = m @ x
        elif t == "scale":
            v = _floats(c.text)
            if len(v) == 3:
                m = m @ np.diag([v[0], v[1], v[2], 1.0])
        elif t == "rotate":
            v = _floats(c.text)
            if len(v) == 4:
                m = m @ _rotation(v[:3], v[3])
        elif t == "lookat":
            v = _floats(c.text)
            if len(v) == 9:
                m = m @ _lookat(v[:3], v[3:6], v[6:9])
    return m


def _rotation(axis, degrees: float) -> np.ndarray:
    a = np.asarray(axis, dtype=np.float64)
    n = np.linalg.norm(a)
    out = np.eye(4)
    if n == 0:
        return out
    x, y, z = a / n
    th = math.radians(float(degrees))
    c, s, C = math.cos(th), math.sin(th), 1 - math.cos(th)
    out[:3, :3] = [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                   [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                   [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]
    return out


def _lookat(eye, interest, up) -> np.ndarray:
    eye, interest, up = (np.asarray(v, dtype=np.float64) for v in (eye, interest, up))
    f = interest - eye
    if np.linalg.norm(f) == 0:
        return np.eye(4)
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    if np.linalg.norm(s) == 0:
        return np.eye(4)
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    out = np.eye(4)
    out[:3, 0], out[:3, 1], out[:3, 2], out[:3, 3] = s, u, -f, eye
    return out


# the scene walk

class _Walk:
    def __init__(self, doc: _Doc) -> None:
        self.doc = doc
        self.parts: list[SurfaceMesh] = []
        self.budget = limits.MAX_TRIANGLES
        self.visits = 0

    def node(self, node, parent: np.ndarray, name: str, stack: tuple[int, ...]) -> None:
        if len(stack) > MAX_DEPTH:
            raise SurfaceError(f"the COLLADA file nests its nodes more than {MAX_DEPTH} deep")
        if id(node) in stack:
            raise SurfaceError("the COLLADA file places a node inside itself")
        self.visits += 1
        if self.visits > MAX_PLACEMENTS:
            raise SurfaceError(f"the COLLADA scene places more than {MAX_PLACEMENTS:,} nodes")
        m = parent @ _node_matrix(node)
        here = node.get("name") or name
        inner = stack + (id(node),)
        for c in node:
            t = _local(c.tag)
            if t == "instance_geometry":
                self.place(self.doc.ref(c.get("url")), m, here)
            elif t == "instance_controller":
                self.controller(self.doc.ref(c.get("url")), m, here)
            elif t == "instance_node":
                target = self.doc.ref(c.get("url"))
                if target is not None and _local(target.tag) == "node":
                    self.node(target, m, here, inner)
            elif t == "node":
                self.node(c, m, here, inner)

    def controller(self, ctrl, m: np.ndarray, name: str, stack: tuple[int, ...] = ()) -> None:
        """A skinned or morphed mesh, as its source geometry in its bind pose."""
        if ctrl is None:
            return
        if id(ctrl) in stack or len(stack) > MAX_DEPTH:
            raise SurfaceError("the COLLADA file has a skin or morph that is built from itself")
        for kind in ("skin", "morph"):
            el = _child(ctrl, kind)
            if el is None:
                continue
            bind = np.eye(4)
            bsm = _child(el, "bind_shape_matrix") if kind == "skin" else None
            if bsm is not None:
                v = _floats(bsm.text)
                if len(v) == 16:
                    bind = v.reshape(4, 4)
            target = self.doc.ref(el.get("source"))
            if target is not None and _local(target.tag) == "controller":
                self.controller(target, m @ bind, name, stack + (id(ctrl),))
            else:
                self.place(target, m @ bind, name)
            return

    def place(self, geom, m: np.ndarray, name: str) -> None:
        if geom is None or _local(geom.tag) != "geometry":
            return
        got = self.doc.geometry(geom)
        if got is None:
            return
        pts, tris = got
        self.budget -= len(tris)
        if self.budget < 0:
            raise SurfaceError(limits.too_many_triangles())
        placed = (np.c_[pts, np.ones(len(pts))] @ m.T)[:, :3]
        if np.linalg.det(m[:3, :3]) < 0:
            tris = tris[:, [0, 2, 1]]                # a mirroring transform turns the faces inside out
        label = name or geom.get("name") or ""
        self.parts.append(SurfaceMesh(placed, tris, np.zeros(len(tris)) if label else None,
                                      [label] if label else []))


def read_collada(path: Path) -> SurfaceMesh:
    """Every triangle the file's scene places, in the unit the file states, turned so up is +Z."""
    p = Path(path)
    frame = read_frame(p)
    try:
        root = ET.parse(p).getroot()  # noqa: S314 - prolog checked: no DTD, no entities
    except ET.ParseError as exc:
        raise SurfaceError(f"the COLLADA file is not valid XML ({str(exc)[:120]})") from exc
    if _local(root.tag) != "COLLADA":
        raise SurfaceError("the file is not a COLLADA document (its root element is not <COLLADA>)")
    doc = _Doc(root)
    walk = _Walk(doc)
    scene_el = _scene(doc)
    notes: list[str] = []
    if scene_el is not None:
        for node in _children(scene_el, "node"):
            walk.node(node, np.eye(4), "", ())
    else:
        # no scene to place anything: each geometry once, where it was drawn
        notes.append("the COLLADA file has no scene; each geometry is read once, where it is drawn")
        for lib in _children(root, "library_geometries"):
            for g in _children(lib, "geometry"):
                walk.place(g, np.eye(4), "")
    if doc.outside:
        # a part kept in another file never arrives with the upload: the model would be read with
        # it missing, so it is refused rather than meshed short
        raise SurfaceError(
            "the COLLADA file keeps some of its geometry in separate files "
            f"({sorted(doc.outside)[0]}), which an upload does not carry - export one .dae "
            "with all the geometry inside it, or an STL")
    if not walk.parts:
        raise SurfaceError("the COLLADA file places no triangle meshes")
    out = concat(walk.parts)
    world = np.eye(4)
    world[:3, :3] = frame.rotation * frame.scale
    out.points = (np.c_[out.points, np.ones(len(out.points))] @ world.T)[:, :3]
    for what, n in sorted(doc.skipped.items()):
        notes.append(f"{n} COLLADA {what} element(s) hold no surface triangles and were left out")
    out.notes = frame.notes() + notes + list(out.notes)
    return out


def _scene(doc: _Doc):
    """The visual scene the file's <scene> instances; else its first visual scene; else None."""
    scene = _child(doc.root, "scene")
    if scene is not None:
        inst = _child(scene, "instance_visual_scene")
        if inst is not None:
            target = doc.ref(inst.get("url"))
            if target is not None and _local(target.tag) == "visual_scene":
                return target
    for lib in _children(doc.root, "library_visual_scenes"):
        vs = _child(lib, "visual_scene")
        if vs is not None:
            return vs
    return None
