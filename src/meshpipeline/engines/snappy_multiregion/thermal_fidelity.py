# Responsibility: Keep a thermal model's multi-region mesh true to the file - plan the refinement its thinnest layers and gaps need, refuse (with the true reason) a model this engine cannot mesh faithfully within its limits, and fail any mesh whose regions, volumes, contacts or cells across a layer are not the file's.
"""A thermal model's mesh is the file's model, or it is not delivered.

ECXML-TEST (2026-10-06) found snappy's castellated mesh losing micrometre layers without a word:
a 25 um TIM with one cell across came out +79.6% in volume, and the die under it touched the
spreader directly (a contact the file does not have); a 25 um die attach vanished; a 4-high die
stack with 1-5 um gaps ran 53 minutes and gave no mesh. Every check passed. Two halves close that,
both general to any thermal model (the ECXML sidecar, staged beside the case as
thermal_model.json), never to one file:

- `plan`, before meshing: the surface level each region needs for CELLS_ACROSS cells across its
  thinnest layer and across the air in its narrow gaps, and what that costs. Levels below the
  need are raised; a model whose need does not fit the cell budget and the run's time is refused
  with the reason, naming the snap-grid conversion (Option B) as the way on.
- `failures`, after meshing: every region of the file present, each region's volume the file's,
  no solid-solid contact the file lacks and none it has missing, CELLS_ACROSS cells across every
  solid's thinnest layer. Each failure is a fatal mesh defect, never a warning.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: A thermal model's physics (cad/ingest/ecxml_build's sidecar), staged beside the geometry.
THERMAL_MODEL = "thermal_model.json"
#: What configure_mesh planned for it (levels raised, predicted cells), for the run's record.
THERMAL_PLAN = ".thermal_plan.json"
#: A layer - a solid's thinnest side, or the air in a gap - needs at least this many cells across.
CELLS_ACROSS = 2
#: A region's meshed volume may differ from the file's by this fraction. Resolved lab meshes of
#: ECXML models come within 0.44% (a cylinder; box parts within 0.01%, 2026-10-06); the wrong
#: meshes ECXML-TEST found were +8.4% and +79.6%.
VOLUME_RTOL = 0.03
#: configure_mesh's background cell is the assembly's diagonal over this.
BACKGROUND_DIVISIONS = 40.0
#: Cells per (surface area / cell size^2) at a surface's refinement level: snappy's cells on and
#: within nCellsBetweenLevels (3) cells of a surface, and the coarser shells beyond. Fitted on four
#: lab meshes of ECXML models (0.85-2.5 M cells, 2026-10-06): 4.6 to 6.6. The low end is used, so
#: a model is refused only when even the fewest cells it could take are too many.
CELLS_PER_SURFACE = 4.5
#: Meshing time on the lab (4 cores, the same four meshes): 380-530 s per million cells; the slow
#: end, since deeper refinement is not faster.
SECONDS_PER_MILLION_CELLS = 530.0
#: The share of the run's time limit a planned mesh may use.
TIME_SHARE = 0.8
#: The way on, wherever this engine cannot keep the file's layers.
OPTION_B = ("the snap-grid conversion (Option B), which builds the mesh on the file's own grid and "
            "keeps micrometre gaps and thin layers exactly, is the way on for this model")


def _um(m: float) -> str:
    return f"{m * 1e6:.3g} um" if m * 1e6 < 999.5 else f"{m * 1e3:.4g} mm"


def _cells(n: float) -> str:
    return f"{n / 1e6:.1f} M" if n >= 1e5 else f"{n:,.0f}"


def load(ws: Path) -> dict | None:
    """The staged thermal model, or None when the case has none (or it cannot be read)."""
    f = Path(ws) / THERMAL_MODEL
    try:
        return json.loads(f.read_text()) if f.is_file() else None
    except (OSError, ValueError):
        return None


def file_regions(rmap: dict, solids: list[dict], sidecar: dict) -> dict[str, list[str]]:
    """Each declared region's regions in the file: by the name each staged solid carries (the
    STEP names every solid), else by the solid's centroid and volume."""
    rows = list(sidecar.get("regions") or [])
    names = {r["name"] for r in rows}
    by_index = {int(s["index"]): s for s in solids}
    out: dict[str, list[str]] = {}
    for region, r in rmap.items():
        got: list[str] = []
        for i in r.get("solids") or []:
            s = by_index.get(int(i)) or {}
            n = s.get("name")
            if n not in names:
                n = _by_centroid(s, rows)
            if n and n not in got:
                got.append(n)
        if not got and region in names:        # no staged solids to go by: the region's own name
            got.append(region)
        out[region] = got
    return out


def _by_centroid(solid: dict, rows: list[dict]) -> str | None:
    c, v = solid.get("centroid"), solid.get("volume")
    placed = [r for r in rows if r.get("centroid_m") and r.get("volume_m3")]
    if not c or v is None or not placed:
        return None
    best = min(placed, key=lambda r: math.dist(c, r["centroid_m"]))
    size = abs(float(v)) ** (1 / 3) or 1.0
    if (math.dist(c, best["centroid_m"]) <= 1e-3 * size
            and abs(abs(float(v)) - best["volume_m3"]) <= 1e-3 * best["volume_m3"]):
        return str(best["name"])
    return None


def level_for(thickness: float, base_cell: float) -> int:
    """The surface level whose cells put CELLS_ACROSS across `thickness`."""
    return max(0, math.ceil(math.log2(CELLS_ACROSS * base_cell / thickness) - 1e-9))


@dataclass
class Plan:
    levels: dict[str, list[int]]                    # region -> [min, max] surface level
    raised: list[str] = field(default_factory=list)  # what was raised, and why
    cells: float = 0.0                               # the fewest cells the mesh can take
    seconds: float = 0.0
    limit_cells: float = 0.0
    refusal: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def plan(sidecar: dict, mapping: dict[str, list[str]], kinds: dict[str, str], base_cell: float,
         surface_level, region_refinement: dict, *, budget_cells: float,
         timeout_s: float) -> Plan:
    """The surface level each region needs, its cost, and whether this engine can afford it."""
    rows = {r["name"]: r for r in sidecar.get("regions") or []}
    region_of = {n: d for d, ns in mapping.items() for n in ns}
    area = {d: sum(float(rows[n].get("surface_area_m2") or 0.0) for n in ns if n in rows)
            for d, ns in mapping.items()}
    current = {d: [int(v) for v in (region_refinement.get(d) or surface_level)] for d in mapping}
    need: dict[str, tuple[int, str]] = {}

    def want(d: str, lvl: int, why: str) -> None:
        if kinds.get(d) == "solid" and lvl > need.get(d, (-1, ""))[0]:
            need[d] = (lvl, why)

    def level(d: str) -> int:
        return max(current[d][0], need.get(d, (0, ""))[0])

    for d, ns in mapping.items():
        for n in ns:
            t = (rows.get(n) or {}).get("thinnest_m")
            if t:
                want(d, level_for(float(t), base_cell),
                     f"{n} is {_um(float(t))} thick ({rows[n].get('thinnest_is') or 'its thinnest side'})")
    # the air in a narrow gap is filled by the buffer cells of the finer of the two surfaces
    # facing across it, so that one must reach the gap's level - the smaller, when neither does
    for g in sidecar.get("thin_gaps") or []:
        a, b = g["between"]
        lvl = level_for(float(g["gap_m"]), base_cell)
        sides = [region_of.get(x) for x in (a, b) if x != "domain"]
        cand = [d for d in sides if d and kinds.get(d) == "solid"]
        if not cand or max(level(d) for d in cand) >= lvl:
            continue
        where = "the domain's side" if b == "domain" else b
        want(min(cand, key=lambda d: area[d]), lvl,
             f"the {_um(float(g['gap_m']))} of air between {a} and {where}")

    levels: dict[str, list[int]] = {}
    raised: list[str] = []
    for d in mapping:
        lo, hi = current[d]
        lvl = need.get(d, (0, ""))[0]
        if lvl > lo:
            raised.append(f"{d}: surface level {lo} -> {lvl}, because {need[d][1]} and "
                          f"{CELLS_ACROSS} cells across it need cells of "
                          f"{_um(base_cell / 2 ** lvl)} or less")
            lo, hi = lvl, max(hi, lvl)
        levels[d] = [lo, hi]

    box = (sidecar.get("domain") or {}).get("box_m") or {}
    size = [float(box["max"][i]) - float(box["min"][i]) for i in range(3)] if box else [0.0] * 3
    cells = math.prod(size) / base_cell ** 3
    sides_area = 2 * (size[0] * size[1] + size[1] * size[2] + size[0] * size[2])
    cells += CELLS_PER_SURFACE * sides_area * (2 ** int(surface_level[0]) / base_cell) ** 2
    per: dict[str, float] = {}
    for d in mapping:
        if kinds.get(d) == "solid":
            per[d] = CELLS_PER_SURFACE * area[d] * (2 ** levels[d][0] / base_cell) ** 2
            cells += per[d]
    seconds = cells / 1e6 * SECONDS_PER_MILLION_CELLS
    by_time = TIME_SHARE * timeout_s / SECONDS_PER_MILLION_CELLS * 1e6
    limit = min(float(budget_cells), by_time)
    out = Plan(levels=levels, raised=raised, cells=cells, seconds=seconds, limit_cells=limit)
    if cells > limit and per:
        worst = max(per, key=lambda d: per[d])
        why = need[worst][1] if worst in need else f"{worst}'s surface is large"
        cap = (f"the {_cells(budget_cells)}-cell budget" if budget_cells <= by_time else
               f"the {_cells(by_time)} cells meshable in {TIME_SHARE * timeout_s / 60:.0f} minutes")
        out.refusal = (
            f"this thermal model cannot be meshed faithfully by snappy within its limits: {why}; "
            f"{CELLS_ACROSS} cells across it need cells of {_um(base_cell / 2 ** levels[worst][0])} "
            f"(surface level {levels[worst][0]}) - about {_cells(per[worst])} cells for {worst} "
            f"alone, at least {_cells(cells)} in all (about {seconds / 60:.0f} minutes), over "
            f"{cap}. With fewer cells the layer comes out lost or thickened, or parts touch where "
            f"the file has air; {OPTION_B}")
    return out


_PATCH = re.compile(r"\n\s*([A-Za-z_][\w.-]*)\s*\n\s*\{(.*?)\}", re.S)


def _touching(ws: Path, region: str) -> dict[str, int]:
    """The regions `region` shares faces with in the split mesh (its <region>_to_<other> patches)."""
    f = Path(ws) / "constant" / region / "polyMesh" / "boundary"
    try:
        text = f.read_text(errors="replace")
    except OSError:
        return {}
    out: dict[str, int] = {}
    prefix = f"{region}_to_"
    for name, body in _PATCH.findall(text):
        m = re.search(r"nFaces\s+(\d+)", body)
        if name.startswith(prefix) and m and int(m.group(1)) > 0:
            out[name[len(prefix):]] = int(m.group(1))
    return out


def failures(ws: Path, rmap: dict, measured: dict[str, dict], solids: list[dict]) -> list[str]:
    """Every way the mesh is not the file's model (empty when it is, or there is no model)."""
    sidecar = load(ws)
    if sidecar is None:
        return []
    rows = {r["name"]: r for r in sidecar.get("regions") or []}
    mapping = file_regions(rmap, solids, sidecar)
    region_of = {n: d for d, ns in mapping.items() for n in ns}
    out: list[str] = []
    lost = sorted(set(rows) - set(region_of))
    if lost:
        out.append(f"the file's region(s) {', '.join(lost)} are in no region of the mesh")
    for d, ns in mapping.items():
        if not ns:
            continue
        m = measured.get(d)
        if m is None:
            out.append(f"{d} (the file's {', '.join(ns)}) is not in the mesh: the mesher lost it")
            continue
        file_vol = sum(float(rows[n]["volume_m3"]) for n in ns)
        vol, cells = m.get("volume_m3"), int(m.get("cells") or 0)
        if vol is None or cells <= 0:
            out.append(f"{d}: its meshed volume could not be read, so it cannot be checked "
                       "against the file")
            continue
        err = (float(vol) - file_vol) / file_vol if file_vol > 0 else 0.0
        if abs(err) > VOLUME_RTOL:
            out.append(f"{d} comes out {err:+.1%} in volume ({float(vol) * 1e9:.4g} mm3 meshed, "
                       f"{file_vol * 1e9:.4g} mm3 in the file; {VOLUME_RTOL:.0%} allowed)")
        if rmap[d].get("type") == "solid":
            thin = [(float(rows[n]["thinnest_m"]), n) for n in ns if rows[n].get("thinnest_m")]
            if thin:
                t, n = min(thin)
                h = (float(vol) / cells) ** (1 / 3)
                if t / h < CELLS_ACROSS:
                    out.append(f"{d} has about {t / h:.1f} cell(s) across its thinnest layer "
                               f"({n}, {_um(t)}; its cells are about {_um(h)}); it needs "
                               f"{CELLS_ACROSS}")
    solid_regions = {d for d, r in rmap.items() if r.get("type") == "solid"}
    want: set[tuple[str, str]] = set()
    for a, b in sidecar.get("solid_contacts") or []:
        da, db = region_of.get(a), region_of.get(b)
        if da and db and da != db:
            want.add((min(da, db), max(da, db)))
    got: dict[tuple[str, str], int] = {}
    for d in sorted(solid_regions & set(measured)):
        for other, faces in _touching(ws, d).items():
            if other in solid_regions and other != d:
                got[(min(d, other), max(d, other))] = faces
    for a, b in sorted(set(got) - want):
        out.append(f"{a} and {b} touch in the mesh ({got[(a, b)]:,} faces) but not in the file: "
                   "the air or the layer between them was lost")
    for a, b in sorted(want - set(got)):
        if a in measured and b in measured:
            out.append(f"{a} and {b} touch in the file but not in the mesh: the heat path between "
                       "them is cut")
    return out


#: Appended once to a failed thermal mesh: what to do.
WAY_ON = ("raise the surface level of the regions named (within the cell budget) - or, where "
          "that cannot fit, " + OPTION_B)
