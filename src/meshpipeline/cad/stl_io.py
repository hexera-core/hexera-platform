# Responsibility: Read and write STL, and answer structural questions about one.
# Boundaries: file-level operations; it makes no meshing decision.
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class StlReadError(ValueError):
    """The file is not a readable STL: empty, cut short before its first triangle, or text that
    is not STL. Said in words, never as a struct or decode error from deep inside a reader."""


# ------------------------------------------------------------------ which kind of STL ------
# An STL is binary or ASCII, and the first five bytes do not say which. Many binary writers put
# "solid <name>" in the 80-byte header (SolidWorks, some CATIA and Rhino exports); an ASCII file
# may open with a bare "solid" and no name, upper-case keywords, a byte-order mark or leading
# blank lines; a binary file's triangle count may disagree with its length (a writer that never
# fills the count in, a file cut short, padding after the last record). So the layout is read
# from the bytes, in this order:
#   1. binary when the count fits the length exactly - decisive whatever the header says;
#   2. ASCII when it reads as STL text: no NUL byte, "solid" first, a facet/vertex/endsolid word;
#   3. binary again when the length holds whole 50-byte records: the count is not trusted then,
#      the records are (never more than the file holds);
#   4. otherwise not an STL, said in words.

_HEADER = 80
_RECORD = 50
_BINARY_DTYPE = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])
_TEXT_WORDS = re.compile(rb"(?i)\b(?:facet|vertex|endsolid)\b")
_SNIFF_BYTES = 4096


@dataclass(frozen=True)
class StlLayout:
    """How an STL's bytes are laid out: `binary` with `triangles` records to read, or ASCII."""

    binary: bool
    triangles: int = 0
    #: why the count was not taken as written (a binary file whose count disagrees with its length)
    note: str = ""


def _reads_as_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    body = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    return body[:5].lower() == b"solid" and (_TEXT_WORDS.search(body) is not None or len(head) < 512)


def stl_layout(head: bytes, size: int) -> StlLayout:
    """The layout of an STL from its first bytes (at least 84 when the file has them; up to 4 KiB
    is best) and its length in bytes. Raises StlReadError for something that is no STL."""
    if size >= _HEADER + 4 and len(head) >= _HEADER + 4:
        count = struct.unpack("<I", head[_HEADER:_HEADER + 4])[0]
        if _HEADER + 4 + _RECORD * count == size:
            return StlLayout(True, count)
    if size > 0 and _reads_as_text(head[:_SNIFF_BYTES]):
        return StlLayout(False)
    if size >= _HEADER + 4 and len(head) >= _HEADER + 4:
        count = struct.unpack("<I", head[_HEADER:_HEADER + 4])[0]
        whole = (size - _HEADER - 4) // _RECORD
        if whole > 0:
            if 0 < count < whole:
                return StlLayout(True, count, f"the file runs on past its {count} triangles")
            return StlLayout(True, whole, f"its triangle count says {count}, its length holds {whole}")
        if count == 0:
            return StlLayout(True, 0)
    if size == 0:
        raise StlReadError("the STL file is empty")
    raise StlReadError(f"not an STL: {size} bytes that are neither STL text nor whole binary records")


def _layout_of(path: Path) -> StlLayout:
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(_SNIFF_BYTES)
    return stl_layout(head, size)


def is_ascii_stl(path: Path) -> bool:
    """True for an STL written as text - read from the bytes, never from the header's first word."""
    return not _layout_of(Path(path)).binary


def read_binary_corners(path: Path, layout: StlLayout | None = None) -> np.ndarray:
    """A binary STL's triangles as an (n, 3, 3) float64 array (the float32 values exactly)."""
    path = Path(path)
    layout = layout or _layout_of(path)
    if not layout.binary:
        raise StlReadError(f"{path.name} is an ASCII STL, not a binary one")
    with path.open("rb") as fh:
        fh.seek(_HEADER + 4)
        raw = fh.read(_RECORD * layout.triangles)
    n = len(raw) // _RECORD
    rec = np.frombuffer(raw[:_RECORD * n], dtype=_BINARY_DTYPE)
    return rec["v"].astype(np.float64)


_FLOAT_LINE = re.compile(r"\s+")


def _ascii_solids(path: Path) -> dict[str, list[tuple]]:
    """An ASCII STL's triangles by solid name, in file order. Keywords in any case; "solid" with
    or without a name (a bare "solid" is the solid named "solid"); a facet loop of more than three
    vertices is fanned into triangles; vertices outside any solid line still count."""
    solids: dict[str, list[tuple]] = {}
    name: str | None = None
    loop: list[list[float]] = []
    in_loop = False

    def flush() -> None:
        if len(loop) >= 3:
            bucket = solids.setdefault(name if name is not None else "solid", [])
            for k in range(1, len(loop) - 1):
                bucket.append((loop[0], loop[k], loop[k + 1]))
        loop.clear()

    with path.open("r", encoding="latin-1", newline=None) as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            word, *tail = s.split(None, 1)
            rest = tail[0] if tail else ""
            word = word.lower()
            if word == "vertex":
                try:
                    xyz = [float(v) for v in _FLOAT_LINE.split(rest.strip())[:3]]
                except ValueError as exc:
                    raise StlReadError(f"{path.name}: unreadable vertex line {s[:60]!r}") from exc
                if len(xyz) != 3:
                    raise StlReadError(f"{path.name}: vertex line without three numbers {s[:60]!r}")
                loop.append(xyz)
                if not in_loop and len(loop) == 3:
                    flush()             # a file without loop lines: every three vertices
            elif word == "outer" or word == "loop":
                flush(); in_loop = True
            elif word == "endloop":
                flush(); in_loop = False
            elif word == "solid":
                flush(); in_loop = False
                name = rest.strip() or "solid"
                solids.setdefault(name, [])
            elif word == "endsolid":
                flush(); in_loop = False
                name = None
    flush()
    return solids


def read_stl_triangles(path: Path) -> list[tuple]:
    """Every triangle of an STL, binary or ASCII, all solids together, as ([x,y,z], [..], [..])."""
    path = Path(path)
    layout = _layout_of(path)
    if layout.binary:
        return [tuple(t) for t in read_binary_corners(path, layout).tolist()]
    return [t for part in _ascii_solids(path).values() for t in part]


def write_stl_binary(path: Path, tris: list[tuple] | np.ndarray) -> None:
    # Plain binary STL (the format StlAPI_Writer emits): 80-byte header, zeroed normals.
    # Coordinates round to float32 exactly as OCC's writer does, so triangles written here
    # stay vertex-identical with triangles that went through an OCC write of the same nodes.
    if isinstance(tris, np.ndarray):
        # an (n, 3, 3) array: the same records, written in one go (float32 rounds to nearest,
        # as struct's "f" does)
        rec = np.zeros(len(tris), dtype=_BINARY_DTYPE)
        rec["v"] = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
        with Path(path).open("wb") as fh:
            fh.write(b"\0" * 80)
            fh.write(struct.pack("<I", len(rec)))
            fh.write(rec.tobytes())
        return
    with Path(path).open("wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            fh.write(struct.pack("<12fH", 0.0, 0.0, 0.0, *a[:3], *b[:3], *c[:3], 0))


def inspect_stl(workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
    p = Path(workspace) / geometry_file
    if not p.exists():
        return {"error": f"{geometry_file} not found in workspace"}
    try:
        tris = read_stl_triangles(p)
    except StlReadError as exc:
        return {"error": f"{geometry_file}: {exc}"}
    if not tris:
        return {"error": f"{geometry_file} holds no triangles"}
    mn =[min(v[i] for t in tris for v in t) for i in range(3)]
    mx = [max(v[i] for t in tris for v in t) for i in range(3)]
    ext = [round(mx[i] - mn[i], 4) for i in range(3)]
    return {
        "bbox_min": [round(v, 4) for v in mn],
        "bbox_max": [round(v, 4) for v in mx],
        "extents_xyz": ext,
        "thinnest_axis": "xyz"[ext.index(min(ext))],
        "n_triangles": len(tris),
        "units": "metres (STL is unitless; assumes the CAD export was in metres)",
    }


def read_stl_solids(path: Path) -> dict:
    """An STL's triangles by solid name. A binary STL stores no names (a "solid ..." in its header
    is not one), so its triangles come back under one entry named for the file, with the
    zero-area slivers whose corners coincide left out (as VTK's reader leaves them out)."""
    path = Path(path)
    layout = _layout_of(path)
    if layout.binary:
        tris = [tuple(tuple(v) for v in t) for t in read_binary_corners(path, layout).tolist()]
        return {path.stem: drop_degenerate(tris)}
    return _ascii_solids(path)


def drop_degenerate(tris: list[tuple]) -> list[tuple]:
    def coincident(p, q): return p[0] == q[0] and p[1] == q[1] and p[2] == q[2]
    return [t for t in tris if not (coincident(t[0], t[1])
            or coincident(t[1], t[2]) or coincident(t[0], t[2]))]


def mirror_y(tris: list[tuple]) -> list[tuple]:
    out = []
    for a, b, c in tris:
        out.append(([a[0], -a[1], a[2]], [c[0], -c[1], c[2]], [b[0], -b[1], b[2]]))
    return out


def _box_triangles(lo, hi) -> list[tuple]:
    x0, y0, z0 = lo; x1, y1, z1 = hi; out: list[tuple] = []
    def quad(a, b, c, d): out.append((a, b, c)); out.append((a, c, d))
    quad([x0, y0, z0], [x0, y1, z0], [x0, y1, z1], [x0, y0, z1])
    quad([x1, y0, z0], [x1, y0, z1], [x1, y1, z1], [x1, y1, z0])
    quad([x0, y0, z0], [x0, y0, z1], [x1, y0, z1], [x1, y0, z0])
    quad([x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1])
    quad([x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0])
    quad([x0, y0, z1], [x0, y1, z1], [x1, y1, z1], [x1, y0, z1])
    return out


def _write_solid(fh, name: str, tris: list[tuple]) -> None:
    fh.write(f"solid {name}\n")
    for a, b, c in tris:
        fh.write("facet normal 0 0 0\n outer loop\n")
        for v in (a, b, c):
            fh.write(f"  vertex {v[0]} {v[1]} {v[2]}\n")
        fh.write(" endloop\nendfacet\n")
    fh.write(f"endsolid {name}\n")


def write_stl_solids(path: Path, solids: dict) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        for name, tris in solids.items():
            _write_solid(fh, name, tris)
