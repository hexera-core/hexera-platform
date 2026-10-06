# Responsibility: Turn an ECXML model into a multi-region OpenFOAM mesh on a snap grid: place the parts, build the grid, paint the cells, name the regions and patches, write the mesh, and prove the result is the file's model.
# Owns: the region and patch naming (the fused build's names), the boundary patches on the domain sides, the thin-layer and staircase report, the fidelity checks, the build report and the physics sidecar.
# Boundaries: pure Python and numpy; no OpenCASCADE, no boolean, no native mesher (engines/snapgrid/native.py splits the regions). The physics and the overlap rule are #163's.
# Collaborates with: cad/ingest/ecxml_place.py, engines/snapgrid/grid.py, engines/snapgrid/polymesh.py, engines/snapgrid/native.py, engines/snapgrid/cli.py.
"""ECXML -> one polyMesh with a cellZone per region (Option B, "no gluing").

The mesh is written once, whole: every cell belongs to exactly one zone (a part, or a connected
air space), every face between two zones is shared by construction, and splitMeshRegions
(-cellZonesOnly) cuts it into one polyMesh per region with matching mappedWall interfaces - the
same case layout as the fused path (Option A) delivers.

What the file says and the mesh holds, checked before anything is written:

* every box part's volume in the mesh equals the file's numbers (to round-off), wherever the
  file's numbers fix it (cad/ingest/ecxml_build._expected_volume, the same independent sum the
  fused build is held to); a cylinder or a round vent is staircased and its volume error is
  reported;
* the regions fill the domain;
* every active object is accounted for, once; region names are unique;
* two solid parts share faces exactly where the file makes them touch, and nowhere else;
* every part has at least one cell through its thickness on every axis - a layer is never merged
  away - and the parts with fewer cells across than asked are listed.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.cad.ingest.ecxml import SPEC, EcxmlError, read_ecxml
from meshpipeline.cad.ingest.ecxml_build import (
    _AXES,
    FLUID_NAME,
    SIDECAR_SUFFIX,
    SIDECAR_VERSION,
    VOLUME_RTOL,
    FidelityError,
    _Box,
    _domain_patches,
    _expected_volume,
    _file_contacts,
    _material,
    _um,
    _unique_namer,
    physics_summary,
)
from meshpipeline.cad.ingest.ecxml_place import Placement, describe, place
from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid.polymesh import SIDES, Patch, side_shape, write_polymesh

REPORT_NAME = "snapgrid_report.json"
SIDECAR_NAME = "thermal_model.json"
MESHER = "snapgrid"
REPORT_VERSION = 1
#: Thin-layer rows the report keeps (thinnest first); the counts cover every part.
_THIN_ROWS = 200


@dataclass
class SnapgridMesh:
    case: Path
    report: dict
    sidecar: dict
    regions: list[dict] = field(default_factory=list)   # name, type (fluid/solid), cells

    @property
    def region_names(self) -> list[str]:
        return [r["name"] for r in self.regions]


# ------------------------------------------------------------------------------ the case ------
def _foam_dict(cls: str, obj: str, body: str, location: str = "system") -> str:
    return ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
            f"    class       {cls};\n    location    \"{location}\";\n    object      {obj};\n}}\n\n"
            + body)


def write_case_skeleton(case: Path) -> None:
    """The three system dicts every OpenFOAM utility reads before it opens a mesh."""
    system = case / "system"
    system.mkdir(parents=True, exist_ok=True)
    (system / "controlDict").write_text(_foam_dict("dictionary", "controlDict", (
        "application     chtMultiRegionFoam;\nstartFrom       startTime;\nstartTime       0;\n"
        "stopAt          endTime;\nendTime         1;\ndeltaT          1;\n"
        "writeControl    timeStep;\nwriteInterval   1;\nwriteFormat     binary;\n"
        "writePrecision  12;\nwriteCompression off;\ntimeFormat      general;\n"
        "timePrecision   6;\nrunTimeModifiable false;\n")))
    (system / "fvSchemes").write_text(_foam_dict("dictionary", "fvSchemes", (
        "ddtSchemes { default steadyState; }\ngradSchemes { default Gauss linear; }\n"
        "divSchemes { default none; }\nlaplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n")))
    (system / "fvSolution").write_text(_foam_dict("dictionary", "fvSolution", "solvers {}\n"))


def render_region_properties(fluids: list[str], solids: list[str]) -> str:
    return _foam_dict("dictionary", "regionProperties", (
        "regions\n(\n"
        f"    fluid       ({' '.join(fluids)})\n"
        f"    solid       ({' '.join(solids)})\n"
        ");\n"), location="constant")


# ------------------------------------------------------------------------------ the mesh ------
def mesh_ecxml(src, case, plan: G.GridPlan | None = None, *, binary: bool | None = None,
               dry_run: bool = False) -> SnapgridMesh:
    """Read `src` (ECXML), mesh it on a snap grid and write the single-region polyMesh with a
    cellZone per region, constant/regionProperties, the report and the physics sidecar into the
    OpenFOAM case `case`. `dry_run` stops after the grid is sized (nothing written but the
    report). Raises EcxmlError (a file that cannot be read or placed), G.OverBudget (a model that
    needs more cells than the budget to keep its layers) or FidelityError (a mesh that would not be
    the file's model - a bug, never a model fault)."""
    plan = plan or G.GridPlan()
    case = Path(case)
    t = {"start": time.perf_counter()}
    model = read_ecxml(src)
    placement = place(model)
    t["placed"] = time.perf_counter()
    grid = G.build_grid(placement, plan)
    t["gridded"] = time.perf_counter()
    report: dict = {"mesher": MESHER, "report_version": REPORT_VERSION,
                    "input": {"file": Path(src).name, "model_name": model.name,
                              "producer": model.producer,
                              "active_objects": sum(1 for o in model.active_objects
                                                    if o.kind not in ("assembly", "heatsink"))},
                    "plan": asdict(plan), "plan_used": asdict(grid.plan),
                    "relaxed": grid.relaxed, "grid": G.stats(grid)}
    if dry_run:
        report["dry_run"] = True
        report["timings_s"] = _timings(t)
        return SnapgridMesh(case=case, report=report, sidecar={})

    zone = G.paint(placement, grid)
    t["painted"] = time.perf_counter()
    vols = G.cell_volumes(grid)
    airs, n_air = G.air_spaces(zone, vols)
    P = len(placement.parts)
    flat = zone.ravel()
    part_cells = np.bincount(flat + 1, minlength=P + 1)[1:]
    part_vol = np.bincount(flat + 1, weights=vols.ravel(), minlength=P + 1)[1:]
    air_flat = airs.ravel()
    air_vol = np.bincount(air_flat + 1, weights=vols.ravel(), minlength=n_air + 1)[1:]
    air_cells = np.bincount(air_flat + 1, minlength=n_air + 1)[1:]
    t["measured"] = time.perf_counter()

    notes = placement.notes
    records = placement.records
    build_report = describe(placement) + list(placement.report)
    live = _settle_parts(placement, part_cells, build_report)
    names = _name_regions(placement, live, n_air)
    part_region = {k: names["parts"][k] for k in live}
    air_names = names["air"]
    fluid_rows = []
    for a in range(n_air):
        fluid_rows.append({"name": air_names[a], "type": "fluid", "kind": "air",
                           "cells": int(air_cells[a]), "volume_m3": float(air_vol[a]),
                           "notes": [] if a == 0 else
                           ["an air space not connected to the main air (a sealed cavity)"]})
    if n_air > 1:
        notes.append(f"the air is {n_air} separate spaces (a sealed enclosure keeps its own air); "
                     "each is its own fluid region")
    if n_air == 0:
        notes.append("the parts fill the whole domain: there is no air to mesh")

    # one cellZone per region: the air spaces first (largest first), then the parts in file order
    zone_names = list(air_names) + [part_region[k] for k in live]
    part_zone = np.full(P, -1, dtype=np.int32)
    for z, k in enumerate(live):
        part_zone[k] = n_air + z
    cell_zone = np.where(zone >= 0, part_zone[np.maximum(zone, 0)], airs).astype(np.int32)
    if (cell_zone < 0).any():
        raise FidelityError("internal: a cell belongs to no region")

    regions = list(fluid_rows)
    snapped_box = _snapped_boxes(placement)
    for k in live:
        p = placement.parts[k]
        regions.append({
            "name": part_region[k], "type": "solid", "kind": p.kind, "material": p.material,
            "power_W": p.power, "cells": int(part_cells[k]), "volume_m3": float(part_vol[k]),
            "objects": [{"name": o.name, "path": list(o.path), "kind": o.kind,
                         "box_m": snapped_box(o).as_dict(), "power_W": o.power,
                         "material": o.material or None} for o in p.objects],
            "notes": list(p.notes)})

    checks, contacts, staircase = _fidelity(placement, grid, zone, part_vol, live, part_region,
                                            regions, vols)
    thin = _thin_layers(placement, grid, live, part_region)
    interfaces = _interfaces(cell_zone, zone_names, n_air)
    patches, side_ids, patch_rows = _boundary(placement, grid, zone_names)
    t["checked"] = time.perf_counter()

    case.mkdir(parents=True, exist_ok=True)
    write_case_skeleton(case)
    written = write_polymesh(case, grid.lines, cell_zone=cell_zone, zone_names=zone_names,
                             side_patches=side_ids, patches=patches, binary=binary)
    fluids = list(air_names)
    solids = [part_region[k] for k in live]
    (case / "constant" / "regionProperties").write_text(render_region_properties(fluids, solids))
    t["written"] = time.perf_counter()
    for row in patch_rows:
        row["faces"] = written["patches"].get(row["mesh_patch"], 0)
    # a side the file's plates cover whole has no patch of its own (expected); any other patch
    # left without a face was overwritten on its side by a later object - said, never hidden
    lost = [r["name"] for r in patch_rows if not r["faces"] and not r.get("covered_side")]
    patch_rows = [r for r in patch_rows if r["faces"] or not r.get("covered_side")]
    if lost:
        build_report.append("Not meshed (a later object on the same domain side covers all of "
                            "it): " + ", ".join(lost[:12]) + ".")

    build_report += _report_lines(placement, grid, thin, staircase, records)
    build_report += checks
    sidecar = _sidecar(placement, regions, patch_rows, build_report, contacts, grid)
    (case / SIDECAR_NAME).write_text(json.dumps(sidecar, indent=1, default=float))
    report.update({
        "regions": [{k: r.get(k) for k in ("name", "type", "kind", "material", "power_W",
                                           "cells", "volume_m3", "file_volume_m3",
                                           "volume_error")}
                    for r in regions],
        "fluid_regions": fluids, "solid_regions": solids,
        "thin_layers": thin, "staircased": staircase,
        "interfaces": interfaces, "patches": written["patches"],
        "solid_contacts": sorted(sorted(c) for c in contacts),
        "polymesh": {k: written[k] for k in ("cells", "faces", "internal_faces", "points",
                                             "binary")},
        "build_report": build_report, "notes": notes,
        "physics": physics_summary(sidecar),
    })
    report["timings_s"] = _timings(t)
    (case / REPORT_NAME).write_text(json.dumps(report, indent=1, default=float))
    return SnapgridMesh(case=case, report=report, sidecar=sidecar,
                        regions=[{"name": r["name"], "type": r["type"], "cells": r["cells"]}
                                 for r in regions])


def _timings(t: dict) -> dict:
    keys = list(t)
    out = {f"{b}": round(t[b] - t[a], 3) for a, b in zip(keys[:-1], keys[1:])}
    out["total"] = round(t[keys[-1]] - t[keys[0]], 3)
    return out


def _snapped_boxes(placement: Placement):
    from meshpipeline.cad.ingest.ecxml_build import _snap_function

    objs = placement.model.active_objects
    snapped, _moved = _snap_function(placement.model, objs, placement.tol.noise)
    return snapped


def _settle_parts(placement: Placement, part_cells: np.ndarray, report: list[str]) -> list[int]:
    """The parts that kept cells (in file order); the others are recorded as not built, with the
    reason: overwritten by parts that take precedence, or thinner than the file can state."""
    live = []
    records = placement.records
    for k, p in enumerate(placement.parts):
        name = p.objects[0].props.get("heatsink") or p.objects[0].name
        if p.dropped:
            why = p.dropped
        elif part_cells[k] > 0:
            live.append(k)
            continue
        elif p.winners:
            who = ", ".join(q.objects[0].props.get("heatsink") or q.objects[0].name
                            for q in p.winners[:6])
            icepak = placement.model.producer.strip().lower() == "icepak"
            rule = ("the embedded solid wins (Icepak)" if icepak else
                    "the later object in the file wins") + ", JEP181A 4.5.1"
            report.append(f"Overlap: {who} overwrite(s) all of {name} ({rule}), so {name} is "
                          "not meshed.")
            why = "objects that take precedence overwrite all of it (JEP181A 4.5.1)"
        else:
            why = (f"it is thinner than the file's own precision ({_um(placement.tol.noise)}) "
                   "on some axis, so the file cannot place both of its faces")
        p.dropped = why
        records["not_built"].append({
            "name": name, "kind": p.kind, "path": list(p.objects[0].path),
            "material": p.material, "power_W": p.power, "why": why})
        for o in p.objects:
            placement.fate[o.order] = f"not built ({why})"
    return live


def _name_regions(placement: Placement, live: list[int], n_air: int) -> dict:
    """The fused build's names, in its order: the air first, then the parts, then any sealed
    air space - so both paths name the same model the same way."""
    namer = _unique_namer()
    first_air = namer(FLUID_NAME)
    per_heatsink: dict[str, int] = {}
    for k in live:
        p = placement.parts[k]
        if p.kind == "heatsink":
            hs = str(p.objects[0].props["heatsink"])
            per_heatsink[hs] = per_heatsink.get(hs, 0) + 1
    parts: dict[int, str] = {}
    for k in live:
        p = placement.parts[k]
        if p.kind == "heatsink":
            hs = str(p.objects[0].props["heatsink"])
            raw = hs if per_heatsink[hs] == 1 else f"{hs}_{p.material or 'part'}"
        else:
            raw = p.objects[0].name
        parts[k] = namer(raw)
        p.name = parts[k]
    air = [first_air] + [namer(f"{FLUID_NAME}_{a + 1}") for a in range(1, n_air)]
    return {"air": air[:max(n_air, 0)] if n_air else [], "parts": parts}


# ------------------------------------------------------------------------------ boundary ------
def _boundary(placement: Placement, grid: G.Grid, region_names: list[str]
              ) -> tuple[list[Patch], list[np.ndarray], list[dict]]:
    """The patches on the six domain sides: each side is its own patch, and every 2D object the
    file puts on a side (a fan, a vent, a wall plate, a surface heat source) takes the faces of its
    own rectangle, under its own name - later objects over earlier ones, as everywhere in ECXML.
    Fans and vents are openings (patch), plates and heat-flux sources walls."""
    from meshpipeline.contracts.patch_names import MAX_NAME_LENGTH, mesh_safe, unreserved

    domain = placement.domain
    objects = [r for r in placement.records["patches"] if r.get("domain_face")]
    sides = _domain_patches(domain, objects)
    taken = {n.casefold() for n in region_names}

    def unique(raw: str, fallback: str) -> str:
        base = unreserved(mesh_safe(raw, fallback=fallback))
        cand, k = base, 1
        while cand.casefold() in taken or "_to_" in cand:
            k += 1
            suffix = f"_{k}"
            cand = (base.replace("_to_", "_") if "_to_" in base else base)[
                :MAX_NAME_LENGTH - len(suffix)] + suffix
        taken.add(cand.casefold())
        return cand

    patches: list[Patch] = []
    rows: list[dict] = []
    side_base: dict[str, int] = {}
    for axis in range(3):
        for sign in "-+":
            face = f"{sign}{_AXES[axis]}"
            row = next((r for r in sides if r["domain_face"] == face), None)
            name = unique(row["name"] if row else f"domain_{_AXES[axis]}"
                          f"{'max' if sign == '+' else 'min'}", "domain")
            side_base[face] = len(patches)
            patches.append(Patch(name=name, type="patch"))
            meta = dict(row) if row else {"name": name, "object": "solutionDomain",
                                          "role": "domain_boundary", "domain_face": face,
                                          "suggested_type": "opening", "covered_side": True}
            meta["mesh_patch"] = name
            meta["mesh_patch_type"] = "patch"
            rows.append(meta)
    tol = G.plane_tolerance(placement)
    side_ids: list[np.ndarray] = []
    shape = grid.shape
    for axis, side in SIDES:
        face = f"{'-+'[side]}{_AXES[axis]}"
        arr = np.full(side_shape(shape, axis), side_base[face], dtype=np.int32)
        side_ids.append(arr)
    for obj in sorted(objects, key=lambda r: r.get("order", 0)):
        face = obj["domain_face"]
        axis = _AXES.index(face[1])
        side = 0 if face[0] == "-" else 1
        ptype = "wall" if obj.get("role") in ("wall", "heat_flux") else "patch"
        name = unique(obj["name"], "patch")
        pid = len(patches)
        patches.append(Patch(name=name, type=ptype))
        rows.append({**obj, "mesh_patch": name, "mesh_patch_type": ptype})
        rect = _rect_box(obj)
        cross = [i for i in range(3) if i != axis]
        rng: list[slice] = []
        for i in cross:
            lo = max(rect.lo[i], domain.lo[i])
            hi = min(rect.hi[i], domain.hi[i])
            if hi - lo <= tol:
                rng = []
                break
            rng.append(slice(grid.index(i, lo, tol), grid.index(i, hi, tol)))
        if len(rng) != 2:
            continue
        arr = side_ids[SIDES.index((axis, side))]
        # side arrays are (slower axis, faster axis) = (cross[1], cross[0])
        arr[rng[1], rng[0]] = pid
    return patches, side_ids, rows


def _rect_box(row: dict) -> _Box:
    c, s = row["centre_m"], row["size_m"]
    axes = [_AXES.index(a) for a in row["size_axes"]]
    lo, hi = list(c), list(c)
    for k, i in enumerate(axes):
        lo[i], hi[i] = c[i] - s[k] / 2, c[i] + s[k] / 2
    return _Box(lo, hi)


# ------------------------------------------------------------------------------ evidence ------
def _neighbours(arr: np.ndarray, axis: int) -> tuple[np.ndarray, np.ndarray]:
    """The values on the two sides of every internal face normal to `axis` (arrays are z, y, x)."""
    if axis == 0:
        return arr[:, :, :-1].ravel(), arr[:, :, 1:].ravel()
    if axis == 1:
        return arr[:, :-1, :].ravel(), arr[:, 1:, :].ravel()
    return arr[:-1, :, :].ravel(), arr[1:, :, :].ravel()


def _interfaces(cell_zone: np.ndarray, names: list[str], n_air: int) -> list[dict]:
    """Every pair of regions that share faces, with the number of faces: what splitMeshRegions
    must turn into a matching pair of mappedWall patches."""
    nz = len(names)
    counts: dict[int, int] = {}
    for axis in range(3):
        a, b = _neighbours(cell_zone, axis)
        diff = a != b
        lo = np.minimum(a[diff], b[diff]).astype(np.int64)
        hi = np.maximum(a[diff], b[diff]).astype(np.int64)
        keys, n = np.unique(lo * nz + hi, return_counts=True)
        for key, c in zip(keys.tolist(), n.tolist()):
            counts[key] = counts.get(key, 0) + c
    out = []
    for key, c in sorted(counts.items()):
        za, zb = divmod(key, nz)
        kind = ("fluid-solid" if (za < n_air) != (zb < n_air) else
                "solid-solid" if za >= n_air else "fluid-fluid")
        out.append({"a": names[za], "b": names[zb], "kind": kind, "faces": int(c)})
    return out


def _thin_layers(placement: Placement, grid: G.Grid, live: list[int], names: dict) -> list[dict]:
    """Each part's thinnest extent and the cells through it (thinnest first): how every thin layer
    - a thermal interface, a die attach, a board - is resolved. A part always has at least one
    cell through each of its boxes on each axis, because the grid holds both of its faces."""
    tol = G.plane_tolerance(placement)
    want = grid.plan.min_cells_across
    rows = []
    for k in live:
        p = placement.parts[k]
        best = None
        for b in _material(p):
            c = b.common(placement.domain)
            if c is None:
                continue
            for i in range(3):
                t = c.hi[i] - c.lo[i]
                if t <= tol:
                    continue
                n = grid.index(i, c.hi[i], tol) - grid.index(i, c.lo[i], tol)
                if best is None or (t, n) < (best[0], best[1]):
                    best = (t, n, i)
        if best is None:
            continue
        t, n, i = best
        rows.append({"part": names[k], "axis": _AXES[i], "thickness_m": t, "cells_across": int(n),
                     "wanted": want, "ok": n >= want})
    rows.sort(key=lambda r: (r["thickness_m"], r["cells_across"]))
    return rows


def _fidelity(placement: Placement, grid: G.Grid, zone: np.ndarray, part_vol: np.ndarray,
              live: list[int], names: dict, regions: list[dict], vols: np.ndarray
              ) -> tuple[list[str], set, list[dict]]:
    """The hard checks that the mesh is the file's model; a FidelityError names the first one
    that fails. Returns the report line for those that pass, the solid contacts, and the
    staircased parts with their volume error."""
    domain = placement.domain
    exact, staircase = 0, []
    by_name = {r["name"]: r for r in regions}
    for k in live:
        p = placement.parts[k]
        want = _expected_volume(p, domain)
        got = float(part_vol[k])
        label = p.objects[0].props.get("heatsink") or p.objects[0].label
        round_ = p.kind == "solidCylinder" or any(c[0] == "round" for c in p.cutters) or \
            any(q.kind == "solidCylinder" or any(c[0] == "round" for c in q.cutters)
                for q in p.winners)
        row = by_name[names[k]]
        if any(q.cutters for q in p.winners):
            want = None              # a winner with a vent in it: only the cells can say
        if want is None:
            continue
        row["file_volume_m3"] = want
        row["volume_error"] = (got - want) / want if want > 0 else 0.0
        if round_:
            staircase.append({"part": names[k], "file_volume_m3": want, "mesh_volume_m3": got,
                              "error_pct": 100.0 * (got - want) / want if want > 0 else 0.0})
            continue
        if abs(got - want) > max(VOLUME_RTOL * 1e-3, 1e-9) * want + 1e-24:
            raise FidelityError(f"{label}: the mesh holds {got * 1e9:.9g} mm3 of it, but the "
                                f"file's numbers give {want * 1e9:.9g} mm3")
        exact += 1
    total = float(vols.sum())
    if abs(total - domain.volume()) > 1e-9 * domain.volume():
        raise FidelityError(f"the cells add up to {total * 1e9:.9g} mm3, but the domain is "
                            f"{domain.volume() * 1e9:.9g} mm3")
    objs = placement.model.active_objects
    missing = [o.label for o in objs if o.kind not in ("assembly", "heatsink")
               and o.order not in placement.fate]
    if missing:
        raise FidelityError(f"the mesh lost {len(missing)} object(s) of the file: "
                            + ", ".join(missing[:8]))
    all_names = [r["name"] for r in regions]
    if len({n.casefold() for n in all_names}) != len(all_names):
        raise FidelityError("two regions came out under the same name")
    # contacts between solid parts: faces the grid shares vs the file's boxes
    P = len(placement.parts)
    pairs: set[int] = set()
    for axis in range(3):
        a, b = _neighbours(zone, axis)
        sel = (a != b) & (a >= 0) & (b >= 0)
        lo = np.minimum(a[sel], b[sel]).astype(np.int64)
        hi = np.maximum(a[sel], b[sel]).astype(np.int64)
        pairs |= set(np.unique(lo * P + hi).tolist())
    contacts = {frozenset((names[a], names[b])) for a, b in (divmod(x, P) for x in pairs)}
    live_parts = [placement.parts[k] for k in live]
    allowed, required = _file_contacts(live_parts, domain, placement.tol)
    invented = contacts - allowed
    if invented:
        pair = sorted(next(iter(invented)))
        raise FidelityError(f"the mesh makes {pair[0]} and {pair[1]} touch, but the file keeps "
                            "them apart")
    lost = required - contacts
    if lost:
        pair = sorted(next(iter(lost)))
        raise FidelityError(f"{pair[0]} and {pair[1]} touch in the file, but the mesh does not "
                            "share the faces between them")
    lines = [f"Checked: {exact} of {len(live)} solid volume(s) match the file's numbers to "
             f"round-off, {len(staircase)} are staircased (listed with their volume error), the "
             f"rest are bounded by the file; the cells fill the domain to "
             f"{abs(total - domain.volume()) / domain.volume():.1e}; all "
             f"{len([o for o in objs if o.kind not in ('assembly', 'heatsink')])} active objects "
             f"are accounted for; {len(contacts)} contact(s) between solids share their faces, "
             "none invented, none lost."]
    return lines, contacts, staircase


def _report_lines(placement: Placement, grid: G.Grid, thin: list[dict], staircase: list[dict],
                  records: dict) -> list[str]:
    st = G.stats(grid)
    lines = [f"Grid: {st['cells']:,} cells ({st['x_cells']} x {st['y_cells']} x {st['z_cells']}); "
             f"cell sizes {_um(min(st['x_min_m'], st['y_min_m'], st['z_min_m']))} to "
             f"{_um(max(st['x_max_m'], st['y_max_m'], st['z_max_m']))}; neighbouring cells "
             f"differ by at most {st['max_neighbour_ratio']:.3g}x; every cell is an orthogonal "
             "hexahedron."]
    lines += [f"Budget: {r}." for r in grid.relaxed]
    if thin:
        t0 = thin[0]
        lines.append(f"Thinnest part: {t0['part']}, {_um(t0['thickness_m'])} on {t0['axis']}, "
                     f"{t0['cells_across']} cell(s) through it.")
    short = [r for r in thin if not r["ok"]]
    if short:
        lines.append(f"Thin layers with fewer than {grid.plan.min_cells_across} cells through "
                     f"them ({len(short)}): " + ", ".join(
                         f"{r['part']} ({_um(r['thickness_m'])}, {r['cells_across']})"
                         for r in short[:8]) + ("..." if len(short) > 8 else "") + ".")
    for s in staircase:
        lines.append(f"Staircased: {s['part']} is built from cubes on the grid; its volume is "
                     f"{s['error_pct']:+.2g}% of the file's.")
    if records["inactive"]:
        names = ", ".join(r["name"] for r in records["inactive"][:8])
        more = f" and {len(records['inactive']) - 8} more" if len(records["inactive"]) > 8 else ""
        lines.append(f"Switched off in the file, so not meshed: {names}{more}.")
    for r in records["not_built"]:
        lines.append(f"Not built: {r['name']} - {r['why']}.")
    for b in records["baffles"]:
        lines.append(f"Not meshed: {b['name']}, a plate without a usable thickness "
                     f"({_um(b['thickness_m'])}): the air flows through it; its outline lies on "
                     "grid planes, so a solver can make it a baffle exactly.")
    meta = {k: len(records[k]) for k in ("fans", "grilles", "flow_resistances",
                                         "volume_heat_sources", "surface_heat_sources",
                                         "monitor_points")}
    if any(meta.values()):
        lines.append("Kept beside the mesh, on grid planes: " + ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in meta.items() if v) + ".")
    model = placement.model
    if model.ignored_elements:
        lines.append("Not read (not in the ECXML schema): " + ", ".join(
            f"{k} x{v}" for k, v in sorted(model.ignored_elements.items())[:12]) + ".")
    lines += [n for n in placement.notes if "opened where" in n]
    return lines


def _sidecar(placement: Placement, regions: list[dict], patches: list[dict],
             build_report: list[str], contacts: set, grid: G.Grid) -> dict:
    """The physics the mesh does not carry, in the fused build's sidecar shape, so whatever reads
    one reads the other: materials, regions (now with their cells), patches (now with the mesh
    patch each became), fans, vents, sources, compact models, monitor points, the report."""
    model, records, tol = placement.model, placement.records, placement.tol
    objs = model.active_objects
    live_power = sum(float(r.get("power_W") or 0.0) for r in regions if r["type"] == "solid")
    return {
        "format": "ECXML", "spec": SPEC, "sidecar_version": SIDECAR_VERSION,
        "model_name": model.name, "producer": model.producer,
        "mesher": MESHER,
        "units": {"length": "m", "temperature": "K", "pressure": "Pa", "power": "W",
                  "thermal_conductivity": "W/mK", "thermal_resistance": "K/W",
                  "specific_heat": "J/kgK", "density": "kg/m3", "flow_rate": "m3/s"},
        "geometry_unit": "m (the mesh is in the file's metres)",
        "domain": {"box_m": placement.domain.as_dict(), "source": placement.domain_source,
                   "ambient": model.domain.ambient if model.domain else None},
        "materials": {n: m.as_dict() for n, m in model.materials.items()},
        "regions": regions,
        "patches": patches,
        "fans": records["fans"], "grilles": records["grilles"],
        "flow_resistances": records["flow_resistances"],
        "volume_heat_sources": records["volume_heat_sources"],
        "surface_heat_sources": records["surface_heat_sources"],
        "baffles": records["baffles"], "compact_models": records["compact_models"],
        "monitor_points": records["monitor_points"],
        "inactive_objects": records["inactive"], "not_built": records["not_built"],
        "ignored_elements": model.ignored_elements,
        "total_power_W": sum(float(o.power or 0.0) for o in objs
                             if o.kind not in ("assembly", "heatsink")),
        "power_W": {
            "in_solid_regions": live_power,
            "in_heat_sources": sum(float(s.get("power_W") or 0.0)
                                   for k in ("volume_heat_sources", "surface_heat_sources")
                                   for s in records[k])
                               + sum(float(p.get("power_W") or 0.0) for p in records["patches"]
                                     if p.get("role") == "heat_flux"),
            "in_plates_not_meshed": sum(float(b.get("power_W") or 0.0)
                                        for b in records["baffles"])
                                    + sum(float(p.get("power_W") or 0.0)
                                          for p in records["patches"] if p.get("role") == "wall"),
            "in_parts_not_built": sum(float(r.get("power_W") or 0.0)
                                      for r in records["not_built"]),
        },
        "tolerances_m": {"file_precision": tol.noise, "boolean_fuzzy": None,
                         "smallest_gap": tol.gap if math.isfinite(tol.gap) else None,
                         "smallest_gap_between": tol.gap_where,
                         "thinnest_part": tol.thick if math.isfinite(tol.thick) else None,
                         "thinnest_part_is": tol.thick_where,
                         "coordinates_snapped": tol.snapped, "largest_snap": tol.snap_shift},
        "grid": G.stats(grid),
        "build_report": build_report,
        "solid_contacts": sorted(sorted(c) for c in contacts),
        "notes": placement.notes,
    }


def write_sidecar_beside(path: Path, sidecar: dict) -> None:
    """The sidecar under the name the fused path uses beside its canonical STEP."""
    Path(str(path) + SIDECAR_SUFFIX).write_text(json.dumps(sidecar, indent=1, default=float))


__all__ = ["MESHER", "REPORT_NAME", "SIDECAR_NAME", "EcxmlError", "SnapgridMesh", "mesh_ecxml",
           "render_region_properties", "write_case_skeleton"]
