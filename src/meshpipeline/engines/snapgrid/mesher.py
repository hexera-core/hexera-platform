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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml
from meshpipeline.cad.ingest.ecxml_build import (
    _AXES,
    FLUID_NAME,
    SIDECAR_SUFFIX,
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
from meshpipeline.cad.ingest.ecxml_place import (
    Placement,
    describe,
    place,
    placement_sidecar,
)
from meshpipeline.engines.snapgrid import blocks as B
from meshpipeline.engines.snapgrid import boxmesh
from meshpipeline.engines.snapgrid import curved as C
from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid.polymesh import SIDES, Patch

REPORT_NAME = "snapgrid_report.json"
SIDECAR_NAME = "thermal_model.json"
MESHER = "snapgrid"
REPORT_VERSION = 2
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


def _write_region_system(case: Path, region: str) -> None:
    """system/<region>/fvSchemes and fvSolution: what an OpenFOAM utility opens beside a region's
    mesh (splitMeshRegions writes the same placeholders)."""
    d = case / "system" / region
    d.mkdir(parents=True, exist_ok=True)
    for name in ("fvSchemes", "fvSolution"):
        (d / name).write_text((case / "system" / name).read_text())


def render_region_properties(fluids: list[str], solids: list[str]) -> str:
    return _foam_dict("dictionary", "regionProperties", (
        "regions\n(\n"
        f"    fluid       ({' '.join(fluids)})\n"
        f"    solid       ({' '.join(solids)})\n"
        ");\n"), location="constant")


# ------------------------------------------------------------------------------ the mesh ------
def mesh_ecxml(src, case, plan: G.GridPlan | None = None, *, binary: bool | None = None,
               dry_run: bool = False, local: bool = True, dialect: str = "esi") -> SnapgridMesh:
    """Read `src` (ECXML), mesh it on a snap grid refined locally (blocks) and write the
    single-region polyMesh with a cellZone per region, constant/regionProperties, the report and
    the physics sidecar into the OpenFOAM case `case`. `dry_run` stops after the grid is sized
    (nothing written but the report); `local=False` meshes one tensor grid over the whole domain.
    Raises EcxmlError (a file that cannot be read or placed), G.OverBudget (a model that needs
    more cells than the budget to keep its layers) or FidelityError (a mesh that would not be the
    file's model - a bug, never a model fault)."""
    plan = plan or G.GridPlan()
    case = Path(case)
    t = {"start": time.perf_counter()}
    model = read_ecxml(src)
    placement = place(model)
    t["placed"] = time.perf_counter()
    layout = B.build_layout(placement, plan, local=local)
    t["gridded"] = time.perf_counter()
    report: dict = {"mesher": MESHER, "report_version": REPORT_VERSION,
                    "input": {"file": Path(src).name, "model_name": model.name,
                              "producer": model.producer,
                              "active_objects": sum(1 for o in model.active_objects
                                                    if o.kind not in ("assembly", "heatsink"))},
                    "plan": asdict(plan), "plan_used": asdict(layout.plan),
                    "relaxed": layout.relaxed, "grid": layout_stats(layout)}
    if dry_run:
        report["dry_run"] = True
        report["timings_s"] = _timings(t)
        return SnapgridMesh(case=case, report=report, sidecar={})

    B.paint_blocks(placement, layout)
    zone = np.concatenate([_zone_of(blk).ravel() for blk in layout.blocks])
    vols = np.concatenate([_block_volumes(layout, blk).ravel() for blk in layout.blocks])
    t["painted"] = time.perf_counter()
    topo = boxmesh.build_topology(layout)
    t["topology"] = time.perf_counter()
    airs, n_air = _air_spaces(layout, topo, vols)
    P = len(placement.parts)
    part_cells = np.bincount(zone + 1, minlength=P + 1)[1:]

    notes = placement.notes
    records = placement.records
    build_report = describe(placement) + list(placement.report)
    live = _settle_parts(placement, part_cells, build_report)
    names = _name_regions(placement, live, n_air)
    part_region = {k: names["parts"][k] for k in live}
    air_names = names["air"]

    # curved parts: the staircase pulled onto the file's ellipse (boxes stay exact)
    keys = np.unique(np.concatenate([topo.face_pts, topo.b_pts]))
    points = _points_of(layout, topo, keys)
    curves = C.curves_of(placement, live, part_region)
    snapped = C.Snapped()
    if curves:
        solid_box = np.array([p.kind != "solidCylinder" for p in placement.parts], dtype=bool)
        snapped = C.snap(curves, points, keys, topo, zone, solid_box,
                         _hanging_keys(topo, points, keys))
        if snapped.topo is not None:            # risers collapsed: fewer faces, fewer points
            topo = snapped.topo
            keys = snapped.keys
        if len(snapped.keys):
            points = snapped.xyz
            ids_i = np.searchsorted(keys, topo.face_pts)
            ids_b = np.searchsorted(keys, topo.b_pts)
            a_i, c_i = C.face_geometry(points, topo.face_off, ids_i)
            a_b, c_b = C.face_geometry(points, topo.b_off, ids_b)
            vols = C.cell_volumes(topo.n_cells, topo.owner, topo.neighbour, a_i, c_i,
                                  topo.b_owner, a_b, c_b)
    curved_rows = C.report(curves, points, keys, topo, zone, part_region) if curves else []
    t["snapped"] = time.perf_counter()
    part_vol = np.bincount(zone + 1, weights=vols, minlength=P + 1)[1:]
    air_vol = np.bincount(airs + 1, weights=vols, minlength=n_air + 1)[1:]
    air_cells = np.bincount(airs + 1, minlength=n_air + 1)[1:]
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

    checks, contacts, staircase = _fidelity(placement, topo, zone, part_vol, live, part_region,
                                            regions, vols)
    staircase = _curved_rows(staircase, curved_rows, snapped)
    thin = _thin_layers(placement, layout, live, part_region)
    interfaces = _interfaces(topo, cell_zone, zone_names, n_air)
    patches, chunk_ids, patch_rows = _boundary(placement, layout, topo, zone_names)
    t["checked"] = time.perf_counter()

    case.mkdir(parents=True, exist_ok=True)
    write_case_skeleton(case)
    as_binary = binary if binary is not None else topo.n_cells > 20_000
    written = boxmesh.write(case, layout, topo, cell_zone=cell_zone, zone_names=zone_names,
                            chunk_patches=chunk_ids, patches=patches, binary=as_binary,
                            points=points)
    # every region's own mesh, written straight from the topology - no splitMeshRegions
    region_meshes = boxmesh.write_regions(case, topo, points, cell_zone=cell_zone,
                                          zone_names=zone_names, chunk_patches=chunk_ids,
                                          patches=patches, binary=as_binary,
                                          dialect=dialect)
    for rname in zone_names:
        _write_region_system(case, rname)
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

    stats = layout_stats(layout, topo)
    report["grid"] = stats
    build_report += _report_lines(placement, layout, stats, thin, staircase, records)
    build_report += checks
    sidecar = _sidecar(placement, regions, patch_rows, build_report, contacts, stats)
    (case / SIDECAR_NAME).write_text(json.dumps(sidecar, indent=1, default=float))
    report.update({
        "regions": [{k: r.get(k) for k in ("name", "type", "kind", "material", "power_W",
                                           "cells", "volume_m3", "file_volume_m3",
                                           "volume_error")}
                    for r in regions],
        "fluid_regions": fluids, "solid_regions": solids,
        "thin_layers": thin[:_THIN_ROWS], "thin_layers_short": [r for r in thin if not r["ok"]],
        "staircased": staircase,
        "interfaces": interfaces, "patches": written["patches"],
        "solid_contacts": sorted(sorted(c) for c in contacts),
        "polymesh": {k: written[k] for k in ("cells", "faces", "internal_faces", "points",
                                             "binary", "hanging_node_faces", "block_faces")},
        "region_meshes": region_meshes,
        "build_report": build_report, "notes": notes,
        "physics": physics_summary(sidecar),
    })
    report["timings_s"] = _timings(t)
    (case / REPORT_NAME).write_text(json.dumps(report, indent=1, default=float))
    return SnapgridMesh(case=case, report=report, sidecar=sidecar,
                        regions=[{"name": r["name"], "type": r["type"], "cells": r["cells"]}
                                 for r in regions])


def _points_of(layout: B.Layout, topo: boxmesh.BoxTopology, keys: np.ndarray) -> np.ndarray:
    ix, iy, iz = boxmesh._decode(keys, topo.dims)
    g = layout.global_lines
    return np.stack([g[0][ix], g[1][iy], g[2][iz]], axis=1)


def _hanging_keys(topo: boxmesh.BoxTopology, points: np.ndarray, keys: np.ndarray) -> np.ndarray:
    """The hanging nodes: points that lie on the straight edge of some face between two of its
    corners (where a finer block meets a coarser one). Moving one would bend that edge, so they
    never move; every other point - a wall point that is a corner of all its faces included -
    may."""
    out = []
    for off, flat in ((topo.face_off, topo.face_pts), (topo.b_off, topo.b_pts)):
        ids = np.searchsorted(keys, flat)
        sizes = np.diff(off)
        prev = np.arange(len(ids)) - 1
        prev[off[:-1]] = off[1:] - 1
        nxt = np.arange(len(ids)) + 1
        nxt[off[1:] - 1] = off[:-1]
        p, a, b = points[ids], points[ids[prev]], points[ids[nxt]]
        cross = np.linalg.norm(np.cross(p - a, b - p), axis=1)
        scale = np.linalg.norm(p - a, axis=1) * np.linalg.norm(b - p, axis=1)
        straight = (cross <= 1e-9 * scale) & (np.repeat(sizes, sizes) > 4)
        out.append(flat[straight])
    return np.unique(np.concatenate(out)) if out else np.zeros(0, dtype=np.int64)


def _curved_rows(staircase: list[dict], curved: list[dict], snapped: C.Snapped) -> list[dict]:
    """The curved parts, as snapped: volume, side area and contacts against the file's."""
    by = {r["part"]: r for r in curved if r["kind"] == "cylinder"}
    out = []
    for s_ in staircase:
        row = dict(s_)
        c = by.get(s_["part"])
        if c is not None:
            row["error_pct"] = c["volume_error_pct"]
            row["mesh_volume_m3"] = c["mesh_volume_m3"]
        out.append(row)
    for c in curved:
        found = next((r for r in out if r["part"] == c["part"]), None)
        row = found if found is not None else {"part": c["part"]}
        if found is None:
            out.append(row)
        row.update({k: v for k, v in c.items() if k != "part"})
        row["points_snapped"] = int(len(snapped.keys))
        row["moves_undone"] = snapped.undone
    return out


def _zone_of(blk: B.Block) -> np.ndarray:
    if blk.zone is None:
        raise FidelityError("internal: a block was never painted")
    return blk.zone


def _block_volumes(layout: B.Layout, blk: B.Block) -> np.ndarray:
    dx, dy, dz = (np.diff(layout.coords(blk, a)) for a in range(3))
    return dz[:, None, None] * dy[None, :, None] * dx[None, None, :]


def _air_spaces(layout: B.Layout, topo: boxmesh.BoxTopology, vols: np.ndarray
                ) -> tuple[np.ndarray, int]:
    """Each cell's air space (-1: not air), the spaces numbered largest first: the air of each
    block labelled by face neighbours, then joined across the faces where blocks meet."""
    from scipy import ndimage
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    comp_parts = []
    n_comp = 0
    for blk in layout.blocks:
        assert blk.zone is not None
        lab, n = ndimage.label(blk.zone == G.AIR)
        lab = lab.ravel().astype(np.int64)
        comp_parts.append(np.where(lab > 0, lab - 1 + n_comp, -1))
        n_comp += n
    comp = np.concatenate(comp_parts) if comp_parts else np.zeros(0, dtype=np.int64)
    if n_comp == 0:
        return np.full(len(comp), -1, dtype=np.int64), 0
    ca, cb = comp[topo.owner], comp[topo.neighbour]
    join = (ca >= 0) & (cb >= 0) & (ca != cb)
    graph = coo_matrix((np.ones(int(join.sum())), (ca[join], cb[join])), shape=(n_comp, n_comp))
    n_space, label = connected_components(graph, directed=False)
    space = np.where(comp >= 0, label[np.maximum(comp, 0)], -1)
    size = np.bincount(space + 1, weights=vols, minlength=n_space + 1)[1:]
    order = np.argsort(-size, kind="stable")
    remap = np.full(n_space + 1, -1, dtype=np.int64)
    remap[order + 1] = np.arange(n_space)
    return remap[space + 1], int(n_space)


def layout_stats(layout: B.Layout, topo: boxmesh.BoxTopology | None = None) -> dict:
    """Plain numbers about the grid: cells, blocks, cell sizes per axis, the largest size ratio
    between neighbouring cells inside a block, the bound on cell aspect."""
    out: dict = {"cells": layout.n_cells, "blocks": len(layout.blocks),
                 "background_cell_m": layout.H,
                 "global_lines_xyz": [len(g) for g in layout.global_lines]}
    worst = 1.0
    wmin_all, wmax_all = [], []
    for a, name in enumerate("xyz"):
        wmin, wmax, ratio = np.inf, 0.0, 1.0
        for blk in layout.blocks:
            w = np.diff(layout.coords(blk, a))
            wmin, wmax = min(wmin, float(w.min())), max(wmax, float(w.max()))
            if len(w) > 1:
                ratio = max(ratio, float(np.max(np.maximum(w[1:] / w[:-1], w[:-1] / w[1:]))))
        out[f"{name}_min_m"], out[f"{name}_max_m"] = wmin, wmax
        out[f"{name}_max_neighbour_ratio"] = ratio
        worst = max(worst, ratio)
        wmin_all.append(wmin)
        wmax_all.append(wmax)
    out["max_neighbour_ratio"] = worst
    out["max_aspect_ratio_bound"] = max(wmax_all) / min(wmin_all)
    if topo is not None:
        out["block_faces"] = topo.block_faces
        out["hanging_node_faces"] = topo.hanging_faces
    return out


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
def _boundary(placement: Placement, layout: B.Layout, topo: boxmesh.BoxTopology,
              region_names: list[str]) -> tuple[list[Patch], list[np.ndarray], list[dict]]:
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
    painted: list[tuple[dict, int, _Box]] = []
    for obj in sorted(objects, key=lambda r: r.get("order", 0)):
        ptype = "wall" if obj.get("role") in ("wall", "heat_flux") else "patch"
        name = unique(obj["name"], "patch")
        painted.append((obj, len(patches), _rect_box(obj)))
        patches.append(Patch(name=name, type=ptype))
        rows.append({**obj, "mesh_patch": name, "mesh_patch_type": ptype})
    tol = G.plane_tolerance(placement)
    chunk_ids: list[np.ndarray] = []
    for ch in topo.chunks:
        axis, side = SIDES[ch.side]
        face = f"{'-+'[side]}{_AXES[axis]}"
        arr = np.full(ch.shape, side_base[face], dtype=np.int32)
        blk = layout.blocks[ch.block]
        cross = [i for i in range(3) if i != axis]
        lines = {i: layout.coords(blk, i) for i in cross}
        for obj, pid, rect in painted:
            if obj["domain_face"] != face:
                continue
            # the rectangle must reach into this block's side on both axes before any of its
            # edges is looked up (an edge it does not reach need not be a line here)
            spans = [(max(rect.lo[i], domain.lo[i], lines[i][0]),
                      min(rect.hi[i], domain.hi[i], lines[i][-1])) for i in cross]
            if any(hi - lo <= tol for lo, hi in spans):
                continue
            rng = [slice(_line_index(lines[i], lo, tol), _line_index(lines[i], hi, tol))
                   for i, (lo, hi) in zip(cross, spans)]
            # side arrays are (slower axis, faster axis) = (cross[1], cross[0])
            arr[rng[1], rng[0]] = pid
        chunk_ids.append(arr)
    return patches, chunk_ids, rows


def _line_index(lines: np.ndarray, value: float, tol: float) -> int:
    k = int(np.searchsorted(lines, value))
    best = min((c for c in (k - 1, k) if 0 <= c < len(lines)), key=lambda c: abs(lines[c] - value))
    if abs(lines[best] - value) > tol:
        raise ValueError(f"internal: {value!r} is not a grid plane of the block")
    return best


def _rect_box(row: dict) -> _Box:
    c, s = row["centre_m"], row["size_m"]
    axes = [_AXES.index(a) for a in row["size_axes"]]
    lo, hi = list(c), list(c)
    for k, i in enumerate(axes):
        lo[i], hi[i] = c[i] - s[k] / 2, c[i] + s[k] / 2
    return _Box(lo, hi)


# ------------------------------------------------------------------------------ evidence ------
def _pairs(a: np.ndarray, b: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Unordered pairs of different values across faces, encoded lo * n + hi, with their counts."""
    diff = a != b
    lo = np.minimum(a[diff], b[diff]).astype(np.int64)
    hi = np.maximum(a[diff], b[diff]).astype(np.int64)
    return np.unique(lo * n + hi, return_counts=True)


def _interfaces(topo: boxmesh.BoxTopology, cell_zone: np.ndarray, names: list[str],
                n_air: int) -> list[dict]:
    """Every pair of regions that share faces, with the number of faces: what splitMeshRegions
    must turn into a matching pair of mappedWall patches."""
    nz = len(names)
    keys, counts = _pairs(cell_zone[topo.owner], cell_zone[topo.neighbour], nz)
    out = []
    for key, c in zip(keys.tolist(), counts.tolist()):
        za, zb = divmod(key, nz)
        kind = ("fluid-solid" if (za < n_air) != (zb < n_air) else
                "solid-solid" if za >= n_air else "fluid-fluid")
        out.append({"a": names[za], "b": names[zb], "kind": kind, "faces": int(c)})
    return out


def _thin_layers(placement: Placement, layout: B.Layout, live: list[int], names: dict
                 ) -> list[dict]:
    """Each part's thinnest extent and the cells through it (thinnest first): how every thin layer
    - a thermal interface, a die attach, a board - is resolved. A part always has at least one
    cell through each of its boxes on each axis: the blocks it reaches into hold both of its faces.
    Counted in every block the part reaches into; the fewest is reported."""
    tol = G.plane_tolerance(placement)
    want = layout.plan.min_cells_across
    rows = []
    bounds = [(np.array([layout.coords(b, a)[0] for a in range(3)]),
               np.array([layout.coords(b, a)[-1] for a in range(3)])) for b in layout.blocks]
    for k in live:
        p = placement.parts[k]
        best = None
        for box in _material(p):
            c = box.common(placement.domain)
            if c is None:
                continue
            for bi, (blo, bhi) in enumerate(bounds):
                if not all(min(c.hi[i], bhi[i]) - max(c.lo[i], blo[i]) > tol for i in range(3)):
                    continue
                blk = layout.blocks[bi]
                for i in range(3):
                    t = c.hi[i] - c.lo[i]
                    if t <= tol or c.lo[i] < blo[i] - tol or c.hi[i] > bhi[i] + tol:
                        continue                  # the part runs on into the next block
                    lines = layout.coords(blk, i)
                    n = int(np.count_nonzero((lines > c.lo[i] + tol) & (lines < c.hi[i] - tol))) + 1
                    if best is None or (t, n) < (best[0], best[1]):
                        best = (t, n, i)
        if best is None:
            continue
        t, n, i = best
        rows.append({"part": names[k], "axis": _AXES[i], "thickness_m": t, "cells_across": int(n),
                     "wanted": want, "ok": n >= want})
    rows.sort(key=lambda r: (r["thickness_m"], r["cells_across"]))
    return rows


def _fidelity(placement: Placement, topo: boxmesh.BoxTopology, zone: np.ndarray,
              part_vol: np.ndarray, live: list[int], names: dict, regions: list[dict],
              vols: np.ndarray) -> tuple[list[str], set, list[dict]]:
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
    za, zb = zone[topo.owner], zone[topo.neighbour]
    solid = (za >= 0) & (zb >= 0)
    keys, _n = _pairs(za[solid], zb[solid], P)
    pairs = set(keys.tolist())
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


def _report_lines(placement: Placement, layout: B.Layout, st: dict, thin: list[dict],
                  staircase: list[dict], records: dict) -> list[str]:
    lines = [f"Grid: {st['cells']:,} cells in {st['blocks']} block(s), refined locally; cell "
             f"sizes {_um(min(st['x_min_m'], st['y_min_m'], st['z_min_m']))} to "
             f"{_um(max(st['x_max_m'], st['y_max_m'], st['z_max_m']))}; inside a block "
             f"neighbouring cells differ by at most {st['max_neighbour_ratio']:.3g}x; every cell is "
             "an axis-aligned box"
             + (f" ({st.get('hanging_node_faces', 0):,} faces carry a hanging node where a finer "
                "block meets a coarser one)." if st.get("hanging_node_faces") else ".")]
    lines += [f"Budget: {r}." for r in layout.relaxed]
    if thin:
        t0 = thin[0]
        lines.append(f"Thinnest part: {t0['part']}, {_um(t0['thickness_m'])} on {t0['axis']}, "
                     f"{t0['cells_across']} cell(s) through it.")
    short = [r for r in thin if not r["ok"]]
    if short:
        lines.append(f"Thin layers with fewer than {layout.plan.min_cells_across} cells through "
                     f"them ({len(short)}): " + ", ".join(
                         f"{r['part']} ({_um(r['thickness_m'])}, {r['cells_across']})"
                         for r in short[:8]) + ("..." if len(short) > 8 else "") + ".")
    for s_ in staircase:
        if "side_area_error_pct" in s_:
            what = "round hole in" if s_.get("kind") == "round hole" else "cylinder"
            vol = (f"volume {s_['error_pct']:+.2g}%, " if "error_pct" in s_ else "")
            lines.append(f"Curved: {s_['part']} ({what}) snapped onto the file's surface: "
                         f"{vol}side area {s_['side_area_error_pct']:+.2g}% of the file's.")
        else:
            lines.append(f"Staircased: {s_['part']} is built from cubes on the grid; its volume "
                         f"is {s_['error_pct']:+.2g}% of the file's.")
    if records["inactive"]:
        names = ", ".join(r["name"] for r in records["inactive"][:8])
        more = f" and {len(records['inactive']) - 8} more" if len(records["inactive"]) > 8 else ""
        lines.append(f"Switched off in the file, so not meshed: {names}{more}.")
    for r in records["not_built"]:
        lines.append(f"Not built: {r['name']} - {r['why']}.")
    for bf in records["baffles"]:
        lines.append(f"Not meshed: {bf['name']}, a plate without a usable thickness "
                     f"({_um(bf['thickness_m'])}): the air flows through it; its outline lies on "
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
             build_report: list[str], contacts: set, stats: dict) -> dict:
    """The fused build's sidecar shape, with the mesh's regions, patches and grid."""
    return placement_sidecar(placement, regions=regions, patches=patches,
                             build_report=build_report, solid_contacts=contacts, mesher=MESHER,
                             geometry_unit="m (the mesh is in the file's metres)",
                             extra={"grid": stats})


def write_sidecar_beside(path: Path, sidecar: dict) -> None:
    """The sidecar under the name the fused path uses beside its canonical STEP."""
    Path(str(path) + SIDECAR_SUFFIX).write_text(json.dumps(sidecar, indent=1, default=float))


__all__ = ["MESHER", "REPORT_NAME", "SIDECAR_NAME", "EcxmlError", "SnapgridMesh", "mesh_ecxml",
           "render_region_properties", "write_case_skeleton"]
