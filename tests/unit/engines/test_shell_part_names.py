# Responsibility: Hold SHWRITE-1 - a shell deck groups its elements by CAD PART, named as the CAD names it - to small
# hand-made meshes and a two-part STEP written here: one property per part and wall (named "<part>" or
# "<part> t=<wall>mm"), the part level in every format (Nastran $HMNAME COMP / PROP and Femap titles, an ELSET per part,
# an Ansys component per part), read back by our readers and pyNastran; the names from the assembly staging's record
# (the viewer's source), with the CAD's instance suffix kept and a repeated document prefix dropped.
# Boundaries: files under tmp_path; no mesher, no solver.
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.engines import shell_writers as sw
from meshpipeline.engines.shell_mesh import ShellMesh


def _two_parts(names: dict[str, str] | None = None) -> ShellMesh:
    # three 6 mm plates: Part_1 one quad, Part_2 two quads (one of 6 mm, one of 8 mm), Part_3 a triangle of 6 mm;
    # the elements of the parts interleave in the mesh's own order (quads first)
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [20, 0, 0], [20, 10, 0], [30, 0, 0],
                    [30, 10, 0], [40, 0, 0], [40, 10, 0]], dtype=float)
    quads = np.array([[4, 6, 7, 5], [0, 1, 2, 3], [6, 8, 9, 7]])
    trias = np.array([[1, 4, 5]])
    m = ShellMesh(xyz=xyz, quads=quads, trias=trias, wall=np.array([6.0, 6.0, 8.0, 6.0]),
                  mid=["Mid_2", "Mid_1", "Mid_3", "Mid_4"], part=["Part_2", "Part_1", "Part_2", "Part_3"],
                  part_names=dict(names or {}))
    m.groups = {"Part_1_Face_1+Part_1_Face_2": np.array([1]), "Part_2_Face_1+Part_2_Face_2": np.array([0]),
                "Part_2_Face_3+Part_2_Face_4": np.array([2]), "Part_3_Face_1+Part_3_Face_2": np.array([3]),
                "Part_1": np.array([1]), "Part_2": np.array([0, 2]), "Part_3": np.array([3])}
    return m


NAMES = {"Part_1": "PEDESTAL FOOTPAD GUSSET.57", "Part_2": "PEDESTAL FOOTPAD GUSSET.58", "Part_3": "Bügel \"A\""}


def test_every_part_is_its_own_property_and_group_named_after_it(tmp_path):
    m = _two_parts(NAMES)
    sw.write_all(tmp_path, m, units="mm")
    prep = sw.prepare(m, units="mm")
    # numbered part by part; two parts of one wall never share a property
    assert prep.props == [("PEDESTAL FOOTPAD GUSSET.57", 6.0, 1), ("PEDESTAL FOOTPAD GUSSET.58 t=6mm", 6.0, 2),
                          ("PEDESTAL FOOTPAD GUSSET.58 t=8mm", 8.0, 2), ("Bugel 'A'", 6.0, 3)]
    assert prep.prop_of.tolist() == [2, 1, 3, 4]
    bdf = sw.read_bdf(tmp_path / "shell.bdf")
    assert bdf.names == bdf.hm_props == {i + 1: p[0] for i, p in enumerate(prep.props)}
    assert bdf.parts == {"PEDESTAL FOOTPAD GUSSET.57": {2}, "PEDESTAL FOOTPAD GUSSET.58": {1, 3}, "Bugel 'A'": {4}}
    assert bdf.groups["PEDESTAL FOOTPAD GUSSET.58"] == {1, 3} and "Part_2_Face_3+Part_2_Face_4" in bdf.groups
    assert bdf.elements == {1: (5, 7, 8, 6), 2: (1, 2, 3, 4), 3: (7, 9, 10, 8), 4: (2, 5, 6)}
    lines = (tmp_path / "shell.bdf").read_text().splitlines()
    k = lines.index('$HMNAME COMP                   2"PEDESTAL FOOTPAD GUSSET.58"')
    assert lines[k + 1].startswith("CQUAD4         1       2") and lines[k + 2].startswith("CQUAD4         3       3")
    k = next(i for i, ln in enumerate(lines) if ln.startswith("PSHELL         2"))
    assert lines[k - 2:k] == ["$ Femap Property 2 : PEDESTAL FOOTPAD GUSSET.58 t=6mm",
                              '$HMNAME PROP                   2"PEDESTAL FOOTPAD GUSSET.58 t=6mm"']
    assert "$   MAT1 1 = PEDESTAL FOOTPAD GUSSET.57 (Part_1)" in lines
    for name in ("shell.inp", "shell_abaqus.inp"):
        inp = sw.read_inp(tmp_path / name)
        assert inp.parts == {"PEDESTAL FOOTPAD GUSSET.57": {2}, "PEDESTAL FOOTPAD GUSSET.58": {1, 3}, "Bugel 'A'": {4}}
        assert {"PEDESTAL_FOOTPAD_GUSSET_57", "PEDESTAL_FOOTPAD_GUSSET_58", "Bugel_A"} <= set(inp.groups)
        assert inp.names == {i + 1: p[0] for i, p in enumerate(prep.props)}
        assert {e: inp.walls[p] for e, p in inp.prop.items()} == {1: 6.0, 2: 6.0, 3: 8.0, 4: 6.0}
    cdb = sw.read_cdb(tmp_path / "shell.cdb")
    assert cdb.parts == {"PEDESTAL FOOTPAD GUSSET.57": {2}, "PEDESTAL FOOTPAD GUSSET.58": {1, 3}, "Bugel 'A'": {4}}
    assert cdb.names[2] == "PEDESTAL_FOOTPAD_GUSSET_58_T_6MM" and "PEDESTAL_FOOTPAD_GUSSET_57" in cdb.groups


def test_pynastran_reads_the_named_deck(tmp_path):
    bdf_mod = pytest.importorskip("pyNastran.bdf.bdf")
    m = _two_parts(NAMES)
    sw.write_all(tmp_path, m, units="m")
    b = bdf_mod.BDF(debug=None)
    b.read_bdf(str(tmp_path / "shell.bdf"), punch=True, xref=False)
    assert len(b.elements) == 4 and len(b.properties) == 4
    assert {p: (round(float(b.properties[p].t), 6), b.properties[p].mid1) for p in b.properties} == \
        {1: (0.006, 1), 2: (0.006, 2), 3: (0.008, 2), 4: (0.006, 3)}
    assert '$HMNAME PROP                   2"PEDESTAL FOOTPAD GUSSET.58 t=6mm"' in b.properties[2].comment


def test_a_part_of_one_wall_is_one_property_named_after_the_part(tmp_path):
    m = _two_parts()
    m.wall = np.array([6.0, 6.0, 6.0, 6.0])
    prep = sw.prepare(m, units="mm")
    assert [p[0] for p in prep.props] == ["Part_1", "Part_2", "Part_3"]
    # no CAD name: every part is Part_k, its ELSET and component as before
    assert sw.inp_part_sets(prep) == {"Part_1": "Part_1", "Part_2": "Part_2", "Part_3": "Part_3"}
    assert {k: v for k, v in sw.cdb_components(prep).items() if k.startswith("Part_") and "Face" not in k} == \
        {"Part_1": "PART_1", "Part_2": "PART_2", "Part_3": "PART_3"}


def test_one_name_on_two_parts_is_told_apart_and_never_another_parts_id():
    m = _two_parts({"Part_1": "Gusset", "Part_2": "Gusset", "Part_3": "Part_1"})
    assert sw.part_labels(m) == {"Part_1": "Gusset", "Part_2": "Gusset (2)", "Part_3": "Part_1"}
    m = _two_parts({"Part_2": "Part_1"})
    assert sw.part_labels(m) == {"Part_1": "Part_1", "Part_2": "Part_1 (2)", "Part_3": "Part_3"}


@pytest.mark.parametrize(("raw", "want"), [("PEDESTAL FOOTPAD GUSSET.57", "PEDESTAL_FOOTPAD_GUSSET_57"),
                                           ("Part_3", "Part_3"), ("57 plate", "PART_57_plate"), ("P_3 bracket", "P_3_bracket"), ("P 3", "PART_P_3"), ("P 3", "PART_P_3"), ("Bügel, links", "Bugel_links")])
def test_a_part_set_name_is_letters_digits_and_underscores(raw, want):
    assert sw.part_set_name(raw) == want


def test_the_shell_run_names_its_parts_as_the_staging_and_the_viewer_do(tmp_path):
    from meshpipeline.engines import shell_run

    (tmp_path / "assembly.json").write_text(json.dumps({"parts": [
        {"id": "Part_1", "name": "PEDESTAL FOOTPAD GUSSET.57"}, {"id": "Part_2", "name": ""},
        {"id": "Part_3", "name": "cover plate"}]}))
    m = _two_parts()
    shell_run.name_parts(m, tmp_path)
    assert m.part_names == {"Part_1": "PEDESTAL FOOTPAD GUSSET.57", "Part_3": "cover plate"}
    faces = shell_run.write_msh(tmp_path / "shell.msh", m)
    assert [(f["name"], f["part"], f["part_name"]) for f in faces] == [
        ("Mid_1", "Part_1", "PEDESTAL FOOTPAD GUSSET.57"), ("Mid_2", "Part_2", "Part_2"), ("Mid_3", "Part_2", "Part_2"),
        ("Mid_4", "Part_3", "cover plate")]
    parts = shell_run.parts_of(m, faces)
    assert [(p["id"], p["name"], p["tags"], p["elset"]) for p in parts] == [
        ("Part_1", "PEDESTAL FOOTPAD GUSSET.57", [1], "PEDESTAL_FOOTPAD_GUSSET_57"), ("Part_2", "Part_2", [2, 3], "Part_2"),
        ("Part_3", "cover plate", [4], "cover_plate")]
    # the names the viewer lists (engines/shell_delivery.part_tags) are the deck's
    from meshpipeline.engines import shell_delivery as SD

    (tmp_path / "shell.json").write_text(json.dumps({"mids": [{"id": f["name"], "body": f["part"]} for f in faces]}))
    assert set(SD.part_tags(tmp_path, {"faces": faces})) == {p["name"] for p in parts}
    # a workspace with no assembly: every part Part_k, as before
    m2 = _two_parts()
    shell_run.name_parts(m2, tmp_path / "none")
    assert m2.part_names == {}


@pytest.mark.parametrize(("product", "instance", "want"), [
    ("PEDESTAL FOOTPAD GUSSET", "PEDESTAL FOOTPAD GUSSET.57", "PEDESTAL FOOTPAD GUSSET.57"),
    ("gusset", "gusset-3", "gusset-3"), ("gusset", "gusset:2", "gusset:2"),
    ("gusset", "1", ""), ("gusset", "left gusset", ""), ("Open CASCADE STEP translator 7.9 51.2", "2", "")])
def test_an_instance_keeps_its_suffix_only_when_it_is_the_products_name_numbered(product, instance, want):
    from meshpipeline.cad.fea_assembly import _instance_name

    assert _instance_name(product, instance) == want


@pytest.mark.parametrize(("name", "want"), [
    ("PEDESTAL ASSY PEDESTAL BASE PLATE.1", "PEDESTAL BASE PLATE.1"), ("PEDESTAL ASSY/Pipe", "Pipe"),
    ("PEDESTAL ASSY.12", "PEDESTAL ASSY.12"), ("PEDESTAL ASSYX", "PEDESTAL ASSYX"),
    ("PEDESTAL FOOTPAD GUSSET.57", "PEDESTAL FOOTPAD GUSSET.57")])
def test_a_repeated_document_prefix_is_dropped_when_a_name_is_left(name, want):
    from meshpipeline.cad.fea_assembly import strip_document_prefix

    assert strip_document_prefix(name, ["PEDESTAL ASSY", "column"]) == want


def _named_step(path, product_names, instance_names, root="COLUMN ASSY"):
    """Two boxes, each its own product placed once in an assembly, written with OCP's XCAF writer."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("cad"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    asm = tool.NewShape()
    TDataStd_Name.Set_s(asm, TCollection_ExtendedString(root))
    for k, (prod, inst) in enumerate(zip(product_names, instance_names, strict=True)):
        box = BRepPrimAPI_MakeBox(gp_Pnt(100.0 * k, 0, 0), 10.0 + k, 20.0, 2.0).Shape()
        lab = tool.AddShape(box, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(prod))
        comp = tool.AddComponent(asm, lab, TopLoc_Location())
        TDataStd_Name.Set_s(comp, TCollection_ExtendedString(inst))
    tool.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.Transfer(doc, STEPControl_AsIs)
    w.Write(str(path))


def test_the_step_name_reader_keeps_instances_and_drops_the_document_prefix(tmp_path):
    from meshpipeline.cad.fea_assembly import read_body_names_here

    _named_step(tmp_path / "a.step", ["PEDESTAL FOOTPAD GUSSET", "PEDESTAL FOOTPAD GUSSET"],
                ["PEDESTAL FOOTPAD GUSSET.57", "PEDESTAL FOOTPAD GUSSET.58"])
    assert sorted(n for n, _c, _v in read_body_names_here(tmp_path / "a.step")) == [
        "PEDESTAL FOOTPAD GUSSET.57", "PEDESTAL FOOTPAD GUSSET.58"]
    _named_step(tmp_path / "b.step", ["COLUMN ASSY BASE PLATE.1", "COLUMN ASSY PIPE.1"], ["1", "2"])
    assert sorted(n for n, _c, _v in read_body_names_here(tmp_path / "b.step")) == ["BASE PLATE.1", "PIPE.1"]
    # products of their own names, instances numbered by the exporter: the products' names, as before
    _named_step(tmp_path / "c.step", ["base plate", "pipe"], ["1", "2"])
    assert sorted(n for n, _c, _v in read_body_names_here(tmp_path / "c.step")) == ["base plate", "pipe"]
