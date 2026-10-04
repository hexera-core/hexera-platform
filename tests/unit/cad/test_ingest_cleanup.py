# Responsibility: Verify a mesh dressed as STEP goes down the surface road, and the canonical surface's safe cleanup changes no shape.
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.cad.ingest import canonicalise
from meshpipeline.cad.ingest.surface import SurfaceMesh, read_stl, stats, tidy
from meshpipeline.contracts.intake_formats import GeometryKind


def _sphere(n_lat: int = 12, n_lon: int = 16, r: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """A closed, consistently outward triangulated sphere (n_lon * (2 n_lat - 2) triangles)."""
    pts = [(0.0, 0.0, r)]
    for i in range(1, n_lat):
        th = np.pi * i / n_lat
        for j in range(n_lon):
            ph = 2 * np.pi * j / n_lon
            pts.append((r * np.sin(th) * np.cos(ph), r * np.sin(th) * np.sin(ph), r * np.cos(th)))
    pts.append((0.0, 0.0, -r))
    ring = lambda i, j: 1 + (i - 1) * n_lon + (j % n_lon)  # noqa: E731
    tris = []
    for j in range(n_lon):
        tris.append((0, ring(1, j), ring(1, j + 1)))
    for i in range(1, n_lat - 1):
        for j in range(n_lon):
            a, b, c, d = ring(i, j), ring(i, j + 1), ring(i + 1, j), ring(i + 1, j + 1)
            tris += [(a, c, b), (b, c, d)]
    south = len(pts) - 1
    for j in range(n_lon):
        tris.append((south, ring(n_lat - 1, j + 1), ring(n_lat - 1, j)))
    return np.asarray(pts, dtype=np.float64), np.asarray(tris, dtype=np.int64)


def _signed_volume(points, tris) -> float:
    c = points[tris]
    return float(np.einsum("ij,ij->i", c[:, 0], np.cross(c[:, 1], c[:, 2])).sum() / 6.0)


# the safe cleanup

def test_a_clean_surface_is_left_exactly_as_it_was():
    p, t = _sphere()
    out, report = tidy(SurfaceMesh(p, t))
    assert not report.changed and out.n_triangles == len(t)
    assert _signed_volume(out.points, out.triangles) == pytest.approx(_signed_volume(p, t))


def test_a_face_written_twice_keeps_one_copy_and_closes():
    p, t = _sphere()
    out, report = tidy(SurfaceMesh(p, np.vstack([t, t[5:6]])))
    assert report.duplicates == 1 and out.n_triangles == len(t)
    assert stats(out)["watertight"]


def test_a_zero_thickness_sliver_of_two_copies_goes_entirely():
    # the Fluent aorta's defect: a tiny triangle written twice, hanging off one edge of the wall
    p, t = _sphere()
    a, b, _ = t[0]
    tip = len(p)
    p2 = np.vstack([p, (p[a] + p[b]) / 2 + np.array([0.01, 0.0, 0.0])])
    fin = np.array([[a, b, tip], [a, b, tip]])
    before = stats(SurfaceMesh(p2, np.vstack([t, fin])))
    assert before["nonmanifold_edges"] == 1
    out, report = tidy(SurfaceMesh(p2, np.vstack([t, fin])))
    assert report.duplicates == 2
    after = stats(out)
    assert after["watertight"] and after["nonmanifold_edges"] == 0 and after["open_edges"] == 0
    assert after["volume"] == pytest.approx(stats(SurfaceMesh(p, t))["volume"])


def test_triangles_facing_the_wrong_way_are_turned_and_nothing_moves():
    p, t = _sphere()
    bad = t.copy()
    bad[:7] = bad[:7, ::-1]
    out, report = tidy(SurfaceMesh(p, bad))
    assert report.turned == 7
    assert _signed_volume(out.points, out.triangles) == pytest.approx(_signed_volume(p, t))
    np.testing.assert_allclose(np.sort(out.points, axis=0), np.sort(p, axis=0))


def test_the_majority_decides_so_a_consistently_inward_file_is_not_flipped():
    p, t = _sphere()
    inward = t[:, ::-1]
    out, report = tidy(SurfaceMesh(p, inward))
    assert report.turned == 0
    assert _signed_volume(out.points, out.triangles) < 0


def test_a_wall_two_unnamed_regions_share_keeps_both_its_copies():
    # two closed tetrahedra glued on face (0, 1, 2), written as ONE unnamed surface - how a
    # multi-region file without names arrives. The shared face is real twice: once per region.
    p = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0, -1]], dtype=float)
    a = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)]
    b = [(0, 1, 2), (0, 4, 1), (1, 4, 2), (2, 4, 0)]
    out, report = tidy(SurfaceMesh(p, np.array(a + b)))
    assert report.duplicates == 0 and out.n_triangles == 8


def test_two_regions_sharing_a_face_keep_their_copy_each():
    p, t = _sphere()
    group = np.r_[np.zeros(len(t), dtype=np.int64), [1]]
    out, report = tidy(SurfaceMesh(p, np.vstack([t, t[3:4]]), group, ["fluid", "solid"]))
    assert report.duplicates == 0 and out.n_triangles == len(t) + 1


def test_an_uploaded_stl_with_a_sliver_is_rewritten_and_a_clean_one_is_not(tmp_path):
    from meshpipeline.cad.ingest.surface import write_binary_stl

    p, t = _sphere()
    clean = tmp_path / "clean.stl"
    write_binary_stl(clean, p[t])
    before = clean.read_bytes()
    c = canonicalise(clean, tmp_path / "a", stem="source")
    assert c.path == clean and not c.converted and clean.read_bytes() == before
    a, b, _ = t[0]
    p2 = np.vstack([p, (p[a] + p[b]) / 2 + np.array([0.01, 0.0, 0.0])])
    fin = np.array([[a, b, len(p)], [a, b, len(p)]])
    dirty = tmp_path / "dirty.stl"
    write_binary_stl(dirty, p2[np.vstack([t, fin])])
    c = canonicalise(dirty, tmp_path / "b", stem="source")
    assert c.converted and any("sliver" in n for n in c.notes)
    assert stats(read_stl(c.path))["watertight"]


# a mesh dressed as STEP

def _faceted_brep_step(path: Path, p: np.ndarray, t: np.ndarray, unit: str = ".METRE.") -> None:
    """A FACETED_BREP the way mesh converters write one: every triangle a POLY_LOOP face."""
    lines = ["ISO-10303-21;", "HEADER;", "FILE_DESCRIPTION(('faceted mesh'),'2;1');",
             "FILE_NAME('m.stp','2024-01-01',(''),(''),'','converter','');",
             "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));", "ENDSEC;", "DATA;"]
    nid = 100
    pid = {}
    for i, xyz in enumerate(p):
        pid[i] = nid
        lines.append(f"#{nid}=CARTESIAN_POINT('',({xyz[0]:.12g},{xyz[1]:.12g},{xyz[2]:.12g}));")
        nid += 1
    faces = []
    for a, b, c in t:
        loop, bound, face = nid, nid + 1, nid + 2
        lines.append(f"#{loop}=POLY_LOOP('',(#{pid[a]},#{pid[b]},#{pid[c]}));")
        lines.append(f"#{bound}=FACE_OUTER_BOUND('',#{loop},.T.);")
        lines.append(f"#{face}=FACE_SURFACE('',(#{bound}),#10,.T.);")
        faces.append(f"#{face}")
        nid += 3
    lines += ["#10=PLANE('',#11);", "#11=AXIS2_PLACEMENT_3D('',#12,#13,#14);",
              "#12=CARTESIAN_POINT('',(0.,0.,0.));", "#13=DIRECTION('',(0.,0.,1.));",
              "#14=DIRECTION('',(1.,0.,0.));",
              f"#20=CLOSED_SHELL('',({','.join(faces)}));", "#21=FACETED_BREP('',#20);",
              f"#22=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT($,{unit}));",
              "ENDSEC;", "END-ISO-10303-21;"]
    path.write_text("\n".join(lines) + "\n")


def test_a_faceted_step_takes_the_surface_road_in_its_own_numbers(tmp_path):
    from meshpipeline.cad.unit_evidence import read_declared_unit

    p, t = _sphere()
    src = tmp_path / "scan.stp"
    _faceted_brep_step(src, p, t)
    c = canonicalise(src, tmp_path / "out", stem="source")
    assert c.kind is GeometryKind.surface and c.path.suffix == ".stl"
    assert any("faceted mesh" in n for n in c.notes)
    st = stats(read_stl(c.path))
    # the numbers as written (a metre label did NOT turn them into millimetres x 1000)
    assert st["watertight"] and st["triangles"] == len(t)
    assert st["volume"] == pytest.approx(abs(_signed_volume(p, t)), rel=1e-5)
    # a converter's unit label is not the part's unit: it is asked, at upload and after
    for path in (src, c.path):
        evidence = read_declared_unit(path)
        assert not evidence.resolved and "faceted mesh" in evidence.detail


def test_an_occ_written_faceted_step_with_edge_loops_takes_the_surface_road(tmp_path):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.StlAPI import StlAPI_Reader
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad.ingest.cad import write_step
    from meshpipeline.cad.ingest.surface import write_binary_stl

    p, t = _sphere()
    stl = tmp_path / "s.stl"
    write_binary_stl(stl, p[t])
    shape = TopoDS_Shape()
    StlAPI_Reader().Read(shape, str(stl))
    sew = BRepBuilderAPI_Sewing(1e-6)
    sew.Add(shape)
    sew.Perform()
    step = tmp_path / "s.step"
    write_step(sew.SewedShape(), step)
    c = canonicalise(step, tmp_path / "out", stem="source")
    assert c.kind is GeometryKind.surface
    st = stats(read_stl(c.path))
    assert st["watertight"] and st["volume"] == pytest.approx(abs(_signed_volume(p, t)), rel=1e-5)


def test_real_cad_stays_cad(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    from meshpipeline.cad.ingest.cad import write_step

    step = tmp_path / "pipe.step"
    write_step(BRepPrimAPI_MakeCylinder(5.0, 40.0).Shape(), step)
    c = canonicalise(step, tmp_path / "out", stem="source")
    assert c.kind is GeometryKind.cad and c.path == step and not c.converted
