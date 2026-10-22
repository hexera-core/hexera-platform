# Responsibility: Say what every semiconductor test part is, measured from its own STL.
# Boundaries: describes parts; it meshes nothing and judges nothing.
"""THE TEST PARTS, AND THE NUMBERS A RUN NEEDS BEFORE IT STARTS.

A conjugate run has to be told four things it cannot work out for itself: the coolant's volume, how
many separate passages it has, the narrowest feature in it, and which way the flow goes. Get any of
them wrong and the run either refuses a good mesh or passes a bad one. Typing them on the command
line each time is how they end up wrong, so they live here, and all but the last are MEASURED FROM
THE STL rather than copied from a note.

  fluid volume     the signed volume of the closed fluid shell, by the divergence theorem
  passages         connected components of the fluid shell, by union-find over shared vertices
  bounds           both bounding boxes, which is what says where the coolant reaches the outside
  narrowest        the only figure that cannot be measured from a triangle soup, so it is declared

A STL is a triangle soup with no notion of a body, so the passage count is not a field anyone wrote
down: it is computed. That matters because it is the check that catches a mesh too coarse to hold a
channel together, and a declared count can be stale the moment the CAD is rebuilt. 100 channels that
never touch each other must come back as 100.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: Vertices of one STL triangle are written in float32, so two triangles that share a corner in the
#: CAD can disagree about where it is by a few parts in 10^7. Snapping to this fraction of the
#: surface's own diagonal makes them the same point, which is what the passage count depends on.
WELD_RTOL = 1e-6


class PartError(ValueError):
    """A part whose files or declared numbers cannot be used."""


# #
# reading an STL, either flavour
# #
def read_stl(path: Path) -> list[tuple[tuple[float, float, float], ...]]:
    """Every triangle of an STL, binary or ASCII.

    Both flavours, because the CAD here is exported binary and the block surface this package
    writes is ASCII (binary carries no patch names). A reader that handles one of them is a
    reader that will be handed the other.
    """
    raw = path.read_bytes()
    looks_ascii = raw[:5].lower() == b"solid" and b"facet" in raw[:2048].lower()
    if looks_ascii:
        tris: list[tuple] = []
        corner: list[tuple[float, float, float]] = []
        for line in raw.decode("ascii", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                corner.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(corner) == 3:
                    tris.append(tuple(corner))
                    corner = []
        if not tris:
            raise PartError(f"{path.name}: ASCII STL with no triangles")
        return tris
    if len(raw) < 84:
        raise PartError(f"{path.name}: too short to be an STL")
    count = struct.unpack_from("<I", raw, 80)[0]
    if len(raw) != 84 + count * 50:
        raise PartError(
            f"{path.name}: header claims {count} triangles, which needs {84 + count * 50} bytes, "
            f"and the file is {len(raw)}")
    out = []
    for i in range(count):
        o = 84 + i * 50 + 12
        out.append(tuple(struct.unpack_from("<3f", raw, o + v * 12) for v in range(3)))
    return out


def write_binary_stl(path: Path, tris) -> int:
    """Write triangles as a binary STL, recomputing every facet normal.

    The normals are recomputed rather than carried over because a scaled triangle's old normal is
    still unit-length and still points the right way, so a stale one is invisible until a mesher
    uses it to decide inside from outside. topoSet does exactly that.
    """
    out = bytearray(b"\0" * 80) + struct.pack("<I", len(tris))
    for a, b, c in tris:
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        w = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        m = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5 or 1.0
        out += struct.pack("<3f", n[0] / m, n[1] / m, n[2] / m)
        for p in (a, b, c):
            out += struct.pack("<3f", *p)
        out += struct.pack("<H", 0)
    path.write_bytes(bytes(out))
    return len(tris)


#: Anything whose longest side is bigger than this is not a chip. The CAD in this package is
#: authored in MILLIMETRES and an OpenFOAM case is in METRES, and a surface handed over still in
#: millimetres makes every cell size a thousand times wrong - which does not fail, it just runs
#: until someone kills it. A 300 mm wafer is the largest thing this work will ever see, so a part
#: measuring more than 0.3 of a unit is being read in the wrong one.
MAX_PART_M = 0.3


def unit_complaint(lo, hi) -> str:
    """Empty when the extent could be metres, otherwise what is wrong with it."""
    longest = max(hi[k] - lo[k] for k in range(3))
    if longest > MAX_PART_M:
        return (f"the longest side is {longest:.4g}, and this package's unit is METRES, so that "
                f"would be {longest:.4g} m - wider than a 300 mm wafer. It is almost certainly "
                f"millimetres, i.e. {longest:.4g} mm. Scale it by 1e-3 on the way in "
                f"(`semi.py import --scale 1e-3`) rather than meshing it as it stands: the run "
                f"does not fail, it just never finishes.")
    return ""


def scale_stl(src: Path, dst: Path, factor: float) -> int:
    """Copy an STL with every coordinate multiplied, which is how millimetres become metres."""
    if factor <= 0:
        raise PartError(f"scale factor must be positive, got {factor}")
    tris = [tuple(tuple(c * factor for c in p) for p in tri) for tri in read_stl(src)]
    return write_binary_stl(dst, tris)


def bounds(tris) -> tuple[tuple, tuple]:
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for tri in tris:
        for p in tri:
            for k in range(3):
                if p[k] < lo[k]:
                    lo[k] = p[k]
                if p[k] > hi[k]:
                    hi[k] = p[k]
    return tuple(lo), tuple(hi)


def area_m2(tris) -> float:
    """The surface area of a triangle soup.

    For the coolant this is the WETTED AREA, and it is wanted for two separate reasons. It is what
    heat crosses, so it is the quantity staircasing distorts that volume does not. And it sets how
    many cells a conjugate build will have: `surfaceMeshRefinement` refines within
    refinementThickness of the coolant surface on BOTH sides, so the metal within that distance is
    filled at the interface cell too, and a cell estimate that puts all the metal at the background
    size comes out badly short. One did, by a factor of 2.4, and the run was OOM-killed.
    """
    total = 0.0
    for a, b, c in tris:
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        w = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        total += (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
    return total / 2.0


def volume_m3(tris) -> float:
    """The volume enclosed by a closed, outward-wound surface, by the divergence theorem.

    Sum of the signed tetrahedron volumes from the origin to each triangle. Exact for any closed
    surface and meaningless for an open one, which is the point: a shell with a hole in it gives a
    number that will not match the CAD, and that mismatch is the check.
    """
    total = 0.0
    for a, b, c in tris:
        total += (a[0] * (b[1] * c[2] - b[2] * c[1])
                  - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return abs(total) / 6.0


def components(tris, *, rtol: float = WELD_RTOL) -> list[list[int]]:
    """Triangle indices grouped into separate closed shells, by union-find over welded vertices.

    The count is the number a conjugate run checks the meshed coolant against: 100 parallel channels
    that never touch are 100 passages, and a mesh reporting 101 has broken one in half while
    `checkMesh` says "Mesh OK".

    The GROUPS matter too, and not only the count, because the flow axis is a property of one
    passage rather than of the part. An array of 200 channels 14 mm long spread across 20 mm of
    silicon has a fluid bounding box that is widest across the channels, so "the axis the coolant
    spans furthest" picks the direction the coolant does not go. Measured per passage it picks the
    channel.
    """
    lo, hi = bounds(tris)
    diag = sum((hi[k] - lo[k]) ** 2 for k in range(3)) ** 0.5
    grid = (rtol * diag) or 1e-12

    def key(p):
        return (round(p[0] / grid), round(p[1] / grid), round(p[2] / grid))

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    vertex_of: dict[tuple, int] = {}
    for i, tri in enumerate(tris):
        if i not in parent:
            parent[i] = i
        for p in tri:
            k = key(p)
            if k in vertex_of:
                union(vertex_of[k], i)
            else:
                vertex_of[k] = i
    groups: dict[int, list[int]] = {}
    for i in parent:
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def passages(tris, *, rtol: float = WELD_RTOL) -> int:
    """How many separate closed shells the surface is."""
    return len(components(tris, rtol=rtol))


def dominant_passage_extent(tris, *, rtol: float = WELD_RTOL) -> tuple[float, float, float]:
    """The x, y and z spans of the LARGEST passage, as a stand-in for where the flow goes.

    Largest by triangle count, which on an array of identical channels is any one of them and on a
    mixed part is the one carrying most of the coolant. Measuring the whole fluid instead gives the
    array's width on a wide, short channel array, and the flow axis comes out across the channels.
    """
    groups = components(tris, rtol=rtol)
    if not groups:
        return (0.0, 0.0, 0.0)
    lo, hi = bounds([tris[i] for i in groups[0]])
    return tuple(hi[k] - lo[k] for k in range(3))  # type: ignore[return-value]


# #
# a part
# #
@dataclass(frozen=True)
class Part:
    name: str
    solid_stl: Path
    fluid_stl: Path
    #: The narrowest coolant passage, in metres. DECLARED, not measured: it cannot be read off a
    #: triangle soup, and it is what the cell size is derived from.
    narrowest_m: float
    fluid_volume_m3: float
    solid_volume_m3: float
    #: The coolant's wetted area in m2: what heat crosses, and what sets how much of the metal a
    #: conjugate build refines. See area_m2.
    wetted_area_m2: float
    passages: int
    #: Spans of the largest single passage, in metres. This and not the whole fluid's bounding box
    #: is what the flow axis is chosen from: see dominant_passage_extent.
    passage_extent: tuple[float, float, float]
    fluid_lo: tuple[float, float, float]
    fluid_hi: tuple[float, float, float]
    solid_lo: tuple[float, float, float]
    solid_hi: tuple[float, float, float]
    note: str = ""
    #: Anything the CAD manifest said that is worth keeping beside the measurements.
    declared: dict = field(default_factory=dict)

    @property
    def extent_mm(self) -> tuple[float, float, float]:
        return tuple((self.solid_hi[k] - self.solid_lo[k]) * 1e3 for k in range(3))

    @property
    def block_volume_m3(self) -> float:
        e = self.solid_hi, self.solid_lo
        return abs((e[0][0] - e[1][0]) * (e[0][1] - e[1][1]) * (e[0][2] - e[1][2]))

    def cell_for(self, cells_across: int, levels: int) -> float:
        """The BACKGROUND cell that puts `cells_across` cells across the narrowest passage.

        The octree halves the cell once per level, so the background has to start 2**levels
        coarser than the cell wanted at the coolant surface. Asking for the interface size and
        handing it over as the background is the mistake that produces a mesh 512 times too big.
        """
        if cells_across < 1:
            raise PartError(f"cells across must be at least 1, got {cells_across}")
        return (self.narrowest_m / cells_across) * (2 ** levels)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "solid_stl": self.solid_stl.name,
            "fluid_stl": self.fluid_stl.name,
            "narrowest_um": round(self.narrowest_m * 1e6, 4),
            "fluid_volume_mm3": round(self.fluid_volume_m3 * 1e9, 9),
            "solid_volume_mm3": round(self.solid_volume_m3 * 1e9, 9),
            "wetted_area_mm2": round(self.wetted_area_m2 * 1e6, 9),
            "passages": self.passages,
            "passage_extent_mm": [round(v * 1e3, 9) for v in self.passage_extent],
            "extent_mm": [round(v, 6) for v in self.extent_mm],
            "fluid_lo": list(self.fluid_lo), "fluid_hi": list(self.fluid_hi),
            "solid_lo": list(self.solid_lo), "solid_hi": list(self.solid_hi),
            "note": self.note,
            "declared": self.declared,
        }


def measure(name: str, solid_stl: Path, fluid_stl: Path, *, narrowest_m: float,
            note: str = "", declared: dict | None = None) -> Part:
    """Read both STLs and work out everything about the part except its narrowest feature."""
    solid = read_stl(solid_stl)
    fluid = read_stl(fluid_stl)
    slo, shi = bounds(solid)
    flo, fhi = bounds(fluid)
    groups = components(fluid)
    plo, phi = bounds([fluid[i] for i in groups[0]]) if groups else (flo, fhi)
    return Part(name=name, solid_stl=solid_stl, fluid_stl=fluid_stl, narrowest_m=narrowest_m,
                fluid_volume_m3=volume_m3(fluid), solid_volume_m3=volume_m3(solid),
                wetted_area_m2=area_m2(fluid),
                passages=len(groups),
                passage_extent=tuple(phi[k] - plo[k] for k in range(3)),
                fluid_lo=flo, fluid_hi=fhi, solid_lo=slo, solid_hi=shi,
                note=note, declared=declared or {})


# #
# the registry on disk
# #
REGISTRY = "parts.json"


def load(cad_dir: Path) -> dict[str, Part]:
    """Read the registry `index_parts` wrote. Raises if it is missing, rather than guessing."""
    path = Path(cad_dir) / REGISTRY
    if not path.is_file():
        raise PartError(
            f"no {REGISTRY} in {cad_dir}. Build it with:"
            f"{chr(10)}    python devtools/semiconductor/index_parts.py")
    out = {}
    for d in json.loads(path.read_text()):
        out[d["name"]] = Part(
            name=d["name"],
            solid_stl=Path(cad_dir) / d["solid_stl"], fluid_stl=Path(cad_dir) / d["fluid_stl"],
            narrowest_m=d["narrowest_um"] * 1e-6,
            fluid_volume_m3=d["fluid_volume_mm3"] * 1e-9,
            solid_volume_m3=d["solid_volume_mm3"] * 1e-9,
            wetted_area_m2=d.get("wetted_area_mm2", 0.0) * 1e-6,
            passages=d["passages"],
            passage_extent=tuple(v * 1e-3 for v in d["passage_extent_mm"]),
            fluid_lo=tuple(d["fluid_lo"]), fluid_hi=tuple(d["fluid_hi"]),
            solid_lo=tuple(d["solid_lo"]), solid_hi=tuple(d["solid_hi"]),
            note=d.get("note", ""), declared=d.get("declared", {}))
    return out


def save(cad_dir: Path, parts: list[Part]) -> Path:
    path = Path(cad_dir) / REGISTRY
    path.write_text(json.dumps([p.as_dict() for p in sorted(parts, key=lambda q: q.name)],
                               indent=2) + chr(10))
    return path


__all__ = ["Part", "PartError", "REGISTRY", "WELD_RTOL", "MAX_PART_M", "bounds", "volume_m3",
           "passages", "components", "dominant_passage_extent", "area_m2",
           "read_stl", "write_binary_stl", "scale_stl", "unit_complaint",
           "measure", "load", "save"]
