# Responsibility: Pin MESH-8: a solid with a zero-thickness skin (a hole cut exactly to a wall, its floor left lying on that wall the other side out) is found, meshed with the skin left out and its outline opened through - every other face named for its CAD origin - and a solid without one is left exactly as it is.
# Boundaries: solids built here with OpenCASCADE; gmsh meshes them coarsely. Nothing is read from disk.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.cad import zero_skins as Z  # noqa: E402

# THE DEFECT (job bf863215, 26 Oct): a tube's four boss pockets were cut from outside down to exactly its
# inner radius, each floor left in the solid on top of the uncut inner wall. OpenCASCADE calls the solid
# valid; gmsh answers "PLC Error: A segment and a facet intersect" at every size, alone and fused.


def _plate_with_a_skin():
    """A 40 x 40 x 10 plate with a 10 mm hole whose bottom is closed by a disk lying ON the uncut bottom face."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepLib import BRepLib
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(40.0, 40.0, 10.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 20, -1), gp_Dir(0, 0, 1)), 5.0, 12.0).Shape()
    through = BRepAlgoAPI_Cut(box, hole).Shape()
    sew = BRepBuilderAPI_Sewing(1e-6)
    bottom = None
    for f in Z._faces(through):
        c = Z._props(f, False)[1]
        if abs(c[2]) < 1e-9 and abs(Z._props(f, False)[0] - 1600.0) > 1.0:
            bottom = f                                   # the bottom, holed
            continue
        sew.Add(f)
    assert bottom is not None
    # the bottom UNCUT, and the hole's own outline as a disk lying on it, the other side out
    pln = gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, -1))
    sew.Add(BRepBuilderAPI_MakeFace(pln, BRepTools.OuterWire_s(bottom)).Face())
    inner = [w for w in _wires(bottom) if not w.IsSame(BRepTools.OuterWire_s(bottom))][0]
    sew.Add(BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), inner).Face())
    sew.Perform()
    ex = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
    solid = TopoDS.Solid_s(BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(ex.Current())).Solid())
    BRepLib.OrientClosedSolid_s(solid)
    return solid


def _wires(face):
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = []
    ex = TopExp_Explorer(face, TopAbs_WIRE)
    while ex.More():
        out.append(TopoDS.Wire_s(ex.Current()))
        ex.Next()
    return out


def _gmsh_fills(solid, tmp_path) -> bool:
    from OCP.BRepTools import BRepTools

    path = tmp_path / "s.brep"
    BRepTools.Write_s(solid, str(path))
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(path))
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 4.0)
        gmsh.model.mesh.generate(3)
        return len(gmsh.model.mesh.getElementsByType(4)[0]) > 0
    except Exception:  # noqa: BLE001 - the fill refused it
        return False
    finally:
        gmsh.finalize()


def test_a_skin_lying_on_a_wall_is_found(tmp_path):
    s = _plate_with_a_skin()
    found = Z.find(s)
    assert len(found) == 1
    faces = Z._faces(s)
    assert Z._props(faces[found[0].skin - 1], False)[0] == pytest.approx(78.54, rel=1e-3)     # the disk
    assert Z._props(faces[found[0].on - 1], False)[0] == pytest.approx(1600.0, rel=1e-6)      # the uncut bottom
    assert not _gmsh_fills(s, tmp_path), "the fixture must be the defect: the fill refuses it as drawn"


def test_the_skin_is_left_out_and_the_hole_goes_through(tmp_path):
    s = _plate_with_a_skin()
    o = Z.open_skins(s)
    assert o.solid is not s and len(o.skins) == 1
    assert len(Z._faces(o.solid)) == len(Z._faces(s)) - 1
    assert Z._props(o.solid, True)[0] == pytest.approx(Z._props(s, True)[0], rel=1e-9)
    # every face meshed is named for the CAD face it came from; the skin is the only one gone
    assert sorted(o.origin) == [k for k in range(1, len(Z._faces(s)) + 1) if k != o.skins[0].skin]
    assert _gmsh_fills(o.solid, tmp_path)


def test_a_solid_without_a_skin_is_meshed_exactly_as_drawn():
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    box = BRepPrimAPI_MakeBox(40.0, 40.0, 10.0).Shape()
    pocket = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 20, 5), gp_Dir(0, 0, 1)), 5.0, 6.0).Shape()
    s = A.solids(BRepAlgoAPI_Cut(box, pocket).Shape())[0]
    assert Z.find(s) == []
    o = Z.open_skins(s)
    assert o.solid is s and o.origin == list(range(1, len(Z._faces(s)) + 1)) and not o.note


def test_an_assembly_part_is_staged_without_its_skin_and_every_face_keeps_its_cad_name(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    plate = _plate_with_a_skin()
    block = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 10), 10.0, 10.0, 5.0).Shape()
    shape = A.compound([plate, block])
    side = A.stage(shape, A.BONDED, tmp_path, tie=True)
    skin = Z.find(A.solids(shape)[0])[0]
    face, on = f"Part_1_Face_{skin.skin}", f"Part_1_Face_{skin.on}"
    # MESH-8: each skin face with the face it lies on, so every CAD face is accounted for
    assert side["skins"] == [{"part": "Part_1", "faces": [face], "on": [on],
                              "pairs": [{"face": face, "on": on, "words": f"{face} lies on {on}: zero thickness, "
                                                                          "no volume - left out of the mesh"}]}]
    named = {n for row in side["surfaces"] for n in row["names"]}
    assert f"Part_1_Face_{skin.skin}" not in named
    assert {f"Part_1_Face_{k}" for k in range(1, len(Z._faces(plate)) + 1) if k != skin.skin} <= named
    assert any("skin" in n and "left out of the mesh" in n for n in side["notes"])
