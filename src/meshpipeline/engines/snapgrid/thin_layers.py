# Responsibility: Put each thin layer the placement carries as a resistance (cad/ingest/ecxml_films) onto the mesh: tag the interface faces over its footprint, give them their own mappedWall pair, and write the temperature-coupling entries an OpenFOAM conjugate solve reads.
# Owns: the face tags, the per-interface resistances (with the grown neighbour's share taken off), the include file per region (both OpenFOAM dialects) and the report rows.
# Boundaries: numpy on the topology the mesher already holds; it changes no cell and no point. Which layers are resistances is ecxml_films'; the patches are written by boxmesh.write_regions.
# Collaborates with: engines/snapgrid/mesher.py (the caller), engines/snapgrid/boxmesh.py (write_regions takes the tags), cad/ingest/ecxml_films.py (the rule).
"""Thin-layer resistances on the snap-grid mesh.

A layer carried as a resistance has no cells: its neighbours meet on one plane. The faces where
they meet over the layer's footprint become their own mappedWall pair, `<a>_to_<b>_via_<layer>`,
and each region gets `constant/<region>/thinLayerResistances`, the boundary entries that make an
OpenFOAM conjugate solve put the layer's resistance on exactly those faces:

* OpenFOAM 11 (Foundation): `coupledTemperature` with `thicknessLayers` / `kappaLayers`;
* OpenFOAM v2412 (ESI): `compressible::turbulentTemperatureRadCoupledMixed` with the same lists.

Both read only their own side's layers and tie their wall temperature to the neighbour's across
1 / R, so the same layers go on both sides, the full R on each (OpenFOAM 11's
coupledTemperatureFvPatchScalarField.C; v2412's turbulentTemperatureRadCoupledMixed). One
equivalent layer is written - thickness the stack's, conductivity thickness / R_applied - so the
resistance is exactly R_applied: the layers' own sum(t/k) less what the neighbour that grew into
their place now adds (cad/ingest/ecxml_films.applied_resistance). Include it inside the
boundaryField of 0/<region>/T:

    #include "$FOAM_CASE/constant/<region>/thinLayerResistances"

after the region's own interface entries (a later entry for a patch replaces an earlier one).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

INCLUDE_NAME = "thinLayerResistances"
#: The longest layer part of a patch name (the patch is <a>_to_<b>_via_<this>).
MAX_TAG_CHARS = 40


def _faces_geometry(topo, points: np.ndarray, keys: np.ndarray, faces: np.ndarray):
    """(lo, hi, area) of each of `faces` (internal faces): their corner bounds and their area
    (every face of the snap grid is an axis-aligned rectangle)."""
    off = topo.face_off
    sizes = (off[faces + 1] - off[faces]).astype(np.int64)
    if not len(faces):
        z = np.zeros((0, 3))
        return z, z, np.zeros(0)
    starts = np.repeat(off[faces].astype(np.int64), sizes)
    first = np.concatenate(([0], np.cumsum(sizes)[:-1]))
    within = np.arange(int(sizes.sum()), dtype=np.int64) - np.repeat(first, sizes)
    xyz = points[np.searchsorted(keys, topo.face_pts[starts + within])]
    lo = np.minimum.reduceat(xyz, first, axis=0)
    hi = np.maximum.reduceat(xyz, first, axis=0)
    ext = hi - lo
    flat = np.argmin(ext, axis=1)
    area = np.prod(np.where(np.arange(3)[None, :] == flat[:, None], 1.0, ext), axis=1)
    return lo, hi, area


def tag_faces(placement, topo, points: np.ndarray, keys: np.ndarray, cell_zone: np.ndarray,
              tol: float) -> tuple[np.ndarray | None, list[tuple[int, ...]]]:
    """The thin-layer tag of every internal face (-1: none) and, per tag, the layers (indices into
    placement.records["thin_layers"]) whose footprint the face lies in. Only faces between two
    regions on a layer's plane are tagged."""
    rows = placement.records.get("thin_layers") or []
    if not rows:
        return None, []
    tags = np.full(len(topo.owner), -1, dtype=np.int64)
    cross = np.flatnonzero(cell_zone[topo.owner] != cell_zone[topo.neighbour])
    lo, hi, _area = _faces_geometry(topo, points, keys, cross)
    centre = (lo + hi) / 2
    combos: list[tuple[int, ...]] = []
    index: dict[tuple[int, ...], int] = {}
    cur = np.full(len(cross), -1, dtype=np.int64)
    for f, row in enumerate(rows):
        a = "xyz".index(row["axis"])
        lat = [i for i in range(3) if i != a]
        box = row["box_m"]
        on = (np.abs(lo[:, a] - row["plane_m"]) <= tol) & (np.abs(hi[:, a] - row["plane_m"]) <= tol)
        for i in lat:
            on &= (centre[:, i] > box["min"][i]) & (centre[:, i] < box["max"][i])
        hit = np.flatnonzero(on)
        if not len(hit):
            continue
        olds, inv = np.unique(cur[hit], return_inverse=True)
        new = np.empty(len(olds), dtype=np.int64)
        for j, old in enumerate(olds.tolist()):
            combo = (combos[old] if old >= 0 else ()) + (f,)
            if combo not in index:
                index[combo] = len(combos)
                combos.append(combo)
            new[j] = index[combo]
        cur[hit] = new[inv]
    tags[cross] = cur
    return (tags if len(combos) else None), combos


def tag_names(placement, combos: list[tuple[int, ...]]) -> list[str]:
    """The patch-name part of each tag: the layer's own name (the layers', in a stack), made a
    safe word, unique."""
    from meshpipeline.contracts.patch_names import mesh_safe

    rows = placement.records["thin_layers"]
    out: list[str] = []
    seen: set[str] = set()
    for combo in combos:
        raw = "_".join(mesh_safe(str(rows[f]["name"]), fallback="layer") for f in combo)
        base = raw[:MAX_TAG_CHARS].strip("_") or "layer"
        cand, k = base, 1
        while cand.casefold() in seen or "_to_" in cand:
            k += 1
            cand = f"{base.replace('_to_', '_')[:MAX_TAG_CHARS - 4]}_{k}"
        seen.add(cand.casefold())
        out.append(cand)
    return out


def interfaces(placement, topo, points: np.ndarray, keys: np.ndarray, cell_zone: np.ndarray,
               zone_names: list[str], zone_parts: dict[int, int], tags: np.ndarray | None,
               combos: list[tuple[int, ...]], names: list[str], tol: float) -> list[dict]:
    """One row per (region pair, tag): the patches, the faces and their area, the layers, and the
    resistance each side carries. `zone_parts`: zone index -> part index (air zones absent)."""
    from meshpipeline.cad.ingest.ecxml_films import applied_resistance

    if tags is None:
        return []
    rows = placement.records["thin_layers"]
    tagged = np.flatnonzero(tags >= 0)
    lo, hi, area = _faces_geometry(topo, points, keys, tagged)
    zo, zn = cell_zone[topo.owner[tagged]], cell_zone[topo.neighbour[tagged]]
    za, zb = np.minimum(zo, zn), np.maximum(zo, zn)
    key = np.stack([za, zb, tags[tagged]], axis=1)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    inv = np.asarray(inv).ravel()
    out = []
    for g, (a_, b_, t_) in enumerate(uniq.tolist()):
        films = [rows[f] for f in combos[t_]]
        axis = "xyz".index(films[0]["axis"])
        plane = films[0]["plane_m"]

        sa, ka = _side(placement, zone_parts.get(a_), axis, plane, tol)
        sb, kb = _side(placement, zone_parts.get(b_), axis, plane, tol)
        # an air side is whichever the solid side is not
        if not sa:
            sa = "below" if sb == "above" else "above"
        if not sb:
            sb = "below" if sa == "above" else "above"
        k_lo = ka if sa == "below" else kb
        k_hi = kb if sa == "below" else ka
        grown_below = max(float(f.get("grown_below_m") or 0.0) for f in films)
        grown_above = max(float(f.get("grown_above_m") or 0.0) for f in films)
        own, applied = applied_resistance(films, k_lo, k_hi, grown_below, grown_above)
        added = ((grown_below / k_lo if k_lo and grown_below > 0 else 0.0)
                 + (grown_above / k_hi if k_hi and grown_above > 0 else 0.0))
        thick = sum(float(f["thickness_m"]) for f in films)
        sel = inv == g
        na, nb = zone_names[a_], zone_names[b_]
        out.append({
            "a": na, "b": nb, "tag": names[t_],
            "patch_a": f"{na}_to_{nb}_via_{names[t_]}", "patch_b": f"{nb}_to_{na}_via_{names[t_]}",
            "faces": int(sel.sum()), "area_m2": float(area[sel].sum()),
            "layers": [f["label"] for f in films], "axis": films[0]["axis"], "plane_m": plane,
            "thickness_m": thick, "R_layers_m2K_W": own, "R_applied_m2K_W": applied,
            "R_taken_off_m2K_W": own - applied,
            "R_stack_excess_m2K_W": max(0.0, added - own),
            "kappa_equivalent_W_mK": (thick / applied) if applied > 0 else None,
            "grown_below_m": grown_below, "grown_above_m": grown_above})
    return out


def _side(placement, k: int | None, axis: int, plane: float, tol: float
          ) -> tuple[str, float | None]:
    """Where a region lies against a layer's plane ('below' | 'above'; '' for air) and its
    conductivity through the layer's axis (None for air)."""
    from meshpipeline.cad.ingest.ecxml_films import _neighbour_k

    if k is None:
        return "", None
    p = placement.parts[k]
    where = "above" if p.box.lo[axis] >= plane - tol else "below"
    return where, _neighbour_k(placement.model.materials.get(p.material or ""), axis)


def _entry(patch: str, row: dict, dialect: str, fluid: bool) -> str:
    lines = [f"{patch}", "{"]
    if dialect == "foundation":
        lines.append("    type            coupledTemperature;")
        lines.append("    Tnbr            T;")
    else:
        lines.append("    type            compressible::turbulentTemperatureRadCoupledMixed;")
        lines.append("    Tnbr            T;")
        lines.append(f"    kappaMethod     {'fluidThermo' if fluid else 'solidThermo'};")
        lines.append("    qrNbr           none;")
        lines.append("    qr              none;")
    if row["R_applied_m2K_W"] > 0:
        lines.append(f"    thicknessLayers ({row['thickness_m']:.17g});")
        lines.append(f"    kappaLayers     ({row['kappa_equivalent_W_mK']:.17g});")
    lines.append("    value           $internalField;")
    lines.append("}")
    return "\n".join(lines)


def write_includes(case: Path, rows: list[dict], dialect: str, fluids: set[str]) -> list[str]:
    """constant/<region>/thinLayerResistances for every region with a thin-layer interface, in the
    reader's dialect. Returns the files written (relative to the case)."""
    by_region: dict[str, list[tuple[str, dict]]] = {}
    for r in rows:
        by_region.setdefault(r["a"], []).append((r["patch_a"], r))
        by_region.setdefault(r["b"], []).append((r["patch_b"], r))
    written = []
    for region, items in sorted(by_region.items()):
        head = ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
                f"    class       dictionary;\n    location    \"constant/{region}\";\n"
                f"    object      {INCLUDE_NAME};\n}}\n\n"
                "// Thin layers carried as thermal resistances (Hexera snap-grid mesh of an ECXML\n"
                "// model). Each patch below lies where a layer too thin to mesh with sound cells\n"
                "// was in the file; the layer is one equivalent layer of the stack's thickness\n"
                "// whose resistance is R_applied (m2K/W). The partner patch carries the same\n"
                "// layer: OpenFOAM reads each side's own. Include inside boundaryField of\n"
                f"// 0/{region}/T, after this region's own interface entries:\n"
                f"//     #include \"$FOAM_CASE/constant/{region}/{INCLUDE_NAME}\"\n\n")
        body = []
        for patch, r in sorted(items, key=lambda x: x[0]):
            body.append(f"// {', '.join(r['layers'][:6])}: {r['thickness_m']:.4g} m, "
                        f"R_layers {r['R_layers_m2K_W']:.6g}, R_applied {r['R_applied_m2K_W']:.6g} "
                        f"m2K/W over {r['area_m2']:.6g} m2")
            body.append(_entry(patch, r, dialect, region in fluids))
        path = Path(case) / "constant" / region / INCLUDE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(head + "\n\n".join(body) + "\n")
        written.append(str(path.relative_to(case)))
    return written


def report_lines(rows: list[dict], placement) -> list[str]:
    """The build report's words for the thin layers on the mesh, one line, plus the layers the rule
    looked at and kept meshed."""
    from meshpipeline.cad.ingest.ecxml_build import length_words

    out = []
    if rows:
        area: dict[str, float] = {}
        for r in rows:
            for name in r["layers"]:
                area[name] = area.get(name, 0.0) + r["area_m2"]
        films = {f["label"]: f for f in placement.records["thin_layers"]}
        short = [f"{n}: {area[n] / f['footprint_m2'] * 100:.4g}% of its footprint"
                 for n, f in films.items() if f.get("footprint_m2") and n in area
                 and abs(area[n] / f["footprint_m2"] - 1) > 1e-6]
        taken = [r for r in rows if r["R_taken_off_m2K_W"] > 0]
        clamped = [r for r in rows if r["R_applied_m2K_W"] == 0 and r["R_layers_m2K_W"] > 0]
        line = (f"Thin-layer interfaces: {len(rows)} interface patch pair(s) carry "
                f"{len(films)} layer(s) as a resistance (OpenFOAM thicknessLayers / kappaLayers, "
                "written per region in constant/<region>/thinLayerResistances): " + "; ".join(
                    f"{r['patch_a']} ({', '.join(r['layers'][:3])}, "
                    f"{length_words(r['thickness_m'])}, R {r['R_applied_m2K_W']:.3g} m2K/W, "
                    f"{r['area_m2'] * 1e6:.4g} mm2)" for r in rows[:6])
                + (f", and {len(rows) - 6} more" if len(rows) > 6 else "") + ".")
        if taken:
            line += (f" On {len(taken)} of them the neighbour that grew into the layer's place "
                     "adds its own resistance, which is taken off the layer's.")
        if clamped:
            worst = max(r["R_stack_excess_m2K_W"] for r in clamped)
            line += (f" On {len(clamped)} the grown neighbour adds more than the layer had (it "
                     "conducts less than the layer), so no resistance is set there; the stack is "
                     f"at most {worst:.3g} m2K/W more resistive than the file's there.")
        if short:
            line += (" Not every layer's footprint lies between two regions: " + "; ".join(short[:4])
                     + " (the rest borders a part the layer overlaps, or the domain).")
        out.append(line)
    kept = placement.records.get("thin_layers_meshed") or []
    if kept:
        out.append("Thin layers kept meshed although their cells are very flat (each still has "
                   "cells through it): " + "; ".join(
                       f"{r['label']} ({length_words(r['thickness_m'])}, cells about "
                       f"{r['aspect_if_meshed']:.0f} times wider than thick: {r['why']})"
                       for r in kept[:6]) + (f", and {len(kept) - 6} more" if len(kept) > 6 else "")
                   + ".")
    return out


__all__ = ["INCLUDE_NAME", "interfaces", "report_lines", "tag_faces", "tag_names",
           "write_includes"]
