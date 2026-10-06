# Responsibility: Let a builder name a large assembly's regions without listing every solid - by index, by the name the CAD file gives a solid, or by a glob pattern over those names, with one region per solid from a single entry - and describe an assembly's solids compactly enough to fit a tool reply.
"""Regions of a large assembly, without the per-solid list.

ECXML-TEST (2026-10-06): on a 351-part server and a 1,000-part board the multi-region
geometry_report ran to 87k and 241k characters against the builder's 16k tool cap, so the
builder never saw a solid, never called configure_mesh, and no mesh was made. Any assembly past
~60 solids hit it. Two halves fix it for every multi-region assembly, not one file:

- configure_mesh takes a region's solids by name or pattern ("air", "Cap_*") as well as by index,
  and one entry {"per_solid": true, "type": "solid", "solids": ["*"]} makes every solid not
  claimed elsewhere its own region, named after the solid; region_refinement keys may be
  patterns over region names. The engine resolves all of it to indices before anything else.
- geometry_report lists a large assembly as name groups plus one compact row per solid, and
  proposes the region map when the file says which solids are the fluid (geometry.py pages the
  rows if they still do not fit).
"""
from __future__ import annotations

import fnmatch
import re

#: Up to this many solids, the report keeps one full dict per solid (the format builders know).
COMPACT_ABOVE = 40
#: At most this many name groups are listed; the rest are counted.
MAX_GROUPS = 60

_GLOB = re.compile(r"[*?\[]")


def _is_index(item: object) -> bool:
    return isinstance(item, int) and not isinstance(item, bool)


def _matches(item: object, names: dict[int, str | None]) -> list[int]:
    """The solids an item names: an index, a solid's exact name, or a glob over the names."""
    if _is_index(item):
        return [int(item)]  # type: ignore[call-overload]
    text = str(item)
    exact = [i for i, n in names.items() if n == text]
    if exact or not _GLOB.search(text):
        return exact
    return [i for i, n in names.items() if n is not None and fnmatch.fnmatchcase(n, text)]


def resolve_regions(regions: list[dict], solids: list[dict]) -> tuple[list[dict], list[str]]:
    """The region map with every solid given by its index, and what could not be resolved.

    Entries without "per_solid" are resolved first; a "per_solid" entry then takes the solids
    its items match that no other entry claimed, one region each, named after the solid."""
    from meshpipeline.contracts.patch_names import mesh_safe, unreserved

    names = {int(s["index"]): s.get("name") for s in solids}
    out: list[dict] = []
    problems: list[str] = []
    claimed: set[int] = set()
    for r in regions:
        if r.get("per_solid"):
            continue
        idx: list[int] = []
        for item in r.get("solids") or []:
            got = _matches(item, names)
            if not got:
                problems.append(f"region {r.get('name')!r}: {item!r} names no solid")
            idx += [i for i in got if i not in idx]
        claimed.update(idx)
        out.append({k: v for k, v in r.items() if k != "per_solid"} | {"solids": idx})
    taken = {str(r.get("name", "")).casefold() for r in out}
    for r in regions:
        if not r.get("per_solid"):
            continue
        got = []
        for item in r.get("solids") or ["*"]:
            m = [i for i in _matches(item, names) if i not in claimed and i not in got]
            if not m and not _GLOB.search(str(item)):
                problems.append(f"a per-solid entry: {item!r} names no unassigned solid")
            got += m
        for i in got:
            claimed.add(i)
            base = unreserved(mesh_safe(names.get(i) or "", fallback=f"solid_{i}"))
            name, k = base, 1
            while name.casefold() in taken:
                k += 1
                name = f"{base}_{k}"
            taken.add(name.casefold())
            out.append({"name": name, "type": str(r.get("type", "solid")), "solids": [i]})
    return out, problems


def expand_refinement(region_refinement: dict, region_names: list[str]) -> tuple[dict, list[str]]:
    """region_refinement with its pattern keys spelled out over the region names: an exact name
    wins over a pattern, an earlier pattern over a later one."""
    out: dict = {k: v for k, v in region_refinement.items() if k in region_names}
    problems: list[str] = []
    for key, lv in region_refinement.items():
        if key in region_names:
            continue
        if not _GLOB.search(str(key)):
            problems.append(f"region_refinement: {key!r} names no region")
            continue
        hit = [n for n in region_names if fnmatch.fnmatchcase(n, str(key))]
        if not hit:
            problems.append(f"region_refinement: {key!r} matches no region")
        for n in hit:
            out.setdefault(n, lv)
    return out, problems


def _stem(name: str | None) -> str:
    """A name without its trailing number: 'Cap_12' -> 'Cap', 'R 7' -> 'R'."""
    if not name:
        return "(unnamed)"
    return re.sub(r"[\W_]*\d+$", "", name) or name


def _ranges(ix: list[int]) -> str:
    ix = sorted(ix)
    out, start = [], ix[0]
    for a, b in zip(ix, ix[1:] + [None]):
        if b is None or b != a + 1:
            out.append(f"{start}" if start == a else f"{start}-{a}")
            if b is not None:
                start = b
    return ",".join(out)


def groups(solids: list[dict], needed: dict[int, int]) -> tuple[list[dict], int]:
    """The solids grouped by name stem, largest groups first, and how many groups were left out."""
    by: dict[str, list[int]] = {}
    for s in solids:
        by.setdefault(_stem(s.get("name")), []).append(int(s["index"]))
    rows = []
    for stem, ix in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lv = [needed.get(i, 0) for i in ix]
        rows.append({"names": f"{stem}*" if len(ix) > 1 else stem, "count": len(ix),
                     "indices": _ranges(ix), "needed_level": [min(lv), max(lv)]})
    return rows[:MAX_GROUPS], max(0, len(rows) - MAX_GROUPS)


def compact_rows(solids: list[dict], needed: dict[int, int]) -> list[list]:
    """One row per solid: index, name, needed level, bounding box (m)."""
    return [[int(s["index"]), s.get("name"), needed.get(int(s["index"]), 0),
             [round(v, 6) for v in s["bbox_min"]], [round(v, 6) for v in s["bbox_max"]]]
            for s in solids]


ROW_FORMAT = ["index", "name", "needed_level", "bbox_min_m", "bbox_max_m"]


def suggested_regions(solids: list[dict], fluid_names: list[str]) -> list[dict] | None:
    """The region map to send when the solids are named and the fluid is known: each fluid by
    name, every other solid its own region."""
    named = {s.get("name") for s in solids}
    fluids = [n for n in fluid_names if n in named]
    if not fluids or None in named:
        return None
    return ([{"name": n, "type": "fluid", "solids": [n]} for n in fluids]
            + [{"per_solid": True, "type": "solid", "solids": ["*"]}])


def enclosing(solids: list[dict]) -> list[str]:
    """Named solids whose box is the whole assembly's: the fluid of an air-box assembly."""
    if not solids:
        return []
    lo = [min(s["bbox_min"][i] for s in solids) for i in range(3)]
    hi = [max(s["bbox_max"][i] for s in solids) for i in range(3)]
    tol = 1e-6 * (sum((hi[i] - lo[i]) ** 2 for i in range(3)) ** 0.5 or 1.0)
    return [str(s["name"]) for s in solids if s.get("name")
            and all(abs(s["bbox_min"][i] - lo[i]) <= tol and abs(s["bbox_max"][i] - hi[i]) <= tol
                    for i in range(3))]
