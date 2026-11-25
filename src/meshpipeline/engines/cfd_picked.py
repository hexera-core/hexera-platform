# Responsibility: A SIZE TYPED WITH FACES PICKED ON THE MODEL, ON A FLOW MESH (docs/RULES.md CFDREQ-34, CFDREQ-35): bind the size to the faces the note carried, resolve each picked CAD face to its own triangles in the staged surface's frame (written as constant/triSurface/picked_<n>.stl, a refinement surface - never a patch), and measure, on the built mesh, the surface cell the walls got on that face (or on a named patch), so "applied" for a size in a place is a measured fact.
# Boundaries: reads the notes as stored on the state, the uploaded CAD (OpenCASCADE through cad/fea_measure) and the built polyMesh; it writes only picked_faces.json and the picked_<n>.stl files in the workspace. It chooses no level: the engines' appliers in engines/cfd_requirements do.
# Collaborates with: engines/cfd_requirements (read_for_attempt binds and resolves; snappy_picked / cfmesh_picked build; report measures), engines/snappy/snappy_runner and engines/cfmesh/cfmesh_runner (the refinement entries), engines/cfd_cell_measures (the polyMesh reader).
"""A SIZE ON A PICKED FACE.

On the website a note sent with faces picked on the model carries them, `faces: [{id, label, sig}]` (the id the
engines name the face by, Face_k; the label the user saw; the signature - area, centre, normal - the pick route
returned). A size typed in that note is for those faces. This module makes that true on a flow mesh:

- bind():    the size entry gets the faces (a "local" entry with `faces`, `labels`, `sigs`), whatever place the
             words name ("this wall", "here"); a wall or overall size typed with a pick becomes a size on the faces.
- resolve(): each face's own triangles from the uploaded CAD (cad/fea_measure._brep_surface, the tessellation that
             numbers Face_k), re-checked against the signature, in metres, written as picked_<n>.stl.
- measure(): on the built polyMesh, the wall faces lying on the picked triangles (or a named patch's faces), and the
             size of their cells (the square root of each face's area): the median is what was built.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: a size typed with faces picked is built on those faces, on snappyHexMesh and cfMesh (rule id, cited in code)
RULE_BUILT = "CFDREQ-34"
#: a size in a place is "applied" only when the built mesh measures it there
RULE_MEASURED = "CFDREQ-35"
#: the file stem of the n-th picked place's own refinement surface (constant/triSurface/picked_<n>.stl)
PICKED = "picked_"
#: what was resolved, per size entry id
FILE = "picked_faces.json"
#: a face is still the face picked when its area and centre agree within these shares (cad/face_map on the website)
SAME_AREA_SHARE = 0.02
SAME_CENTRE_SHARE = 0.01
#: a size is met when the median wall cell on the place is at most this much over the size asked (a maximum)
MET_SLACK = 1.05
#: CFDREQ-35: the place "the body's walls" as a row's patches - every patch of type wall in the built mesh (a mesher
#: that names its wall patches only when it writes them: snappy's one-body surface)
WALL_PATCHES = "*walls"
#: the most points the picked surface is sampled with for the measure
MAX_SAMPLES = 400_000
#: the fewest wall faces on a place for a measure
MIN_FACES = 3
#: words that keep a size typed with a pick for the whole mesh all the same ("2 mm overall")
_WHOLE = re.compile(r"\b(?:global(?:ly)?|whole|overall|everywhere|entire|all\s+over|everything|rest\s+of)\b", re.I)


def _squash(text: str) -> str:
    return re.sub(r"[^\w.]+", "", str(text or "").casefold()).replace("_", "")


def _carrying(notes) -> list[tuple[str, list[dict]]]:
    out = []
    for n in notes or ():
        if not isinstance(n, Mapping):
            continue
        faces = [dict(f) for f in n.get("faces") or () if isinstance(f, Mapping) and str(f.get("id") or "")]
        if faces:
            out.append((_squash(str(n.get("text") or "")), faces))
    return out


def where_words(faces) -> str:
    """The place as the user saw it: "the face you picked (Face 12 · round Ø15 mm)"."""
    labels = [str(f.get("label") or f.get("id") or "") for f in faces or ()]
    head = "the face you picked" if len(labels) == 1 else f"the {len(labels)} faces you picked"
    return f"{head} ({'; '.join(labels[:3])}{'; ...' if len(labels) > 3 else ''})"


def bind(req: dict | None, notes) -> dict | None:
    """CFDREQ-34: a size or a refinement typed in a note sent with faces picked is for THOSE faces. A place entry
    gets the faces whatever place its words name; a wall size or a global size typed with a pick becomes a size on
    the faces (never the size of every wall); words that say the whole mesh stay as written. An entry the reading
    already bound (`faces` set) is kept. Matched in code by the note's words, never asked of the model."""
    carrying = _carrying(notes)
    if not carrying or not isinstance(req, dict):
        return req

    def faces_of(quote) -> list | None:
        q = _squash(str(quote or ""))
        if len(q) < 2 or _WHOLE.search(str(quote or "")):
            return None
        return next((f for text, f in carrying if q in text), None)

    def put(e: dict, faces: list) -> None:
        e.update(where=where_words(faces), faces=[str(f.get("id")) for f in faces],
                 labels=[str(f.get("label") or "") for f in faces], sigs=[f.get("sig") for f in faces],
                 rule=RULE_BUILT)
        for r in req.get("rows") or ():
            if r.get("id") == e.get("id"):
                r["field"] = "local"
                mm = e.get("mm")
                r["asked"] = (f"cells of {mm:g} mm at {e['where']}" if e.get("what") == "size" and mm is not None
                              else f"{'finer' if e.get('what') == 'finer' else 'coarser'} cells at {e['where']}")

    req.setdefault("local", [])
    for e in req["local"]:
        if isinstance(e, dict) and not e.get("faces"):
            faces = faces_of(e.get("quote"))
            if faces:
                put(e, faces)
    sizes = req.get("sizes") or {}
    for key, field in (("near_wall_mm", "near_wall"), ("global_mm", "global")):
        if sizes.get(key) is None:
            continue
        r = next((r for r in req.get("rows") or () if r.get("field") == field), None)
        faces = faces_of(r.get("quote") or r.get("asked")) if r else None
        if r and faces:
            e = {"id": r["id"], "where": "", "what": "size", "mm": float(sizes[key]),
                 "quote": r.get("quote") or r.get("asked")}
            sizes[key] = None
            req["local"].append(e)
            put(e, faces)
    return req


def picked_entries(local) -> list[dict]:
    return [dict(e) for e in local or () if isinstance(e, Mapping) and e.get("faces")]


def _cad_faces(source_path: str):
    """The uploaded CAD's faces as one labelled surface in the FILE's own unit (cad/fea_measure numbers them Face_k);
    the unit is settled against the staged surface later (framed)."""
    from meshpipeline.cad.fea_measure import _brep_surface
    from meshpipeline.cad.scout import read_cad

    surface, _faces, _kinds = _brep_surface(read_cad(source_path), 1.0)
    return surface


def _norm_id(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).casefold()).strip("_")


def _same(area: float, cen: np.ndarray, sig: Mapping | None, diag: float) -> bool:
    if not isinstance(sig, Mapping) or "area_m2" not in sig:
        return True                       # no signature sent: the id alone (told as not re-checked nowhere)
    try:
        a = float(sig["area_m2"])
        c = np.asarray(sig["centroid_m"], dtype=float).reshape(3)
    except (KeyError, TypeError, ValueError):
        return False
    return abs(area - a) <= SAME_AREA_SHARE * max(a, 1e-300) and float(np.linalg.norm(cen - c)) <= SAME_CENTRE_SHARE * diag


def resolve(ws, req: Mapping | None, source_path: str, *, surface=None) -> dict:
    """CFDREQ-34: every picked entry's faces as their own triangles (metres), written to picked_<n>.stl, kept in
    picked_faces.json: {entry id: {"file", "where", "area_m2", "faces"}} or {entry id: {"gone": why}} - a face not on
    this model (or not the face picked: its signature differs) or a triangle file (no CAD faces to pick): told, never
    put on another face. Written as if the file's unit were millimetres; framed() settles the unit against the staged
    surface. `surface` (tests): the labelled surface in the file's unit. Never raises."""
    ws = Path(ws)
    out: dict[str, Any] = {}
    entries = picked_entries((req or {}).get("local"))
    (ws / FILE).unlink(missing_ok=True)
    if not entries:
        return out
    try:
        s = surface if surface is not None else _cad_faces(source_path)
        xyz = np.asarray(s.xyz, dtype=float).reshape(-1, 3) / 1000.0
        tris = np.asarray(s.tris, dtype=np.int64).reshape(-1, 3)
        lab = np.asarray(s.label, dtype=np.int64).reshape(-1)
        names = [_norm_id(n) for n in s.names]
    except Exception as exc:  # noqa: BLE001 - told, never a failed run
        logger.warning("picked faces: the model's faces could not be read (%s)", type(exc).__name__)
        for e in entries:
            out[str(e["id"])] = {"gone": f"{e.get('where') or 'the faces you picked'}: the model's faces could not be "
                                         "read on this run (a triangle file has none to pick)"}
        _save(ws, out)
        return out
    diag = float(np.linalg.norm(xyz.max(0) - xyz.min(0))) if len(xyz) else 0.0
    if len(xyz):
        out["_model_box"] = [xyz.min(0).tolist(), xyz.max(0).tolist()]
    # CFDREQ-36: the whole model's surface, so each picked place's share of it prices its refinement against the
    # plan's own wall (a ratio of the file's own areas: no unit in it)
    try:
        model_area = float(0.5 * np.linalg.norm(np.cross(xyz[tris[:, 1]] - xyz[tris[:, 0]],
                                                         xyz[tris[:, 2]] - xyz[tris[:, 0]]), axis=1).sum())
    except (IndexError, ValueError):
        model_area = 0.0
    tri_dir = ws / "constant" / "triSurface"
    n = 0
    for e in entries:
        ids, sigs = [str(f) for f in e["faces"]], list(e.get("sigs") or [])
        picked, missing = [], []
        for i, fid in enumerate(ids):
            k = names.index(_norm_id(fid)) if _norm_id(fid) in names else -1
            sel = tris[lab == k] if k >= 0 else np.zeros((0, 3), dtype=np.int64)
            if not len(sel):
                missing.append(fid)
                continue
            a, b, c = xyz[sel[:, 0]], xyz[sel[:, 1]], xyz[sel[:, 2]]
            ar = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
            area = float(ar.sum())
            cen = ((a + b + c) / 3.0 * ar[:, None]).sum(0) / max(area, 1e-300)
            if not _same(area, cen, sigs[i] if i < len(sigs) else None, diag):
                missing.append(fid)
                continue
            picked.append(np.stack([a, b, c], 1))
        if not picked:
            out[str(e["id"])] = {"gone": f"{e.get('where') or 'the faces you picked'} is not on this model as picked"}
            continue
        t = np.concatenate(picked)
        n += 1
        name = f"{PICKED}{n}"
        try:
            from meshpipeline.cad.stl_io import _write_solid
            tri_dir.mkdir(parents=True, exist_ok=True)
            with (tri_dir / f"{name}.stl").open("w") as fh:
                _write_solid(fh, name, [tuple(map(tuple, tr)) for tr in t])
        except OSError as exc:
            out[str(e["id"])] = {"gone": f"{e.get('where')}: its surface could not be written ({type(exc).__name__})"}
            continue
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        here = float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
        out[str(e["id"])] = {"file": name, "where": str(e.get("where") or ""), "faces": ids, "area_m2": here,
                             **({"model_share": min(1.0, here / model_area)} if model_area > 0 else {}),
                             **({"missing": missing} if missing else {})}
    _save(ws, out)
    return out


def _save(ws: Path, out: dict) -> None:
    try:
        (ws / FILE).write_text(json.dumps(out, indent=1))
    except OSError:
        logger.warning("picked faces: %s not written", FILE)


def read(ws) -> dict:
    try:
        got = json.loads((Path(ws) / FILE).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


#: the file units a CAD may be in, as a factor on millimetres (mm, m, inch, cm, micrometre)
_UNITS = (1.0, 1000.0, 25.4, 10.0, 0.001)


def framed(ws, box) -> dict:
    """CFDREQ-34: the picked places in the staged surface's frame. The first time, each picked_<n>.stl is scaled by
    the file unit that puts its triangles inside the staged surface's box ([min, max] metres, within 2% of its
    diagonal) - millimetres first; a place that fits no unit is told as gone (another frame), never moved. Returns
    read(ws) after it. Never raises."""
    ws = Path(ws)
    got = read(ws)
    if not got or got.get("_framed") or box is None:
        return {k: v for k, v in got.items() if not k.startswith("_")}
    try:
        lo, hi = np.asarray(box[0], dtype=float).reshape(3), np.asarray(box[1], dtype=float).reshape(3)
        tol = 0.02 * float(np.linalg.norm(hi - lo))
    except (TypeError, ValueError, IndexError):
        return {k: v for k, v in got.items() if not k.startswith("_")}
    # the unit: the one that makes the whole model nearest the staged one in size first
    mb = got.get("_model_box")
    units = list(_UNITS)
    if isinstance(mb, list) and len(mb) == 2:
        d_model = float(np.linalg.norm(np.asarray(mb[1], dtype=float) - np.asarray(mb[0], dtype=float)))
        d_staged = float(np.linalg.norm(hi - lo))
        if d_model > 0 and d_staged > 0:
            # every unit stays a candidate: an internal flow's staged fluid can be far smaller than the solid
            # (a small bore in a big block: 00139975, 00188096 were told "another frame" when only units within a
            # factor 2 were tried)
            units = sorted(_UNITS, key=lambda u: abs(math.log(d_staged / (d_model * u))))
    for rid, r in list(got.items()):
        if not isinstance(r, dict) or not r.get("file"):
            continue
        t = triangles(ws, r["file"])
        fit = next((u for u in units if t is not None and np.all(t.reshape(-1, 3) * u >= lo - tol)
                    and np.all(t.reshape(-1, 3) * u <= hi + tol)), None)
        if fit is None:
            got[rid] = {"gone": f"{r.get('where') or 'the faces you picked'}: the faces do not lie on the staged model "
                                "(another frame)"}
            continue
        if fit != 1.0:
            from meshpipeline.cad.stl_io import _write_solid
            with (ws / "constant" / "triSurface" / f"{r['file']}.stl").open("w") as fh:
                scaled = np.asarray(t, dtype=float) * float(fit)
                _write_solid(fh, r["file"], [tuple(map(tuple, tr)) for tr in scaled])
            r["area_m2"] = float(r.get("area_m2") or 0.0) * fit * fit
        r["unit_factor"] = fit
    got["_framed"] = True
    _save(ws, got)
    return {k: v for k, v in got.items() if not k.startswith("_")}


def triangles(ws, name: str) -> np.ndarray | None:
    """(m, 3, 3) metres of picked_<n>.stl, or None."""
    p = Path(ws) / "constant" / "triSurface" / f"{name}.stl"
    try:
        v = [list(map(float, ln.split()[1:4])) for ln in p.read_text().splitlines() if ln.strip().startswith("vertex")]
    except OSError:
        return None
    return np.asarray(v, dtype=float).reshape(-1, 3, 3) if v and len(v) % 3 == 0 else None


def _samples(t: np.ndarray, h: float) -> np.ndarray:
    """Points over the triangles no farther apart than `h` (each triangle split into k^2 by its longest edge)."""
    pts = []
    for a, b, c in t:
        e = max(np.linalg.norm(b - a), np.linalg.norm(c - b), np.linalg.norm(a - c))
        k = int(min(200, max(1, math.ceil(e / max(h, 1e-12)))))
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        m = (i + j) <= k
        u, v = i[m] / k, j[m] / k
        pts.append(a + u[:, None] * (b - a) + v[:, None] * (c - a))
    return np.concatenate(pts) if pts else np.zeros((0, 3))


def _boundary(case: Path) -> dict[str, tuple[int, int, str]]:
    try:
        t = (case / "constant" / "polyMesh" / "boundary").read_text(errors="replace")
    except OSError:
        return {}
    out = {}
    for name, blk in re.findall(r"(\w[\w.:-]*)\s*\{([^}]*)\}", t):
        nf, sf, ty = re.search(r"nFaces\s+(\d+)", blk), re.search(r"startFace\s+(\d+)", blk), re.search(
            r"\btype\s+(\w+)", blk)
        if nf and sf:
            out[name] = (int(sf.group(1)), int(nf.group(1)), ty.group(1) if ty else "")
    return out


def measure(case, *, picked: str | None = None, patches=None, size_m: float | None = None) -> dict | None:
    """CFDREQ-35: the wall cells the built mesh has on a place - the boundary faces lying on picked_<n> (within half
    the size asked of its triangles), or the faces of the named patches - as {"median_m", "p90_m", "n_faces"} (the
    square root of each face's area, the face's own cell size along the wall). None when not measurable."""
    from meshpipeline.engines import cfd_cell_measures as CM

    case = Path(case)
    bnd = _boundary(case)
    if not bnd:
        return None
    try:
        m = CM.read(case)
    except Exception:  # noqa: BLE001 - not measured, never a failed run
        return None
    if m is None:
        return None
    t: np.ndarray | None = None
    if picked:
        t = triangles(case, picked)
        if t is None:
            return None
        rows = [(s, k) for s, k, ty in bnd.values() if ty == "wall"] or [(s, k) for s, k, _t in bnd.values()]
    else:
        want = {str(p) for p in patches or ()}
        rows = [(s, k) for nm, (s, k, ty) in bnd.items() if nm in want or (WALL_PATCHES in want and ty == "wall")]
    idx = np.concatenate([np.arange(s, s + k) for s, k in rows]) if rows else np.zeros(0, dtype=np.int64)
    if not len(idx):
        return None
    P, V, first, size = m["points"], m["verts"], m["start"], m["size"]
    sub = {"points": P, "verts": np.concatenate([V[first[f]:first[f] + size[f]] for f in idx]),
           "size": size[idx]}
    sub["start"] = np.cumsum(sub["size"]) - sub["size"]
    ctr, Sf = CM._faces(sub)
    w = np.sqrt(np.linalg.norm(Sf, axis=1))
    if t is not None:
        from scipy.spatial import cKDTree

        tol = 0.5 * float(size_m) if size_m else 0.5 * float(np.median(w))
        tol = max(tol, 0.25 * float(np.median(w)))
        # samples no farther apart than half the reach, but never more than MAX_SAMPLES of them (a large face asked
        # fine: the elbow's 0.58 m2 bend at 0.5 mm was 20 M points and half an hour of the report)
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        area = float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
        h = max(0.5 * tol, math.sqrt(2.0 * area / MAX_SAMPLES))
        tol = max(tol, h)
        tree = cKDTree(_samples(t, h))
        d, _i = tree.query(ctr, distance_upper_bound=tol)
        on = np.isfinite(d)
        w = w[on]
    if len(w) < MIN_FACES:
        return None
    return {"median_m": float(np.median(w)), "p90_m": float(np.quantile(w, 0.9)), "n_faces": int(len(w))}
