# Responsibility: The areas INSIDE a part that a user may name in their notes ("1 mm at the mounting holes", "fine on the fillets", "coarser on the big flat faces"): each hole or group of alike holes, each bore, each group of alike fillets, each boss or shaft, each thin wall (a rib or a flange) and the largest flat faces - read off the FEA measurement's own faces and hot spots, so cad/part_words can tie the words to them as it ties words to parts (NOTE-6).
# Boundaries: geometry only, deterministic, no model. It reads the measurement record (cad/fea_measure: faces, hot spots, bores) and its labelled surface; it plans nothing.
# Collaborates with: cad/part_words.py (the features join the parts table), engines/fea_plan.user_sized (a feature's size wins on its faces), render/part_pictures.py (the features are drawn in colour, the rest of the part grey).
"""AREAS INSIDE A PART (NOTE-6).

Each face belongs to at most one feature, taken in this order: holes (alike ones grouped), bores, notches, fillets
(alike ones grouped), bosses and shafts (convex cylinders, alike ones grouped), thin walls (ribs, flanges: faces whose
wall is under half the part's typical wall), and the largest flat faces. Feature_k rows carry "feature": True, the
part they are in, a plain name ("4 holes, 8.5 mm across"), how many, their box and their faces."""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

RULE = "NOTE-6"
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
    bosses = by_part([(float(row["radius_mm"]), [name]) for name, row in faces.items()
                      if row.get("kind") == "cylinder" and row.get("radius_mm") and not row.get("concave")
                      and name not in taken])
    for _p, seen in bosses.items():
        for r, group in _groups(seen):
            n = len(group)
            take("bosses", f"{n} bosses or shafts, {_mm(2 * r)} across" if n > 1
                 else f"a boss or shaft, {_mm(2 * r)} across", n, [f for fs in group for f in fs])
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
        if thin:
            t = min(walls[n] for n in thin)
            take("thin wall", f"thin walls (ribs or flanges), {_mm(t)} thick", len(thin), thin)
        area = sum(float(faces[n].get("area_mm2") or 0.0) for n in names) or 1.0
        flats = sorted((n for n in names if faces[n].get("kind") == "plane" and n not in taken
                        and float(faces[n].get("area_mm2") or 0.0) >= FLAT_SHARE * area),
                       key=lambda n: -float(faces[n].get("area_mm2") or 0.0))[:FLAT_COUNT]
        for flat in flats:
            take("flat face", f"a large flat face, {float(faces[flat].get('area_mm2') or 0):,.0f} mm²", 1, [flat])
    found = found[:MAX_FEATURES]
    index = {n: k for k, n in enumerate(getattr(surface, "names", []) or [])}
    label = np.asarray(getattr(surface, "label", []), dtype=np.int64)
    tris = np.asarray(getattr(surface, "tris", []), dtype=np.int64).reshape(-1, 3)
    xyz = np.asarray(getattr(surface, "xyz", []), dtype=float).reshape(-1, 3)
    rows: list[dict] = []
    faces_of: dict[str, list[str]] = {}
    for k, (kind, name, count, fs) in enumerate(found, start=1):
        fid = f"Feature_{k}"
        row: dict = {"id": fid, "feature": True, "kind": kind, "name": name.strip(), "count": count,
                     "in_part": _part_of(fs[0]), "faces": len(fs)}
        labs = [index[f] for f in fs if f in index]
        if labs and len(tris):
            sel = tris[np.isin(label, labs)]
            if len(sel):
                pts = xyz[np.unique(sel)]
                ext = sorted((pts.max(axis=0) - pts.min(axis=0)).tolist(), reverse=True)
                row["size_mm"] = [float(f"{e:.4g}") for e in ext]
                row["diag_mm"] = float(f"{float(np.linalg.norm(ext)):.4g}")
        rows.append(row)
        faces_of[fid] = list(fs)
    return rows, faces_of


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
