# Responsibility: The areas INSIDE a part that a user may name in their notes ("1 mm at the mounting holes", "fine on the fillets", "coarser on the big flat faces"): each hole or group of alike holes, each bore, each group of alike fillets, each boss or shaft, each thin wall (a rib or a flange) and the largest flat faces - read off the FEA measurement's own faces and hot spots, so cad/part_words can tie the words to them as it ties words to parts (NOTE-6).
# Boundaries: geometry only, deterministic, no model. It reads the measurement record (cad/fea_measure: faces, hot spots, bores) and its labelled surface; it plans nothing.
# Collaborates with: cad/part_words.py (the features join the parts table), engines/fea_plan.user_sized (a feature's size wins on its faces), render/part_pictures.py (the features are drawn in colour, the rest of the part grey).
"""AREAS INSIDE A PART (NOTE-6).

Each face belongs to at most one feature, taken in this order: holes (alike ones grouped), bores, notches, fillets
(alike ones grouped; the stress fillets, then the rounded edges the plan keeps the shape of), bosses and shafts (convex cylinders, alike ones grouped), thin walls (ribs, flanges: faces whose
wall is under half the part's typical wall), and the largest flat faces. Feature_k rows carry "feature": True, the
part they are in, a plain name ("4 holes, 8.5 mm across"), how many, their box and their faces."""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

RULE = "NOTE-6"
#: What the areas mean, for a check that kept them (application/geometry_reuse keys on it: a check of the same bytes
#: answered from an earlier one reuses its areas only when they were read by these rules). Bumped whenever the
#: areas change: 4 (round 12) - walls that lie apart are areas apart (round 9), a part that is one plate offers no
#: flat face of itself (round 11), an area that is the whole of its part says so (round 12). m483 round 13: 6b and
#: 6c were answered from checks of earlier sessions whose areas predate them.
VERSION = 4
#: the most features listed (the largest kept)
MAX_FEATURES = 40
#: alike: the same radius within this share
ALIKE_SHARE = 0.03
#: a thin wall: under this share of the part's typical wall
THIN_SHARE = 0.5
#: the largest flat faces listed, and the least share of the part's area one must have
FLAT_COUNT = 4
FLAT_SHARE = 0.04


def _part_of(name: str) -> str:
    head, sep, _tail = str(name).rpartition("_Face_")
    return head if sep else "Part_1"


def _mm(v: float) -> str:
    return f"{float(v):.3g} mm"


def _groups(items: list[tuple[float, list[str]]]) -> list[tuple[float, list[list[str]]]]:
    """Alike radii together: [(radius, [faces of each one])]."""
    out: list[tuple[float, list[list[str]]]] = []
    for r, faces in sorted(items, key=lambda x: x[0]):
        if out and abs(out[-1][0] - r) <= ALIKE_SHARE * max(r, 1e-9):
            out[-1][1].append(faces)
        else:
            out.append((r, [faces]))
    return out


def features(record: Mapping, surface) -> tuple[list[dict], dict[str, list[str]]]:
    """(the feature rows, {feature id: its face names}); ([], {}) when the record names no faces."""
    faces = dict(record.get("faces") or {})
    if not faces:
        return [], {}
    taken: set[str] = set()
    found: list[tuple[str, str, int, list[str]]] = []          # (kind, name, count, faces)

    def take(kind: str, name: str, count: int, fs: list[str]) -> None:
        fs = [f for f in fs if f in faces and f not in taken]
        if fs:
            taken.update(fs)
            found.append((kind, name, count, fs))

    spots = list(record.get("hot_spots") or [])

    def by_part(rows: list[tuple[float, list[str]]]) -> dict[str, list[tuple[float, list[str]]]]:
        out: dict[str, list[tuple[float, list[str]]]] = {}
        for r, fs in rows:
            if fs:
                out.setdefault(_part_of(fs[0]), []).append((r, fs))
        return out

    holes = by_part([(float(h["radius_mm"]), list(h.get("faces") or [])) for h in spots
                     if h.get("type") == "hole" and h.get("radius_mm")])
    for _p, seen in holes.items():
        for r, group in _groups(seen):
            n = len(group)
            take("hole group" if n > 1 else "hole", f"{n} holes, {_mm(2 * r)} across" if n > 1
                 else f"a hole, {_mm(2 * r)} across", n, [f for fs in group for f in fs])
    for b in record.get("bores") or []:
        take("bore", f"a bore, {_mm(2 * float(b.get('radius_mm') or 0))} across", 1, list(b.get("faces") or []))
    for h in spots:
        if h.get("type") == "notch":
            take("notch", f"a notch, radius {_mm(h.get('radius_mm') or 0)}", 1, list(h.get("faces") or []))
    fillets = by_part([(float(h["radius_mm"]), list(h.get("faces") or [])) for h in spots
                       if h.get("type") == "fillet" and h.get("radius_mm")])
    for _p, seen in fillets.items():
        for r, group in _groups(seen):
            n = len(group)
            take("fillets", f"{n} fillets, radius {_mm(r)}" if n > 1 else f"a fillet, radius {_mm(r)}", n,
                 [f for fs in group for f in fs])
    # ROUNDED EDGES the plan keeps the shape of (engines/fea_plan's PLAN-fea-7 rounds) that are no stress fillet: a
    # user's "the fillets" means them too (fea0254's 17 rounds were listed nowhere, the reader said "No fillet feature
    # is listed" and nothing was refined; on its own record they are 16 B-spline faces of radius 0.141 mm). A blend
    # is a curved face with a radius that is no plane, cylinder or cone (a torus, a B-spline round: the plan's own
    # PLAN-fea-7 rounds), or a hollow cylinder that is no hole or bore; a convex cylinder stays a boss or shaft
    # (below) - the record cannot tell a rounded outer edge from a shaft.
    blends = by_part([(float(fr["radius_mm"]), [fn]) for fn, fr in faces.items()
                      if fr.get("radius_mm") and fn not in taken and float(fr["radius_mm"]) > 0
                      and (fr.get("kind") not in ("plane", "cylinder", "cone")
                           or (fr.get("kind") == "cylinder" and fr.get("concave")))])
    for _p, seen in blends.items():
        for r, group in _groups(seen):
            n = len(group)
            take("fillets", f"{n} rounded edges (fillets), radius {_mm(r)}" if n > 1
                 else f"a rounded edge (fillet), radius {_mm(r)}", n, [f for fs in group for f in fs])
    bosses = by_part([(float(row["radius_mm"]), [name]) for name, row in faces.items()
                      if row.get("kind") == "cylinder" and row.get("radius_mm") and not row.get("concave")
                      and name not in taken])
    for _p, seen in bosses.items():
        for r, group in _groups(seen):
            n = len(group)
            take("bosses", f"{n} bosses or shafts, {_mm(2 * r)} across" if n > 1
                 else f"a boss or shaft, {_mm(2 * r)} across", n, [f for fs in group for f in fs])
    index = {n: k for k, n in enumerate(getattr(surface, "names", []) or [])}
    label = np.asarray(getattr(surface, "label", []), dtype=np.int64)
    tris = np.asarray(getattr(surface, "tris", []), dtype=np.int64).reshape(-1, 3)
    xyz = np.asarray(getattr(surface, "xyz", []), dtype=float).reshape(-1, 3)
    part_pts: dict[str, np.ndarray] = {}

    def lie_of(names_: list[str]) -> str:
        # how these faces lie in the reader's pictures (_lie), '' when the surface does not name them
        labs_ = [index[f] for f in names_ if f in index]
        if not labs_ or not len(tris):
            return ""
        p_ = _part_of(names_[0])
        if p_ not in part_pts:
            mine = [index[n] for n in index if _part_of(n) == p_]
            part_pts[p_] = xyz[np.unique(tris[np.isin(label, mine)])]
        return _lie(xyz[tris[np.isin(label, labs_)]], part_pts[p_])

    def normal_of(name_: str) -> np.ndarray | None:
        # a flat face's unit normal, sign-free (its largest triangle's direction), None when the surface lacks it
        if name_ not in index or not len(tris):
            return None
        t_ = xyz[tris[label == index[name_]]]
        if not len(t_):
            return None
        n_ = np.cross(t_[:, 1] - t_[:, 0], t_[:, 2] - t_[:, 0])
        big = n_[int(np.argmax(np.linalg.norm(n_, axis=1)))]
        size_ = float(np.linalg.norm(big))
        return big / size_ if size_ > 0 else None

    walls = {name: float(row.get("thickness_wall_mm") or row.get("thickness_median_mm") or 0.0)
             for name, row in faces.items()}
    per_part: dict[str, list[str]] = {}
    for name in faces:
        per_part.setdefault(_part_of(name), []).append(name)
    for _p, names in per_part.items():
        ws = sorted(w for n in names if (w := walls[n]) > 0)
        if len(ws) < 4:
            continue
        typical = ws[len(ws) // 2]
        thin = [n for n in names if 0 < walls[n] < THIN_SHARE * typical and n not in taken]
        area = sum(float(faces[n].get("area_mm2") or 0.0) for n in names) or 1.0
        if _one_plate(thin, faces, area, normal_of):
            thin = []                       # (the part is one plate: its two sides are the part, below)
        if thin:
            # walls that lie apart (a bent bracket's upright leg and its level base) are areas apart, each said how it
            # lies, so "the vertical leg" is the upright one (round 9); walls that all lie alike stay one area
            by_lie: dict[str, list[str]] = {}
            for wn in thin:
                by_lie.setdefault(lie_of([wn]) if faces[wn].get("kind") == "plane" else "", []).append(wn)
            groups = list(by_lie.items()) if len([k for k in by_lie if k]) > 1 else [("", thin)]
            for lie, fs in groups:
                t = min(walls[n] for n in fs)
                take("thin wall", f"thin walls (ribs or flanges), {_mm(t)} thick" + (f", {lie}" if lie else ""),
                     len(fs), fs)
        flats = sorted((n for n in names if faces[n].get("kind") == "plane" and n not in taken
                        and float(faces[n].get("area_mm2") or 0.0) >= FLAT_SHARE * area),
                       key=lambda n: -float(faces[n].get("area_mm2") or 0.0))[:FLAT_COUNT]
        if _one_plate(flats, faces, area, normal_of):
            # NOTE-6: A PART THAT IS ONE PLATE (its two big faces, parallel, are most of it) has no flat face that is a
            # piece of it - offered as areas, "the vertical leg" of an L-shaped plate was tied to one of them in one
            # run and told the nearest in another (m483 round 10, 6b). The part itself is the place
            flats = []
        for flat in flats:
            take("flat face", f"a large flat face, {float(faces[flat].get('area_mm2') or 0):,.0f} mm²", 1, [flat])
    found = found[:MAX_FEATURES]
    # NOTE-6: AN AREA THAT IS THE WHOLE PART - every face of its part outside it is an opening (a hole, a bore, a
    # notch) or an edge (a face too small to be any area of its own): words that name it name the whole part
    openings = {f for kind, _n, _c, fs in found if kind in OPENING_KINDS for f in fs}
    rows: list[dict] = []
    faces_of: dict[str, list[str]] = {}
    for k, (kind, name, count, fs) in enumerate(found, start=1):
        fid = f"Feature_{k}"
        row: dict = {"id": fid, "feature": True, "kind": kind, "name": name.strip(), "count": count,
                     "in_part": _part_of(fs[0]), "faces": len(fs)}
        if kind not in OPENING_KINDS and whole_of_part(fs, per_part.get(_part_of(fs[0]), []), faces, openings):
            row["whole_part"] = True
        labs = [index[f] for f in fs if f in index]
        if labs and len(tris):
            sel = tris[np.isin(label, labs)]
            if len(sel):
                pts = xyz[np.unique(sel)]
                ext = sorted((pts.max(axis=0) - pts.min(axis=0)).tolist(), reverse=True)
                row["size_mm"] = [float(f"{e:.4g}") for e in ext]
                row["diag_mm"] = float(f"{float(np.linalg.norm(ext)):.4g}")
                if kind == "flat face":
                    where = lie_of(fs)
                    if where:
                        row["name"] = f"{row['name']}, {where}"
                        row["lie"] = where
                elif kind == "thin wall" and name.endswith((", upright", ", level", "at the top", "at the bottom")):
                    row["lie"] = name.rsplit(" thick, ", 1)[-1]
        rows.append(row)
        faces_of[fid] = list(fs)
    return rows, faces_of


#: NOTE-6: a part is one plate when its two largest flat faces are parallel and together this share of its area
ONE_PLATE_SHARE = 0.7
#: the areas that are openings of a part, not a place of it
OPENING_KINDS = ("hole", "hole group", "bore", "notch")


def whole_of_part(fs: list[str], part_faces: list[str], faces: Mapping, openings: set[str]) -> bool:
    """NOTE-6: whether faces `fs` are the whole of their part as a place - every other face of the part is an opening
    (`openings`) or an edge: a face under FLAT_SHARE of the part's area, too small to be an area of its own."""
    area = sum(float(faces[n].get("area_mm2") or 0.0) for n in part_faces if n in faces)
    if area <= 0 or not fs:
        return False
    mine = set(fs)
    return all(n in mine or n in openings or float(faces[n].get("area_mm2") or 0.0) < FLAT_SHARE * area
               for n in part_faces if n in faces)


def _one_plate(flats: list[str], faces: Mapping, area: float, normal_of) -> bool:
    """NOTE-6: whether the part is one plate - exactly two large flat faces, parallel and a wall apart (each other's
    other side: their centroids lie their measured thickness apart along the normal), that together are most of its
    area: neither is a piece of the part, each is the part's own side."""
    if len(flats) != 2 or any(faces[f].get("kind") != "plane" for f in flats):
        return False
    a, b = (normal_of(f) for f in flats)
    if a is not None and b is not None and abs(float(np.dot(a, b))) < 0.99:
        return False
    c1, c2 = (faces[f].get("centroid_mm") for f in flats)
    t = float(faces[flats[0]].get("thickness_median_mm") or 0.0)
    if not (isinstance(c1, (list, tuple)) and isinstance(c2, (list, tuple)) and len(c1) == len(c2) == 3 and t > 0):
        return False
    d = np.asarray(c1, dtype=float) - np.asarray(c2, dtype=float)
    # (along the normal; with no surface to read it from - the stage's checklist reads the record alone, and the L
    # plate's two sides were offered there in round 10 - the centroids of two sides of one outline lie a wall apart)
    apart = abs(float(np.dot(d, a))) if a is not None and b is not None else float(np.linalg.norm(d))
    if abs(apart - t) > 0.25 * t:
        return False
    return sum(float(faces[f].get("area_mm2") or 0.0) for f in flats) >= ONE_PLATE_SHARE * float(area)


#: NOTE-6: a flat face this close to level (or upright) is said so: |n.z| over LEVEL_COS, under UPRIGHT_COS
LEVEL_COS = 0.9
UPRIGHT_COS = 0.1


def _lie(face_tris: np.ndarray, part_pts: np.ndarray) -> str:
    """How a flat face lies in the pictures the reader is shown (z up, render/part_pictures): "upright", or "level"
    with "at the top" / "at the bottom" of its part - so "the vertical leg", "the base plate" or "the top face" can be
    tied to the face that is that piece (a bent L bracket's legs are two plates, each its own mid-face on a shell).
    '' for a face that is neither."""
    if not len(face_tris) or not len(part_pts):
        return ""
    n = np.cross(face_tris[:, 1] - face_tris[:, 0], face_tris[:, 2] - face_tris[:, 0])
    # a plane's normal, sign-free: the largest triangle's direction, the others summed along it
    ref = n[int(np.argmax(np.linalg.norm(n, axis=1)))]
    total = (n * np.sign(n @ ref)[:, None]).sum(axis=0)
    size = float(np.linalg.norm(total))
    if size <= 0:
        return ""
    nz = abs(float(total[2])) / size
    if nz <= UPRIGHT_COS:
        return "upright"
    if nz < LEVEL_COS:
        return ""
    z = float(face_tris[:, :, 2].mean())
    lo, hi = float(part_pts[:, 2].min()), float(part_pts[:, 2].max())
    if hi - lo <= 0:
        return "level"
    share = (z - lo) / (hi - lo)
    return "level, at the top" if share >= 0.75 else "level, at the bottom" if share <= 0.25 else "level"


def labelled(surface, faces_of: Mapping[str, list[str]], *, parts: bool):
    """The surface named for the pictures: a feature's faces as Feature_k_Face_j, every other face as its part
    (Part_p_Face_j) when `parts` (an assembly's parts are drawn too), else as unnamed (drawn grey, no number)."""
    from types import SimpleNamespace

    owner = {f: fid for fid, fs in faces_of.items() for f in fs}
    names = []
    for j, n in enumerate(getattr(surface, "names", []) or []):
        if n in owner:
            names.append(f"{owner[n]}_Face_{j}")
        else:
            names.append(n if parts else f"Rest_Face_{j}")
    return SimpleNamespace(xyz=surface.xyz, tris=surface.tris, label=surface.label, names=names)


__all__ = ["MAX_FEATURES", "features", "labelled"]
