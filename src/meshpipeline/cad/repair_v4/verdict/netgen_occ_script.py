# Responsibility: Mesh one STEP file with Netgen's OCC interface and write the volume mesh, in the isolated netgen interpreter.
# Owns: the netgen-side half of the NetgenOCC panel member; it prints exactly one JSON line.
# Boundaries: runs under /opt/netgen-env/bin/python, which has netgen and NOT meshpipeline, gmsh or OCP -
#             import nothing from the app here. Quality is measured by the caller, in gmsh.
"""usage: netgen_occ_script.py STEP OUT_MSH [MAXH_DIVISOR [glue|noglue [SIZING_JSON]]]   (stdout: one JSON object)

SIZING_JSON is thin_sizing's plan plus a "netgen" block (closeedgefac, curvaturesafety, minh in mm)."""

import json
import sys


def _emit(**fields) -> None:
    print("NETGEN_RESULT " + json.dumps(fields), flush=True)


def _write_msh2(mesh, out_msh: str) -> None:
    """Write the volume tets as Gmsh MSH 2.2, with gmsh's node order.

    Netgen's own `Export("Gmsh2 Format")` keeps Netgen's tet orientation, which is the mirror of
    gmsh's: every element then reads as inverted (minSICN about -1) in a perfectly good mesh, and the
    quality floor would fail every file. Swapping vertices 2 and 3 converts the convention uniformly;
    a truly inverted tet still reads negative afterwards.
    """
    points = list(mesh.Points())
    tets = [[int(v.nr) for v in el.vertices] for el in mesh.Elements3D()]
    with open(out_msh, "w") as fh:
        fh.write(f"$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n{len(points)}\n")
        for i, pt in enumerate(points, start=1):
            x, y, z = pt.p
            fh.write(f"{i} {x!r} {y!r} {z!r}\n")
        fh.write(f"$EndNodes\n$Elements\n{len(tets)}\n")
        for i, (a, b, c, d) in enumerate(tets, start=1):
            fh.write(f"{i} 4 2 1 1 {a} {b} {d} {c}\n")
        fh.write("$EndElements\n")


def _named_face(geo, message: str) -> dict:
    """When Netgen's error names a face ("...could not be built for face 1"), that face's number and box in the shape
    it was meshing, so the caller can find the face by its box. Best effort: any failure here adds nothing."""
    import re

    match = re.search(r"\bface\s+(\d+)", message)
    if not match:
        return {}
    try:
        number = int(match.group(1))
        (x0, y0, z0), (x1, y1, z1) = geo.shape.faces[number - 1].bounding_box
        return {"face": number, "face_box": [x0, y0, z0, x1, y1, z1]}
    except Exception:
        return {}


def _wall_points(sizing) -> list:
    """thin_sizing's walls on Netgen, which has no field law: (x, y, z, h) at each wall's thin points (its closest
    points and thin samples), where Netgen's local size is held (`MeshingParameters.RestrictH`) and grows from at its
    own grading."""
    out = []
    for wall in (sizing or {}).get("walls") or []:
        out += [(float(p[0]), float(p[1]), float(p[2]), float(wall["h_mm"])) for p in wall.get("points") or []]
    return out


def main(step: str, out_msh: str, maxh_divisor: float = 10.0, glue: bool = True, sizing_path=None) -> None:
    sizing = None
    if sizing_path:
        with open(sizing_path) as fh:
            sizing = json.load(fh)
    try:
        from netgen.occ import Glue, OCCGeometry
    except Exception as exc:  # the interpreter has no netgen: not a finding about the file
        _emit(ok=False, stage="import", error=f"{type(exc).__name__}: {exc}")
        return
    try:
        geo = OCCGeometry(step)
        shape = geo.shape
        solids = list(shape.solids)
    except Exception as exc:
        _emit(ok=False, stage="read", error=f"{type(exc).__name__}: {exc}")
        return
    if not solids:
        _emit(ok=False, stage="read", error="STEP file holds no solids", solids=0)
        return
    (x0, y0, z0), (x1, y1, z1) = shape.bounding_box
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    if not diag > 0.0:
        _emit(ok=False, stage="read", error=f"degenerate bounding box (diagonal {diag})", solids=len(solids))
        return
    maxh = diag / maxh_divisor
    glued = False
    local = (sizing or {}).get("netgen") or {}
    spots = _wall_points(sizing)
    try:
        if glue and len(solids) > 1:
            # Touching solids share faces only if the faces are identified; Glue does that, so the
            # result is one conformal mesh instead of N meshes that happen to sit side by side.
            geo = OCCGeometry(Glue(solids))
            glued = True
        # owner decision 2026-11-26 (thin_sizing): close-edge refinement, the curvature safety and a floor, from the
        # sizing record so they cannot drift from the member's cache key; none of them when no record came
        options = {k: float(local[k]) for k in ("closeedgefac", "curvaturesafety", "minh") if k in local}
        if spots:
            from netgen.meshing import MeshingParameters

            mp = MeshingParameters(maxh=maxh, **options)
            for x, y, z, h in spots:
                mp.RestrictH(x=x, y=y, z=z, h=h)
            mesh = geo.GenerateMesh(mp=mp)
        else:
            mesh = geo.GenerateMesh(maxh=maxh, **options)
    except Exception as exc:
        _emit(ok=False, stage="mesh", error=f"{type(exc).__name__}: {exc}", solids=len(solids), maxh=maxh, diag=diag,
              **_named_face(geo, str(exc)))
        return
    tets = len(mesh.Elements3D())
    if tets == 0:
        _emit(ok=False, stage="mesh", error="Netgen returned a mesh with no volume elements",
              solids=len(solids), maxh=maxh)
        return
    try:
        _write_msh2(mesh, out_msh)
    except Exception as exc:
        _emit(ok=False, stage="export", error=f"{type(exc).__name__}: {exc}", solids=len(solids), maxh=maxh)
        return
    _emit(ok=True, tets=tets, solids=len(solids), glued=glued, maxh=maxh, diag=diag, wall_points=len(spots))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 10.0,
         sys.argv[4] != "noglue" if len(sys.argv) > 4 else True, sys.argv[5] if len(sys.argv) > 5 else None)
