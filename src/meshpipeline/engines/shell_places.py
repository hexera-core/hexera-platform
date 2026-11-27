# Responsibility: A SIZE IN ONE NAMED PLACE ON A SHELL MESH (SHMESH-52): which faces of the shell a phrase of the user's notes means - a whole part (NOTE-4) or an area inside one (NOTE-6), as the parts reading tied it - the size each of those faces then takes (a number or band, or its own size halved or doubled for "finer" / "coarser", PLAN-fea-36's one step), and, on the built mesh, the report row that says what was asked and what the elements there came out at.
# Boundaries: pure over plain data and numpy; no engine, no file but the small reading kept in the workspace (FILE), no model. A place that cannot be found on the shell is told "not applied" with why, never guessed. Nothing asked in one place: nothing here runs (the shell route reads no parts before the shell, and the plan's rows are untouched).
# Collaborates with: engines/fea_driver.py (reads the parts before a shell when the notes ask a size in one place, writes FILE), engines/shell_run.py (hands it to the mesher), engines/control_appliers.shell_place_sizes (the registry's shell lane, NOTE-13), engines/gmsh/shell_adapter.py (sizes the faces), engines/shell_requirements.report (the row), cad/part_words.py (the reading), cad/midsurface.py (each mid-face's body and CAD faces).
"""SIZES IN ONE NAMED PLACE, ON A SHELL.

The notes' reader (NOTE-7) files "5 mm elements on the base plate" as a `local` row: a size and a place in the user's
words. Which faces of the CAD that place is comes from the parts reading (NOTE-4, cad/part_words): a whole part
(Part_k) or an area inside one (NOTE-6: a group of holes, fillets, a thin wall, a large flat face - with its CAD faces).
On a shell every mid-face knows its body and the two CAD faces it is the middle of (cad/midsurface's record), so:

* a part is every sewn face whose mid-face is of that body;
* an area is every sewn face whose mid-face was made from one of the area's CAD faces. A solid boss is not in the
  shell at all: told not applied, with why.
* SHMESH-52b: a hole's bore, a notch, the plate's edge wall - a face picked on the model or a named hole that meets
  no mid-face - is only an EDGE of the shell (the shell is the middle of the plate). Its size goes on the mid-surface
  curves that lie on that face (`edge_curves`, found on the mesher's own model): the elements round the hole are
  about the size asked, graded out to the rest. A face that meets no mid-face and no curve is told not applied.
* SHMESH-52c: "finer" / "coarser" with no number there: the size the mesh would have had along those curves halved
  or doubled (`edge_scaled`; coarser no more than the plate it bounds and 8 round a hole), met only when the elements
  along it came out clearly finer (coarser) than before.

Only a reading whose quote is the quote of a `local` row is applied (so what is meshed is what is told); an area wins
over its part on its faces (PLAN-fea-32); a part sized with a number keeps it over a "finer" / "coarser"."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np

RULE = "SHMESH-52"
#: the parts reading as the shell run reads it (written by engines/fea_driver before the shell is made)
FILE = "shell_places.json"
#: "finer" / "coarser" on a whole part with no number: its own size halved or doubled (PLAN-fea-36's one step)
SCALES = {"refine": 0.5, "coarsen": 2.0}
#: an area whose CAD faces are only edges of a shell, or are not in it
EDGE_KINDS = ("hole", "hole group", "bore", "notch")
SOLID_KINDS = ("bosses",)
#: SHMESH-52b: the areas whose CAD faces may be only edges of the shell (their triangles are kept for the mesher)
EDGE_SHAPED = (*EDGE_KINDS, "picked")
#: ... a mid-surface curve is on such a face when every point read along it lies within this share of the wall
EDGE_TOL_WALL = 0.25
#: ... the most CAD triangles kept per area (a larger face is told not applied as before)
EDGE_TRIS_MAX = 6000
#: ... points read along each curve
EDGE_SAMPLES = 41
#: SHMESH-52c: "finer" / "coarser" with no number on such an edge: the size the mesh would have had along it (the
#: plan's size round a hole there, else the plate's) halved or doubled - SCALES, the same one step as on a face and on
#: a solid (PLAN-fea-36). Off: told not applied, as before
EDGE_SCALE_RULE = "SHMESH-52c"
EDGE_SCALED = True
#: ... told met only when the elements along the edge came out clearly finer (coarser) than before: at most this share
#: of the plate round it and of the size it would have had (at least its inverse, coarser). 0.75: the solid's own
#: band for a halved size (fea_requirements: met within 1.5 x the size asked, 1.5 x 0.5 = 0.75)
EDGE_MARGIN = 0.75
#: a plan past this share of what a shell mesh is made of in the time allowed is stepped back (as SHMESH-24 does)
FIT_SHARE = 0.95
#: a place whose typical element came out under this share of the size asked says why (its holes, bends and finer
#: neighbours keep their own sizes, graded out: SHMESH-52)
SMALLER_SHARE = 0.8


def _r(v: float) -> float:
    return float(f"{float(v):.6g}")


def _g(v: float) -> str:
    return f"{float(f'{float(v):.3g}'):g}"


# #
# the reading, kept for the shell run
# #

def wanted(req: Mapping | None) -> bool:
    """Whether the notes ask a size in one named place (NOTE-7's `local`): only then are the parts read before a
    shell. False with nothing asked (the shell route runs exactly as before)."""
    return bool((req or {}).get("local"))


def from_reading(ps: Mapping | None) -> dict | None:
    """The parts reading (cad/part_words' section) cut to what a shell can size: {"status", "note", "entries":
    [{"quote", "ask", "min_mm", "max_mm", "targets": [{"id", "name", "area", "kind", "faces"}]}], "dropped",
    "unsure"}. None with no reading at all; a reading that read nothing ("none": the AI step off, no parts measured)
    is kept with its status, so each ask can say why it was not applied."""
    if not ps or ps.get("status") is None:
        return None
    out: dict = {"status": str(ps.get("status")), "note": str(ps.get("note") or ""), "entries": [],
                 "dropped": [str(d) for d in ps.get("dropped") or []],
                 "unsure": [str(u.get("quote") or "") for u in ps.get("unsure") or [] if isinstance(u, Mapping)]}
    if ps.get("status") != "used":
        return out
    rows = {str(r.get("id")): r for r in ps.get("parts") or [] if isinstance(r, Mapping)}
    faces_of = {str(k): [str(f) for f in v] for k, v in (ps.get("features") or {}).items()}
    for e in ps.get("entries") or []:
        ask = str(e.get("ask") or "size")
        if ask != "size" and ask not in SCALES:
            continue
        rest = [str(p) for p in e.get("rest_parts") or []]
        targets = []
        for p in [*(str(x) for x in e.get("parts") or []), *rest]:
            row = rows.get(p) or {}
            area = p in faces_of or bool(row.get("feature"))
            targets.append({"id": p, "name": str(row.get("name") or ""), "area": area,
                            "kind": str(row.get("kind") or ""), "rest": p in rest,
                            **({"faces": faces_of.get(p, [])} if area else {}),
                            # the words meant a piece of this part no row is: the whole part, the nearest there is
                            **({"piece": True} if e.get("piece") and not area and p not in rest else {})})
        if targets:
            out["entries"].append({"quote": str(e.get("quote") or ""), "ask": ask,
                                   **({"min_mm": float(e["min_mm"]), "max_mm": float(e["max_mm"])}
                                      if ask == "size" else {}), "targets": targets})
    return out


def write(workspace, places: Mapping | None) -> None:
    """Keep the reading for the shell run; with none, no file (a carried one is removed)."""
    path = Path(workspace) / FILE
    if places is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(places, indent=1, sort_keys=True), encoding="utf-8")


def read(workspace) -> dict | None:
    try:
        got = json.loads((Path(workspace) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


# #
# SHMESH-52b: a face the shell collapses to an edge
# #

def with_shapes(places: Mapping | None, surface) -> Mapping | None:
    """The reading with each hole-like or picked area's CAD triangles (`tris_mm`: [[x, y, z] x 3], mm) from the run's
    own surface (metres, as cad/face_map.from_surface reads it), so the mesher can find the shell's curves on them.
    Unchanged when there is nothing such or no surface; never raises."""
    want = [t for e in (places or {}).get("entries") or [] for t in e.get("targets") or []
            if t.get("area") and str(t.get("kind") or "") in EDGE_SHAPED and t.get("faces")]
    if not want:
        return places
    try:
        from meshpipeline.cad import face_map as FM
        fm = FM.from_surface(surface, scale_to_m=1.0)
    except Exception:  # noqa: BLE001 - no surface: such a place is told not applied, as before
        return places
    for t in want:
        ids = [fm.names.index(str(f)) for f in t["faces"] if str(f) in fm.names]
        tris = fm.xyz[fm.tris[np.isin(fm.label, ids)]].astype(float) * 1000.0
        if 0 < len(tris) <= EDGE_TRIS_MAX:
            t["tris_mm"] = np.round(tris, 4).tolist()
    return places


def _seg_dist(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(n, m): each point's distance to each segment a-b."""
    ab = b - a
    t = np.einsum("nmk,mk->nm", p[:, None, :] - a[None], ab) / np.maximum(np.einsum("mk,mk->m", ab, ab), 1e-30)
    q = a[None] + np.clip(t, 0.0, 1.0)[..., None] * ab[None]
    return np.linalg.norm(p[:, None, :] - q, axis=2)


def tri_dist(points, tris) -> np.ndarray:
    """Each point's distance to the nearest of the triangles (mm in, mm out)."""
    p = np.asarray(points, dtype=float).reshape(-1, 3)
    t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if not len(t):
        return np.full(len(p), np.inf)
    a, b, c = t[:, 0], t[:, 1], t[:, 2]
    n = np.cross(b - a, c - a)
    nn = np.linalg.norm(n, axis=1)
    un = n / np.maximum(nn, 1e-30)[:, None]
    out = np.empty(len(p))
    step = max(1, 2_000_000 // len(t))
    for s in range(0, len(p), step):
        q = p[s:s + step]
        h = np.einsum("nmk,mk->nm", q[:, None, :] - a[None], un)
        f = q[:, None, :] - h[..., None] * un[None]
        inside = np.broadcast_to(nn[None] > 1e-12, h.shape).copy()
        for u, v in ((a, b), (b, c), (c, a)):
            inside &= np.einsum("nmk,mk->nm", np.cross((v - u)[None], f - u[None]), n) >= 0.0
        d = np.where(inside, np.abs(h), np.inf)
        for u, v in ((a, b), (b, c), (c, a)):
            d = np.minimum(d, _seg_dist(q, u, v))
        out[s:s + step] = d.min(axis=1)
    return out


def edge_curves(curves: Mapping[int, np.ndarray], tris, tol_mm: float) -> list[int]:
    """SHMESH-52b: the curves (tag -> the points read along it, mm) lying on the face whose triangles are `tris`: every
    point within `tol_mm` of it. A curve that only touches the face (a line running up to a hole) is not one."""
    t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if not len(t):
        return []
    lo, hi = t.reshape(-1, 3).min(axis=0) - tol_mm, t.reshape(-1, 3).max(axis=0) + tol_mm
    near = {c: np.asarray(p, dtype=float) for c, p in curves.items()
            if len(p) and bool(np.all(np.asarray(p) >= lo)) and bool(np.all(np.asarray(p) <= hi))}
    return sorted(c for c, p in near.items() if float(tri_dist(p, t).max()) <= tol_mm)


def edge_found(applied: Mapping | None, j: int, polylines: list | None, target: Mapping) -> None:
    """SHMESH-52b: what the mesher found for edge place `j`: the curves' points (`rim_mm`, one polyline each; the
    report measures the elements touching them), or none - then told not applied with why, as before."""
    p = (applied or {}).get("places", [])[j]
    if polylines:
        p.update(edges=len(polylines), rim_mm=[np.round(np.asarray(pl, dtype=float), 4).tolist()
                                               for pl in polylines], why="")
        return
    _not_applied(applied, j, _why_not(target))


def _not_applied(applied: Mapping | None, j: int, why: str) -> None:
    p = (applied or {}).get("places", [])[j]
    p.update(edge=False, edges=0, why=why)
    p.pop("rim_mm", None)
    for a in (applied or {}).get("asks") or []:
        if j in a.get("places") or ():
            a["places"].remove(j)
            a["not_applied"].append({"name": p["name"], "why": p["why"]})


#: SHMESH-52c: why a "coarser" on an edge built nothing (each curve already at the most it can take)
NO_COARSER_WHY = ("on a shell it is an edge of the plate round it, and the elements along it are already as coarse as "
                  "that plate and the roundness of the hole allow (at least 8 elements round a hole), so nothing was "
                  "made coarser")


def edge_scaled(applied: Mapping | None, j: int, curves: list[int], own_mm: list[float], cap_mm: list[float],
                mids: list[str]) -> dict[float, list[int]]:
    """SHMESH-52c: edge place `j`'s "finer" / "coarser" set on the curves the mesher found on it: {size (mm): curves}.
    `own_mm[i]`: the size the mesh would have had along curve i (the plan's size round a hole there, else the plate's);
    `cap_mm[i]`: the most it can take (the plate it bounds - an edge is never coarser than the plate meeting it - and
    SR.HOLE_PER_TURN round a hole). Finer: own x 0.5; coarser: own x 2, no more than the cap. The place keeps
    `before_mm` / `set_mm` (medians), `mids` (the plate's mid-faces, for the row's "the plate round it") and `curves`.
    A coarser that cannot be built anywhere is told not applied, with why. {} with no curve."""
    p = (applied or {}).get("places", [])[j]
    scale = float(p.get("scale") or 1.0)
    if not curves:
        return {}
    sets = [o * scale if scale < 1.0 else max(o, min(o * scale, c)) for o, c in zip(own_mm, cap_mm, strict=True)]
    if scale > 1.0 and all(s <= o * (1 + 1e-6) for s, o in zip(sets, own_mm, strict=True)):
        _not_applied(applied, j, NO_COARSER_WHY)
        return {}
    p.update(before_mm=_r(float(np.median(own_mm))), set_mm=_r(float(np.median(sets))), mids=sorted(set(mids)),
             curves=sorted(int(c) for c in curves))
    out: dict[float, list[int]] = {}
    for c, s in zip(curves, sets, strict=True):
        out.setdefault(float(f"{s:.6g}"), []).append(int(c))
    return out


def _near_rim(xyz: np.ndarray, rims: list, tol: float) -> np.ndarray:
    """Which nodes lie within `tol` of the polylines (mm)."""
    hit = np.zeros(len(xyz), dtype=bool)
    for pl in rims:
        pts = np.asarray(pl, dtype=float).reshape(-1, 3)
        if len(pts) < 2:
            continue
        lo, hi = pts.min(axis=0) - tol, pts.max(axis=0) + tol
        cand = np.flatnonzero(np.all((xyz >= lo) & (xyz <= hi), axis=1))
        for s in range(0, len(cand), 4096):
            k = cand[s:s + 4096]
            hit[k] |= _seg_dist(xyz[k], pts[:-1], pts[1:]).min(axis=1) <= tol
    return hit


def _edge_elements(mesh, rims: list, size_mm: float) -> np.ndarray:
    """Each element (quads then triangles, as shell_requirements._edge_stats) with a node on the rim."""
    xyz = np.asarray(mesh.xyz, dtype=float)
    on = _near_rim(xyz, rims, max(0.25 * float(size_mm), 1e-3))
    parts = [on[np.asarray(c, dtype=int)].any(axis=1) for c in (mesh.quads, mesh.trias) if len(c)]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=bool)


# #
# which reading is which ask
# #

def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", str(text).lower()))


def _flat(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", str(text).lower()))


#: words that tell no place apart ("3 mm on the bracket" and "10 mm on the frame" share only these)
_COMMON = frozenset(("a", "an", "the", "on", "at", "in", "of", "for", "to", "and", "or", "with", "around", "near",
                     "mm", "cm", "m", "in", "inch", "element", "elements", "size", "sizes", "mesh", "use", "please",
                     "make", "it", "be", "is", "all", "about", "approx", "approximately"))


def same_words(a: str, b: str) -> bool:
    """Whether two quotes are the same words of the user's - one inside the other, or the same numbers and most of
    the shorter's own words (not "the", "mm", "elements") in the longer: how a parts reading is tied to the `local`
    row it serves."""
    na, nb = _flat(a), _flat(b)
    if na and nb and (na in nb or nb in na):
        return True
    ta, tb = _tokens(a), _tokens(b)
    nums_a = {t for t in ta if t[0].isdigit()}
    nums_b = {t for t in tb if t[0].isdigit()}
    if not (nums_a <= nums_b or nums_b <= nums_a):
        return False
    wa, wb = ta - nums_a - _COMMON, tb - nums_b - _COMMON
    small = min(len(wa), len(wb))
    return small > 0 and len(wa & wb) >= max(1, math.ceil(0.6 * small))


def matched(local: Mapping, places: Mapping | None) -> list[int]:
    """The entries of the reading that serve one `local` row (by its quote)."""
    return [i for i, e in enumerate((places or {}).get("entries") or []) if same_words(local.get("quote") or "",
                                                                                       e.get("quote") or "")]


# #
# the plan's faces sized (engines/gmsh/shell_adapter, through control_appliers.shell_place_sizes)
# #

def _faces_of(target: Mapping, owners: list[str], mids: Mapping[str, Mapping]) -> tuple[list[int], int]:
    """(the sewn faces a target means, how many of an area's CAD faces lie on the shell)."""
    if target.get("area"):
        want = {str(f) for f in target.get("faces") or []}
        hit = {m for m, row in mids.items() if want & {str(f) for f in row.get("faces") or []}}
        on = len(want & {str(f) for m in hit for f in mids[m].get("faces") or []})
        return [k for k, o in enumerate(owners) if o in hit], on
    return [k for k, o in enumerate(owners) if str((mids.get(o) or {}).get("body") or "") == str(target["id"])], 0


def _why_not(target: Mapping) -> str:
    """Why a target has no face on the shell, in plain words."""
    if not target.get("area"):
        # SHSTAGE-9: no solid is made beside a shell - a part with no plate is not in the mesh at all
        return "this part has no plate in it, so it is not in this shell mesh"
    kind = str(target.get("kind") or "")
    if kind in EDGE_KINDS:
        return "a hole is only an edge on a shell (the shell is the middle of the plate), so it has no faces to size"
    if kind in SOLID_KINDS:
        return "it is solid, not a plate, so it is not in this shell mesh"
    return "it is not on the shell's mid-surface"


def name_of(target: Mapping) -> str:
    return str(target.get("name") or "").strip() or str(target.get("id") or "")


def plan(rows: list[dict], owners: list[str], record: Mapping, places: Mapping | None, local: list[Mapping], *,
         areas_mm2: list[float], curved: list[tuple[float | None, float]] | None = None,
         planned_max: float = math.inf) -> dict | None:
    """SHMESH-52: the plan's rows (one per sewn face, `owners[k]` its mid-face) sized in place for every place the
    notes ask a size of; None (rows untouched) when no `local` row has a reading. Returns {"rule", "asks": [{"id"
    (the local row), "places": [...], "not_applied": [{"name", "why"}], "why"}], "places": [{"name", "id", "area",
    "ask", "min_mm", "max_mm", "faces", "mids", "before_mm", "set_mm", "finer", "coarser"}], "planned", "held",
    "scale", "finest_mm"}.

    A number (or the middle of a band) sets each face's size; "finer" / "coarser" halve or double the face's own.
    A curved face keeps its shape (SHMESH-24: shell_requirements.curved_size; asked coarser, it keeps its own size)
    and a hole edge's finer size stays. The user's size beats our minimums (finer than half the wall, coarser than
    our rules: said); only a plan past `planned_max` elements is stepped to the nearest that builds (said)."""
    from meshpipeline.engines import shell_requirements as SR

    if not places or not local:
        return None
    mids = {str(m["id"]): m for m in record.get("mids") or []}
    entries = list(places.get("entries") or [])
    asks: list[dict] = []
    serving: list[int] = []
    for loc in local:
        got = matched(loc, places)
        asks.append({"id": loc.get("id"), "entries": got})
        serving += [i for i in got if i not in serving]
    if not serving:
        # no reading serves any ask: the rows stay as they are, and each ask says why
        return {"rule": RULE, "asks": [{"id": loc.get("id"), "places": [], "not_applied": [], "why": _unread(loc, places)}
                                       for loc in local], "places": [], "applied": False, "held": False}
    # who sizes each face: areas over parts (PLAN-fea-32); a number over a "finer" / "coarser" (PLAN-fea-36)
    order = sorted(((i, ti, t) for i in serving for ti, t in enumerate(entries[i]["targets"])),
                   key=lambda it: (0 if it[2].get("area") else 1, 0 if entries[it[0]]["ask"] == "size" else 1))
    owner_of: dict[int, int] = {}
    found: list[dict] = []
    for i, ti, t in order:
        ks, on = _faces_of(t, owners, mids)
        e = entries[i]
        # SHMESH-52b: on no mid-face but with its triangles kept: sized on the shell's curves lying on it (the
        # mesher finds them, edge_found). SHMESH-52c: "finer" / "coarser" there too - the size the mesh would have
        # had along those curves halved or doubled, set by the mesher once it has found them (edge_scaled)
        edge = not ks and bool(t.get("tris_mm")) and (e["ask"] == "size" or (EDGE_SCALED and e["ask"] in SCALES))
        p = {"entry": i, "id": t["id"], "name": name_of(t), "area": bool(t.get("area")), "ask": e["ask"],
             **({"min_mm": e["min_mm"], "max_mm": e["max_mm"]} if e["ask"] == "size" else {}),
             "faces": 0, "mids": [], "why": "" if ks or edge else _why_not(t),
             **({"piece": True} if t.get("piece") else {}),
             **({"edge": True, "target": ti} if edge else {}),
             **({"set_mm": _r(0.5 * (e["min_mm"] + e["max_mm"]))} if edge and e["ask"] == "size" else {}),
             **({"scale": SCALES[e["ask"]], "rule": EDGE_SCALE_RULE} if edge and e["ask"] in SCALES else {})}
        if t.get("area") and ks:
            p["cad_faces"] = [on, len(t.get("faces") or [])]
        for k in ks:
            owner_of.setdefault(k, len(found))
        found.append(p)
    before = {k: float(rows[k]["size_mm"]) for k in owner_of}
    for k, j in sorted(owner_of.items()):
        p, r = found[j], rows[k]
        h0 = before[k]
        bend, width = curved[k] if curved else (None, 0.0)
        bent = bend is not None and bend > 0
        if p["ask"] == "size":
            lo, hi = float(p["min_mm"]), float(p["max_mm"])
            h = 0.5 * (lo + hi)
            if bend is not None and bend > 0:
                h = min(h, SR.curved_size(float(bend), float(width), floor=lo, rule=h, chord=SR.CHORD_SHARE * h))
        elif p["ask"] == "refine":
            h = SCALES["refine"] * h0
        else:
            h = h0 if bent else SCALES["coarsen"] * h0
        r["size_mm"] = _r(h)
        if "curve_mm" in r and float(r["curve_mm"]) >= r["size_mm"]:
            r.pop("curve_mm")
            r.pop("curve_rule", None)
        r["floored"] = False
        r["rule"] = f"{r['rule']}+{RULE}"
        r["place"] = j
    planned = sum(a / r["size_mm"] ** 2 for a, r in zip(areas_mm2, rows, strict=True))
    out: dict = {"rule": RULE, "held": False}
    if planned > planned_max and owner_of:
        mine = sum(areas_mm2[k] / rows[k]["size_mm"] ** 2 for k in owner_of)
        room = FIT_SHARE * planned_max - (planned - mine)
        if room > 0:
            # the nearest that builds: the places' sizes up by one factor, and said
            s = math.sqrt(mine / room)
            for k in owner_of:
                rows[k]["size_mm"] = _r(rows[k]["size_mm"] * s)
            planned = sum(a / r["size_mm"] ** 2 for a, r in zip(areas_mm2, rows, strict=True))
            out.update(held=True, scale=round(s, 4))
    for j, p in enumerate(found):
        ks = sorted(k for k, o in owner_of.items() if o == j)
        p["faces"] = len(ks)
        p["mids"] = sorted({owners[k] for k in ks}, key=lambda m: (len(m), m))
        if ks:
            p["before_mm"] = _r(float(np.median([before[k] for k in ks])))
            p["set_mm"] = _r(float(np.median([rows[k]["size_mm"] for k in ks])))
            p["finer"] = any(rows[k]["size_mm"] < 0.5 * float(rows[k]["wall_mm"]) * (1 - 1e-6) for k in ks)
            p["coarser"] = any(rows[k]["size_mm"] > before[k] * (1 + 1e-6) for k in ks)
            p["held"] = bool(out["held"])
        elif not p["why"] and not p.get("edge"):
            # every face it means is sized by an area or an earlier phrase
            p["why"] = "its faces are sized by another phrase of your notes"
    for a, loc in zip(asks, local, strict=True):
        mine = [p for p in found if p["entry"] in a["entries"]]
        a["places"] = [j for j, p in enumerate(found) if p["entry"] in a["entries"] and (p["faces"] or p.get("edge"))]
        a["not_applied"] = [{"name": p["name"], "why": p["why"]} for p in mine if not (p["faces"] or p.get("edge"))]
        a["why"] = "" if a["entries"] else _unread(loc, places)
        del a["entries"]
    # (a "finer" / "coarser" edge's size is set by the mesher, SHMESH-52c: it lowers the mesh's floor itself)
    sized = [rows[k]["size_mm"] for k in owner_of] + [float(p["set_mm"]) for p in found
                                                       if p.get("edge") and p.get("set_mm") is not None]
    out.update(asks=asks, places=found, planned=int(round(planned)),
               applied=bool(owner_of) or any(p.get("edge") for p in found),
               finest_mm=_r(min(sized)) if sized else None)
    return out


def _unread(local: Mapping, places: Mapping | None) -> str:
    """Why a `local` row has no reading to apply, in plain words."""
    p = places or {}
    if p.get("status") == "none":
        return ("your words were not tied to the parts of your CAD this run (that step is off), so the whole shell "
                "takes the sizes asked for the whole mesh")
    if p.get("status") != "used":
        return ("the step that ties your words to the parts of your CAD did not answer, so the whole shell takes the "
                "sizes asked for the whole mesh")
    quote = str(local.get("quote") or "")
    for d in p.get("dropped") or []:
        m = re.match(r'^"(.*)": (.*)$', str(d))
        if m and same_words(quote, m.group(1)):
            why = re.sub(r",? so (nothing was applied|it was not applied).*$", "", m.group(2)).strip()
            return why or "it could not be tied to a part of your CAD"
    if any(same_words(quote, u) for u in p.get("unsure") or []):
        return "it was not clear which part you meant"
    return "no part or area of your CAD was matched to these words; the whole shell takes the sizes asked for the " \
           "whole mesh"


# #
# asked against delivered (the row of shell_requirements.report)
# #

def row(local_id, applied: Mapping | None, mesh) -> dict | None:
    """SHMESH-52: the report row of one `local` row on the built shell: what each place it names came out at (the
    typical element there: the median of each element's mean of its shortest and longest edge, as the global size's
    row), met when inside the asked size or band read as "about" (shell_requirements.BAND); what could not be placed
    told with why. None when nothing was planned for it (the caller keeps its own row)."""
    from meshpipeline.engines import shell_requirements as SR

    ask = next((a for a in (applied or {}).get("asks") or [] if a.get("id") == local_id), None)
    if ask is None:
        return None
    places = list((applied or {}).get("places") or [])
    if not ask.get("places"):
        whys = [f"{n['name']}: {n['why']}" for n in ask.get("not_applied") or []]
        return {"applied": False, "met": None,
                "delivered": "not applied - " + ("; ".join(whys) if whys else str(ask.get("why") or ""))}
    lo_e, hi_e = SR._edge_stats(mesh)
    mid = np.asarray(mesh.mid, dtype=object)
    said, met, measured, nearest = [], True, None, False
    for j in ask["places"]:
        p = places[j]
        if p.get("edge"):
            # SHMESH-52b: the elements touching the shell's edge on that face
            sel = _edge_elements(mesh, p.get("rim_mm") or [], float(p["set_mm"]))
        else:
            sel = np.isin(mid, p["mids"]) if len(mid) else np.zeros(0, dtype=bool)
        n = int(sel.sum())
        typical = float(np.median(0.5 * (lo_e[sel] + hi_e[sel]))) if n else 0.0
        measured = _r(typical) if measured is None else measured
        if p.get("edge") and p["ask"] != "size":
            # SHMESH-52c: "finer" / "coarser" on an edge, against the plate round it (its mid-faces' elements not on
            # the edge) and the size it would have had there
            plate_sel = (np.isin(mid, p.get("mids") or []) & ~sel) if len(mid) else np.zeros(0, dtype=bool)
            plate = float(np.median(0.5 * (lo_e[plate_sel] + hi_e[plate_sel]))) if plate_sel.any() else None
            before = float(p.get("before_mm") or 0.0)
            ref = min(before, plate) if plate is not None else before
            if p["ask"] == "refine":
                ok = bool(n) and typical <= EDGE_MARGIN * ref
            else:
                ok = bool(n) and typical >= before / EDGE_MARGIN
            met = met and ok
            line = (f"{p['name']}: on a shell it is an edge; elements along it are about {_g(typical)} mm "
                    f"({n:,} elements)" + (f", the plate round it about {_g(plate)} mm" if plate is not None else "")
                    + f"; the size it would have had there, {_g(before)} mm, "
                    + ("halved" if p["ask"] == "refine" else "doubled") + f" to {_g(p['set_mm'])} mm")
            if p["ask"] == "coarsen" and float(p["set_mm"]) < 2.0 * before * (1 - 1e-3):
                line += " (no coarser than the plate round it, and at least 8 elements round a hole)"
            if not ok:
                line += ("; it did not come out clearly finer than before" if p["ask"] == "refine"
                         else "; it did not come out clearly coarser than before")
            if p.get("piece"):
                line = "applied to the whole part, the nearest there is - " + line
                nearest = True
            said.append(line)
            continue
        if p["ask"] == "size":
            lo, hi = float(p["min_mm"]), float(p["max_mm"])
        else:
            lo = hi = float(p["set_mm"])
        ok = bool(n) and SR.BAND[0] * lo <= typical <= SR.BAND[1] * hi and not p.get("held")
        met = met and ok
        line = f"{p['name']}: elements are about {_g(typical)} mm ({n:,} elements)"
        if p.get("edge"):
            line = (f"{p['name']}: on a shell it is an edge; the elements along it are about {_g(typical)} mm "
                    f"({n:,} elements)")
            if n and typical < SMALLER_SHARE * lo:
                line += "; the plate round it is already finer, and keeps its own size"
        if p["ask"] != "size":
            line = (f"{p['name']}: its own size {'halved' if p['ask'] == 'refine' else 'doubled'}, from "
                    f"{_g(p['before_mm'])} to {_g(p['set_mm'])} mm; elements are about {_g(typical)} mm "
                    f"({n:,} elements)")
        if p.get("area") and p.get("cad_faces") and p["cad_faces"][0] < p["cad_faces"][1]:
            line += f" (on {p['cad_faces'][0]} of its {p['cad_faces'][1]} CAD faces; the rest are only edges of the shell)"
        if p.get("held"):
            line += "; that many elements would take longer than a shell mesh is given, so the nearest size that builds was used"
        if n and p["ask"] == "size" and typical < SMALLER_SHARE * lo and not p.get("edge"):
            # the holes, bends and finer faces beside it keep their own sizes (SHMESH-52): said, not hidden
            line += "; smaller near holes, bends and finer faces beside it, so shapes stay true and sizes change smoothly"
        if p.get("finer"):
            line += "; some are smaller than half the wall, finer than a shell usually needs"
        elif p.get("coarser") and p["ask"] != "refine" and n:
            # told from the built elements: the largest there against the size our rules gave those faces
            big = float(np.percentile(0.5 * (lo_e[sel] + hi_e[sel]), 90))
            if big > (1 + 1e-3) * float(p.get("before_mm") or 0.0):
                line += (f"; its largest are about {_g(big)} mm, coarser than the {_g(p['before_mm'])} mm our "
                         "accuracy rules use there, so results may be less exact")
        if p.get("piece"):
            # the words meant a piece of the part that is no face of its own: never told as plainly met
            line = "applied to the whole part, the nearest there is - " + line
            nearest = True
        said.append(line)
    for n_ in ask.get("not_applied") or []:
        met = False
        said.append(f"not applied on {n_['name']}: {n_['why']}")
    return {"applied": True, "measured": measured, "met": None if met and nearest else met,
            **({"nearest": True} if met and nearest else {}), "delivered": "; ".join(said)}


def summary_words(applied: Mapping | None) -> str:
    """What the applier did, for the registry's row (control_appliers): "sized 2 places on the shell"."""
    n = sum(1 for p in (applied or {}).get("places") or [] if p.get("faces") or p.get("edges"))
    if not n:
        return "no place your notes name is on the shell"
    return f"sized {n} place{'s' if n != 1 else ''} on the shell" + (
        "; the nearest size that builds was used" if (applied or {}).get("held") else "")


def told(_lane: str, _key: str) -> dict:
    """The registry's verify for a size in one place (contracts/mesh_controls): its rows are measured place by place on
    the built mesh (`row`); a route that only has the registry to go by says so."""
    return {"applied": True, "met": None, "delivered": "measured on the built mesh, place by place"}


__all__ = ["FILE", "RULE", "SCALES", "edge_curves", "edge_found", "edge_scaled", "from_reading", "matched", "name_of", "plan", "read",
           "row", "same_words", "summary_words", "told", "tri_dist", "wanted", "with_shapes", "write"]
