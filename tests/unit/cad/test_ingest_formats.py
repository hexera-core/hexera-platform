# Responsibility: Verify every accepted format becomes the canonical CAD solid or STL surface, at its true size, with its named groups.
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.cad.ingest import IngestError, canonicalise, check_upload, sniff_format
from meshpipeline.cad.ingest.surface import read_stl, stats
from meshpipeline.contracts.intake_formats import GeometryKind

#: The shape every fixture holds: a 10 x 10 x 10 box (millimetres) whose x=0 face is "inlet",
#: x=10 face "outlet" and the other four "wall".
EDGE = 10.0
AREA, VOLUME = 6 * EDGE ** 2, EDGE ** 3
GROUPS = {"inlet", "outlet", "wall"}


@pytest.fixture(scope="module")
def box(tmp_path_factory) -> dict:
    """The box meshed once by gmsh (tets inside, named triangles on the boundary) and written in
    every format gmsh itself writes, plus the boundary triangles for the surface writers."""
    import gmsh

    out = tmp_path_factory.mktemp("box")
    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("box")
        v = gmsh.model.occ.addBox(0, 0, 0, EDGE, EDGE, EDGE)
        gmsh.model.occ.synchronize()
        faces = [s for _, s in gmsh.model.getBoundary([(3, v)], oriented=False)]
        x = {s: gmsh.model.occ.getCenterOfMass(2, s)[0] for s in faces}
        named = {"inlet": [s for s in faces if abs(x[s]) < 1e-9],
                 "outlet": [s for s in faces if abs(x[s] - EDGE) < 1e-9]}
        named["wall"] = [s for s in faces if s not in named["inlet"] + named["outlet"]]
        for name, tags in named.items():
            gmsh.model.setPhysicalName(2, gmsh.model.addPhysicalGroup(2, tags), name)
        gmsh.model.setPhysicalName(3, gmsh.model.addPhysicalGroup(3, [v]), "fluid")
        gmsh.option.setNumber("Mesh.MeshSizeMax", 4.0)
        gmsh.model.mesh.generate(3)
        files = {}
        gmsh.option.setNumber("Mesh.MshFileVersion", 4.1)
        for ext in ("msh", "inp", "bdf", "su2", "mesh", "vtk"):
            files[ext] = out / f"box.{ext}"
            gmsh.write(str(files[ext]))
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        gmsh.option.setNumber("Mesh.Binary", 1)
        files["msh22bin"] = out / "box_v22.msh"
        gmsh.write(str(files["msh22bin"]))
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.option.setNumber("Mesh.StlOneSolidPerSurface", 2)
        files["stl_named"] = out / "box_named.stl"
        gmsh.write(str(files["stl_named"]))
        tags, coords, _ = gmsh.model.mesh.getNodes()
        index = {int(t): i for i, t in enumerate(tags)}
        pts = np.asarray(coords).reshape(-1, 3)
        tris, names = [], []
        for name, ents in named.items():
            for ent in ents:
                etypes, _, enodes = gmsh.model.mesh.getElements(2, ent)
                for et, en in zip(etypes, enodes):
                    for row in np.asarray(en, dtype=np.int64).reshape(-1, 3):
                        tris.append([index[int(n)] for n in row])
                        names.append(name)
        etypes, _, enodes = gmsh.model.mesh.getElements(3, v)
        tets = np.asarray([[index[int(n)] for n in row]
                           for row in np.asarray(enodes[0], dtype=np.int64).reshape(-1, 4)])
        gmsh.model.remove()
    finally:
        if mine:
            gmsh.finalize()
    return {"dir": out, "files": files, "points": pts, "tris": np.asarray(tris), "names": names,
            "tets": tets}


def _assert_box(canonical, *, scale: float = 1.0, groups: set | None = None) -> None:
    assert canonical.kind is GeometryKind.surface
    assert canonical.path.suffix == ".stl"
    st = stats(read_stl(canonical.path))
    assert st["watertight"], st
    assert st["area"] * scale ** 2 == pytest.approx(AREA, rel=1e-5)
    assert st["volume"] * scale ** 3 == pytest.approx(VOLUME, rel=1e-5)
    assert np.allclose(np.asarray(st["bounds_min"]) * scale, 0.0, atol=1e-6)
    assert np.allclose(np.asarray(st["bounds_max"]) * scale, EDGE, atol=1e-6)
    if groups is not None:
        assert set(canonical.regions) == groups


# volume and surface meshes written by gmsh: the boundary comes back, named where the file names it

@pytest.mark.parametrize("which,groups", [
    ("msh", GROUPS), ("msh22bin", GROUPS), ("inp", GROUPS), ("su2", GROUPS),
    ("bdf", set()), ("mesh", set()), ("vtk", set()), ("stl_named", GROUPS)])
def test_a_gmsh_written_mesh_becomes_its_named_boundary_surface(box, tmp_path, which, groups):
    canonical = canonicalise(box["files"][which], tmp_path, stem="source")
    _assert_box(canonical, groups=groups)


def test_named_solids_reach_the_readers_downstream_as_patches(box, tmp_path):
    from meshpipeline.cad.regions import regions_of
    from meshpipeline.cad.stl_io import read_stl_solids

    canonical = canonicalise(box["files"]["msh"], tmp_path, stem="source")
    assert set(read_stl_solids(canonical.path)) == GROUPS
    assert set(regions_of(canonical.path).names) == GROUPS


def test_a_volume_mesh_without_named_faces_keeps_its_volume_regions(box, tmp_path):
    import meshio

    # two volume regions and no 2D cells: each region's skin keeps its region's name
    pts, tets = box["points"], box["tets"]
    cx = pts[tets].mean(axis=1)[:, 0]
    left, right = tets[cx < EDGE / 2], tets[cx >= EDGE / 2]
    mesh = meshio.Mesh(pts, [("tetra", left), ("tetra", right)],
                       cell_data={"gmsh:physical": [np.full(len(left), 1), np.full(len(right), 2)],
                                  "gmsh:geometrical": [np.full(len(left), 1),
                                                       np.full(len(right), 2)]},
                       field_data={"front": np.array([1, 3]), "back": np.array([2, 3])})
    src = tmp_path / "two.msh"
    meshio.write(str(src), mesh, file_format="gmsh22", binary=False)
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    _assert_box(canonical, groups={"front", "back"})


@pytest.mark.parametrize("fmt,ext", [("vtu", ".vtu"), ("ansys", ".msh")])
def test_a_tet_volume_becomes_its_closed_outward_boundary(box, tmp_path, fmt, ext):
    import meshio

    src = tmp_path / f"vol{ext}"
    meshio.write(str(src), meshio.Mesh(box["points"], [("tetra", box["tets"])]), file_format=fmt)
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    _assert_box(canonical)
    # outward: the divergence theorem gives +volume for an outward-facing closed surface
    c = read_stl(canonical.path).corners()
    signed = np.einsum("ij,ij->i", c[:, 0], np.cross(c[:, 1], c[:, 2])).sum() / 6.0
    assert signed == pytest.approx(VOLUME, rel=1e-5)


# surface formats, written from the same boundary triangles

def _write_obj(path, pts, tris, names):
    lines = [f"v {p[0]:.17g} {p[1]:.17g} {p[2]:.17g}" for p in pts]
    current = None
    for t, n in zip(tris, names):
        if n != current:
            lines.append(f"g {n}")
            current = n
        lines.append(f"f {t[0] + 1}/1 {t[1] + 1}/1 {t[2] + 1}/1")
    path.write_text("\n".join(lines) + "\n")


def _write_3mf(path, pts, tris, names, unit="millimeter", extra_members=()):
    objs, items = [], []
    for oid, name in enumerate(sorted(set(names)), start=1):
        sel = tris[[i for i, n in enumerate(names) if n == name]]
        used = np.unique(sel)
        remap = {int(u): i for i, u in enumerate(used)}
        vx = "".join(f'<vertex x="{pts[u][0]:.17g}" y="{pts[u][1]:.17g}" z="{pts[u][2]:.17g}"/>'
                     for u in used)
        tx = "".join(f'<triangle v1="{remap[int(a)]}" v2="{remap[int(b)]}" v3="{remap[int(c)]}"/>'
                     for a, b, c in sel)
        objs.append(f'<object id="{oid}" type="model" name="{name}"><mesh><vertices>{vx}'
                    f'</vertices><triangles>{tx}</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}"/>')
    unit_attr = f' unit="{unit}"' if unit else ""
    model = (f'<?xml version="1.0"?><model{unit_attr} xmlns="http://schemas.microsoft.com/'
             f'3dmanufacturing/core/2015/02"><resources>{"".join(objs)}</resources>'
             f'<build>{"".join(items)}</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" '
                    'Id="r0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/'
                    '3dmodel"/></Relationships>')
        zf.writestr("3D/3dmodel.model", model)
        for name, data in extra_members:
            zf.writestr(name, data)


def _write_gltf(glb, gltf, pts, tris, names):
    import trimesh

    scene = trimesh.Scene()
    for name in sorted(set(names)):
        sel = tris[[i for i, n in enumerate(names) if n == name]]
        used = np.unique(sel)
        remap = np.full(len(pts), -1)
        remap[used] = np.arange(len(used))
        scene.add_geometry(trimesh.Trimesh(pts[used], remap[sel], process=False),
                           node_name=name, geom_name=f"{name}_mesh")
    scene.export(str(glb))
    gltf.write_bytes(trimesh.exchange.gltf.export_gltf(scene, embed_buffers=True)["model.gltf"])


def _write_3dm(path, pts, tris, names, *, unit="Millimeters"):
    import rhino3dm

    model = rhino3dm.File3dm()
    model.Settings.ModelUnitSystem = getattr(rhino3dm.UnitSystem, unit)
    for name in sorted(set(names)):
        sel = tris[[i for i, n in enumerate(names) if n == name]]
        used = np.unique(sel)
        remap = np.full(len(pts), -1)
        remap[used] = np.arange(len(used))
        mesh = rhino3dm.Mesh()
        for u in used:
            mesh.Vertices.Add(*(float(c) for c in pts[u]))
        for a, b, c in remap[sel]:
            mesh.Faces.AddFace(int(a), int(b), int(c))
        attr = rhino3dm.ObjectAttributes()
        attr.Name = name
        model.Objects.AddMesh(mesh, attr)
    model.Write(str(path), 7)


def test_obj_with_groups(box, tmp_path):
    src = tmp_path / "box.obj"
    _write_obj(src, box["points"], box["tris"], box["names"])
    _assert_box(canonicalise(src, tmp_path / "out", stem="source"), groups=GROUPS)


def test_3mf_with_named_objects_and_its_unit(box, tmp_path):
    from meshpipeline.cad.unit_evidence import read_declared_unit
    from meshpipeline.contracts.geometry_units import LengthUnit

    src = tmp_path / "box.3mf"
    _write_3mf(src, box["points"], box["tris"], box["names"], unit="inch")
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    _assert_box(canonical, groups=GROUPS)
    # the unit the 3MF states travels with the converted STL, which states none itself
    for path in (src, canonical.path):
        evidence = read_declared_unit(path)
        assert evidence.resolved and evidence.unit is LengthUnit.inch


@pytest.mark.parametrize("unit,expected", [("", "mm"), ("centimeter", "cm"), ("micron", None)])
def test_a_3mf_without_a_unit_is_millimetres_and_an_unknown_one_is_asked(box, tmp_path, unit,
                                                                         expected):
    from meshpipeline.cad.unit_evidence import read_declared_unit

    src = tmp_path / "u.3mf"
    _write_3mf(src, box["points"], box["tris"], box["names"], unit=unit)
    evidence = read_declared_unit(src)
    assert (evidence.unit.value if evidence.resolved else None) == expected


@pytest.mark.parametrize("ext", ["glb", "gltf"])
def test_gltf_with_named_nodes_and_no_unit_claimed(box, tmp_path, ext):
    from meshpipeline.cad.unit_evidence import read_declared_unit

    glb, gltf = tmp_path / "box.glb", tmp_path / "box.gltf"
    _write_gltf(glb, gltf, box["points"], box["tris"], box["names"])
    src = glb if ext == "glb" else gltf
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    _assert_box(canonical, groups=GROUPS)
    # glTF says metres by specification; real files ignore it, so the unit is asked
    assert not read_declared_unit(src).resolved


@pytest.mark.parametrize("fmt,ext,binary", [("ply", ".ply", True), ("ply", ".ply", False),
                                            ("off", ".off", False)])
def test_ply_and_off(box, tmp_path, fmt, ext, binary):
    import meshio

    src = tmp_path / f"box{ext}"
    mesh = meshio.Mesh(box["points"], [("triangle", box["tris"].astype(np.int32))])
    if fmt == "ply":
        meshio.write(str(src), mesh, file_format=fmt, binary=binary)
    else:
        meshio.write(str(src), mesh, file_format=fmt)
    _assert_box(canonicalise(src, tmp_path / "out", stem="source"))


def test_rhino_meshes_with_names_and_unit(box, tmp_path):
    from meshpipeline.cad.unit_evidence import read_declared_unit
    from meshpipeline.contracts.geometry_units import LengthUnit

    src = tmp_path / "box.3dm"
    _write_3dm(src, box["points"], box["tris"], box["names"], unit="Meters")
    _assert_box(canonicalise(src, tmp_path / "out", stem="source"), groups=GROUPS)
    evidence = read_declared_unit(src)
    assert evidence.resolved and evidence.unit is LengthUnit.metre


def test_a_rhino_surface_without_render_meshes_is_refused_with_the_export_path(tmp_path):
    import rhino3dm

    model = rhino3dm.File3dm()
    model.Objects.AddBrep(rhino3dm.Brep.CreateFromBox(
        rhino3dm.Box(rhino3dm.BoundingBox(0, 0, 0, 1, 2, 3))))
    src = tmp_path / "nurbs.3dm"
    model.Write(str(src), 7)
    with pytest.raises(IngestError, match="File > Export Selected"):
        canonicalise(src, tmp_path / "out", stem="source")
    verdict = check_upload(src, ".3dm")
    assert not verdict.ok and "File > Export Selected" in verdict.refusal


# exact CAD

def test_brep_becomes_a_step_that_is_the_same_solid(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad.ingest.cad import read_step, shape_stats
    from meshpipeline.cad.unit_evidence import parser_applied_unit, read_declared_unit
    from meshpipeline.contracts.geometry_units import LengthUnit

    src = tmp_path / "box.brep"
    BRepTools.Write_s(BRepPrimAPI_MakeBox(EDGE, EDGE, EDGE).Shape(), str(src))
    assert sniff_format(src) == "brep"
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    assert canonical.kind is GeometryKind.cad and canonical.path.suffix == ".step"
    st = shape_stats(read_step(canonical.path))
    assert st["solids"] == 1 and st["volume"] == pytest.approx(VOLUME, rel=1e-9)
    # coordinates unchanged under OCC's own label, so only the CONFIRMED unit scales them...
    assert parser_applied_unit(canonical.path) is LengthUnit.millimetre
    # ...and nobody reads the converter's label as the user's unit
    assert not read_declared_unit(canonical.path).resolved


@pytest.mark.parametrize("unit", ["mm", "in", "m"])
def test_a_brep_meshes_at_the_confirmed_size_whatever_the_unit(tmp_path, unit):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from tests._geometry_support import interpretation_ref, source_ref

    from meshpipeline.application.geometry_materializer import canonical_path
    from meshpipeline.cad.staging import _consumed_state
    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    from meshpipeline.contracts.geometry_units import SCALE_TO_METRES, LengthUnit

    src = tmp_path / "source.brep"
    BRepTools.Write_s(BRepPrimAPI_MakeBox(EDGE, EDGE, EDGE).Shape(), str(src))
    ref = source_ref(local_file=src, filename="part.brep")
    geom = MaterializedGeometry(
        ref=ref, interpretation=interpretation_ref(geometry_source_id=ref.source_id,
                                                   unit=LengthUnit(unit)),
        local_path=canonical_path(src))
    consumed = _consumed_state(geom, geom.local_path.suffix)
    assert consumed.to_metres == pytest.approx(SCALE_TO_METRES[LengthUnit(unit)])


# STL: today's files pass through untouched; the ones readers downstream choke on are rewritten

def _binary_stl(path, corners, header=b"plain header"):
    rec = np.zeros(len(corners), dtype=[("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["v"] = corners
    path.write_bytes(header.ljust(80, b" ")[:80] + np.uint32(len(corners)).tobytes()
                     + rec.tobytes())


def test_a_well_formed_stl_is_the_uploaded_file_itself(box, tmp_path):
    src = tmp_path / "source.stl"
    _binary_stl(src, box["points"][box["tris"]])
    before = src.read_bytes()
    canonical = canonicalise(src, tmp_path, stem="source")
    assert canonical.path == src and not canonical.converted and src.read_bytes() == before
    named = canonicalise(box["files"]["stl_named"], tmp_path / "n", stem="source")
    assert named.path == box["files"]["stl_named"] and set(named.regions) == GROUPS


def test_a_binary_stl_whose_header_says_solid_is_rewritten_so_every_reader_reads_it(box,
                                                                                     tmp_path):
    from meshpipeline.cad.stl_io import read_stl_solids, read_stl_triangles

    src = tmp_path / "source.stl"
    _binary_stl(src, box["points"][box["tris"]], header=b"solid part exported by SolidWorks")
    canonical = canonicalise(src, tmp_path, stem="source")
    assert canonical.converted and canonical.path != src
    _assert_box(canonical)
    assert len(read_stl_triangles(canonical.path)) == len(box["tris"])
    assert sum(len(t) for t in read_stl_solids(canonical.path).values()) == len(box["tris"])


def test_an_upper_case_ascii_stl_is_rewritten(box, tmp_path):
    src = tmp_path / "source.stl"
    c = box["points"][box["tris"]]
    src.write_text("  SOLID PART\n" + "".join(
        "FACET NORMAL 0 0 0\nOUTER LOOP\n" + "".join(f"VERTEX {v[0]:.17E} {v[1]:.17E} {v[2]:.17E}\n"
                                                     for v in t) + "ENDLOOP\nENDFACET\n"
        for t in c) + "ENDSOLID PART\n")
    _assert_box(canonicalise(src, tmp_path, stem="source"))


def test_an_unreadable_stl_still_goes_on_as_it_always_did(tmp_path):
    src = tmp_path / "source.stl"
    src.write_bytes(b"\x00\x01garbage that no reader here follows")
    canonical = canonicalise(src, tmp_path, stem="source")
    assert canonical.path == src and not canonical.converted


# the bytes decide, not the name

def test_an_stl_named_obj_is_read_as_the_stl_it_is(box, tmp_path):
    src = tmp_path / "part.obj"
    _binary_stl(src, box["points"][box["tris"]])
    assert sniff_format(src) == "stl"
    verdict = check_upload(src, ".obj")
    assert verdict.ok and verdict.suffix == ".stl"
    _assert_box(canonicalise(src, tmp_path / "out", stem="source"))


def test_a_step_named_stl_becomes_a_step(tmp_path):
    src = tmp_path / "part.stl"
    src.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n")
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    assert canonical.kind is GeometryKind.cad and canonical.path.suffix == ".step"


@pytest.mark.parametrize("head,named,tool", [
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 200, ".stl", "native CAD part"),
    (b"**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz**\n**PART1;\n", ".obj", "Parasolid"),
    (b"V5_CFV2" + b"\0" * 100, ".step", "CATIA"),
])
def test_a_renamed_native_cad_file_is_told_how_to_export(tmp_path, head, named, tool):
    src = tmp_path / f"part{named}"
    src.write_bytes(head)
    verdict = check_upload(src, named)
    assert not verdict.ok and tool in verdict.refusal and "STEP" in verdict.refusal
    with pytest.raises(IngestError, match=tool):
        canonicalise(src, tmp_path / "out")


def test_iges_is_recognised_by_its_fixed_columns(tmp_path):
    src = tmp_path / "part.igs"
    src.write_text(" " * 72 + "S0000001\n" + ",,31HOpen CASCADE IGES processor".ljust(72)
                   + "G0000001\n")
    assert sniff_format(src) == "iges"


# upload-time safety: the cheap checks that run on the API

def test_a_step_without_its_header_is_refused_as_before(tmp_path):
    src = tmp_path / "input.step"
    src.write_bytes(b"not a step file at all")
    verdict = check_upload(src, ".step")
    assert verdict.status == 400 and "ISO-10303-21" in verdict.refusal


def test_a_strongly_marked_format_whose_bytes_are_something_else_is_refused(tmp_path):
    src = tmp_path / "input.ply"
    src.write_bytes(b"hello, this is not a mesh")
    verdict = check_upload(src, ".ply")
    assert not verdict.ok and "not a PLY mesh file" in verdict.refusal


def test_a_gltf_that_needs_another_file_is_refused_with_the_way_on(tmp_path):
    src = tmp_path / "input.gltf"
    src.write_text(json.dumps({"asset": {"version": "2.0"}, "meshes": [{"primitives": []}],
                               "buffers": [{"uri": "../../etc/passwd", "byteLength": 10}]}))
    verdict = check_upload(src, ".gltf")
    assert not verdict.ok and ".glb" in verdict.refusal
    with pytest.raises(IngestError):
        canonicalise(src, tmp_path / "out")


def test_a_draco_compressed_gltf_is_refused(tmp_path):
    src = tmp_path / "input.gltf"
    src.write_text(json.dumps({"asset": {"version": "2.0"}, "meshes": [{"primitives": []}],
                               "extensionsRequired": ["KHR_draco_mesh_compression"]}))
    verdict = check_upload(src, ".gltf")
    assert not verdict.ok and "compress" in verdict.refusal


def test_a_3mf_with_an_unsafe_member_path_is_refused(box, tmp_path):
    src = tmp_path / "input.3mf"
    _write_3mf(src, box["points"], box["tris"], box["names"],
               extra_members=[("../../escape.txt", "x")])
    verdict = check_upload(src, ".3mf")
    assert not verdict.ok and "unsafe path" in verdict.refusal


def test_a_zip_bomb_posing_as_3mf_is_refused_before_anything_expands(box, tmp_path, monkeypatch):
    from meshpipeline.cad.ingest import limits

    monkeypatch.setattr(limits, "ZIP_RATIO_FLOOR_BYTES", 1024)
    src = tmp_path / "input.3mf"
    _write_3mf(src, box["points"], box["tris"], box["names"],
               extra_members=[("3D/padding.bin", b"\0" * 4_000_000)])
    verdict = check_upload(src, ".3mf")
    assert not verdict.ok and "zip bomb" in verdict.refusal


def test_a_3mf_that_expands_past_the_cap_while_reading_stops(box, tmp_path, monkeypatch):
    from meshpipeline.cad.ingest import limits

    src = tmp_path / "input.3mf"
    _write_3mf(src, box["points"], box["tris"], box["names"])
    monkeypatch.setattr(limits, "MAX_UNZIPPED_BYTES", 2048)
    with pytest.raises(IngestError):
        canonicalise(src, tmp_path / "out")


def test_a_weakly_marked_format_is_left_to_its_reader(tmp_path):
    src = tmp_path / "input.inp"
    src.write_text("** a comment only\n")
    assert check_upload(src, ".inp").ok
    with pytest.raises(IngestError):
        canonicalise(src, tmp_path / "out")


# the facts other workstreams read, and where they reach

def test_the_canonical_record_says_what_kind_and_from_what(box, tmp_path):
    canonical = canonicalise(box["files"]["msh"], tmp_path, stem="source")
    facts = canonical.facts()
    assert facts["geometry_kind"] == "surface" and facts["source_format"] == "msh"
    assert set(facts["source_regions"]) == GROUPS and facts["converted"] is True
    side = json.loads(Path(str(canonical.path) + ".ingest.json").read_text())
    assert side["source_format"] == "msh" and side["kind"] == "surface"


def test_the_job_materialiser_hands_the_engines_the_canonical_file(box, tmp_path):
    from meshpipeline.application.geometry_materializer import canonical_path
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    from meshpipeline.errors import FailureClass

    src = tmp_path / "source.obj"
    _write_obj(src, box["points"], box["tris"], box["names"])
    assert canonical_path(src) == tmp_path / "source.stl"
    step = tmp_path / "s" / "source.step"
    step.parent.mkdir()
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    assert canonical_path(step) == step              # today's formats: the bytes themselves
    bad = tmp_path / "bad" / "source.obj"
    bad.parent.mkdir()
    bad.write_text("# nothing\n")
    with pytest.raises(GeometrySourceError) as err:
        canonical_path(bad)
    assert err.value.failure_class is FailureClass.USER_INPUT


def test_a_surface_no_engine_reads_natively_is_staged_as_stl(box, tmp_path):
    from tests._geometry_support import interpretation_ref, source_ref

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    from meshpipeline.contracts.geometry_units import LengthUnit

    vtp = tmp_path / "source.vtp"
    import pyvista as pv
    faces = np.hstack([np.full((len(box["tris"]), 1), 3), box["tris"]]).ravel()
    pv.PolyData(box["points"], faces).save(str(vtp))
    ref = source_ref(local_file=vtp, filename="vessel.vtp")
    geom = MaterializedGeometry(
        ref=ref, interpretation=interpretation_ref(geometry_source_id=ref.source_id,
                                                   unit=LengthUnit.millimetre),
        local_path=vtp)
    # snappy reads only STL: the VTP is converted, then scaled to metres like any STL
    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl", engine="snappy")
    st = stats(read_stl(surface.path))
    assert st["volume"] == pytest.approx(VOLUME * 1e-9, rel=1e-5)


def test_vmtk_still_reads_its_own_vtp_natively():
    from meshpipeline.engines.runtime import get_engine

    assert ".vtp" in get_engine("vmtk").native_surface_suffixes
    assert not getattr(get_engine("snappy"), "native_surface_suffixes", ())
