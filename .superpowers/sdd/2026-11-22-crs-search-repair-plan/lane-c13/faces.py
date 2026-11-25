"""Lane C13 (2): is an inflated state WRONG, or only its bounding box? For the kept parent/inflated BREPs: overall
diagonal three ways (BRepBndLib.Add with triangulation, Add without, AddOptimal), validity, solids and volume; then
every face of the inflated state whose OWN box reaches outside the parent's box: surface type, its box (plain and
optimal), its area, whether it is in the parent (IsSame), and the max distance of its sample points from the parent.
Usage: python faces.py <parent.brep> <inflated.brep>"""
import sys


def main(parent_p, infl_p):
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def read(p):
        s = TopoDS_Shape(); BRepTools.Read_s(s, p, BRep_Builder()); return s

    def box(s, how):
        b = Bnd_Box()
        if how == "opt":
            BRepBndLib.AddOptimal_s(s, b, False, False)
        else:
            BRepBndLib.Add_s(s, b, how == "tri")
        return b.Get() if not b.IsVoid() else None

    def diag(bx):
        return ((bx[3] - bx[0]) ** 2 + (bx[4] - bx[1]) ** 2 + (bx[5] - bx[2]) ** 2) ** 0.5

    def mapof(s, k):
        m = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(s, k, m); return m

    par, inf = read(parent_p), read(infl_p)
    for name, s in (("parent", par), ("inflated", inf)):
        v = GProp_GProps(); BRepGProp.VolumeProperties_s(s, v)
        print(name, "diag tri/notri/opt", [round(diag(box(s, h)), 1) for h in ("tri", "notri", "opt")],
              "valid", BRepCheck_Analyzer(s).IsValid(), "solids", mapof(s, TopAbs_SOLID).Extent(),
              "faces", mapof(s, TopAbs_FACE).Extent(), "volume", round(v.Mass(), 1))
    pb = box(par, "opt")
    pf = mapof(par, TopAbs_FACE)
    f = mapof(inf, TopAbs_FACE)
    out = 0
    for i in range(1, f.Extent() + 1):
        face = TopoDS.Face_s(f.FindKey(i))
        b = box(face, "notri")
        if b is None:
            continue
        outside = max(pb[0] - b[0], pb[1] - b[1], pb[2] - b[2], b[3] - pb[3], b[4] - pb[4], b[5] - pb[5])
        if outside <= 1.0:
            continue
        out += 1
        a = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, a)
        surf = BRepAdaptor_Surface(face)
        bo = box(face, "opt")
        print(f" F{i} {surf.GetType().name} outside_by {round(outside,1)} box_notri_diag {round(diag(b),1)} "
              f"box_opt_diag {round(diag(bo),1) if bo else None} area {round(a.Mass(),2)} in_parent {pf.Contains(face)} "
              f"tol {BRep_Tool.Tolerance_s(face):.2e} valid {BRepCheck_Analyzer(face).IsValid()} "
              f"u[{surf.FirstUParameter():.3g},{surf.LastUParameter():.3g}] v[{surf.FirstVParameter():.3g},{surf.LastVParameter():.3g}]")
    print("faces reaching >1 mm outside the parent's optimal box:", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
