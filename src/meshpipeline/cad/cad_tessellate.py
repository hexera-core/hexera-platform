# Responsibility: Turn a CAD solid into a triangulated surface.
# Boundaries: tessellation only; the unit and scale it works in are decided before it runs.
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.cad.stl_io import read_stl_triangles

logger = logging.getLogger(__name__)


def _occ_to_metres(prepared):
    from meshpipeline.cad.normalise import occ_scale_transform

    if prepared is None:
        raise ValueError(
            "CAD tessellation needs the OCC-output coordinate state; without it the conversion "
            "to metres would be a guess that is right only for well-formed files")
    return occ_scale_transform(prepared)


def tessellate_to_stl(geom_path, out_stl, *, prepared=None, angular_deflection: float = 0.2,
                      linear_deflection: float | None = None) -> Path:
    import shutil as _sh
    geom_path, out_stl = Path(geom_path), Path(out_stl)
    if geom_path.suffix.lower() == ".stl":
        if geom_path.resolve() != out_stl.resolve():
            _sh.copy2(geom_path, out_stl)
        return out_stl
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.StlAPI import StlAPI_Writer
    if geom_path.suffix.lower() in (".igs", ".iges"):
        from OCP.IGESControl import IGESControl_Reader as _Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as _Reader
    reader = _Reader()
    if reader.ReadFile(str(geom_path)) != IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read CAD file: {geom_path.name}")
    reader.TransferRoots()
    shape = reader.OneShape()
    trsf = _occ_to_metres(prepared)
    shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
    box = Bnd_Box(); BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    lin = linear_deflection if linear_deflection is not None else diag / 2500.0
    BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)
    StlAPI_Writer().Write(shape, str(out_stl))
    return out_stl


def select_declared_openings(candidates: list, declared: list) -> list:
    """Pick which candidate faces are the DECLARED openings. candidates: (idx, area_m2,
    centroid) per planar face; declared: {name, area_m2|None, near_m|None} per port. Hints
    claim the nearest unclaimed face; sizes claim within the +/-25% band (the binder's own
    tolerance). Deterministic: ports in name order, hinted ports first. A port no face can
    satisfy refuses with the measured face list - the geometry and the words disagree, and
    only the user can settle that."""
    remaining = {int(i): (float(a), tuple(c)) for i, a, c in candidates}
    chosen: list[int] = []

    def _dist(p, q):
        return sum((p[k] - q[k]) ** 2 for k in range(3)) ** 0.5

    hinted = sorted((p for p in declared if p.get("near_m")), key=lambda p: str(p.get("name")))
    sized = sorted((p for p in declared if not p.get("near_m")),
                   key=lambda p: str(p.get("name")))
    for port in hinted + sized:
        best = None
        for idx, (area, cen) in remaining.items():
            if port.get("area_m2"):
                ratio = area / float(port["area_m2"])
                if not (0.75 <= ratio <= 1.25):
                    continue
            if port.get("near_m"):
                score = _dist(tuple(port["near_m"]), cen)
            else:
                score = abs(area - float(port.get("area_m2") or area))
            if best is None or score < best[1] or (score == best[1] and idx < best[0]):
                best = (idx, score)
        if best is None:
            faces = "; ".join(f"face[{i}]: {a * 1e6:.0f} mm² at ({c[0]:.3f}, {c[1]:.3f}, "
                              f"{c[2]:.3f}) m" for i, (a, c) in sorted(remaining.items()))
            raise ValueError(
                f"declared port {port.get('name')!r} matches none of the remaining flat "
                f"faces - measured: {faces}. State the port's size or rough location so the "
                "opening can be identified, or correct the declaration.")
        chosen.append(best[0])
        del remaining[best[0]]
    return chosen


def tessellate_internal(geom_path, out_dir, *, prepared=None, angular_deflection: float = 0.2,
                        linear_deflection: float | None = None,
                        opening_faces: list[int] | None = None,
                        declared_ports: list | None = None) -> dict:
    import math as _m

    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.gp import gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopAbs import (
        TopAbs_FACE,
        TopAbs_IN,
        TopAbs_REVERSED,
        TopAbs_SOLID,
        TopAbs_WIRE,
    )
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    geom_path = Path(geom_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if geom_path.suffix.lower() in (".igs", ".iges"):
        from OCP.IGESControl import IGESControl_Reader as _Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as _Reader
    reader = _Reader()
    if reader.ReadFile(str(geom_path)) != IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read CAD file: {geom_path.name}")
    reader.TransferRoots()
    shape = reader.OneShape()
    trsf = _occ_to_metres(prepared)
    shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()

    solid_exp = TopExp_Explorer(shape, TopAbs_SOLID)
    if not solid_exp.More():
        raise RuntimeError(
            "internal-flow input is not a watertight SOLID - the fluid volume must be a "
            "closed solid (a loose surface/shell is the pipe skin, not the flow passage)")
    solid = TopoDS.Solid_s(solid_exp.Current())

    diag = 0.0
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box(); BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    lin = linear_deflection if linear_deflection is not None else diag / 2500.0
    BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)

    # classify faces: planar -> opening candidate, curved -> wall (see docstring)
    faces: list = []
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        faces.append(TopoDS.Face_s(e.Current())); e.Next()

    def _face_props(f):
        g = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        return g.Mass(), (c.X(), c.Y(), c.Z())

    wall_idx: list[int] = []
    open_idx: list[int] = []
    for i, f in enumerate(faces):
        is_open = (i in opening_faces) if opening_faces is not None \
            else (BRepAdaptor_Surface(f).GetType() == GeomAbs_Plane)
        (open_idx if is_open else wall_idx).append(i)

    if (declared_ports and opening_faces is None
            and len(open_idx) > len(declared_ports) >= 2):
        # More flat faces than declared ports: a box duct's walls are as planar as its ends,
        # and calling them all openings left the wall with zero triangles. The DECLARATION
        # says which ones are real - select those, wall the rest.
        cands = [(i, *_face_props(faces[i])) for i in open_idx]
        keep = set(select_declared_openings(cands, declared_ports))
        wall_idx.extend(i for i in open_idx if i not in keep)
        open_idx = [i for i in open_idx if i in keep]
        logger.info("tessellate_internal: declaration selected %d of %d planar faces as "
                    "openings; %d fold to the wall", len(open_idx), len(cands),
                    len(cands) - len(open_idx))
    if len(open_idx) < 2:
        raise RuntimeError(
            f"internal-flow geometry needs >=2 flat openings (inlet+outlet); found "
            f"{len(open_idx)}. If the walls are planar (box duct), pass opening_faces "
            f"explicitly.")
    # EVERY flat opening is a port. Keeping only the two largest and folding the rest into the wall
    # SEALS them, and nothing downstream can tell: a Y-junction manifold came back with one branch
    # capped shut, meshed 1,659,660 cells, and was reported production-grade. The mesh was
    # physically wrong - flow could only leave through one branch - and looked fine.
    open_idx.sort(key=lambda i: _face_props(faces[i])[0], reverse=True)

    if len(open_idx) == 2:
        # A plain duct: which end is the inlet is arbitrary by area, so keep the original rule and
        # order the two ports along the axis of greatest separation.
        ca = _face_props(faces[open_idx[0]])[1]
        cb = _face_props(faces[open_idx[1]])[1]
        axis = max(range(3), key=lambda k: abs(ca[k] - cb[k]))
        ports = list(open_idx) if ca[axis] <= cb[axis] else [open_idx[1], open_idx[0]]
        inlet_i, outlet_ids = ports[0], [ports[1]]
    else:
        # A manifold. Area is the only signal available here - the brief names the patches, but this
        # runs before any of that reaches us - so the widest opening is taken as the feed. That is a
        # GUESS and it is wrong whenever the part diffuses or combines: a 40 mm feed into two 60 mm
        # branches labels a branch the inlet and the feed an outlet, and a solver run on it has the
        # flow backwards. The caller states the assumption to the user; the real fix is to bind the
        # brief's declared roles to these ports instead of inferring one.
        inlet_i, outlet_ids = open_idx[0], list(open_idx[1:])

    def _write_group(idxs, path, extra_faces=()):
        comp = TopoDS_Compound(); bld = BRep_Builder(); bld.MakeCompound(comp)
        for i in idxs:
            bld.Add(comp, faces[i])
        for xf in extra_faces:
            bld.Add(comp, xf)
        w = StlAPI_Writer(); w.ASCIIMode = False
        w.Write(comp, str(path))

    def _mouth_caps(port_i):
        """Cap face(s) that SEAL a hollow part's port mouth.

        A solid-model duct's port face is a single-wire disc that already spans the
        opening. A HOLLOW part's (thin-shell wall) declared port face is the metal's
        ANNULAR end ring: its inner wire bounds the bore hole, and writing only the ring
        leaves that hole OPEN in the port STL - the channel then connects to the exterior
        void through the mouth, the snappy carve keeps channel+exterior as ONE region,
        and the delivered mesh grows a spurious 'outer' (blockMesh-skin) patch (jobs
        95bd0197, 0de57541). Every inner wire is closed with a planar face built on the
        ring's own plane and tessellated like the rest, so the port STL spans the whole
        opening. A single-wire face yields no caps - solid-model inputs (the y_duct
        class) come out byte-identical.
        """
        f = faces[port_i]
        wires = []
        we = TopExp_Explorer(f, TopAbs_WIRE)
        while we.More():
            wires.append(TopoDS.Wire_s(we.Current())); we.Next()
        if len(wires) < 2:
            return []
        outer_w = BRepTools.OuterWire_s(f)
        pln = BRepAdaptor_Surface(f).Plane()
        caps = []
        for wire in wires:
            if wire.IsSame(outer_w):
                continue
            cap = None
            # the ring orients its hole wire so material lies OUTSIDE it; a face built
            # from that orientation can come out empty, so fall back to the reversal
            for cand_wire in (wire, TopoDS.Wire_s(wire.Reversed())):
                mk = BRepBuilderAPI_MakeFace(pln, cand_wire, True)
                if not mk.IsDone():
                    continue
                cand = mk.Face()
                g = GProp_GProps(); BRepGProp.SurfaceProperties_s(cand, g)
                if g.Mass() <= 0:
                    continue
                BRepMesh_IncrementalMesh(cand, lin, False, angular_deflection, True)
                if _triangles_of(cand):
                    cap = cand
                    break
            if cap is None:
                logger.error(
                    "tessellate_internal: could not build a sealing cap for an inner "
                    "wire of port face %d - the port STL leaves the mouth open and the "
                    "internal carve may keep the exterior void (finalize flags it)",
                    port_i)
                continue
            caps.append(cap)
        if caps:
            logger.info("tessellate_internal: annular port face %d - sealed %d bore "
                        "hole(s) so the port STL closes the full mouth", port_i, len(caps))
        return caps

    # one patch per outlet, so a branch can carry its own boundary condition and its own flow split
    outlet_names = (["outlet"] if len(outlet_ids) == 1
                    else [f"outlet_{n}" for n in range(1, len(outlet_ids) + 1)])
    stls = {"wall": out_dir / "wall.stl", "inlet": out_dir / "inlet.stl"}
    for nm in outlet_names:
        stls[nm] = out_dir / f"{nm}.stl"
    _write_group(wall_idx, stls["wall"])
    _write_group([inlet_i], stls["inlet"], extra_faces=_mouth_caps(inlet_i))
    for nm, oi in zip(outlet_names, outlet_ids):
        _write_group([oi], stls[nm], extra_faces=_mouth_caps(oi))

    # verified interior point for locationInMesh. Candidates, cheapest-first:
    # volume centroid, then each port centroid nudged inward along its (oriented) normal.
    def _inside(p) -> bool:
        cls = BRepClass3d_SolidClassifier(solid)
        cls.Perform(gp_Pnt(*p), 1e-9)
        return cls.State() == TopAbs_IN

    gv = GProp_GProps(); BRepGProp.VolumeProperties_s(solid, gv)
    vc = gv.CentreOfMass()
    candidates = [(vc.X(), vc.Y(), vc.Z())]
    for pi in (inlet_i, *outlet_ids):
        f = faces[pi]
        area, c = _face_props(f)
        ax = BRepAdaptor_Surface(f).Plane().Axis().Direction()
        n = [ax.X(), ax.Y(), ax.Z()]
        if f.Orientation() == TopAbs_REVERSED:
            n = [-v for v in n]
        step = 0.5 * _m.sqrt(area / _m.pi)              # ~half the port radius, inward
        candidates.append(tuple(c[k] - step * n[k] for k in range(3)))
        candidates.append(tuple(c[k] + step * n[k] for k in range(3)))
    interior = next((p for p in candidates if _inside(p)), None)
    if interior is None:
        # HOLLOW-WALL FALLBACK. Everything above assumes the input solid IS the fluid
        # volume (a duct modeled as a solid rod), where inside-the-solid means inside the
        # flow. A real machined part - a rocket nozzle - is METAL with a channel through
        # it: the channel is NOT inside the solid, so every rod-semantics candidate is
        # correctly rejected and the old code died here. For a hollow part the right test
        # inverts: a point IN the channel is NOT in the metal. Direction is what makes it
        # safe - nudging a port centroid TOWARD THE OTHER PORT walks down the channel by
        # construction (an annular mouth's centroid sits in the void at the channel mouth),
        # so a not-in-metal point found this way is in the flow region, never in the
        # exterior void around the part.
        pc_in = _face_props(faces[inlet_i])[1]
        for oi in outlet_ids:
            pc_out = _face_props(faces[oi])[1]
            seg = [pc_out[k] - pc_in[k] for k in range(3)]
            seg_len = _m.sqrt(sum(v * v for v in seg)) or 1.0
            u = [v / seg_len for v in seg]
            r_in = _m.sqrt(_face_props(faces[inlet_i])[0] / _m.pi)
            r_out = _m.sqrt(_face_props(faces[oi])[0] / _m.pi)
            hollow = []
            for base, direction, radius in ((pc_in, 1.0, r_in), (pc_out, -1.0, r_out)):
                step = min(0.5 * radius, 0.1 * seg_len)
                hollow.append(tuple(base[k] + direction * step * u[k] for k in range(3)))
            hollow.append(tuple(pc_in[k] + 0.5 * seg_len * u[k] for k in range(3)))
            interior = next((p for p in hollow if not _inside(p)), None)
            if interior is not None:
                logger.info("tessellate_internal: hollow-wall fluid point found on the "
                            "inlet-outlet segment (rod-semantics candidates were all "
                            "inside the metal's complement)")
                break
    if interior is None:
        raise RuntimeError("could not locate a point inside the fluid solid for "
                           "locationInMesh (geometry may not be a closed volume)")

    n_wall_faces = sum(len(read_stl_triangles(stls["wall"])) for _ in [0])
    return {
        "stls": {k: str(v) for k, v in stls.items()},
        "interior_point": [round(v, 6) for v in interior],
        "bbox_min": [round(v, 6) for v in (x0, y0, z0)],
        "bbox_max": [round(v, 6) for v in (x1, y1, z1)],
        "openings": {
            "inlet": {"area": round(_face_props(faces[inlet_i])[0], 8),
                      "centroid": [round(v, 6) for v in _face_props(faces[inlet_i])[1]]},
            **{nm: {"area": round(_face_props(faces[oi])[0], 8),
                    "centroid": [round(v, 6) for v in _face_props(faces[oi])[1]]}
               for nm, oi in zip(outlet_names, outlet_ids)}},
        "n_wall_faces": n_wall_faces,
    }


def tessellate_regions_to_stl(geom_path, out_stl, *, prepared=None, angular_deflection: float = 0.2,
                              linear_deflection: float | None = None) -> list[str]:
    # Each named component tessellated into its own STL solid; returns the names written.
    #
    # The flat path above writes OneShape(), which fuses every component into one unnamed solid.
    # That is right for a body meshed as a single wall, and lossy for a surface whose parts are
    # named - and the loss happens here, before any engine could have chosen. A file that
    # distinguishes nothing takes the flat path unchanged and reports no names.
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    from meshpipeline.cad.regions import components_of, regions_of
    from meshpipeline.cad.stl_io import write_stl_solids

    geom_path, out_stl = Path(geom_path), Path(out_stl)

    def _flat() -> list[str]:
        tessellate_to_stl(geom_path, out_stl, prepared=prepared,
                          angular_deflection=angular_deflection,
                          linear_deflection=linear_deflection)
        return []

    if regions_of(geom_path).count < 2:
        return _flat()

    _doc, tool, found, _roots = components_of(geom_path)
    trsf = _occ_to_metres(prepared)
    solids: dict[str, list] = {}
    for index, (label, name) in enumerate(found):
        shape = tool.GetShape_s(label)
        if shape is None or shape.IsNull():
            continue
        shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
        lin = linear_deflection if linear_deflection is not None else max(diag / 2500.0, 1e-9)
        BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)
        tris = _triangles_of(shape)
        if tris:
            solids[name or f"region_{index + 1}"] = tris
    if len(solids) < 2:
        # Fewer than two solids survived tessellation, so there is nothing to keep apart.
        return _flat()
    write_stl_solids(out_stl, solids)
    return list(solids)


def _triangles_of(shape) -> list:
    # Every triangulated face of one shape, in world coordinates.
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    out: list = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            transform = loc.Transformation()
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                pts = []
                for node in (a, b, c):
                    p = tri.Node(node).Transformed(transform)
                    pts.append((float(p.X()), float(p.Y()), float(p.Z())))
                out.append(tuple(pts))
        explorer.Next()
    return out
