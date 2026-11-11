#!/usr/bin/env python3
# Responsibility: Write a viewer payload whose surface is the delivered mesh EXACTLY, by merging
# coplanar faces losslessly instead of clustering them to a triangle budget.
# Boundaries: it reads one sidecar bundle and one existing payload, and writes one file. It meshes
# nothing, judges nothing, and copies every claim about the MESH from the payload that already
# states it rather than recomputing a second opinion.
"""Build an exact-surface payload from a sidecar bundle.

WHY THERE IS A SECOND PAYLOAD AND NOT A REPLACEMENT. The lossy one is the evidence. Measured
against it on the delivered plate: 2,635,540 triangles against 5,487,927, 76.2 MB against 152.6,
5.0 s to load against 18.7, and an area error of 1.0e-12 against 1.35%. None of those numbers can
be checked once the thing they are compared with is gone.

They look almost the same on screen - 0.59% to 0.76% of canvas pixels differ at an identical
camera - so this is a cost and exactness result, not a visible-detail one. Keeping both pages is
what stops that being overstated later.

WHAT IS COPIED AND WHAT IS REBUILT. Everything that describes the MESH is copied from the source
payload unchanged: `engine`, `quality` (checkMesh's own numbers), `review`, `quality_fields`,
`parts` (the roster), `cell_stats`. Only the DRAWING changes, so only `patches` is rebuilt, and
`reduced` - the budget's record of what it threw away - is replaced by `exact`, which records that
nothing was thrown away and what that cost in polygons.

WHICH PATCHES. Exactly the ones the source payload drew, by name. The delivered plate has
`solid/solid_to_fluid` and `solid/solid_to_chip` in the mesh and NOT in the payload, because they
are the far side of the conjugate interface and sit exactly on top of `fluid/fluid_to_solid` and
`chip/chip_to_solid`; two coincident surfaces fight for the same depth and neither reads. Deciding
that again here, differently, would put 1.2 M polygons of invisible back-face into the payload and
make the two pages incomparable. So the source decides.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("build_exact_payload")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def regions_of(sidecar: Path) -> list[tuple[str, Path]]:
    """(region, polyMesh) for every region in the bundle, by the glob a workspace uses.

    `detail_window.delivered_regions` finds a region exactly this way, which is why a sidecar is
    shaped like a workspace; a bundle laid out any other way is simply not found.
    """
    out = []
    for pm in sorted(sidecar.glob("constant/*/polyMesh")):
        out.append((pm.parent.name, pm))
    if not out:
        pm = sidecar / "constant" / "polyMesh"
        if pm.is_dir():
            out.append(("", pm))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sidecar", required=True, type=Path,
                    help="the bundle, shaped constant/<region>/polyMesh/boundaryIndex")
    ap.add_argument("--source", required=True, type=Path,
                    help="the existing payload. Its mesh claims and its patch LIST are copied.")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--no-quality", action="store_true",
                    help="leave the per-polygon quality arrays out, for a smaller file")
    ap.add_argument("--all-patches", action="store_true",
                    help="draw every patch in the mesh, not only the ones the source drew. This "
                         "puts the far side of a conjugate interface into the payload; see the "
                         "module docstring for why that is normally wrong.")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from meshpipeline.render.exact_surface import exact_surface

    src = json.loads(a.source.read_text())
    ssurf = src["surface"]
    want = [str(p["name"]) for p in ssurf.get("patches") or []]
    if not want:
        raise SystemExit("%s draws no patches, so there is no list to follow" % a.source)
    logger.info("source %s draws %d patches: %s", a.source.name, len(want), ", ".join(want))

    rows: list[dict] = []
    prov = {"patches": {}, "regions": {}}
    t_all = time.time()
    for region, pm in regions_of(a.sidecar):
        if not (pm / "boundaryIndex").is_dir():
            logger.info("  %s: no boundary index, skipped", region or "(single)")
            continue
        t0 = time.time()
        got, totals = exact_surface(pm, region=region, with_quality=not a.no_quality)
        kept = [r for r in got if a.all_patches or r["name"] in want]
        dropped = [r["name"] for r in got if r not in kept]
        logger.info("  %-6s %s faces -> %s polygons in %.1f s   area error %.2e%s",
                    region or "(single)", format(totals["delivered_faces"], ","),
                    format(totals["drawn_polygons"], ","), time.time() - t0,
                    totals["area_error"],
                    ("   not drawn: " + ", ".join(dropped)) if dropped else "")
        rows.extend(kept)
        prov["regions"][region or "mesh"] = {k: v for k, v in totals.items() if k != "patches"}

    have = {r["name"] for r in rows}
    missing = [n for n in want if n not in have]
    if missing:
        # NAMED, not skipped in silence. A page that drew the coolant and was handed the metal
        # would read the metal as the coolant.
        raise SystemExit("the source draws %s and this build produced no such patch - the sidecar "
                         "and the payload are not the same mesh" % ", ".join(missing))

    patches = []
    n_poly = n_face = 0
    for r in rows:
        row = {"name": r["name"], "type": r["type"],
               # `face_count` IS THE DRAWN COUNT IN A SERVED PAYLOAD, not the mesh's. That is the
               # convention `fit_to_budget` already set and three things in viewer.js depend on
               # it: the quality ramp is applied only when the value array's length equals
               # face_count (viewer.js:1589), the whole-screen edge budget is sized from it
               # (viewer.js:1600), and the parts roster prints it (viewer.js:548). The mesh's own
               # count goes where the lossy route puts it, in the provenance block below, which is
               # what viewer.js:1133 reads to print "of N faces".
               "face_count": int(r["poly_count"]),
               "poly_count": int(r["poly_count"]),
               "delivered_faces": int(r["face_count"]),
               "area_m2": r["area_m2"],
               "points_b64": b64(r["points"]),
               "polys_b64": b64(r["polys"])}
        patches.append(row)
        prov["patches"][r["name"]] = r["exact"]
        n_poly += int(r["poly_count"])
        n_face += int(r["face_count"])

    # triangles, counted off the poly streams themselves rather than assumed from the polygon
    # count: these streams carry 4 to 8 corner faces and an n-gon fans to n - 2
    import numpy as np
    tri = 0
    for r in rows:
        vals = np.frombuffer(r["polys"], dtype=np.uint32)
        if not vals.size:
            continue
        # the corner counts sit at the stream's own stride, found by walking the first of each
        at = 0
        starts = []
        while at < vals.size:
            starts.append(at)
            at += int(vals[at]) + 1
        k = vals[np.asarray(starts, dtype=np.int64)].astype(np.int64)
        tri += int(np.maximum(k - 2, 0).sum())

    surf = dict(ssurf)
    surf["patches"] = patches
    surf.pop("reduced", None)

    # PER-POLYGON COLOURS, in the keys viewer.js already decodes, so the whole part gets a heatmap
    # for the first time. The budget route cannot carry one - it clusters by position and keeps no
    # record of which delivered faces went under a drawn one - and `face_quality` drops the arrays
    # on a reduced mesh for exactly that reason. The lossless merge knows, because the merge is
    # what put them there. The value is the WORST of the faces a polygon replaced, which is an
    # upper bound and not the value of any one face; `faces_per_polygon_b64` is how the probe
    # says so. Same thing `cfmesh_conjugate/viewer._attach_exact_colours` does on the product path.
    keymap = {"non_ortho": "non_ortho_b64", "skewness": "skewness_b64",
              "aspect_ratio": "aspect_ratio_b64"}
    qf = dict(surf.get("quality_fields") or {})
    per: dict = {}
    for r in rows:
        got = (r.get("quality") or {}).get("values") or {}
        n = int(r["poly_count"])
        one: dict = {}
        for metric, key in keymap.items():
            v = (got.get(metric) or {}).get("worst")
            if v and len(v) == n * 4:
                one[key] = b64(v)
        fp = r.get("faces_per_polygon")
        if one and fp and len(fp) == n * 4:
            one["faces_per_polygon_b64"] = b64(fp)
        if one:
            per[r["name"]] = one
    if per and qf.get("metrics"):
        qf["patches"] = per
        qf["per_polygon"] = ("a merged polygon carries the WORST of the delivered faces it "
                             "replaced, and faces_per_polygon says how many that was; a face "
                             "kept whole carries its own value")
        surf["quality_fields"] = qf
        logger.info("  per-polygon colours on %d patch(es): %s", len(per), ", ".join(sorted(per)))
    surf["exact"] = {
        "delivered_faces": n_face,
        "drawn_polygons": n_poly,
        "drawn_triangles": tri,
        "collapse": (n_face / n_poly) if n_poly else 0.0,
        "lossless": True,
        "how": "coplanar axis-aligned faces merged into the rectangles they already tile; "
               "adjacency by exact point equality, no tolerance",
        "area_error": max((v.get("area_error") or 0.0) for v in prov["regions"].values())
        if prov["regions"] else 0.0,
        "rect_tol": next(iter(prov["regions"].values())).get("rect_tol")
        if prov["regions"] else None,
        "patches": prov["patches"],
        "regions": prov["regions"],
        "was": {k: ssurf.get("reduced", {}).get(k)
                for k in ("delivered_faces", "drawn_triangles", "resolution_m")},
    }
    out = dict(src)
    out["surface"] = surf
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out))

    was = ssurf.get("reduced") or {}
    logger.info("")
    logger.info("%s", "=" * 78)
    logger.info("  delivered boundary faces drawn   %s", format(n_face, ","))
    logger.info("  exact polygons                   %s", format(n_poly, ","))
    logger.info("  exact triangles                  %s", format(tri, ","))
    if was.get("drawn_triangles"):
        logger.info("  the lossy payload's triangles    %s   (%.2fx more, at %.1f um)",
                    format(int(was["drawn_triangles"]), ","),
                    was["drawn_triangles"] / tri if tri else 0.0,
                    (was.get("resolution_m") or 0.0) * 1e6)
    logger.info("  collapse                         %.1f : 1", (n_face / n_poly) if n_poly else 0)
    logger.info("  worst region area error          %.2e", surf["exact"]["area_error"])
    logger.info("  wrote %s  (%.1f MB)  in %.1f s", a.out, a.out.stat().st_size / 1e6,
                time.time() - t_all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
