# Responsibility: SHMESH-51o64b - why the delivered shell mesh's elements are outside the user's edge lines: each
# element under their shortest edge (and over their longest) by cause, measured on the mesh and the mid-surface it was
# made from - forced by the geometry (a CAD edge shorter than the shortest edge, a hole too small for HOLE_PER_TURN
# segments of it, a gap narrower than it), by a structure (bolt rings, mapped faces), or ours.
# Boundaries: reads the mid-surface (gmsh OCC, its own session) and plain arrays; changes nothing; never raises past
# `record` (a record figure must never cost the mesh).
# Collaborates with: engines/gmsh/shell_adapter.py (the hook that fills quality.short_edge / quality.long_edge),
# engines/mesh_requirements.py (the told sentence reads "forced" and "forced_by").
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

RULE = "SHMESH-51o64b"
#: a round CAD edge forces short edges when HOLE_PER_TURN segments of the shortest edge do not fit round its turn
HOLE_PER_TURN = 8
#: two CAD edges of one face meeting at under this angle (degrees) make a wedge
SHARP_DEG = 60.0
#: samples per shortest edge along every CAD edge
SAMPLES_PER_LOW = 20
FORCED = ("short_curve", "small_hole", "narrow", "sharp_corner")
STRUCTURE = ("ring", "mapped")
OURS = ("planned_under", "beside_forced", "on_curve", "inner")


def _cad(brep: Path, mm: float, low: float):
    import gmsh

    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("edge_causes")
        gmsh.model.occ.importShapes(str(brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        curves: dict[int, dict] = {}
        for _d, c in gmsh.model.getEntities(1):
            c = int(c)
            length = float(gmsh.model.occ.getMass(1, c)) * mm
            ends = {abs(int(q)) for _dd, q in gmsh.model.getBoundary([(1, c)], oriented=False)}
            lo, hi = gmsh.model.getParametrizationBounds(1, c)
            n = min(20000, max(16, int(length / max(low / SAMPLES_PER_LOW, 1e-9)) + 1))
            pts = np.array(gmsh.model.getValue(1, c, np.linspace(lo[0], hi[0], n).tolist())).reshape(-1, 3) * mm
            closed = len(ends) <= 1
            radius = None
            if gmsh.model.getType(1, c) in ("Circle", "Ellipse") or closed:
                k = gmsh.model.getCurvature(1, c, [0.5 * (lo[0] + hi[0])])
                radius = 1.0 / (k[0] / mm) if k and k[0] > 1e-12 else None
            curves[c] = {"len": length, "ends": ends, "pts": pts, "closed": closed, "r": radius}
        faces = {int(t): sorted({abs(int(c)) for _dd, c in gmsh.model.getBoundary([(2, t)], oriented=False,
                                                                                     combined=False)})
                 for _d, t in gmsh.model.getEntities(2)}
        vxyz = {int(v): np.array(gmsh.model.getValue(0, v, [])) * mm for _d, v in gmsh.model.getEntities(0)}
    finally:
        gmsh.finalize()
    return curves, faces, vxyz


def _tangent(pts: np.ndarray, at: np.ndarray, reach: float) -> np.ndarray:
    seq = pts if np.linalg.norm(pts[0] - at) <= np.linalg.norm(pts[-1] - at) else pts[::-1]
    far = np.linalg.norm(seq - seq[0], axis=1)
    k = min(max(int(np.searchsorted(far, reach)), 1), len(seq) - 1)
    v = seq[k] - seq[0]
    return v / max(float(np.linalg.norm(v)), 1e-12)


def classify(brep: Path | str, mm: float, xyz: np.ndarray, elements: list, mids: list[str], low: float | None,
             high: float | None, *, holes: list[dict] | None = None, face_sizes: dict[str, float] | None = None,
             tol_g: float = 0.0) -> dict:
    """The causes of every element under `low` and over `high` (mm). `elements`: node index lists; `mids`: each
    element's mid-face; `holes`: the record's structure.hole_list; `face_sizes`: the plan's smallest size per
    mid-face; `tol_g`: the judge's tolerance (mm). Returns {"short": {...}, "long": {...}} (empty parts when none)."""
    from scipy.spatial import cKDTree

    ne = len(elements)
    conn = np.full((ne, 4), -1, dtype=np.int64)
    for i, c in enumerate(elements):
        conn[i, :len(c)] = c
    nxt = np.where(np.roll(conn, -1, axis=1) >= 0, np.roll(conn, -1, axis=1), conn[:, :1])
    el = np.linalg.norm(xyz[conn] - xyz[nxt], axis=2)
    el[conn < 0] = np.nan
    lens = el
    shortest = np.nanmin(el, axis=1) if ne else np.zeros(0)
    longest = np.nanmax(el, axis=1) if ne else np.zeros(0)
    under = np.flatnonzero(shortest < low - tol_g) if low else np.zeros(0, int)
    over = np.flatnonzero(longest > high + tol_g) if high else np.zeros(0, int)
    out: dict = {"short": {}, "long": {}}
    if not len(under) and not len(over):
        return out
    lowc = float(low or high or 1.0)
    curves, faces, vxyz = _cad(Path(brep), float(mm), lowc)
    if not curves:
        return out
    keys = list(curves)
    owner = np.concatenate([[k] * len(curves[k]["pts"]) for k in keys])
    tree = cKDTree(np.vstack([curves[k]["pts"] for k in keys]))
    trees = {k: cKDTree(curves[k]["pts"]) for k in keys}
    tol = max(0.03 * lowc, 1e-6)
    near_d, _near_i = tree.query(xyz)
    node_on: dict[int, set[int]] = {}

    def on(v: int) -> set[int]:
        if near_d[v] > tol:
            return set()
        if v not in node_on:
            node_on[v] = {int(owner[j]) for j in tree.query_ball_point(xyz[v], r=tol)}
        return node_on[v]

    def along(a: int, b: int) -> set[int]:
        com = on(a) & on(b)
        if not com:
            return set()
        mid = 0.5 * (xyz[a] + xyz[b])
        reach = tol + 0.2 * float(np.linalg.norm(xyz[a] - xyz[b]))      # a segment's sagitta on a curved edge
        return {k for k in com if trees[k].query(mid)[0] < reach}

    # pairs of CAD edges of one face under the shortest edge apart: narrow (they do not meet there) or a wedge
    narrow: dict[tuple[int, int], float] = {}
    meet_of: dict[tuple[int, int], list] = {}
    sharp: set[tuple[int, int]] = set()
    box = {k: (v["pts"].min(axis=0), v["pts"].max(axis=0)) for k, v in curves.items()}
    for cs in faces.values():
        cs_in = [k for k in cs if k in curves]
        if len(cs_in) < 2:
            continue
        # (the pairs whose boxes stand under `low` apart, all at once; the rest are further apart than low)
        b_lo = np.array([box[k][0] for k in cs_in])
        b_hi = np.array([box[k][1] for k in cs_in])
        gaps = np.maximum(0.0, np.maximum(b_lo[:, None, :] - b_hi[None, :, :], b_lo[None, :, :] - b_hi[:, None, :]))
        near_p = np.linalg.norm(gaps, axis=2) < lowc
        pos = {k: n for n, k in enumerate(cs_in)}
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                if a not in curves or b not in curves or not near_p[pos[a], pos[b]]:
                    continue
                pa, pb = curves[a]["pts"], curves[b]["pts"]
                d, ia = trees[a].query(pb)
                if d.min() >= lowc or np.median(d) < tol:
                    continue
                meet = [vxyz[v] for v in curves[a]["ends"] & curves[b]["ends"] if v in vxyz]
                if not meet:
                    j = int(d.argmin())
                    if (d[j] < 3 * tol and min(np.linalg.norm(e - pa[ia[j]]) for e in (pa[0], pa[-1])) < 3 * tol
                            and min(np.linalg.norm(e - pb[j]) for e in (pb[0], pb[-1])) < 3 * tol):
                        meet = [0.5 * (pa[ia[j]] + pb[j])]
                if not meet:
                    narrow[(a, b)] = float(d.min())
                    continue
                ang = min(math.degrees(math.acos(max(-1.0, min(1.0, float(
                    _tangent(pa, v, 0.25 * lowc) @ _tangent(pb, v, 0.25 * lowc)))))) for v in meet)
                if ang < SHARP_DEG:
                    sharp.add((a, b))
                far = np.ones(len(pb), bool)
                for v in meet:
                    far &= np.linalg.norm(pb - v, axis=1) > 2 * lowc
                if far.any() and float(d[far].min()) < lowc:
                    narrow[(a, b)] = float(d[far].min())
                    meet_of[(a, b)] = meet

    def is_narrow(x: int, y: int, at: np.ndarray) -> bool:
        pr = (x, y) if (x, y) in narrow else ((y, x) if (y, x) in narrow else None)
        return pr is not None and all(float(np.linalg.norm(at - v)) > lowc for v in meet_of.get(pr, ()))

    hole = {k for k, v in curves.items() if v["r"] and 2 * math.pi * v["r"] / HOLE_PER_TURN < lowc}
    shortc = {k for k, v in curves.items() if v["len"] < lowc and not v["closed"]}
    forcing = hole | shortc | {c for pr in list(narrow) + list(sharp) for c in pr}
    ftree = cKDTree(np.vstack([curves[k]["pts"] for k in forcing])) if forcing else None

    # mapped faces: elements joined across edges on no CAD edge; a patch of quads whose inner nodes are each in 4
    # (each mesh edge and the elements on it, at once: the edges two elements of one mid-face share)
    valid_e = conn >= 0
    e_of = np.repeat(np.arange(ne, dtype=np.int64), valid_e.sum(axis=1))
    ea, eb = conn[valid_e], nxt[valid_e]
    lo_e, hi_e = np.minimum(ea, eb), np.maximum(ea, eb)
    srt = np.lexsort((e_of, hi_e, lo_e))
    lo_s, hi_s, e_s = lo_e[srt], hi_e[srt], e_of[srt]
    new_g = np.ones(len(srt), dtype=bool)
    new_g[1:] = (lo_s[1:] != lo_s[:-1]) | (hi_s[1:] != hi_s[:-1])
    starts = np.flatnonzero(new_g)
    n_in = np.diff(np.append(starts, len(srt)))
    s2 = starts[n_in == 2]
    mids_a = np.asarray(mids, dtype=object)
    same = mids_a[e_s[s2]] == mids_a[e_s[s2 + 1]] if len(s2) else np.zeros(0, dtype=bool)
    s2 = s2[same.astype(bool)]
    # (all inner mesh edges at once: on a CAD edge when both ends and the middle lie on it - the middle's nearest
    # CAD edge being one of the ends' nearest)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if len(s2):
        arr = np.stack([lo_s[s2], hi_s[s2], e_s[s2], e_s[s2 + 1]], axis=1).astype(np.int64)
        na, nb = arr[:, 0], arr[:, 1]
        cad = (near_d[na] <= tol) & (near_d[nb] <= tol)
        if cad.any():
            k = np.flatnonzero(cad)
            dm, im = tree.query(0.5 * (xyz[na[k]] + xyz[nb[k]]))
            reach = tol + 0.2 * np.linalg.norm(xyz[na[k]] - xyz[nb[k]], axis=1)
            om = owner[im]
            cad[k] = (dm < reach) & ((om == owner[_near_i[na[k]]]) | (om == owner[_near_i[nb[k]]]))
        keep = arr[~cad]
        g = coo_matrix((np.ones(len(keep)), (keep[:, 2], keep[:, 3])), shape=(ne, ne))
        _n, patch = connected_components(g, directed=False)
    else:
        patch = np.arange(ne)
    # (a patch is mapped when all quads, two or more, each inner node - on no CAD edge - in 4 of them)
    patch = np.asarray(patch, dtype=np.int64)
    n_p = int(patch.max()) + 1 if ne else 0
    nv = valid_e.sum(axis=1)
    all_quad = np.bincount(patch, weights=(nv != 4).astype(float), minlength=n_p) == 0
    p_size = np.bincount(patch, minlength=n_p)
    pn = patch[e_of] * (int(len(xyz)) + 1) + ea
    u_pn, n_pn = np.unique(pn, return_counts=True)
    v_pn = u_pn % (int(len(xyz)) + 1)
    off4 = (n_pn != 4) & (near_d[v_pn] > tol)
    p_bad = np.zeros(n_p, dtype=bool)
    p_bad[u_pn[off4] // (int(len(xyz)) + 1)] = True
    mapped_p = all_quad & (p_size >= 2) & ~p_bad
    mapped = {int(p): bool(v) for p, v in enumerate(mapped_p.tolist())}
    rings, gridded = [], set()
    for h in holes or []:
        n_r = int(h.get("rings") or 0) or (int(h.get("planned_rows") or 0) if h.get("washer") else 0)
        if "grid" in str(h.get("kind") or "") and h.get("face"):
            gridded.add(str(h["face"]))
        if n_r >= 1 and h.get("at_mm") and h.get("diameter_mm"):
            r0 = 0.5 * float(h["diameter_mm"])
            rings.append((np.asarray(h["at_mm"], float), r0, r0 + 1.1 * float(h.get("planned_width_mm") or 0) + tol))

    def in_ring(cen: np.ndarray) -> bool:
        return any(r0 - tol <= float(np.linalg.norm(cen - at)) <= r1 for at, r0, r1 in rings)

    def structured(i: int) -> bool:
        return bool(mapped.get(int(patch[i])) or mids[i] in gridded)

    rank = {"short_curve": 0, "small_hole": 1, "narrow": 2}
    cause_of = {}
    hit_holes: set[int] = set()
    hit_short: set[int] = set()
    touch: Counter[str] = Counter()
    low_f = float(low) if low else -math.inf          # (no shortest edge: nothing is under it)
    # (the per-element figures at once: centres, the short edges, which of them have both ends on the CAD's edges,
    #  the rings, the distance to a forcing CAD edge - the loop below reads them; the same causes as one by one)
    valid = conn >= 0
    cen_all = (xyz[np.where(valid, conn, 0)] * valid[:, :, None]).sum(axis=1) / valid.sum(axis=1)[:, None]
    short_e = np.nan_to_num(lens, nan=np.inf) < low_f - tol_g
    cad_node = near_d <= tol
    both_cad = short_e & cad_node[np.where(valid, conn, 0)] & cad_node[np.where(valid, nxt, 0)]
    u_cen = cen_all[under]
    ring_of = np.zeros(len(under), dtype=bool)
    for at, r0, r1 in rings:
        d_r = np.linalg.norm(u_cen - at, axis=1)
        ring_of |= (r0 - tol <= d_r) & (d_r <= r1)
    beside = (ftree.query(u_cen)[0] < 2 * lowc) if (ftree is not None and len(under)) else np.zeros(len(under), bool)
    pair_check = bool(narrow) or bool(sharp)
    close_of: list = []
    dist_of: dict[tuple[int, int], float] = {}
    if pair_check and len(under):
        # (the CAD edges within `low` of each centre and the centre's distance to each, in one query per CAD edge)
        close_of = [np.unique(owner[np.asarray(b_, dtype=np.int64)]).tolist() if len(b_) else []
                    for b_ in tree.query_ball_point(u_cen, r=lowc)]
        need: dict[int, list[int]] = defaultdict(list)
        for n_u, cl in enumerate(close_of):
            if len(cl) > 1:
                for kc in cl:
                    need[int(kc)].append(n_u)
        for kc, idx in need.items():
            d_k = trees[kc].query(u_cen[idx])[0]
            dist_of.update(zip(((n_u, kc) for n_u in idx), d_k.tolist()))
    for n_u, i in enumerate(under.tolist()):
        c = [int(v) for v in elements[i]]
        cen = cen_all[i]
        found, on_cad = [], False
        for j in np.flatnonzero(both_cad[i]).tolist():
            a, b = c[j], c[(j + 1) % len(c)]
            al = along(a, b)
            if al & shortc:
                found.append(("short_curve", al & shortc))
            elif al & hole:
                found.append(("small_hole", al & hole))
            elif any(is_narrow(x, y, 0.5 * (xyz[a] + xyz[b])) for x in on(a) for y in on(b)):
                found.append(("narrow", set()))
            on_cad = on_cad or bool(al)
        if found:
            cause, ks = min(found, key=lambda f: rank[f[0]])
            (hit_short if cause == "short_curve" else hit_holes).update(ks)
        else:
            cause = None
            if pair_check:
                close = [int(k) for k in close_of[n_u]]
                if len(close) > 1:
                    dist = {k: dist_of[(n_u, k)] for k in close}
                    pairs = [(x, y) for ii, x in enumerate(close) for y in close[ii + 1:]]
                    if any(dist[x] + dist[y] < lowc and is_narrow(x, y, cen) for x, y in pairs):
                        cause = "narrow"
                    elif any(dist[x] + dist[y] < lowc and ((x, y) in sharp or (y, x) in sharp) for x, y in pairs):
                        cause = "sharp_corner"
            if cause is not None:
                pass
            elif ring_of[n_u]:
                cause = "ring"
            elif structured(i):
                cause = "mapped"
            elif face_sizes and face_sizes.get(mids[i], math.inf) < low_f - tol_g:
                cause = "planned_under"
            elif beside[n_u]:
                cause = "beside_forced"
            elif on_cad:
                cause = "on_curve"
            else:
                cause = "inner"
        cause_of[i] = cause
    forced_nodes = {int(v) for i, kind_ in cause_of.items() if kind_ in FORCED for v in elements[i]}
    for i, kind_ in cause_of.items():
        if kind_ == "beside_forced":
            t = any(int(v) in forced_nodes or (on(int(v)) & forcing) for v in elements[i])
            touch["touching_forced" if t else "not_touching"] += 1
    by = Counter(cause_of.values())
    by_face: dict[str, Counter] = defaultdict(Counter)
    for i, kind_ in cause_of.items():
        by_face[mids[i]][kind_] += 1
    diams = sorted(2 * curves[k]["r"] for k in hit_holes if curves[k]["r"])
    shorts = sorted(curves[k]["len"] for k in hit_short)
    gaps = sorted(v for v in narrow.values() if v >= tol)
    n_forced = sum(by[k] for k in FORCED)
    if len(under):
        out["short"] = {
            "rule": RULE, "shortest_mm": float(f"{low_f:.6g}"), "elements": ne, "under": int(len(under)),
            "forced": n_forced, "structure": sum(by[k] for k in STRUCTURE), "ours": sum(by[k] for k in OURS),
            "cannot_meet": n_forced > 0, "by_cause": {k: by[k] for k in (*FORCED, *STRUCTURE, *OURS) if by[k]},
            "beside_forced": dict(touch),
            "forced_by": {"holes": len(hit_holes), "holes_under_mm": float(f"{diams[-1]:.4g}") if diams else None,
                          "smallest_hole_mm": float(f"{diams[0]:.4g}") if diams else None,
                          "short_cad_edges": len(hit_short),
                          "shortest_cad_edge_mm": float(f"{shorts[0]:.4g}") if shorts else None,
                          "narrow_gaps": len(gaps) if by["narrow"] else 0,
                          "narrowest_gap_mm": float(f"{gaps[0]:.4g}") if gaps and by["narrow"] else None},
            "per_face": {m: dict(v) for m, v in sorted(by_face.items())},
            "worst_mm": float(f"{float(shortest[under].min()):.4g}")}
    if len(over):
        lc: Counter = Counter()
        for i in over.tolist():
            c = [int(v) for v in elements[i]]
            m = int(np.nanargmax(lens[i]))
            if structured(i):
                kind_ = "mapped"
            elif in_ring(xyz[c].mean(axis=0)):
                kind_ = "ring"
            elif along(c[m], c[(m + 1) % len(c)]):
                kind_ = "on_curve"
            else:
                kind_ = "inner"
            lc[kind_] += 1
        out["long"] = {"rule": RULE, "longest_mm": float(f"{float(high or 0.0):.6g}"), "over": int(len(over)), "forced": 0,
                       "structure": lc["mapped"] + lc["ring"], "ours": lc["on_curve"] + lc["inner"],
                       "cannot_meet": False, "by_cause": dict(lc),
                       "worst_mm": float(f"{float(longest[over].max()):.4g}")}
    return out


def record(brep: Path | str, mm: float, mesh, applied: dict) -> None:
    """SHMESH-51o64b: quality.short_edge / quality.long_edge on the delivered mesh when the user typed a shortest or
    longest edge and some element is outside it. Never raises."""
    sz = applied.get("sizing") or {}
    low, high = sz.get("low_mm"), sz.get("high_mm")
    if (low is None and high is None) or not mesh.n_elements:
        return
    try:
        q = applied.setdefault("quality", {})
        holes = (q.get("structure") or {}).get("hole_list") or []
        sizes: dict[str, float] = {}
        for r in applied.get("faces") or []:
            if r.get("mid") and r.get("size_mm"):
                sizes[r["mid"]] = min(sizes.get(r["mid"], math.inf), float(r["size_mm"]))
        els = [np.asarray(c) for c in mesh.quads] + [np.asarray(c) for c in mesh.trias]
        G = float(sz.get("global_mm") or low or high or 1.0)
        got = classify(brep, mm, np.asarray(mesh.xyz, float), els, list(mesh.mid), low, high, holes=holes,
                       face_sizes=sizes, tol_g=1e-6 * G)
    except Exception as exc:  # noqa: BLE001 - a record figure must never cost the mesh
        logger.info("shell mesh: the edge-line causes were not measured (%s)", exc)
        return
    if got.get("short"):
        q["short_edge"] = got["short"]
    if got.get("long"):
        q["long_edge"] = got["long"]
    _rows(applied, got)


def _rows(applied: dict, got: dict) -> None:
    """The shortest / longest edge rows of the run's requirements: forced, structure, ours, cannot_meet - and the
    one sentence of what cannot be met (mesh_requirements.forced_words) after the row's own words. "met" is left."""
    from meshpipeline.engines import mesh_requirements as MR

    for row in (applied.get("requirements") or {}).get("rows") or []:
        rec = got.get({"min_edge": "short", "max_edge": "long"}.get(str(row.get("field")), ""), {})
        if not rec:
            continue
        row.update(forced=int(rec["forced"]), structure=int(rec["structure"]), ours=int(rec["ours"]),
                   cannot_meet=bool(rec["cannot_meet"]))
        said = MR.forced_words(rec, float(rec.get("shortest_mm") or 0.0)) if row.get("field") == "min_edge" else ""
        if said and row.get("delivered"):
            row["delivered"] = f"{row['delivered']}. {said}"


__all__ = ["FORCED", "HOLE_PER_TURN", "OURS", "RULE", "STRUCTURE", "classify", "record"]
