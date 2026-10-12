# Responsibility: Build the "thermal model" view of an ECXML upload - what an electronics-cooling
# engineer checks before meshing: the model and who wrote it, the parts as the file groups them
# (material, power, size, thinnest layer), the totals, the room air, the air box and its sides, the
# fans, vents, heat sources, compact models and probes with the file's own numbers, plain warnings
# only when something is really wrong, and the three detail levels with their expected cell counts.
# Owns: the `thermal` block the geometry check serves for a format whose check_mode is "thermal".
# Boundaries: pure. It reads the physics sidecar cad/ingest wrote and the placement the snap-grid
# mesher meshes; it never changes a number of the file (display conversions only: mm, degC, L/s)
# and never asks about openings - the air box's sides are the file's, described in words.
# Collaborates with: application/geometry_check.py (the scout attaches it), cad/ingest/ecxml_place.py
# (the placement), engines/snapgrid (the plans and the layout the estimate runs).
from __future__ import annotations

import logging
import math
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: The detail levels the user picks from, in the snap-grid mesher's fidelity words.
LEVELS: tuple[tuple[str, str], ...] = (("draft", "Quick look"), ("standard", "Standard"), ("max", "Fine"))
DEFAULT_LEVEL = "standard"
#: How long the check may spend sizing the grid for the three levels together. A level not reached
#: in time shows its limits only, and is sized when the mesh runs.
DETAIL_TIME_BUDGET_S = 30.0
#: Rough run time of a snap-grid mesh, from the ECXML-hard sweep (2026-10-11): about 30 s to start,
#: 40 s per million cells, and ~0.75 s per round part per million cells for snapping round parts.
_RUN_BASE_S, _RUN_S_PER_MCELL, _ROUND_S_PER_PART_MCELL = 30.0, 40.0, 0.75
#: Parts listed in the tree; a bigger model keeps its count in the totals and says how many show.
MAX_TREE_ROWS = 5000
#: Names listed in one warning before the rest are counted.
_NAMES_SHOWN = 6

_KIND_WORDS = {"solid3dBlock": "block", "printedCircuitBoard": "PCB", "enclosure": "enclosure",
               "heatsink": "heat sink", "twoResistorModel": "2R package", "solidCylinder": "cylinder",
               "solid2dBlock": "plate"}
_PRODUCERS = {"flotherm": "Flotherm", "icepak": "Icepak"}
_FACES = ("-x", "+x", "-y", "+y", "-z", "+z")
_FACE_WORDS = {"-x": "−x", "+x": "+x", "-y": "−y", "+y": "+y", "-z": "−z", "+z": "+z"}


# ------------------------------------------------------------------------------ helpers ----
def _mm(m) -> float | None:
    try:
        v = float(m) * 1000.0
    except (TypeError, ValueError):
        return None
    return _round(v)


def _round(v: float, sig: int = 4) -> float:
    if v == 0 or not math.isfinite(v):
        return 0.0 if v == 0 else v
    return round(v, max(0, sig - 1 - int(math.floor(math.log10(abs(v))))))


def _size_mm(box: dict | None) -> list[float] | None:
    if not box:
        return None
    try:
        return [_round((float(box["max"][i]) - float(box["min"][i])) * 1000.0) for i in range(3)]
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _dims(size_mm) -> str:
    return " × ".join(_num(v) for v in size_mm) + " mm" if size_mm else ""


def _num(v) -> str:
    """A number as an engineer writes it: no trailing zeros, at most 4 significant digits."""
    try:
        f = _round(float(v))
    except (TypeError, ValueError):
        return str(v)
    return f"{f:g}" if abs(f) < 1e6 else f"{f:,.0f}"


def _watts(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0 W"
    return f"{_num(round(f, 3))} W"


def _celsius(kelvin) -> float | None:
    try:
        return round(float(kelvin) - 273.15, 2)
    except (TypeError, ValueError):
        return None


def _names(names, total: int | None = None) -> str:
    """The first few names and how many more there are (of total, when the list is cut short)."""
    names = [str(n) for n in names]
    shown = ", ".join(names[:_NAMES_SHOWN])
    more = max(len(names), total or 0) - _NAMES_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def _producer(raw) -> str:
    text = str(raw or "").strip()
    return _PRODUCERS.get(text.lower(), text)


def _label(row: dict) -> str:
    """A sidecar row's name with the assemblies it sits in, as the file nests it."""
    return "/".join([*(str(p) for p in row.get("path") or []), str(row.get("name") or "")])


# --------------------------------------------------------------------------- the domain ----
def _domain_block(sidecar: dict) -> dict:
    """The air box: its size, whether the file sets it, and each side as the file leaves it -
    open to the room, walled, or carrying a fan or a vent. Words only: no side is given a role."""
    from meshpipeline.application.geometry_check import _open_rects, _side_area

    dom = sidecar.get("domain") or {}
    box = dom.get("box_m") or {}
    size = _size_mm(box)
    from_file = dom.get("source") == "solutionDomain"
    patches = [p for p in sidecar.get("patches") or [] if p.get("domain_face")]
    sides = []
    for n, face in enumerate(_FACES):
        on = [p for p in patches if p["domain_face"] == face]
        side = next((p for p in on if p.get("role") == "domain_boundary"), None)
        fans = [p for p in on if p.get("role") == "fan"]
        vents = [p for p in on if p.get("role") == "vent"]
        plates = [p for p in on if p.get("role") in ("wall", "heat_flux")]
        devices = [str(p.get("name")) for p in fans + vents]
        if side is None:
            # one object covers the whole side: it is that object
            state = "fan" if fans else "vent" if vents else "walled"
        elif plates:
            walls = sum(float(p.get("area_m2") or 0.0) for p in plates if p.get("role") == "wall")
            area = _side_area(box, n // 2)             # _FACES runs -x, +x, -y, +y, -z, +z
            try:
                left = [] if area > 0 and walls >= 0.99 * area else _open_rects(side, plates)
            except Exception:  # noqa: BLE001 - a side we cannot read is described, never failed
                left = [side]
            state = ("fan" if fans else "vent" if vents else "partly open") if left else "walled"
        else:
            state = "fan" if fans else "vent" if vents else "open"
        sides.append({"face": face, "name": str((side or {}).get("name") or f"side {face}"),
                      "state": state, "devices": devices,
                      "plates": [str(p.get("name")) for p in plates]})
    return {"size_mm": size, "source": "solutionDomain" if from_file else "derived",
            "sides": sides, "summary": _domain_summary(size, from_file, sides)}


def _domain_summary(size, from_file: bool, sides: list[dict]) -> str:
    dims = _dims(size)
    if not from_file:
        return (f"{dims}: the box around the file's objects. The file sets no air box, so there is "
                "no room air around the model - only the spaces between its parts.")
    if all(s["state"] == "open" for s in sides):
        return f"{dims}, open to the room on all six sides."
    said = []
    for s in sides:
        if s["state"] == "open":
            continue
        names = ", ".join(s["devices"] + s["plates"])
        what = {"walled": "walled", "partly open": "partly closed",
                "fan": "a fan", "vent": "a vent"}[s["state"]]
        if s["state"] == "partly open":
            what = f"partly closed by {names}"
        elif names:
            what = f"{what} ({names})"
        said.append(f"{_FACE_WORDS[s['face']]}: {what}")
    opened = [s for s in sides if s["state"] == "open"]
    rest = "; others open to the room" if opened else ""
    return f"{dims}. " + "; ".join(said) + rest + "."


# ---------------------------------------------------------------------------- the parts ----
def _part_name(p) -> str:
    if p.kind == "heatsink":
        return str(p.objects[0].props.get("heatsink") or p.objects[0].name)
    return str(p.objects[0].name)


def _part_path(p) -> list[str]:
    path = [str(x) for x in p.objects[0].path]
    if p.kind == "heatsink" and path:
        path = path[:-1]                   # the fins' path ends in the heat sink itself
    return path


def _thinnest_m(p, domain) -> float | None:
    from meshpipeline.cad.ingest.ecxml_build import _material

    best = None
    for b in _material(p):
        c = b.common(domain) or b
        t = min(c.hi[i] - c.lo[i] for i in range(3))
        if t > 0 and (best is None or t < best):
            best = t
    return best


def _part_detail(p) -> str:
    o = p.objects[0]
    if p.kind == "enclosure" and o.props.get("wall_thickness_m"):
        return f"{_num(float(o.props['wall_thickness_m']) * 1000)} mm walls"
    if p.kind == "heatsink":
        n = len(p.objects)
        return f"{n} block{'s' if n != 1 else ''}"
    if p.kind == "twoResistorModel":
        return (f"θJC {_num(o.props.get('theta_jc_K_W'))} K/W, "
                f"θJB {_num(o.props.get('theta_jb_K_W'))} K/W")
    if p.kind == "printedCircuitBoard" and o.plane:
        return f"in the {o.plane[1:].upper()} plane"
    return ""


def _tree(placement) -> list[dict]:
    from meshpipeline.cad.ingest.ecxml_place import PART_COLOURS

    rows = []
    for k, p in enumerate(placement.parts):
        thin = _thinnest_m(p, placement.domain)
        row = {"name": _part_name(p), "path": _part_path(p), "kind": _KIND_WORDS.get(p.kind, p.kind),
               "detail": _part_detail(p), "material": p.material, "power_W": float(p.power or 0.0),
               "size_mm": [_round((p.box.hi[i] - p.box.lo[i]) * 1000.0) for i in range(3)],
               "thinnest_mm": _mm(thin) if thin else None,
               "color": list(PART_COLOURS[k % len(PART_COLOURS)]),
               "region": None if p.dropped else placement.names[k]}
        if p.dropped:
            row["left_out"] = str(p.dropped)
        elif p.clipped:
            row["clipped"] = True
        rows.append(row)
    return rows


def _tree_from_sidecar(sidecar: dict) -> list[dict]:
    """The parts as the sidecar's solid regions say them, for a caller without the placement."""
    rows = []
    for r in sidecar.get("regions") or []:
        if r.get("type") != "solid":
            continue
        objs = r.get("objects") or [{}]
        rows.append({"name": str(objs[0].get("name") or r.get("name")),
                     "path": [str(x) for x in objs[0].get("path") or []],
                     "kind": _KIND_WORDS.get(str(r.get("kind")), str(r.get("kind"))), "detail": "",
                     "material": r.get("material"), "power_W": float(r.get("power_W") or 0.0),
                     "size_mm": _size_mm(r.get("box_m")), "thinnest_mm": None, "color": None,
                     "region": r.get("name")})
    return rows


# ------------------------------------------------------------------------- the materials ----
def _conductivity(k: dict):
    kind = (k or {}).get("kind")
    if kind == "isotropic":
        return k.get("W_mK")
    if kind == "orthotropic":
        return list(k.get("W_mK") or [])
    return "varies with T"


def _materials(sidecar: dict, tree: list[dict]) -> list[dict]:
    used: dict[str, int] = {}
    for row in tree:
        if row.get("material") and not row.get("left_out"):
            used[str(row["material"])] = used.get(str(row["material"]), 0) + 1
    out = []
    for name, m in (sidecar.get("materials") or {}).items():
        out.append({"name": name, "k_W_mK": _conductivity(m.get("thermal_conductivity") or {}),
                    "density_kg_m3": m.get("density_kg_m3"), "cp_J_kgK": m.get("specific_heat_J_kgK"),
                    "emissivity": m.get("surface_emissivity"), "used_by": used.get(name, 0)})
    return out


# ------------------------------------------------------------ fans, vents and the rest ----
def _flow(flow: dict) -> dict:
    flow = flow or {}
    if flow.get("kind") == "fixed_flow_rate":
        q = float(flow.get("m3_s") or 0.0)
        return {"kind": "fixed", "m3_s": q, "L_s": round(q * 1000.0, 4), "cfm": round(q * 2118.88, 3),
                "text": f"fixed flow {_num(q * 1000.0)} L/s ({_num(q * 2118.88)} CFM)"}
    pts = [[float(q), float(p)] for q, p in flow.get("points_m3_s_Pa") or []]
    if not pts:
        return {"kind": "unknown", "text": "no flow given"}
    qmax = max(q for q, _ in pts) * 1000.0
    pmax = max(p for _, p in pts)
    return {"kind": "curve", "points_m3_s_Pa": pts,
            "text": f"fan curve ({len(pts)} points, up to {_num(qmax)} L/s and {_num(pmax)} Pa)"}


def _where(row: dict) -> str:
    face = row.get("domain_face")
    return f"on the {_FACE_WORDS.get(face, face)} side of the air box" if face else "inside the air box"


def _fans(sidecar: dict) -> list[dict]:
    rows = list(sidecar.get("fans") or [])
    rows += [p for p in sidecar.get("patches") or [] if p.get("role") == "fan"]
    out = []
    for r in rows:
        axial = r.get("kind") == "axial_3d"
        flow = _flow(r.get("flow") or {})
        if axial:
            dia = [_mm(v) for v in r.get("outer_diameter_m") or []]
            size = (f"{_num(dia[0])} mm" if len(dia) == 2 and dia[0] == dia[1]
                    else " × ".join(_num(v) for v in dia) + " mm")
            hub = _mm(r.get("hub_diameter_m"))
            words = f"axial, {size}" + (f", hub {_num(hub)} mm" if hub else "")
            item: dict = {"name": str(r.get("name")), "path": list(r.get("path") or []), "kind": "axial",
                    "size_mm": dia, "hub_mm": hub}
        else:
            size_mm = [_mm(v) for v in r.get("size_m") or []]
            words = f"rectangular, {_dims(size_mm)}"
            item = {"name": str(r.get("name")), "path": list(r.get("path") or []), "kind": "rectangular",
                    "size_mm": size_mm}
        direction = str(r.get("flow_direction") or r.get("axis") or "")
        item.update(blows=direction, where=_where(r), flow=flow,
                    text=f"{r.get('name')}: {words}, blows {direction or 'as the file says'}, "
                         f"{flow['text']}, {_where(r)}")
        out.append(item)
    return out


def _vents(sidecar: dict) -> list[dict]:
    rows = list(sidecar.get("grilles") or [])
    rows += [p for p in sidecar.get("patches") or [] if p.get("role") == "vent"]
    out = []
    for r in rows:
        size_mm = [_mm(v) for v in r.get("size_m") or []]
        free = r.get("free_area_ratio")
        open_pct = round(float(free) * 100.0, 1) if free is not None else None
        loss = r.get("loss_coefficient")
        bits = [_dims(size_mm)]
        if open_pct is not None:
            bits.append(f"{_num(open_pct)}% open")
        if loss is not None:
            bits.append(f"loss coefficient {_num(loss)}")
        out.append({"name": str(r.get("name")), "path": list(r.get("path") or []), "size_mm": size_mm,
                    "open_pct": open_pct, "loss_coefficient": loss, "where": _where(r),
                    "text": f"{r.get('name')}: " + ", ".join(b for b in bits if b) + f", {_where(r)}"})
    return out


def _resistances(sidecar: dict) -> list[dict]:
    out = []
    for r in sidecar.get("flow_resistances") or []:
        out.append({"name": str(r.get("name")), "path": list(r.get("path") or []),
                    "size_mm": _size_mm(r.get("box_in_domain_m") or r.get("box_m")),
                    "loss_coefficient_xyz": r.get("loss_coefficient_xyz"),
                    "open_pct_xyz": [round(float(v) * 100.0, 1) for v in r.get("free_area_ratio_xyz") or []]})
    return out


def _sources(sidecar: dict) -> list[dict]:
    out = []
    for kind, key in (("volume", "volume_heat_sources"), ("surface", "surface_heat_sources")):
        for r in sidecar.get(key) or []:
            item = {"name": str(r.get("name")), "path": list(r.get("path") or []), "kind": kind,
                    "power_W": float(r.get("power_W") or 0.0)}
            if "lands_in" in r:
                item["lands_in"] = r["lands_in"]
            out.append(item)
    for p in sidecar.get("patches") or []:
        if p.get("role") == "heat_flux":
            out.append({"name": str(p.get("name")), "path": list(p.get("path") or []), "kind": "surface",
                        "power_W": float(p.get("power_W") or 0.0), "where": _where(p)})
    return out


def _compact_models(sidecar: dict) -> list[dict]:
    return [{"name": str(r.get("name")), "path": list(r.get("path") or []),
             "theta_jc_K_W": r.get("theta_jc_K_W"), "theta_jb_K_W": r.get("theta_jb_K_W"),
             "power_W": float(r.get("power_W") or 0.0)}
            for r in sidecar.get("compact_models") or []]


def _probes(sidecar: dict) -> list[dict]:
    return [{"name": str(r.get("name")), "path": list(r.get("path") or []),
             "at_mm": [_mm(v) for v in r.get("location_m") or []]}
            for r in sidecar.get("monitor_points") or []]


# ------------------------------------------------------------------------------ totals ----
def _totals(sidecar: dict, placement, tree: list[dict]) -> dict:
    power = sidecar.get("power_W") or {}
    total = float(sidecar.get("total_power_W") or 0.0)
    in_mesh = float(power.get("in_solid_regions") or 0.0) + float(power.get("in_heat_sources") or 0.0)
    dropped = sum(float(p.power or 0.0) for p in placement.parts if p.dropped) if placement is not None else 0.0
    left_out = (float(power.get("in_parts_not_built") or 0.0)
                + float(power.get("in_plates_not_meshed") or 0.0) + dropped)
    live = [r for r in tree if not r.get("left_out")]
    return {"power_W": round(total, 6), "power_in_mesh_W": round(in_mesh, 6),
            "power_left_out_W": round(left_out, 6), "parts": len(live),
            "materials": len({r["material"] for r in live if r.get("material")}),
            # how many separate air spaces the parts leave (a sealed case keeps its own) is
            # counted when the grid is painted, at the mesh
            "air_spaces": None}


def _ambient(sidecar: dict) -> dict | None:
    amb = (sidecar.get("domain") or {}).get("ambient")
    if not amb:
        return None
    return {"temperature_C": _celsius(amb.get("temperature_K")),
            "radiant_C": _celsius(amb.get("radiant_temperature_K")),
            "pressure_Pa": amb.get("pressure_Pa")}


# ----------------------------------------------------------------------------- warnings ----
def _warn(level: str, text: str, parts=()) -> dict:
    return {"level": level, "text": text, "parts": [str(p) for p in parts]}


def _warnings(sidecar: dict, placement, tree: list[dict], detail: dict | None) -> list[dict]:
    """Plain sentences, each from a structured field - never from the build report's prose."""
    out: list[dict] = []
    left = [r for r in tree if r.get("left_out")]
    outside = [r for r in left if "outside the solution domain" in str(r["left_out"])]
    if outside:
        w = sum(r["power_W"] for r in outside)
        n = len(outside)
        out.append(_warn("warn", f"{n} part{'s are' if n != 1 else ' is'} outside the air box, so "
                                 f"{'they are' if n != 1 else 'it is'} left out"
                                 + (f", with {_watts(w)}" if w else "") + f": {_names(r['name'] for r in outside)}.",
                         [r["name"] for r in outside]))
    flat = [r for r in left if r not in outside]
    if flat:
        from meshpipeline.cad.ingest.ecxml_build import _um

        tol = (sidecar.get("tolerances_m") or {}).get("file_precision")
        prec = f" ({_um(float(tol))})" if tol else ""
        n = len(flat)
        out.append(_warn("warn", f"{n} part{'s are' if n != 1 else ' is'} thinner than the file's own "
                                 f"precision{prec} on some side, so the file cannot place both faces and "
                                 f"{'they are' if n != 1 else 'it is'} left out: {_names(r['name'] for r in flat)}.",
                         [r["name"] for r in flat]))
    not_built = sidecar.get("not_built") or []
    mcad = [r for r in not_built if r.get("kind") == "externalMcadFile"]
    if mcad:
        out.append(_warn("warn", "The file refers to CAD that is not in the upload, so it is left out: "
                                 + _names(f"{r.get('name')} ({r.get('file_name')})" for r in mcad) + ".",
                         [r.get("name") for r in mcad]))
    away = [r for r in not_built if r.get("kind") != "externalMcadFile"]
    if away:
        w = sum(float(r.get("power_W") or 0.0) for r in away)
        out.append(_warn("warn", f"{len(away)} {'objects lie' if len(away) != 1 else 'object lies'} "
                                 "outside the air box, so the mesh does not carry "
                                 + ("them" if len(away) != 1 else "it")
                                 + (f" ({_watts(w)} left out)" if w else "") + ": "
                                 + _names(f"{_label(r)} ({r.get('kind')})" for r in away) + ".",
                         [r.get("name") for r in away]))
    baffles = sidecar.get("baffles") or []
    if baffles:
        w = sum(float(b.get("power_W") or 0.0) for b in baffles)
        out.append(_warn("warn", f"{len(baffles)} plate{'s have' if len(baffles) != 1 else ' has'} no "
                                 "thickness, so the air passes through and the mesh does not carry "
                                 + ("them" if len(baffles) != 1 else "it")
                                 + (f" ({_watts(w)} left out)" if w else "") + f": {_names(_label(b) for b in baffles)}.",
                         [b.get("name") for b in baffles]))
    inactive = sidecar.get("inactive_objects") or []
    if inactive:
        out.append(_warn("info", f"{len(inactive)} object{'s are' if len(inactive) != 1 else ' is'} switched "
                                 f"off in the file and left out: {_names(_label(r) for r in inactive)}.",
                         [r.get("name") for r in inactive]))
    clipped = [r for r in tree if r.get("clipped")]
    sticking = [r for k in ("fans", "volume_heat_sources", "flow_resistances", "grilles")
                for r in sidecar.get(k) or [] if r.get("partly_outside_domain")]
    if clipped or sticking:
        names = [r["name"] for r in clipped] + [str(r.get("name")) for r in sticking]
        out.append(_warn("info", "Sticks out of the air box, so only the part inside is meshed: "
                                 f"{_names(names)}.", names))
    defined = set(sidecar.get("materials") or {})
    for r in tree:
        if r.get("left_out"):
            continue
        if r["power_W"] and not r.get("material") and r["kind"] != _KIND_WORDS["twoResistorModel"]:
            out.append(_warn("warn", f"{r['name']} gives off {_watts(r['power_W'])} but has no material.",
                             [r["name"]]))
        elif r.get("material") and defined and r["material"] not in defined:
            out.append(_warn("warn", f"{r['name']} is made of {r['material']}, which the file does not "
                                     "define.", [r["name"]]))
    negative = [r for r in tree if r["power_W"] < 0] + [
        {"name": s.get("name"), "power_W": float(s.get("power_W") or 0.0)}
        for k in ("volume_heat_sources", "surface_heat_sources") for s in sidecar.get(k) or []
        if float(s.get("power_W") or 0.0) < 0]
    if negative:
        out.append(_warn("warn", "Negative power (heat taken out) on "
                                 + _names(f"{r['name']} ({_watts(r['power_W'])})" for r in negative)
                                 + ". Check that this is meant.", [r["name"] for r in negative]))
    total = float(sidecar.get("total_power_W") or 0.0)
    if total <= 0:
        out.append(_warn("warn", f"The model's total power is {_watts(total)}: "
                                 + ("nothing in it gives off heat." if total == 0 else "it takes out more heat than it puts in.")))
    for s in _sources(sidecar):
        lands = s.get("lands_in")
        if lands is None:
            continue
        if not lands:
            out.append(_warn("warn", f"The heat source {s['name']} lands in no cell.", [s["name"]]))
        elif len(lands) > 1:
            out.append(_warn("info", f"The heat source {s['name']} spreads over "
                                     + ", ".join(str(x.get("region")) for x in lands[:4]) + ".", [s["name"]]))
    if any(f["kind"] == "axial" for f in _fans(sidecar)) and _producer(sidecar.get("producer")) != "Flotherm":
        out.append(_warn("info", "Fan hub sizes are shown as the file writes them: the ECXML standard "
                                 "calls the number a diameter, Icepak's export calls it a radius."))
    std = (detail or {}).get(DEFAULT_LEVEL) or {}
    short = std.get("thin_layers_short") or []
    if short:
        want = std.get("min_cells_across")
        n = int(std.get("thin_layers_short_count") or len(short))
        out.append(_warn("warn", f"At Standard, {n} thin layer{'s get' if n != 1 else ' gets'} "
                                 f"fewer than {want} cells through {'them' if n != 1 else 'it'}: "
                                 + _names((f"{r['part']} ({_num(r['thickness_mm'])} mm, {r['cells_across']} "
                                          f"cell{'s' if r['cells_across'] != 1 else ''})" for r in short), n)
                                 + ". Fine gives more.", [r["part"] for r in short]))
    return out


# ------------------------------------------------------------------------- detail levels ----
def estimate_seconds(cells: int, round_parts: int) -> float:
    """A rough run time for a snap-grid mesh of `cells`, from the sweep's measured ladder."""
    m = cells / 1e6
    return round(_RUN_BASE_S + _RUN_S_PER_MCELL * m + _ROUND_S_PER_PART_MCELL * round_parts * m, -1)


#: Rows of thin layers a level lists as short; the count says how many there are in all.
_SHORT_ROWS = 50
#: Said for a level the check did not size: its limits stand, and the run sizes it.
SIZED_AT_RUN = "sized when the mesh runs"


def _level_row(level: str, label: str) -> dict:
    from meshpipeline.engines.snapgrid.runner import plan_for

    plan = plan_for(level) or {}
    return {"label": label, "max_cells": int(plan.get("max_cells") or 0),
            "min_cells_across": int(plan.get("min_cells_across") or 1),
            "cylinder_cells": int(plan.get("cylinder_cells") or 12),
            "est_cells": None, "est_seconds": None, "thin_layers_short": [], "relaxed": [],
            "estimate": SIZED_AT_RUN}


def _sized_levels(placement, *, budget_s: float, clock=time.monotonic):
    """(level, row) for each detail level in turn, each sized as the mesher sizes it: its limits
    and, once the grid is laid out (lines and blocks only - nothing is painted or written), the
    cells it holds, a run time, and the thin layers that get fewer cells than the level asks for.
    A level not started within `budget_s` of the first keeps its limits only."""
    from meshpipeline.engines.snapgrid import blocks as B
    from meshpipeline.engines.snapgrid import grid as G
    from meshpipeline.engines.snapgrid.mesher import _thin_layers

    live = [k for k, p in enumerate(placement.parts) if not p.dropped]
    names = {k: placement.names[k] for k in live}
    round_parts = sum(1 for k in live if placement.parts[k].kind == "solidCylinder")
    started = clock()
    # the default level first: it is the one the user is offered, and the one the warnings read
    for level, label in sorted(LEVELS, key=lambda lv: lv[0] != DEFAULT_LEVEL):
        row = _level_row(level, label)
        if clock() - started > budget_s:
            yield level, row
            continue
        plan = G.GridPlan(max_cells=row["max_cells"], min_cells_across=row["min_cells_across"],
                          cylinder_cells=row["cylinder_cells"])
        try:
            layout = B.build_layout(placement, plan)
        except G.OverBudget as exc:
            row.update(fits=False, needs_cells=int(exc.needed),
                       estimate=(f"needs at least {exc.needed:,} cells to keep one cell through every "
                                 f"part, more than {label} allows"))
            yield level, row
            continue
        cells = int(layout.n_cells)
        thin = _thin_layers(placement, layout, live, names, row["min_cells_across"])
        short = [r for r in thin if not r["ok"]]
        row.update(fits=True, est_cells=cells, est_seconds=estimate_seconds(cells, round_parts),
                   relaxed=list(layout.relaxed), estimate="",
                   thin_layers_short=[{"part": r["part"], "thickness_mm": _mm(r["thickness_m"]),
                                       "cells_across": int(r["cells_across"])} for r in short[:_SHORT_ROWS]],
                   thin_layers_short_count=len(short),
                   thinnest={"part": thin[0]["part"], "thickness_mm": _mm(thin[0]["thickness_m"]),
                             "cells_across": int(thin[0]["cells_across"])} if thin else None)
        yield level, row


def detail_levels(placement, *, budget_s: float = DETAIL_TIME_BUDGET_S, clock=time.monotonic) -> dict:
    """The three detail levels, sized in this process (_sized_levels). The check itself sizes them
    in a child it can stop (detail_levels_bounded): laying out a 2,000-part board takes minutes."""
    out: dict = {"default": DEFAULT_LEVEL}
    for level, row in _sized_levels(placement, budget_s=budget_s, clock=clock):
        out[level] = row
    return out


def limits_only() -> dict:
    """The three detail levels with their limits alone, each sized when the mesh runs."""
    out: dict = {"default": DEFAULT_LEVEL}
    for level, label in LEVELS:
        out[level] = _level_row(level, label)
    return out


#: Seconds the child gets beyond its budget to start: the interpreter, numpy and the placement.
_CHILD_START_S = 20.0


def _pythonpath() -> str:
    """The import path the child needs: where this meshpipeline is, then this process's own."""
    import os
    import sys

    import meshpipeline

    here = str(Path(meshpipeline.__file__).resolve().parents[1])
    return os.pathsep.join(dict.fromkeys([here] + [p for p in sys.path if p]))


def detail_levels_bounded(ecxml_path, *, budget_s: float = DETAIL_TIME_BUDGET_S) -> dict:
    """The three detail levels as the check serves them: sized in a child process that is stopped
    `budget_s` after it starts (plus its start-up), so a model whose grid takes minutes to lay out
    never holds the check. Each level the child finished is kept; any other shows its limits and
    is sized when the mesh runs. Never raises."""
    import json
    import subprocess
    import sys

    from meshpipeline.sandbox.safe_exec import run_guarded, scrubbed_subprocess_env

    out = limits_only()
    cmd = [sys.executable, "-m", "meshpipeline.application.thermal_view", str(ecxml_path),
           "--budget", f"{float(budget_s):g}"]
    env = scrubbed_subprocess_env({"PYTHONPATH": _pythonpath()})
    try:
        p = run_guarded(cmd, env=env, capture_output=True, text=True, timeout=budget_s + _CHILD_START_S)
        text = p.stdout or ""
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    except Exception as exc:  # noqa: BLE001 - the levels then show their limits
        logger.warning("thermal view: the detail levels could not be sized (%s: %s)",
                       type(exc).__name__, str(exc)[:200])
        return out
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("level") in out and isinstance(item.get("row"), dict):
            out[item["level"]] = item["row"]
    return out



# ------------------------------------------------------------------------------ the view ----
def thermal_view(sidecar: dict, placement=None, *, detail: dict | None = None) -> dict:
    """The `thermal` block for an ECXML upload: from the physics sidecar cad/ingest wrote and the
    placement the snap-grid mesher meshes (`placement`; without it the parts come from the
    sidecar's regions, with no thinnest layer or colour). `detail`: the detail levels, when the
    caller sized them (detail_levels); without them each level shows its limits only."""
    tree = _tree(placement) if placement is not None else _tree_from_sidecar(sidecar)
    if detail is None:
        detail = limits_only()
    totals = _totals(sidecar, placement, tree)
    view: dict = {
        "title": str(sidecar.get("model_name") or "") or "the thermal model",
        "producer": _producer(sidecar.get("producer")),
        "spec": str(sidecar.get("spec") or ""),
        "totals": totals,
        "ambient": _ambient(sidecar),
        "domain": _domain_block(sidecar),
        "tree": tree[:MAX_TREE_ROWS],
        "materials": _materials(sidecar, tree),
        "fans": _fans(sidecar), "vents": _vents(sidecar), "resistances": _resistances(sidecar),
        "sources": _sources(sidecar), "compact_models": _compact_models(sidecar),
        "probes": _probes(sidecar),
        "detail": detail,
        "warnings": _warnings(sidecar, placement, tree, detail),
        # the converter's own report, for an engineer who wants every line of it
        "build_report": [str(x) for x in list(sidecar.get("build_report") or [])[:200]]
                        + [str(x) for x in list(sidecar.get("notes") or [])[:200]],
    }
    if len(tree) > MAX_TREE_ROWS:
        view["tree_shown"] = MAX_TREE_ROWS
    view["headline"] = headline(view)
    return view


def headline(view: dict) -> str:
    """One line, as the chat would say it: "Set-top box: a Flotherm thermal model with 8 parts,
    8.7 W in total and 35 °C room air." """
    t = view.get("totals") or {}
    n = int(t.get("parts") or 0)
    by = f"a {view['producer']} thermal model" if view.get("producer") else "a thermal model"
    amb = (view.get("ambient") or {}).get("temperature_C")
    room = f" and {_num(amb)} °C room air" if amb is not None else ""
    return (f"{view.get('title')}: {by} with {n} part{'s' if n != 1 else ''}, "
            f"{_watts(t.get('power_W'))} in total{room}.")


__all__ = ["DEFAULT_LEVEL", "DETAIL_TIME_BUDGET_S", "LEVELS", "SIZED_AT_RUN", "detail_levels",
           "detail_levels_bounded", "estimate_seconds", "headline", "limits_only", "thermal_view"]


def _main(argv: list[str] | None = None) -> int:
    """The child detail_levels_bounded runs: the levels of one ECXML file, one JSON line each, as
    each is sized - so a child stopped part-way still hands over the levels it finished."""
    import argparse
    import json

    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_place import place

    ap = argparse.ArgumentParser(prog="thermal_view")
    ap.add_argument("ecxml", type=Path)
    ap.add_argument("--budget", type=float, default=DETAIL_TIME_BUDGET_S)
    a = ap.parse_args(argv)
    placement = place(read_ecxml(a.ecxml))
    for level, row in _sized_levels(placement, budget_s=a.budget):
        print(json.dumps({"level": level, "row": row}, default=float), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
