# Responsibility: Phase-4 ground truth for CAD simplification in CFD: for one part, remove each candidate feature on its own, mesh (snappyHexMesh) and solve (simpleFoam, steady incompressible) the original and every simplified part in the same flow case, and record cells saved, mesh and solve seconds, and each flow quantity of interest's change per feature, against a measured mesh-noise floor.
# Boundaries: a developer tool run on the CFD bench VM (dev-raymond-cfd): the OCP geometry side runs in /opt/bench/venv, every OpenFOAM step runs inside the repo's mesh image (docker, OpenFOAM v2412). Nothing in the product imports it. It invents the flow case because the corpus parts come without one.
# Collaborates with: devtools/simplify/harness.py (read_step, recognise, remove, volume, _bbox_of - imported, not copied), docs/superpowers/specs/2026-11-19-cad-simplification-design.md (phase P4), docs/engines/snappy.md, docs/simplification/CFD-NOTES.md (what it measured).
"""ONE FEATURE AT A TIME, MEASURED IN A FLOW.

    python devtools/simplify/cfd_harness.py part.step --out results/part [--mode external|internal]
        [--re 500] [--aoa 8] [--base 14] [--levels 2,4] [--turbulence laminar|kOmegaSST]
        [--jobs 14] [--core-offset 0] [--noise 0.96,0.98,1.02,1.04] [--features id,id]
        [--extra simplified=part__simplified.step]
    python devtools/simplify/cfd_harness.py --report results/part/result.json   # markdown tables

EXTERNAL (default): the part sits in a box tunnel. The box is fixed by the ORIGINAL part's bounding
box and is identical for every variant and every jittered mesh, so the inlet (min face of the flow
axis), outlet (max face) and the four slip sides are the same exact planes in every run - the CFD
form of the FEA lesson that boundary conditions must sit on exact geometric faces. Flow runs along
the part's longest axis; lift is read along its shortest; --aoa turns the part about its middle
axis. QoIs: drag, lift, side force on the part, and the pressure drop inlet-outlet (area averages
over the two exact box faces). Lift and side force are reported as their change per unit drag (a
near-zero force has no useful relative change).

INTERNAL (a part with a through-passage): the fluid is the passage, capped by the part's bounding
box planes (BRepAlgoAPI_Cut of the box by the part, keeping the solids that do not touch the box
corners). Inlet and outlets are the fluid's own planar faces on the box planes - exact geometric
faces, written as separate STL solids that snappy keeps as patches. QoIs: pressure drop inlet to each
outlet and the mass-flow split between outlets (an absolute change of the fraction). A feature whose
faces do not touch the fluid cannot change it; it is recorded as DRY and not solved.

Features: harness.recognise (holes, torus/cylinder blends) plus freeform blends (B-spline faces of
small curvature radius - a tee's junction fillets), removed alone with harness.remove. --extra adds a
whole simplified part from a file (the corpus's __simplified.step), for features remove() cannot do.

Each case runs in its own named container pinned to one core (`--cpuset-cpus`), killed by name on
timeout; every stage is timed and its peak RSS read from /usr/bin/time -v (bind-mounted from the
host: the image has none).

Both: steady incompressible simpleFoam (SIMPLEC), laminar by default, at Re = U * L / nu with U = 1 m/s
and L the diagonal (external) or the inlet hydraulic diameter (internal). The part is scaled by
--scale to metres. snappyHexMesh refines the castellated mesh at the wall from --levels lmin up to
lmax wherever the surface normal turns by more than resolveFeatureAngle inside a cell (curvature-
driven, so a small hole or fillet costs cells as it does in FEA), and at the surface's feature edges.
No prism layers: layer addition is a second source of mesh noise, kept out until the floor is known.

The original is meshed at the base size and at each --noise factor of it: different meshes of the same
continuum problem. Their spread is the NOISE FLOOR; an error below it is not a measurement.
A run is CONVERGED when simpleFoam's residualControl stopped it (initial residuals below --tol-p and
--tol-u) before --iters; every row also records how far each QoI still moved over its last 10% of
iterations.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402  (a sibling script, not a package)

IMAGE = os.environ.get("CFD_IMAGE", "hexera-mesh:cfd")
OF_BASHRC = "/usr/lib/openfoam/openfoam2412/etc/bashrc"
#: how close (x diagonal) a face must lie to a bounding-box plane to be an opening on it. Bnd_Box pads
#: by the shape's tolerance, which does NOT scale with the part (1e-7 m after scaling mm to m):
#: measured on manifold_3port (diagonal 142 mm), 1e-6 x diagonal = 1.4e-7 m found no openings at all
PLANE_TOL = 1e-5
HEADER = "FoamFile {{ version 2.0; format ascii; class {cls}; object {obj}; }}\n"


# ------------------------------------------------------------------------------------------------
# geometry: scale, tessellate, and (internal) build the fluid

def scaled(shape, s: float, rot: tuple | None = None):
    """The shape scaled by s about the origin, then (rot = (axis, degrees, centre)) turned about the
    line through centre along that coordinate axis: the angle of attack."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
    t = gp_Trsf()
    t.SetScale(gp_Pnt(0, 0, 0), s)
    if rot and rot[1]:
        r = gp_Trsf()
        d = [0.0, 0.0, 0.0]
        d[rot[0]] = 1.0
        r.SetRotation(gp_Ax1(gp_Pnt(*(c * s for c in rot[2])), gp_Dir(*d)), math.radians(rot[1]))
        t = r.Multiplied(t)
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def _tessellate(shape, lin: float, ang: float) -> None:
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    BRepMesh_IncrementalMesh(shape, lin, False, ang, True)


def _facets(face) -> list[tuple]:
    """The triangles of one tessellated face, outward-oriented, in world coordinates."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        return []
    tr = loc.Transformation()
    pts = [tri.Node(i).Transformed(tr) for i in range(1, tri.NbNodes() + 1)]
    rev = face.Orientation() == TopAbs_REVERSED
    out = []
    for i in range(1, tri.NbTriangles() + 1):
        a, b, c = tri.Triangle(i).Get()
        if rev:
            b, c = c, b
        out.append(tuple((pts[k - 1].X(), pts[k - 1].Y(), pts[k - 1].Z()) for k in (a, b, c)))
    return out


def write_stl(shape, path: Path, lin: float, ang: float, regions: dict[str, list] | None = None) -> int:
    """ASCII STL of the shape, one `solid` per region (name -> faces); all faces in `part` when no
    regions are given. Returns the triangle count."""
    _tessellate(shape, lin, ang)
    if regions is None:
        regions = {"part": H._faces(shape)[0]}
    n = 0
    with path.open("w") as fh:
        for name, faces in regions.items():
            fh.write(f"solid {name}\n")
            for f in faces:
                for a, b, c in _facets(f):
                    nrm = np.cross(np.subtract(b, a), np.subtract(c, a))
                    ln = np.linalg.norm(nrm)
                    nrm = nrm / ln if ln > 0 else nrm
                    fh.write(f"facet normal {nrm[0]:.6e} {nrm[1]:.6e} {nrm[2]:.6e}\nouter loop\n")
                    for p in (a, b, c):
                        fh.write(f"vertex {p[0]:.9e} {p[1]:.9e} {p[2]:.9e}\n")
                    fh.write("endloop\nendfacet\n")
                    n += 1
            fh.write(f"endsolid {name}\n")
    return n


def _solids(shape):
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    out = []
    while ex.More():
        out.append(TopoDS.Solid_s(ex.Current()))
        ex.Next()
    return out


def fluid_of(part, box_bb, tol: float):
    """INTERNAL: the passage fluid = (bounding box) - part, keeping the largest solid that does not
    reach all six box planes (the exterior air does). Returns (fluid, {patch: faces}) with every face lying on
    a box plane named as an opening, the rest `wall`."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound
    box = BRepPrimAPI_MakeBox(gp_Pnt(*box_bb[:3]), gp_Pnt(*box_bb[3:])).Shape()
    c = BRepAlgoAPI_Cut(box, part)
    c.Build()
    if not c.IsDone():
        return None, {}
    # the exterior air reaches all six box planes; a passage reaches only the planes it opens on.
    # (Measured: a bbox-contains-a-corner test took manifold_3port's passage - which opens on three
    # planes meeting at a corner - for exterior air.)
    keep = []
    for s in _solids(c.Shape()):
        b = H._bbox_of([s])
        planes = sum(abs(b[i] - box_bb[i]) < tol for i in range(6))
        if planes < 6:
            keep.append(s)
    if not keep:
        return None, {}
    # one passage: the largest. A through bolt hole is a second, unconnected "passage" of its own
    keep = [max(keep, key=H.volume)]
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    for s in keep:
        bld.Add(comp, s)
    regions: dict[str, list] = {"wall": []}
    openings = []
    for f in H._faces(comp)[0]:
        fb = H._bbox_of([f])
        plane = None
        for ax in range(3):
            for side, v in ((0, box_bb[ax]), (1, box_bb[ax + 3])):
                if abs(fb[ax] - v) < tol and abs(fb[ax + 3] - v) < tol:
                    plane = (ax, side)
        if plane is None:
            regions["wall"].append(f)
        else:
            openings.append((plane, f))
    # named in a geometric order (plane, then centroid), not in face-enumeration order, so a variant's
    # openings carry the same names as the original's and the case's boundary conditions still apply
    openings.sort(key=lambda pf: (pf[0], tuple(round(c, 6) for c in _face_centroid(pf[1]))))
    for i, (plane, f) in enumerate(openings):
        regions[f"open{i}_{'xyz'[plane[0]]}{('min', 'max')[plane[1]]}"] = [f]
    return comp, regions


def _face_area(f) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(f, g)
    return g.Mass()


def _face_centroid(f) -> tuple:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(f, g)
    c = g.CentreOfMass()
    return (c.X(), c.Y(), c.Z())


def _face_perimeter(f) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.LinearProperties_s(f, g)
    return g.Mass()


def _inside_point(solid, bb, tol: float) -> tuple:
    """A point strictly inside `solid`, away from its walls (for snappy's locationInMesh)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN
    rng = np.random.default_rng(7)
    best, bestd = None, -1.0
    lo, hi = np.array(bb[:3]), np.array(bb[3:])
    for _ in range(4000):
        p = lo + rng.random(3) * (hi - lo)
        cl = BRepClass3d_SolidClassifier(solid, gp_Pnt(*p), tol)
        if cl.State() != TopAbs_IN:
            continue
        d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex(), solid)
        # distance to the solid's boundary: measure to its shell
        dist = d.Value() if d.IsDone() else 0.0
        if dist == 0.0:
            from OCP.TopAbs import TopAbs_SHELL
            from OCP.TopExp import TopExp_Explorer
            ex = TopExp_Explorer(solid, TopAbs_SHELL)
            d2 = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex(), ex.Current())
            dist = d2.Value() if d2.IsDone() else 0.0
        if dist > bestd:
            best, bestd = p, dist
        if bestd > 0 and _ > 400:
            break
    if best is None:
        raise SystemExit("no point found inside the fluid")
    return tuple(float(v) for v in best)


def recognise(shape, diag: float, blend_ratio: float = 0.03):
    """harness.recognise, plus FREEFORM blends it does not see: connected groups of B-spline faces
    whose smallest principal radius of curvature is under blend_ratio x diagonal. A fillet where two
    cylinders meet (a tee's branch junction, inside and out) is a B-spline, not a torus, and the
    bore-side one is the only flow-relevant feature of the synthetic tees."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_REVERSED
    feats, faces, info = H.recognise(shape, diag, blend_ratio=blend_ratio)
    used = {j for f in feats for j in f.faces}
    _, fmap = H._faces(shape)
    adj = H._adjacency(shape, fmap)
    radius, concave = {}, {}
    for i, fi in enumerate(info):
        if i in used or fi.kind != "bspl":
            continue
        u0, u1, v0, v1 = BRepTools.UVBounds_s(faces[i])
        props = BRepLProp_SLProps(BRepAdaptor_Surface(faces[i]), 0.5 * (u0 + u1), 0.5 * (v0 + v1), 2, 1e-9)
        if not props.IsCurvatureDefined():
            continue
        kmax, kmin = props.MaxCurvature(), props.MinCurvature()
        k = kmax if abs(kmax) >= abs(kmin) else kmin
        if faces[i].Orientation() == TopAbs_REVERSED:
            k = -k
        if k and 1.0 / abs(k) < blend_ratio * diag:
            # OCCT: curvature > 0 when the surface bends toward its normal; with the outward normal
            # that is a concave blend (material on the far side)
            radius[i], concave[i] = 1.0 / abs(k), k > 0
    seen: set[int] = set()
    for i in sorted(radius):
        if i in seen:
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k2 in adj[j]:
                if k2 in radius and k2 not in group and concave[k2] == concave[i]:
                    group.add(k2)
                    stack.append(k2)
        seen |= group
        g = sorted(group)
        w = [info[j].area for j in g]
        f = H.Feature(f"fblend{len(feats)}", "freeform-blend", g, max(radius[j] for j in g), concave[i],
                      tuple(np.average([info[j].centroid for j in g], axis=0, weights=w).tolist()))
        f.area = float(sum(w))
        f.bbox = H._bbox_of([faces[j] for j in g])
        feats.append(f)
    return feats, faces, info


# ------------------------------------------------------------------------------------------------
# the flow case

@dataclass
class Case:
    mode: str                  # external / internal
    box: tuple                 # blockMesh box (metres): x0 y0 z0 x1 y1 z1
    flow: int                  # flow axis (external) / inlet normal axis (internal)
    lift: int
    side: int
    U: float
    nu: float
    L: float                   # the Re length
    re: float
    location: tuple            # locationInMesh (metres)
    cofr: tuple
    inlet: str = "inlet"
    outlets: list = field(default_factory=lambda: ["outlet"])
    inlet_normal: tuple = ()   # internal: inward unit normal of the inlet face
    turbulence: str = "laminar"


@dataclass
class MeshSpec:
    h0: float                  # background cell size (metres) before jitter
    lmin: int
    lmax: int
    feature_level: int
    resolve_angle: float = 20.0
    jitter: float = 1.0


def _vec(v) -> str:
    return "(" + " ".join(f"{x:.9g}" for x in v) + ")"


def write_case(cd: Path, case: Case, ms: MeshSpec, stl: Path, tol_p: float, tol_u: float, iters: int,
               patches: list[str]) -> dict:
    """Write the OpenFOAM case into cd: background box, snappy, features, schemes, fields, Allrun."""
    (cd / "system").mkdir(parents=True, exist_ok=True)
    (cd / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    (cd / "0.orig").mkdir(exist_ok=True)
    shutil.copy(stl, cd / "constant" / "triSurface" / "part.stl")
    x0, y0, z0, x1, y1, z1 = case.box
    h = ms.h0 * ms.jitter
    n = [max(1, round((case.box[a + 3] - case.box[a]) / h)) for a in range(3)]
    k = case.flow
    names = {}
    for a in range(3):
        for s in (0, 1):
            if case.mode == "external" and a == k:
                names[(a, s)] = "inlet" if s == 0 else "outlet"
            else:
                names[(a, s)] = "sides"
    fverts = {(0, 0): "(0 4 7 3)", (0, 1): "(2 6 5 1)", (1, 0): "(1 5 4 0)", (1, 1): "(3 7 6 2)",
              (2, 0): "(0 3 2 1)", (2, 1): "(4 5 6 7)"}
    groups: dict[str, list[str]] = {}
    for key, nm in names.items():
        groups.setdefault(nm, []).append(fverts[key])
    bnd = "\n".join(f" {nm} {{ type patch; faces ({' '.join(fs)}); }}" for nm, fs in groups.items())
    (cd / "system" / "blockMeshDict").write_text(
        HEADER.format(cls="dictionary", obj="blockMeshDict") + "convertToMeters 1;\nvertices (\n"
        + "\n".join(_vec(v) for v in [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                                         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)])
        + f"\n);\nblocks ( hex (0 1 2 3 4 5 6 7) ({n[0]} {n[1]} {n[2]}) simpleGrading (1 1 1) );\n"
        f"boundary (\n{bnd}\n);\n")
    (cd / "system" / "surfaceFeatureExtractDict").write_text(
        HEADER.format(cls="dictionary", obj="surfaceFeatureExtractDict")
        + "part.stl { extractionMethod extractFromSurface; includedAngle 150; subsetFeatures "
          "{ nonManifoldEdges no; openEdges yes; } writeObj no; }\n")
    if case.mode == "external":
        surf = f"part {{ level ({ms.lmin} {ms.lmax}); patchInfo {{ type wall; }} }}"
        region_ref = f"part {{ mode distance; levels (({0.04 * case.L:.6g} {ms.lmin}) ({0.12 * case.L:.6g} {max(ms.lmin - 1, 0)})); }}"
    else:
        regs = " ".join(f"{p} {{ level ({ms.lmin} {ms.lmin}); patchInfo {{ type patch; }} }}" for p in patches
                        if p != "wall")
        surf = (f"part {{ level ({ms.lmin} {ms.lmax}); patchInfo {{ type wall; }} regions {{ {regs} "
                f"wall {{ level ({ms.lmin} {ms.lmax}); patchInfo {{ type wall; }} }} }} }}")
        region_ref = ""
    (cd / "system" / "snappyHexMeshDict").write_text(
        HEADER.format(cls="dictionary", obj="snappyHexMeshDict") + f"""
castellatedMesh true; snap true; addLayers false;
geometry {{ part.stl {{ type triSurfaceMesh; name part; }} }}
castellatedMeshControls {{
 maxLocalCells 4000000; maxGlobalCells 12000000; minRefinementCells 0; maxLoadUnbalance 0.10;
 nCellsBetweenLevels 3;
 features ( {{ file "part.eMesh"; level {ms.feature_level}; }} );
 refinementSurfaces {{ {surf} }}
 resolveFeatureAngle {ms.resolve_angle};
 refinementRegions {{ {region_ref} }}
 locationInMesh {_vec(case.location)};
 allowFreeStandingZoneFaces true;
}}
snapControls {{ nSmoothPatch 3; tolerance 2.0; nSolveIter 50; nRelaxIter 5; nFeatureSnapIter 10;
 implicitFeatureSnap false; explicitFeatureSnap true; multiRegionFeatureSnap {'true' if case.mode == 'internal' else 'false'}; }}
addLayersControls {{ relativeSizes true; layers {{ }} expansionRatio 1.0; finalLayerThickness 0.3;
 minThickness 0.1; nGrow 0; featureAngle 60; nRelaxIter 3; nSmoothSurfaceNormals 1; nSmoothNormals 3;
 nSmoothThickness 10; maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.3; minMedialAxisAngle 90;
 nBufferCellsNoExtrude 0; nLayerIter 50; }}
meshQualityControls {{ maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80;
 minVol 1e-16; minTetQuality 1e-15; minArea -1; minTwist 0.02; minDeterminant 0.001; minFaceWeight 0.05;
 minVolRatio 0.01; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75; relaxed {{ maxNonOrtho 75; }} }}
mergeTolerance 1e-6;
""")
    fo = f"""
 forces {{ type forces; libs (forces); writeControl timeStep; writeInterval 1; log false;
  patches ({'"part.*"' if case.mode == 'external' else 'part_wall'}); rho rhoInf; rhoInf 1; CofR {_vec(case.cofr)}; }}"""
    for p in [case.inlet] + case.outlets:
        fo += f"""
 p_{p} {{ type surfaceFieldValue; libs (fieldFunctionObjects); writeControl timeStep; writeInterval 1;
  log false; writeFields false; regionType patch; name {p}; operation areaAverage; fields (p); }}
 phi_{p} {{ type surfaceFieldValue; libs (fieldFunctionObjects); writeControl timeStep; writeInterval 1;
  log false; writeFields false; regionType patch; name {p}; operation sum; fields (phi); }}"""
    (cd / "system" / "controlDict").write_text(
        HEADER.format(cls="dictionary", obj="controlDict") + f"""
application simpleFoam; startFrom startTime; startTime 0; stopAt endTime; endTime {iters}; deltaT 1;
writeControl timeStep; writeInterval {iters}; purgeWrite 1; writeFormat binary; writePrecision 10;
writeCompression off; timeFormat general; timePrecision 6; runTimeModifiable false;
functions {{ {fo}
}}
""")
    (cd / "system" / "fvSchemes").write_text(
        HEADER.format(cls="dictionary", obj="fvSchemes") + """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; grad(U) cellLimited Gauss linear 1; grad(k) cellLimited Gauss linear 1; grad(omega) cellLimited Gauss linear 1; }
divSchemes { default none; div(phi,U) bounded Gauss linearUpwind grad(U); div(phi,k) bounded Gauss upwind;
 div(phi,omega) bounded Gauss upwind; div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes { default Gauss linear limited corrected 0.33; }
interpolationSchemes { default linear; }
snGradSchemes { default limited corrected 0.33; }
wallDist { method meshWave; }
""")
    (cd / "system" / "fvSolution").write_text(
        HEADER.format(cls="dictionary", obj="fvSolution") + f"""
solvers {{
 p {{ solver GAMG; smoother GaussSeidel; tolerance 1e-9; relTol 0.05; }}
 "(U|k|omega)" {{ solver smoothSolver; smoother symGaussSeidel; tolerance 1e-10; relTol 0.1; }}
}}
SIMPLE {{ nNonOrthogonalCorrectors 1; consistent yes; residualControl {{ p {tol_p:g}; U {tol_u:g}; "(k|omega)" {tol_u:g}; }} }}
relaxationFactors {{ equations {{ U 0.8; ".*" 0.8; }} fields {{ p 1; }} }}
""")
    (cd / "constant" / "transportProperties").write_text(
        HEADER.format(cls="dictionary", obj="transportProperties") + f"transportModel Newtonian;\nnu {case.nu:.9g};\n")
    if case.turbulence == "laminar":
        turb = "simulationType laminar;\n"
    else:
        turb = "simulationType RAS;\nRAS { RASModel kOmegaSST; turbulence on; printCoeffs on; }\n"
    (cd / "constant" / "turbulenceProperties").write_text(HEADER.format(cls="dictionary", obj="turbulenceProperties") + turb)
    _write_fields(cd / "0.orig", case)
    (cd / "Allrun").write_text(f"""#!/bin/bash
# one CFD case: mesh, check, solve; every stage timed, peak RSS by /usr/bin/time -v (bind-mounted)
source {OF_BASHRC} >/dev/null 2>&1
cd "$(dirname "$0")"
st() {{ local name=$1; shift; local t0=$(date +%s.%N)
  /usr/bin/time -v -o time.$name "$@" > log.$name 2>&1; local rc=$?
  echo "$name $rc $t0 $(date +%s.%N)" >> stages.txt; return $rc; }}
rm -f stages.txt
st blockMesh blockMesh || exit 1
st surfaceFeatureExtract surfaceFeatureExtract || exit 1
st snappyHexMesh snappyHexMesh -overwrite || exit 1
st checkMesh checkMesh -constant || true
rm -rf 0 && cp -r 0.orig 0
st simpleFoam simpleFoam || exit 2
rm -rf processor* [1-9]*/uniform
""")
    return {"cells_background": n[0] * n[1] * n[2], "h": h, "n": n}


def _write_fields(d: Path, case: Case) -> None:
    Uv = [0.0, 0.0, 0.0]
    if case.mode == "external":
        Uv[case.flow] = case.U
    else:
        Uv = [case.U * c for c in case.inlet_normal]
    wall = '"part.*|wall"'
    if case.mode == "external":
        ub = (f" inlet {{ type fixedValue; value uniform {_vec(Uv)}; }}\n outlet {{ type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }}\n"
              f" sides {{ type slip; }}\n {wall} {{ type noSlip; }}\n")
        pb = f" inlet {{ type zeroGradient; }}\n outlet {{ type fixedValue; value uniform 0; }}\n sides {{ type zeroGradient; }}\n {wall} {{ type zeroGradient; }}\n"
        uint = _vec(Uv)
    else:
        # every outlet by its exact name: OpenFOAM tries regex keys LAST-declared first, so an outlet
        # regex declared before the wall's "part.*" would lose to it (measured: no fixed-p patch)
        ub = (f" {case.inlet} {{ type fixedValue; value uniform {_vec(Uv)}; }}\n"
              + "".join(f" {o} {{ type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }}\n" for o in case.outlets)
              + f" {wall} {{ type noSlip; }}\n sides {{ type noSlip; }}\n")
        pb = (f" {case.inlet} {{ type zeroGradient; }}\n"
              + "".join(f" {o} {{ type fixedValue; value uniform 0; }}\n" for o in case.outlets)
              + f" {wall} {{ type zeroGradient; }}\n sides {{ type zeroGradient; }}\n")
        uint = "(0 0 0)"
    (d / "U").write_text(HEADER.format(cls="volVectorField", obj="U")
                         + f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform {uint};\nboundaryField {{\n{ub}}}\n")
    (d / "p").write_text(HEADER.format(cls="volScalarField", obj="p")
                         + f"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\nboundaryField {{\n{pb}}}\n")
    if case.turbulence != "laminar":
        k_in = 1.5 * (0.01 * case.U) ** 2
        om_in = math.sqrt(k_in) / (0.09 ** 0.25 * 0.07 * case.L)
        for name, val, dims, wf in (("k", k_in, "[0 2 -2 0 0 0 0]", "kqRWallFunction"),
                                    ("omega", om_in, "[0 0 -1 0 0 0 0]", "omegaWallFunction"),
                                    ("nut", 0.0, "[0 2 -1 0 0 0 0]", "nutUSpaldingWallFunction")):
            inl = f"fixedValue; value uniform {val:.6g}" if name != "nut" else "calculated; value uniform 0"
            out = f"inletOutlet; inletValue uniform {val:.6g}; value uniform {val:.6g}" if name != "nut" else "calculated; value uniform 0"
            outs = case.outlets
            side = "slip" if case.mode == "external" else f"{wf}; value uniform {val:.6g}"
            (d / name).write_text(HEADER.format(cls="volScalarField", obj=name)
                                  + f"dimensions {dims};\ninternalField uniform {val:.6g};\nboundaryField {{\n"
                                  + f" {case.inlet} {{ type {inl}; }}\n" + "".join(f" {o} {{ type {out}; }}\n" for o in outs)
                                  + f" sides {{ type {side}; }}\n"
                                  + f" {wall} {{ type {wf}; value uniform {val:.6g}; }}\n}}\n")


# ------------------------------------------------------------------------------------------------
# run one case in the mesh image, read it back

def run_case(name: str, cd: Path, core: int, timeout_s: int, tag: str) -> dict:
    """docker run the case's Allrun pinned to one core. `timeout docker run` would leave the container
    running, so the container is named and killed on timeout."""
    cname = f"cfd-{tag}-{name}"[:60]
    subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
    cmd = ["docker", "run", "--rm", "--name", cname, f"--cpuset-cpus={core}", "--user", f"{os.getuid()}:{os.getgid()}",
           "-e", "HOME=/tmp", "-e", "OMP_NUM_THREADS=1", "-v", "/usr/bin/time:/usr/bin/time:ro",
           "-v", f"{cd}:{cd}", IMAGE, "bash", str(cd / "Allrun")]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", cname], capture_output=True)
        rc = -9
    r = read_case(cd)
    r["wall_s"] = time.time() - t0
    r["rc"] = rc
    if rc == -9:
        r["ok"], r["why"] = False, f"timed out after {timeout_s} s"
    elif rc != 0 and r.get("ok"):
        r["ok"], r["why"] = False, f"Allrun exit {rc}"
    (cd / "result.json").write_text(json.dumps(r, indent=1, default=float))
    return r


def _stages(cd: Path) -> dict:
    out = {}
    f = cd / "stages.txt"
    if f.exists():
        for ln in f.read_text().splitlines():
            nm, rc, t0, t1 = ln.split()
            peak = None
            tf = cd / f"time.{nm}"
            if tf.exists():
                m = re.search(r"Maximum resident set size \(kbytes\): (\d+)", tf.read_text())
                peak = int(m.group(1)) / 1024 if m else None
            out[nm] = {"rc": int(rc), "s": float(t1) - float(t0), "peak_mb": peak}
    return out


def _dat(path: Path) -> tuple[list[str], np.ndarray]:
    """An OpenFOAM postProcessing .dat: (column names, rows). Vectors '(a b c)' are flattened."""
    if not path.exists():
        return [], np.zeros((0, 0))
    cols, rows = [], []
    for ln in path.read_text().splitlines():
        if ln.startswith("#"):
            hdr = ln[1:].split()
            if hdr and hdr[0] == "Time":
                cols = hdr
            continue
        vals = ln.replace("(", " ").replace(")", " ").split()
        if vals:
            try:
                rows.append([float(v) for v in vals])
            except ValueError:
                continue
    if not rows:
        return cols, np.zeros((0, 0))
    w = min(len(r) for r in rows)
    return cols, np.array([r[:w] for r in rows])


def _pp(cd: Path, fo: str, fname: str) -> Path | None:
    d = cd / "postProcessing" / fo
    if not d.exists():
        return None
    cands = sorted(d.glob(f"*/{fname}"))
    return cands[-1] if cands else None


def read_case(cd: Path) -> dict:
    r: dict = {"ok": False, "why": "", "stages": _stages(cd)}
    cm = cd / "log.checkMesh"
    if cm.exists():
        t = cm.read_text()
        m = re.search(r"^\s+cells:\s+(\d+)", t, re.M)
        r["cells"] = int(m.group(1)) if m else 0
        m = re.search(r"Mesh non-orthogonality Max: ([\d.eE+-]+)", t)
        r["max_non_ortho"] = float(m.group(1)) if m else None
        m = re.search(r"Max skewness = ([\d.eE+-]+)", t)
        r["max_skew"] = float(m.group(1)) if m else None
        r["mesh_ok"] = "Mesh OK." in t
        m = re.search(r"Failed (\d+) mesh checks", t)
        r["failed_checks"] = int(m.group(1)) if m else 0
        wf = 0
        for ln in t.splitlines():
            mm = re.match(r"\s+(part\S*|wall)\s+(\d+)\s+\d+", ln)
            if mm:
                wf += int(mm.group(2))
        r["wall_faces"] = wf
    st = r["stages"]
    r["mesh_s"] = sum(st.get(k, {}).get("s", 0.0) for k in ("blockMesh", "surfaceFeatureExtract", "snappyHexMesh"))
    r["solve_s"] = st.get("simpleFoam", {}).get("s", 0.0)
    r["mesh_peak_mb"] = (st.get("snappyHexMesh") or {}).get("peak_mb")
    r["solve_peak_mb"] = (st.get("simpleFoam") or {}).get("peak_mb")
    if "snappyHexMesh" not in st or st["snappyHexMesh"]["rc"] != 0:
        bad = [k for k, v in st.items() if v["rc"] != 0]
        r["why"] = f"meshing failed at {bad[0] if bad else 'start'}"
        return r
    log = cd / "log.simpleFoam"
    if not log.exists():
        r["why"] = "no solver log"
        return r
    t = log.read_text(errors="replace")
    its = re.findall(r"^Time = (\d+)", t, re.M)
    r["iterations"] = int(its[-1]) if its else 0
    r["converged"] = "SIMPLE solution converged" in t
    res = {}
    for fld in ("Ux", "Uy", "Uz", "p", "k", "omega"):
        m = re.findall(rf"Solving for {fld}, Initial residual = ([\d.eE+-]+)", t)
        if m:
            res[fld] = float(m[-1])
    r["final_residuals"] = res
    # "Floating point exception trapping enabled" is the banner; a real trap prints sigFpe's stack
    if "sigFpe::sigHandler" in t or "FOAM FATAL" in t or re.search(r"residual = nan", t, re.I) or not re.search(r"^End", t, re.M):
        m = (re.search(r"FOAM FATAL.*\n+.*", t) or re.search(r"sigFpe::sigHandler.*", t)
             or re.search(r"residual = nan.*", t, re.I))
        r["why"] = "solver diverged or failed: " + (m.group(0).replace("\n", " ") if m else "no End in the log")[:200]
        return r
    # QoIs: forces on the wall, and per-patch p area average and flux
    q, drift = {}, {}
    cols, a = _dat(_pp(cd, "forces", "force.dat") or Path("/nonexistent"))
    if a.size:
        tail = a[-max(50, len(a) // 10):]
        for i, ax in enumerate("xyz"):
            q[f"F{ax}"] = float(a[-1, 1 + i])
            q[f"Fp{ax}"] = float(a[-1, 4 + i]) if a.shape[1] > 6 else None
            q[f"Fv{ax}"] = float(a[-1, 7 + i]) if a.shape[1] > 9 else None
            drift[f"F{ax}"] = float(tail[:, 1 + i].max() - tail[:, 1 + i].min())
    for fo in sorted((cd / "postProcessing").glob("p_*")) if (cd / "postProcessing").exists() else []:
        _, b = _dat(_pp(cd, fo.name, "surfaceFieldValue.dat") or Path("/nonexistent"))
        if b.size:
            q[fo.name] = float(b[-1, 1])
            tail = b[-max(50, len(b) // 10):, 1]
            drift[fo.name] = float(tail.max() - tail.min())
    for fo in sorted((cd / "postProcessing").glob("phi_*")) if (cd / "postProcessing").exists() else []:
        _, b = _dat(_pp(cd, fo.name, "surfaceFieldValue.dat") or Path("/nonexistent"))
        if b.size:
            q[fo.name] = float(b[-1, 1])
            tail = b[-max(50, len(b) // 10):, 1]
            drift[fo.name] = float(tail.max() - tail.min())
    if not q:
        r["why"] = "no function-object output"
        return r
    r["qoi"] = q
    r["drift_abs"] = drift
    r["ok"] = True
    return r


# ------------------------------------------------------------------------------------------------
# QoIs and comparison

def derived(case: Case, q: dict) -> dict:
    """The named QoIs from raw function-object values."""
    ax = "xyz"
    if case.mode == "external":
        return {"drag": q[f"F{ax[case.flow]}"], "lift": q[f"F{ax[case.lift]}"], "side": q[f"F{ax[case.side]}"],
                "dp": q.get("p_inlet", 0.0) - q.get("p_outlet", 0.0)}
    out = {"dp_" + o: q.get(f"p_{case.inlet}", 0.0) - q.get(f"p_{o}", 0.0) for o in case.outlets}
    tot = sum(q.get(f"phi_{o}", 0.0) for o in case.outlets)
    for o in case.outlets:
        out["split_" + o] = q.get(f"phi_{o}", 0.0) / tot if tot else 0.0
    out["drag"] = math.sqrt(sum(q.get(f"F{a}", 0.0) ** 2 for a in ax))
    return out


def compare(d: dict, ref: dict, mode: str) -> dict:
    """Relative change of each QoI. Lift and side force are near zero on a symmetric case, so their
    change is reported relative to the reference DRAG (a change in force coefficient per unit drag),
    and a mass-flow split as an absolute change of the fraction."""
    out = {}
    for k, v in d.items():
        r = ref.get(k)
        if r is None:
            continue
        if k in ("lift", "side"):
            out[k + "/drag"] = (v - r) / abs(ref["drag"]) if ref.get("drag") else None
        elif k.startswith("split_"):
            out[k] = v - r
        else:
            out[k] = (v - r) / abs(r) if r else None
    return out


# ------------------------------------------------------------------------------------------------
# the run

def build(a, placed, order, diag, scale):
    """EXTERNAL: the tunnel around the placed part (metres), as (Case, None). Axes come from the part
    as drawn (order: longest first), so an angle of attack does not reorder them."""
    bb = np.array(H._bbox_of([placed]))
    D = diag * scale
    if a.mode == "external":
        flow, side, lift = order[0], order[1], order[2]
        lo, hi = bb[:3].copy(), bb[3:].copy()
        lo -= a.lateral * D
        hi += a.lateral * D
        lo[flow] = bb[flow] - a.upstream * D
        hi[flow] = bb[flow + 3] + a.downstream * D
        L = D
        nu = a.U * L / a.re
        loc = lo + 0.5 * (bb[:3] - lo) + 0.0123 * D   # in the upstream / lateral gap
        loc[flow] = lo[flow] + 0.37 * (bb[flow] - lo[flow])
        cofr = tuple(0.5 * (bb[:3] + bb[3:]))
        return Case("external", tuple(lo) + tuple(hi), flow, lift, side, a.U, nu, L, a.re, tuple(loc), cofr), None
    return None, None


def fluid_box(a, shape, scale) -> tuple:
    """The box whose planes cut the passage's openings: the part's bounding box, inset by --inset
    (file units) on every side. An inset past a port-mouth chamfer puts the openings on exact cut
    planes inside the straight bore, the same in every variant - the FEA harness's trimmed ends."""
    bb = np.array(H._bbox_of([scaled(shape, scale)]))
    d = a.inset * scale
    return tuple(list(bb[:3] + d) + list(bb[3:] - d))


def build_internal(a, shape, diag, scale):
    """INTERNAL: fluid passage, its openings as patches; the largest opening is the inlet."""
    s = scaled(shape, scale)
    bb = fluid_box(a, shape, scale)
    tol = PLANE_TOL * diag * scale
    fluid, regions = fluid_of(s, bb, tol)
    if fluid is None or len(regions) < 3:
        raise SystemExit(f"no through-passage: {len(regions) - 1 if regions else 0} openings")
    opens = [k for k in regions if k != "wall"]
    areas = {k: _face_area(regions[k][0]) for k in opens}
    # the largest opening is the inlet; near-equal areas (a tee's run ends) break by name, so the
    # choice is deterministic
    inlet = sorted(opens, key=lambda k: (-round(areas[k] / max(areas.values()), 3), k))[0]
    outlets = [k for k in opens if k != inlet]
    ax = "xyz".index(inlet.split("_")[1][0])
    sign = 1.0 if inlet.endswith("min") else -1.0
    nrm = [0.0, 0.0, 0.0]
    nrm[ax] = sign
    f = regions[inlet][0]
    dh = 4 * areas[inlet] / _face_perimeter(f)
    nu = a.U * dh / a.re
    fb = H._bbox_of([fluid])
    margin = 0.05 * diag * scale   # the blockMesh box encloses the fluid; snappy keeps the STL's inside
    box = tuple(list(np.array(fb[:3]) - margin) + list(np.array(fb[3:]) + margin))
    loc = _inside_point(_solids(fluid)[0], fb, tol)
    cofr = tuple(0.5 * (np.array(fb[:3]) + np.array(fb[3:])))
    case = Case("internal", box, ax, (ax + 1) % 3, (ax + 2) % 3, a.U, nu, dh, a.re, loc, cofr,
                inlet="part_" + inlet, outlets=["part_" + o for o in outlets], inlet_normal=tuple(nrm))
    return case, fluid, regions


def report(path: str) -> str:
    """A markdown summary of one result.json: the original, the noise floor, one row per feature."""
    d = json.loads(Path(path).read_text())
    o, fl = d["original"], d.get("noise_floor", {})
    keys = list(next((r["error"] for r in d["features"].values() if r.get("error")), {}))
    out = [f"part `{Path(d['part']).name}`, {d['mode']}, Re {d['case']['re']:g} on L {d['case']['L'] * 1000:.1f} mm, "
           f"h0 {d['mesh']['h0'] * 1000:.2f} mm, levels {d['mesh']['lmin']}-{d['mesh']['lmax']}", "",
           f"original: {o.get('cells')} cells, mesh {o.get('mesh_s', 0):.0f} s (peak {o.get('mesh_peak_mb') or 0:.0f} MB), "
           f"solve {o.get('solve_s', 0):.0f} s / {o.get('iterations')} it (peak {o.get('solve_peak_mb') or 0:.0f} MB), "
           f"converged {o.get('converged')}, residuals {o.get('final_residuals')}", "",
           "| noise run | cells | it | conv | " + " | ".join(o.get("named", {})) + " |",
           "|---|---|---|---|" + "---|" * len(o.get("named", {}))]
    for n, r in sorted(d.get("noise", {}).items()):
        if r.get("ok"):
            out.append(f"| {n} | {r['cells']} | {r['iterations']} | {r['converged']} | "
                       + " | ".join(f"{v:.6g}" for v in r["named"].values()) + " |")
        else:
            out.append(f"| {n} | failed: {r.get('why')} |")
    out += ["", "noise floor (max-min over the noise runs): " + ", ".join(
        f"{k} {v:.3%}" if isinstance(v, float) else f"{k} {v}" for k, v in fl.items()), ""]
    out += ["| feature | kind | r mm | concave | dV | cells saved | mesh s | solve s | it | conv | "
            + " | ".join(keys) + " |", "|---" * (10 + len(keys)) + "|"]

    def cell(row, k):
        v = row["error"].get(k)
        if v is None:
            return "-"
        above = row.get("above_floor", {}).get(k)
        return f"**{v:+.3%}**" if above else f"{v:+.3%}"

    for fid, row in d["features"].items():
        r = row.get("result", {})
        head = (f"| {fid} | {row.get('kind')} | {row.get('size', 0):.2f} | {row.get('concave', '')} | "
                f"{row.get('volume_change', 0):+.2%} |")
        if "skipped" in row:
            out.append(head + f" skipped: {row['skipped']} |")
        elif not r.get("ok"):
            out.append(head + f" failed: {r.get('why')} |")
        else:
            out.append(head + f" {row['cells_saved']:+.2%} | {r['mesh_s']:.0f} | {r['solve_s']:.0f} | "
                       f"{r['iterations']} | {r['converged']} | " + " | ".join(cell(row, k) for k in keys) + " |")
    out.append("")
    out.append("Bold: |change| above the noise floor of that QoI.")
    return "\n".join(out)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--report"]:
        print(report(argv[1]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=("external", "internal"), default="external")
    ap.add_argument("--scale", type=float, default=1e-3, help="file units to metres")
    ap.add_argument("--inset", type=float, default=0.0,
                    help="internal: cut the openings this far (file units) inside the bounding box")
    ap.add_argument("--re", type=float, default=100.0)
    ap.add_argument("--U", type=float, default=1.0)
    ap.add_argument("--aoa", type=float, default=0.0,
                    help="external: degrees the part is turned about its middle axis (lift axis = shortest)")
    ap.add_argument("--turbulence", choices=("laminar", "kOmegaSST"), default="laminar")
    ap.add_argument("--upstream", type=float, default=0.6, help="x diagonal")
    ap.add_argument("--downstream", type=float, default=1.5, help="x diagonal")
    ap.add_argument("--lateral", type=float, default=0.5, help="x diagonal")
    ap.add_argument("--base", type=float, default=14.0, help="background cell = Re length / BASE")
    ap.add_argument("--levels", default="2,5", help="lmin,lmax surface refinement")
    ap.add_argument("--feature-level", type=int, default=None, help="feature-edge level (default lmax)")
    ap.add_argument("--resolve-angle", type=float, default=20.0)
    ap.add_argument("--noise", default="0.96,0.98,1.02,1.04", help="jitter factors on the base size")
    ap.add_argument("--iters", type=int, default=3000)
    ap.add_argument("--tol-p", type=float, default=1e-5)
    ap.add_argument("--tol-u", type=float, default=1e-6)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--timeout", type=int, default=5400)
    ap.add_argument("--core-offset", type=int, default=0, help="first core to pin to (several harnesses at once)")
    ap.add_argument("--max-features", type=int, default=80)
    ap.add_argument("--features", default="", help="comma list of feature ids to run (default: all)")
    ap.add_argument("--only", default="", help="comma list of run names to (re)run; others are read back")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--extra", action="append", default=[], metavar="NAME=STEP",
                    help="also run this whole simplified part (same placement) as a variant")
    ap.add_argument("--reread", action="store_true", help="run nothing; parse every case directory again")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    (out / "geom").mkdir(parents=True, exist_ok=True)
    lmin, lmax = (int(v) for v in a.levels.split(","))
    fl = lmax if a.feature_level is None else a.feature_level

    shape = H.read_step(a.step)
    bb = H._bbox_of([shape])
    diag = math.dist(bb[:3], bb[3:])
    feats, faces, _ = recognise(shape, diag)
    v0 = H.volume(shape)
    lin, ang = 2e-4 * diag * a.scale, 0.15

    order = [int(i) for i in np.argsort(-(np.array(bb[3:]) - np.array(bb[:3])))]
    centre = tuple(0.5 * (np.array(bb[:3]) + np.array(bb[3:])))
    rot = (order[1], a.aoa, centre) if a.mode == "external" else None

    def xf(shp):
        return scaled(shp, a.scale, rot)

    if a.mode == "external":
        case, _ = build(a, xf(shape), order, diag, a.scale)
        orig_regions = None
    else:
        case, fluid0, orig_regions = build_internal(a, shape, diag, a.scale)
        v_fluid0 = H.volume(fluid0)
    case.turbulence = a.turbulence
    ms0 = MeshSpec(case.L / a.base, lmin, lmax, fl, a.resolve_angle)
    patches = ["part"] if orig_regions is None else list(orig_regions)

    def wetted(face_ids) -> float:
        """Distance (metres) from a feature's faces to the original fluid: 0 when it is wetted."""
        from OCP.BRep import BRep_Builder
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        from OCP.TopoDS import TopoDS_Compound
        comp = TopoDS_Compound()
        bld = BRep_Builder()
        bld.MakeCompound(comp)
        for j in face_ids:
            bld.Add(comp, faces[j])
        d = BRepExtrema_DistShapeShape(scaled(comp, a.scale), fluid0)
        return d.Value() if d.IsDone() else 0.0

    def stl_for(name, shp) -> tuple[Path | None, str, dict]:
        """Write the variant's STL; internal variants rebuild the fluid and must keep the openings."""
        path = out / "geom" / f"{name}.stl"
        info: dict = {}
        if a.mode == "external":
            info["triangles"] = write_stl(xf(shp), path, lin, ang)
            return path, "", info
        s = scaled(shp, a.scale)
        fl_, regs = fluid_of(s, fluid_box(a, shape, a.scale), PLANE_TOL * diag * a.scale)
        if fl_ is None:
            return None, "no fluid", info
        if sorted(k for k in regs if k != "wall") != sorted(k for k in orig_regions if k != "wall"):
            return None, "openings changed", info
        # a feature ON an opening (a port-mouth chamfer) changes the boundary-condition face itself:
        # with a fixed inlet velocity the flow rate moves with it, and the run compares two different
        # problems (measured on manifold_3port's package: inlet area -22%, dp -51%). Not a candidate,
        # as the spec says of features under a load or a fixture.
        moved = {k: _face_area(regs[k][0]) / _face_area(orig_regions[k][0]) - 1 for k in regs if k != "wall"}
        info["opening_area_change"] = moved
        if max(abs(v) for v in moved.values()) > 1e-6:
            return None, "an opening (a boundary-condition face) changed area", info
        dv = (H.volume(fl_) - v_fluid0) / v_fluid0
        info["fluid_volume_change"] = dv
        # a boolean recomputed on an unchanged region reproduces its volume to ~1e-12, not exactly
        if name != "original" and abs(dv) < 1e-9 and len(H._faces(fl_)[0]) == len(H._faces(fluid0)[0]):
            return None, "dry: the fluid is unchanged", info
        info["triangles"] = write_stl(fl_, path, lin, ang, regs)
        return path, "", info

    runs: list[tuple[str, Path, MeshSpec]] = []
    rows: dict[str, dict] = {}
    p0, why0, info0 = stl_for("original", shape)
    if p0 is None:
        raise SystemExit(f"original: {why0}")
    runs.append(("original", p0, ms0))
    for j in (float(v) for v in a.noise.split(",") if v):
        runs.append((f"noise_j{j:g}", p0, MeshSpec(ms0.h0, lmin, lmax, fl, a.resolve_angle, j)))
    pick = set(a.features.split(",")) if a.features else None
    for f in feats[: a.max_features]:
        if pick is not None and f.id not in pick:
            continue
        row = asdict(f)
        if a.mode == "internal":
            # DRY: a feature that does not touch the fluid cannot change it. Tested on the faces, not
            # on the rebuilt fluid's volume: measured on tee_dn50_4b, removing a flange-root fillet
            # moved the recomputed fluid volume by 6e-8 although no wetted face changed.
            row["distance_to_fluid_m"] = wetted(f.faces)
            if row["distance_to_fluid_m"] > 1e-6 * diag * a.scale:
                rows[f.id] = row | {"skipped": "dry: not on the wetted surface"}
                continue
        simp, why = H.remove(shape, [faces[j] for j in f.faces])
        if simp is None:
            rows[f.id] = row | {"skipped": why}
            continue
        row["volume_change"] = (H.volume(simp) - v0) / v0
        p, why, info = stl_for(f.id, simp)
        row.update(info)
        if p is None:
            rows[f.id] = row | {"skipped": why}
            continue
        rows[f.id] = row
        runs.append((f.id, p, ms0))
    for spec in a.extra:
        # a whole-package variant from a file (e.g. the corpus's __simplified.step), same placement
        nm, path = spec.split("=", 1)
        simp = H.read_step(path)
        row = {"id": nm, "kind": "package", "file": path, "volume_change": (H.volume(simp) - v0) / v0}
        p, why, info = stl_for(nm, simp)
        row.update(info)
        rows[nm] = row | ({"skipped": why} if p is None else {})
        if p is not None:
            runs.append((nm, p, ms0))
    meta = {"part": a.step, "mode": a.mode, "diag_m": diag * a.scale, "case": asdict(case), "mesh": asdict(ms0),
            "stl": {"lin": lin, "ang": ang, "triangles": info0.get("triangles")}, "volume": v0,
            "n_features": len(feats), "args": vars(a)}
    print(f"{len(runs)} runs ({len(feats)} features, {sum('skipped' in r for r in rows.values())} skipped), "
          f"Re {a.re:g} on L {case.L:.4g} m, nu {case.nu:.4g}, h0 {ms0.h0:.4g} m", flush=True)
    for name, p, ms in runs:
        cd = out / "work" / name
        if a.reread or (a.only and name not in a.only.split(",")):
            continue
        if cd.exists():
            shutil.rmtree(cd)
        bg = write_case(cd, case, ms, p, a.tol_p, a.tol_u, a.iters, patches)
        (cd / "spec.json").write_text(json.dumps({"mesh": asdict(ms)} | bg, default=float))
    (out / "meta.json").write_text(json.dumps(meta, indent=1, default=float))
    if a.prepare_only:
        (out / "prepared.json").write_text(json.dumps(rows, indent=1, default=float))
        return 0

    cores: queue.Queue = queue.Queue()
    for c in range(min(a.jobs, os.cpu_count() or 1)):
        cores.put((c + a.core_offset) % (os.cpu_count() or 1))
    tag = out.name.replace("_", "-")[:20]

    def job(name):
        c = cores.get()
        try:
            return name, run_case(name, out / "work" / name, c, a.timeout, tag)
        finally:
            cores.put(c)

    results: dict[str, dict] = {}
    todo = [] if a.reread else [n for n, _, _ in runs if not a.only or n in a.only.split(",")]
    # the original and the noise runs first: they are what every row is read against
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(job, n) for n in todo]
        for fut in as_completed(futs):
            name, r = fut.result()
            results[name] = r
            print(f"[{time.time() - t0:7.1f}s] {name}: {'ok' if r['ok'] else r['why']} cells={r.get('cells')} "
                  f"it={r.get('iterations')} conv={r.get('converged')} mesh={r.get('mesh_s', 0):.0f}s "
                  f"solve={r.get('solve_s', 0):.0f}s", flush=True)
    for name, _, _ in runs:
        if a.reread and (out / "work" / name / "stages.txt").exists():
            results[name] = read_case(out / "work" / name) | {k: v for k, v in results.get(name, {}).items()
                                                                if k in ("wall_s", "rc")}
        if name not in results:
            rf = out / "work" / name / "result.json"
            results[name] = json.loads(rf.read_text()) if rf.exists() else {"ok": False, "why": "not run"}
    summarise(out, case, meta, rows, results)
    return 0


def summarise(out: Path, case: Case, meta: dict, rows: dict, results: dict) -> None:
    ref = results.get("original", {})
    if not ref.get("ok"):
        print("original did not solve:", ref.get("why"), file=sys.stderr)
    for r in results.values():
        if r.get("ok"):
            r["named"] = derived(case, r["qoi"])
    noise = {n: r for n, r in results.items() if n.startswith("noise_") or n == "original"}
    floor: dict = {}
    oks = [r for r in noise.values() if r.get("ok")]
    if len(oks) >= 2:
        for k in oks[0]["named"]:
            vals = [r["named"][k] for r in oks]
            if k in ("lift", "side"):
                scale = abs(statistics.median(r["named"]["drag"] for r in oks))
                floor[k + "/drag"] = (max(vals) - min(vals)) / scale
            elif k.startswith("split_"):
                floor[k] = max(vals) - min(vals)
            else:
                med = statistics.median(vals)
                floor[k] = (max(vals) - min(vals)) / abs(med) if med else None
        floor["cells"] = [r["cells"] for r in oks]
        floor["n"] = len(oks)
    for fid, row in rows.items():
        r = results.get(fid)
        if not r:
            continue
        row["result"] = r
        if r.get("ok") and ref.get("ok"):
            row["cells_saved"] = 1 - r["cells"] / ref["cells"]
            row["mesh_s_saved"] = 1 - r["mesh_s"] / max(ref["mesh_s"], 1e-9)
            row["solve_s_saved"] = 1 - r["solve_s"] / max(ref["solve_s"], 1e-9)
            row["error"] = compare(r["named"], ref["named"], case.mode)
            row["above_floor"] = {k: (abs(v) > floor.get(k, 0) if v is not None and floor.get(k) is not None else None)
                                  for k, v in row["error"].items()}
    summary = meta | {"original": ref, "noise": {n: results[n] for n in noise}, "noise_floor": floor,
                      "features": rows}
    (out / "result.json").write_text(json.dumps(summary, indent=1, default=float))
    print("noise floor:", json.dumps(floor, default=float))
    print(f"wrote {out / 'result.json'}")


if __name__ == "__main__":
    sys.exit(main())
