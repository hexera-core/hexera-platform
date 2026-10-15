# Responsibility: A delivered snap-grid mesh of a thermal model as an electronics-cooling engineer reads
# it: every part by the file's name with its cells, the cells through its thinnest layer and how
# closely its volume matches the file; the quality in plain words; ready-made slices through the board
# and the hottest parts; and the per-part table and the thermal model as files to download.
# Owns: the slice planes of a thermal model (also the review's inspection slices) and the results
# block the viewer payload carries (`thermal`).
# Boundaries: reads the report and the physics sidecar the mesher wrote beside the case
# (snapgrid_report.json, thermal_model.json) and, for the slices, the delivered case itself; it
# changes nothing and never fails a mesh - a block it cannot build is simply not shown.
# Collaborates with: engines/snapgrid/runner.py (the manifest's slices), engines/snapgrid/viewer.py
# (the viewer payload), the console's viewer/thermal_results.js (shows it).
from __future__ import annotations

import base64
import csv
import io
import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

REPORT_NAME = "snapgrid_report.json"
SIDECAR_NAME = "thermal_model.json"
#: The hottest parts a slice is cut through.
HOT_PARTS = 3
#: The thermal model rides along with the viewer payload up to this size; a bigger one is in the
#: case download only.
MAX_SIDECAR_BYTES = 4_000_000
#: Slices are read from the delivered regions only up to this many regions, and shipped only up to
#: this many polygons in all: a slice is a look at the mesh, never worth a viewer that will not load.
MAX_SLICE_REGIONS = 3000
MAX_SLICE_POLYS = 400_000

_KIND_WORDS = {"solid3dBlock": "block", "printedCircuitBoard": "PCB", "enclosure": "enclosure",
               "heatsink": "heat sink", "twoResistorModel": "2R package", "solidCylinder": "cylinder",
               "solid2dBlock": "plate"}
_AXES = "xyz"


# --------------------------------------------------------------------------- reading ----
def _read(ws: Path, name: str) -> dict:
    try:
        data = json.loads((Path(ws) / name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _label(name: str) -> str:
    """A region name as the file wrote it: the mesher's underscores back to spaces."""
    return str(name).replace("_", " ")


def _box(region: dict) -> tuple[list[float], list[float]] | None:
    """The box around every object of one sidecar region, in metres (the mesh's own unit)."""
    lo, hi = [np.inf] * 3, [-np.inf] * 3
    for obj in region.get("objects") or []:
        b = (obj or {}).get("box_m") or {}
        try:
            mn, mx = [float(v) for v in b["min"]], [float(v) for v in b["max"]]
        except (KeyError, TypeError, ValueError):
            continue
        lo = [min(a, c) for a, c in zip(lo, mn)]
        hi = [max(a, c) for a, c in zip(hi, mx)]
    return (lo, hi) if all(np.isfinite(lo)) and all(np.isfinite(hi)) else None


def _contains(box: tuple[list[float], list[float]], point: list[float]) -> bool:
    lo, hi = box
    tol = 1e-9 + 1e-6 * max(h - lo_ for lo_, h in zip(lo, hi))
    return all(lo[k] - tol <= point[k] <= hi[k] + tol for k in range(3))


def power_by_region(sidecar: dict) -> dict[str, float]:
    """The heat each part carries: its own power, plus the heat sources that land in it - as the
    file's sources say where they land, or, for a source that does not say, the smallest part
    whose box holds its centre (a die's top-face source lands in the die)."""
    regions = [r for r in sidecar.get("regions") or [] if isinstance(r, dict) and r.get("type") == "solid"]
    power = {str(r["name"]): float(r.get("power_W") or 0.0) for r in regions}
    boxes = [(str(r["name"]), b) for r in regions for b in [_box(r)] if b]
    for src in list(sidecar.get("volume_heat_sources") or []) + list(sidecar.get("surface_heat_sources") or []):
        if not isinstance(src, dict):
            continue
        watts = float(src.get("power_W") or 0.0)
        landed = [x for x in src.get("lands_in") or [] if isinstance(x, dict) and x.get("region") in power]
        if landed:
            for x in landed:
                power[x["region"]] += watts * float(x.get("fraction") or 0.0)
            continue
        centre = src.get("centre_m")
        if centre is None and isinstance(src.get("box_m"), dict):
            b = src["box_m"]
            centre = [(float(a) + float(c)) / 2 for a, c in zip(b.get("min") or [], b.get("max") or [])]
        try:
            centre = [float(v) for v in centre or []]
        except (TypeError, ValueError):
            continue
        holding = [(n, b) for n, b in boxes if len(centre) == 3 and _contains(b, centre)]
        if holding:
            name, _b = min(holding, key=lambda nb: float(np.prod([h - lo for lo, h in zip(*nb[1])])))
            power[name] += watts
    return power


# ----------------------------------------------------------------------------- slices ----
def slice_planes(sidecar: dict, report: dict | None = None) -> list[dict]:
    """THE READY-MADE SLICES of a thermal model: across the board at its mid-plane, and upright
    through the centre of each of the hottest parts - the cuts an engineer makes first to see the
    cells through the board, the chips and the layers above them. Each is an inspection region
    (engines/manifest `inspection_regions`): name, kind "slice", normal, origin, clip_box, plus the
    words the console shows. [] when the file places no part."""
    regions = [r for r in sidecar.get("regions") or [] if isinstance(r, dict) and r.get("type") == "solid"]
    dom = (sidecar.get("domain") or {}).get("box_m") or {}
    try:
        dlo, dhi = [float(v) for v in dom["min"]], [float(v) for v in dom["max"]]
    except (KeyError, TypeError, ValueError):
        boxes = [b for b in (_box(r) for r in regions) if b]
        if not boxes:
            return []
        dlo = [min(b[0][k] for b in boxes) for k in range(3)]
        dhi = [max(b[1][k] for b in boxes) for k in range(3)]
    frame = [dlo[0], dhi[0], dlo[1], dhi[1], dlo[2], dhi[2]]
    planes: list[dict] = []

    def _plane(name: str, label: str, why: str, axis: int, lo: list[float], hi: list[float],
               clip: list[float]) -> None:
        mid = [(a + b) / 2 for a, b in zip(lo, hi)]
        # off the exact middle by a hair: a part meshed with an even number of cells has a grid
        # line there, and a cut along a face is no cut through cells
        mid[axis] += 1e-3 * max(hi[axis] - lo[axis], 1e-9)
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        planes.append({"name": name, "kind": "slice", "normal": normal, "origin": mid,
                       "clip_box": clip, "label": label, "why": why})

    power = power_by_region(sidecar)
    hot = sorted((r for r in regions if power.get(str(r.get("name")), 0.0) > 0.0),
                 key=lambda r: -power.get(str(r.get("name")), 0.0))[:HOT_PARTS]
    boards = [(r, b) for r in regions if r.get("kind") == "printedCircuitBoard" for b in [_box(r)] if b]
    if boards:
        r, (lo, hi) = max(boards, key=lambda rb: (rb[1][1][0] - rb[1][0][0]) * (rb[1][1][1] - rb[1][0][1]))
        _plane("board_midplane", f"Across {_label(r['name'])}, mid-plane",
               "the cells through the board and the parts on it", 2, lo, hi, frame)
    elif hot and _box(hot[0]):
        # no board (a package): across the hottest part instead, at its mid-height
        lo, hi = _box(hot[0])  # type: ignore[misc]
        _plane("hot_midplane", f"Across {_label(hot[0]['name'])}, mid-height",
               "the cells across the hottest part and everything level with it", 2, lo, hi, frame)
    elif regions:
        _plane("model_midplane", "Across the model, mid-height", "the cells across every part at "
               "mid-height", 2, [dlo[0], dlo[1], dlo[2]], [dhi[0], dhi[1], dhi[2]], frame)
    for r in hot:
        b = _box(r)
        if not b:
            continue
        lo, hi = b
        size = [h - lo_ for lo_, h in zip(lo, hi)]
        pad = max(size)                     # the part and its own size around it
        clip = [max(frame[0], lo[0] - pad), min(frame[1], hi[0] + pad),
                max(frame[2], lo[1] - pad), min(frame[3], hi[1] + pad),
                max(frame[4], lo[2] - pad), min(frame[5], hi[2] + pad)]
        # upright, across the part's longer side: the layers above and below it in one cut
        axis = 0 if size[0] <= size[1] else 1
        watts = round(power.get(str(r.get("name")), 0.0), 3)
        _plane(f"part_{r['name']}"[:80], f"Through {_label(r['name'])} ({watts:g} W)",
               "the cells through the part and the layers above and below it", axis, lo, hi, clip)
    return planes


def _pack(points: np.ndarray, polys: np.ndarray) -> dict:
    return {"points_b64": base64.b64encode(points.astype(np.float32).tobytes()).decode("ascii"),
            "polys_b64": base64.b64encode(polys.astype(np.uint32).tobytes()).decode("ascii")}


def slice_pieces(ws: Path, planes: list[dict]) -> dict[str, list[dict]]:
    """The delivered mesh cut by each plane, region by region: {plane name: [{region, cells,
    points_b64, polys_b64}]}, the cut cells as polygons in the mesh's own unit. Read from the case
    itself (every region's polyMesh), so what the user sees is the mesh they download. {} when
    the case cannot be read - the slices are then simply not offered."""
    if not planes:
        return {}
    try:
        import pyvista as pv
        import vtk
    except Exception:  # noqa: BLE001 - a worker without VTK shows no slices
        return {}
    ws = Path(ws)
    try:
        reader = vtk.vtkOpenFOAMReader()
        reader.SetFileName(str(ws / "system" / "controlDict"))
        reader.UpdateInformation()
        names = [reader.GetPatchArrayName(i) for i in range(reader.GetNumberOfPatchArrays())]
        meshes = [n for n in names if n.endswith("/internalMesh")]
        if not meshes or len(meshes) > MAX_SLICE_REGIONS:
            return {}
        reader.DisableAllPatchArrays()
        for n in meshes:
            reader.SetPatchArrayStatus(n, 1)
        reader.SetCreateCellToPoint(0)
        reader.Update()
        whole = pv.wrap(reader.GetOutput())
    except Exception:  # noqa: BLE001 - a look at the mesh, never a failure of it
        logger.exception("snapgrid results: the case could not be read for slices")
        return {}

    def _blocks(block, path=""):
        if isinstance(block, pv.MultiBlock):
            for i in range(block.n_blocks):
                yield from _blocks(block[i], f"{path}/{block.get_block_name(i)}")
        elif block is not None and block.n_cells:
            yield path, block

    grids = []
    for path, grid in _blocks(whole):
        parts = [p for p in path.split("/") if p]
        region = parts[-2] if len(parts) >= 2 and parts[-1] == "internalMesh" else (parts[0] if parts else "")
        grids.append((region, grid))
    out: dict[str, list[dict]] = {}
    budget = MAX_SLICE_POLYS
    for plane in planes:
        pieces = []
        for region, grid in grids:
            try:
                cut = grid.slice(normal=tuple(plane["normal"]), origin=tuple(plane["origin"]),
                                 generate_triangles=False)
            except Exception as exc:  # noqa: BLE001 - one region that will not cut costs only itself
                logger.info("snapgrid results: %s not cut by %s (%s)", region, plane["name"], type(exc).__name__)
                continue
            if not cut.n_cells:
                continue
            cut = cut.extract_surface() if not isinstance(cut, pv.PolyData) else cut
            faces = np.asarray(cut.faces, dtype=np.int64)
            if not len(faces) or cut.n_cells > budget:
                continue
            budget -= cut.n_cells
            pieces.append({"region": region, "cells": int(cut.n_cells),
                           **_pack(np.asarray(cut.points), faces)})
        if pieces:
            out[plane["name"]] = pieces
    return out


# ---------------------------------------------------------------------------- results ----
def _pct(err) -> float | None:
    try:
        pct = round(float(err) * 100.0, 3)
    except (TypeError, ValueError):
        return None
    return 0.0 if abs(pct) < 5e-4 else pct          # never "-0"


def _match_words(pct: float | None) -> str:
    if pct is None:
        return ""
    return "exact" if abs(pct) < 0.01 else f"{pct:+.2f}%"


def part_rows(report: dict, sidecar: dict) -> list[dict]:
    """One row per part, in the file's order: the file's name, its kind, material and power, the
    cells it got, its thinnest layer and the cells through it (and how many were asked for), how
    closely its meshed volume matches the file, and how many parts it touches."""
    thin: dict[str, dict] = {}
    for t in report.get("thin_layers") or []:
        if isinstance(t, dict) and t.get("part"):
            have = thin.get(t["part"])
            if have is None or float(t.get("thickness_m") or 0) < float(have.get("thickness_m") or 0):
                thin[t["part"]] = t
    touches: dict[str, int] = {}
    for i in report.get("interfaces") or []:
        if isinstance(i, dict) and i.get("kind") == "solid-solid":
            for k in ("a", "b"):
                touches[str(i.get(k))] = touches.get(str(i.get(k)), 0) + 1
    side = {str(r.get("name")): r for r in sidecar.get("regions") or [] if isinstance(r, dict)}
    heat = power_by_region(sidecar) if sidecar else {}
    rows = []
    for r in report.get("regions") or []:
        if not isinstance(r, dict) or r.get("type") != "solid":
            continue
        name = str(r.get("name"))
        t = thin.get(name) or {}
        pct = _pct(r.get("volume_error"))
        kind = str(r.get("kind") or (side.get(name) or {}).get("kind") or "")
        thick = t.get("thickness_m")
        rows.append({
            "name": name, "label": _label(name), "kind": _KIND_WORDS.get(kind, kind),
            "material": r.get("material") or (side.get(name) or {}).get("material") or "",
            "power_W": round(heat.get(name, float(r.get("power_W") or 0.0)), 6),
            "cells": int(r.get("cells") or 0),
            "thinnest_mm": round(float(thick) * 1000.0, 4) if thick is not None else None,
            "thinnest_axis": t.get("axis") or "",
            "cells_through": t.get("cells_across"), "cells_asked": t.get("wanted"),
            "layer_ok": t.get("ok") if t else None,
            "volume_vs_file_pct": pct, "volume_match": _match_words(pct),
            "touches": touches.get(name, 0)})
    return rows


def per_part_csv(rows: list[dict]) -> str:
    """The per-part table as CSV, one row per part, in the mesh's numbers."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["part", "kind", "material", "power_W", "cells", "thinnest_mm", "thinnest_axis",
                "cells_through_thinnest", "cells_asked", "volume_vs_file_pct", "parts_touching"])
    for r in rows:
        w.writerow([r["name"], r["kind"], r["material"], f"{r['power_W']:g}", r["cells"],
                    "" if r["thinnest_mm"] is None else f"{r['thinnest_mm']:g}", r["thinnest_axis"],
                    "" if r["cells_through"] is None else r["cells_through"],
                    "" if r["cells_asked"] is None else r["cells_asked"],
                    "" if r["volume_vs_file_pct"] is None else f"{r['volume_vs_file_pct']:g}",
                    r["touches"]])
    return buf.getvalue()


def quality_lines(report: dict, rows: list[dict]) -> list[str]:
    """The mesh's quality as an electronics-cooling engineer asks about it, in plain words:
    checked clean, the shape of the cells, the worst angle, the thinnest layer and how closely
    the round parts follow the file."""
    native = report.get("native") or {}
    whole = native.get("whole") or {}
    regions = native.get("regions") or {}
    lines: list[str] = []
    n_regions = len(report.get("regions") or [])
    bad_regions = [_label(n) for n, q in regions.items() if isinstance(q, dict) and not q.get("mesh_ok", True)]
    if native.get("ok") and not bad_regions:
        lines.append(f"Every region checked clean by OpenFOAM: {n_regions} region"
                     f"{'s' if n_regions != 1 else ''}, every part its own beside the air.")
    else:
        problems = [str(p) for p in native.get("problems") or []]
        lines.append("OpenFOAM found a problem"
                     + (f" in {', '.join(bad_regions)}" if bad_regions else "")
                     + (f": {'; '.join(problems[:3])}." if problems else "."))
    cells = int(whole.get("cells") or native.get("cells") or 0)
    hexa = int(whole.get("hexahedra") or 0)
    fatal = list(whole.get("fatal") or [])
    skew = int(whole.get("skew_faces") or native.get("skew_faces") or 0)
    angle = whole.get("max_non_ortho", native.get("max_non_ortho"))
    bad = ("no bad cells" if not fatal and not skew
           else f"{skew:,} skewed face{'s' if skew != 1 else ''}" if not fatal
           else "bad cells: " + ", ".join(str(f) for f in fatal))
    worst = ("" if angle is None else "no angled faces" if float(angle) < 0.5
             else f"worst angle {float(angle):.0f}°")
    if cells and hexa >= cells:
        lines.append("Every cell is a box aligned with the model; " + ", ".join(w for w in (worst, bad) if w) + ".")
    elif cells and hexa:
        rest = cells - hexa
        lines.append(f"{hexa:,} of {cells:,} cells ({100.0 * hexa / cells:.1f}%) are plain boxes aligned "
                     f"with the model; the other {rest:,} sit where the grid steps finer or wraps a "
                     "round part. " + "; ".join(w for w in (worst.capitalize(), bad) if w) + ".")
    elif worst or bad:
        lines.append("; ".join(w for w in (worst.capitalize(), bad) if w) + ".")
    layered = [r for r in rows if r["thinnest_mm"] is not None and r["cells_through"] is not None]
    if layered:
        thin = min(layered, key=lambda r: r["thinnest_mm"])
        asked = f" (asked for {thin['cells_asked']})" if thin["cells_asked"] else ""
        lines.append(f"Thinnest layer: {thin['label']}, {thin['thinnest_mm']:g} mm with "
                     f"{thin['cells_through']} cell{'s' if thin['cells_through'] != 1 else ''} "
                     f"through it{asked}.")
        short = [r for r in layered if r["layer_ok"] is False]
        if short:
            lines.append(f"{len(short)} part{'s' if len(short) != 1 else ''} got fewer cells through "
                         "a layer than asked: " + ", ".join(
                             f"{r['label']} ({r['cells_through']} of {r['cells_asked']})" for r in short[:6])
                         + (f", and {len(short) - 6} more" if len(short) > 6 else "") + ".")
    rounds = [s for s in report.get("staircased") or [] if isinstance(s, dict) and s.get("part")]
    if rounds:
        worst_pct = max(abs(float(s.get("error_pct") or 0.0)) for s in rounds)
        names = sorted({_label(str(s["part"])) for s in rounds})
        lines.append(f"Round parts follow the file within {worst_pct:.1f}% of their volume "
                     f"({', '.join(names[:6])}{', …' if len(names) > 6 else ''}); box parts are exact.")
    return lines


def thermal_results(ws: Path) -> dict | None:
    """The results block a thermal model's viewer payload carries, or None when the case has no
    snap-grid report beside it."""
    ws = Path(ws)
    report = _read(ws, REPORT_NAME)
    if not report.get("regions"):
        return None
    sidecar = _read(ws, SIDECAR_NAME)
    rows = part_rows(report, sidecar)
    air = [{"name": str(r.get("name")), "label": _label(str(r.get("name"))), "cells": int(r.get("cells") or 0)}
           for r in report.get("regions") or [] if isinstance(r, dict) and r.get("type") == "fluid"]
    whole = (report.get("native") or {}).get("whole") or {}
    cells = int(whole.get("cells") or (report.get("polymesh") or {}).get("cells") or
                sum(int(r.get("cells") or 0) for r in report.get("regions") or [] if isinstance(r, dict)))
    inp = report.get("input") or {}
    physics = report.get("physics") or {}
    producer = str(physics.get("producer") or inp.get("producer") or "")
    producer = {"flotherm": "Flotherm", "icepak": "Icepak"}.get(producer.lower(), producer)
    manifest = _read(ws, "mesh_manifest.json")
    planes = [p for p in manifest.get("inspection_regions") or []
              if isinstance(p, dict) and p.get("label")] or slice_planes(sidecar, report)
    pieces = slice_pieces(ws, planes)
    slices = [{"name": p["name"], "label": p["label"], "why": p.get("why", ""),
               "normal": p["normal"], "origin": p["origin"], "clip_box": p.get("clip_box"),
               "pieces": pieces[p["name"]]}
              for p in planes if p["name"] in pieces]
    sidecar_text = json.dumps(sidecar, indent=1, default=str) if sidecar else ""
    # the file's total, heat sources outside the parts included - as the thermal model panel said it
    total = sidecar.get("total_power_W")
    try:
        power = float(total) if total is not None else sum(r["power_W"] for r in rows)
    except (TypeError, ValueError):
        power = sum(r["power_W"] for r in rows)
    return {
        "title": str(sidecar.get("model_name") or inp.get("model_name") or ""),
        "producer": producer,
        "summary": (f"{len(rows)} part{'s' if len(rows) != 1 else ''} + air · {cells:,} cells · "
                    f"{round(power, 3):g} W"),
        "cells": cells,
        "quality": quality_lines(report, rows),
        "parts": rows,
        "air": air,
        "slices": slices,
        "files": {
            "per_part_csv": per_part_csv(rows),
            # the file's physics, kept whole: what a solver set-up reads beside the mesh
            "thermal_model_json": sidecar_text if len(sidecar_text) <= MAX_SIDECAR_BYTES else "",
        },
    }


__all__ = ["HOT_PARTS", "part_rows", "power_by_region", "per_part_csv", "quality_lines", "slice_pieces", "slice_planes",
           "thermal_results"]
