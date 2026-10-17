# Responsibility: Verify a COLLADA (.dae) file becomes the canonical STL at its true size, the right way up, with every instance its scene places.
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.cad.ingest import IngestError, canonicalise, check_upload, sniff_format
from meshpipeline.cad.ingest.surface import read_stl, stats
from meshpipeline.contracts.geometry_units import LengthUnit

#: A box 10 x 20 x 30 (file units) from the origin: three different sides, so a turn shows.
SIZE = (10.0, 20.0, 30.0)
_CORNERS = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], dtype=float)
#: The 12 outward-facing triangles of the unit cube on those corners (index = 4x + 2y + z).
_TRIS = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
_QUADS = np.array([[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4],
                   [1, 5, 7, 3]])


def _box_points(size=SIZE) -> np.ndarray:
    return _CORNERS * np.asarray(size)


def _geometry(gid: str, size=SIZE, *, prim: str = "triangles", name: str = "") -> str:
    pts = " ".join(f"{v:g}" for v in _box_points(size).reshape(-1))
    named = f' name="{name}"' if name else ""
    # a NORMAL input interleaved with the VERTEX one: the index stride is 2
    normals = (f'<source id="{gid}-n"><float_array id="{gid}-na" count="3">0 0 1</float_array>'
               f'<technique_common><accessor source="#{gid}-na" count="1" stride="3">'
               '<param name="X" type="float"/><param name="Y" type="float"/>'
               '<param name="Z" type="float"/></accessor></technique_common></source>')
    inputs = (f'<input semantic="VERTEX" source="#{gid}-v" offset="0"/>'
              f'<input semantic="NORMAL" source="#{gid}-n" offset="1"/>')

    def p(idx) -> str:
        return " ".join(f"{i} 0" for i in np.asarray(idx).reshape(-1))

    if prim == "triangles":
        body = f'<triangles count="12">{inputs}<p>{p(_TRIS)}</p></triangles>'
    elif prim == "polylist":
        body = (f'<polylist count="6">{inputs}<vcount>{" ".join("4" * 6)}</vcount>'
                f'<p>{p(_QUADS)}</p></polylist>')
    elif prim == "polygons":
        body = f'<polygons count="6">{inputs}' + "".join(f"<p>{p(q)}</p>" for q in _QUADS) + \
            "</polygons>"
    elif prim == "trifans":
        body = f'<trifans count="6">{inputs}' + "".join(f"<p>{p(q)}</p>" for q in _QUADS) + \
            "</trifans>"
    else:
        raise ValueError(prim)
    return (f'<geometry id="{gid}"{named}><mesh>'
            f'<source id="{gid}-p"><float_array id="{gid}-pa" count="24">{pts}</float_array>'
            f'<technique_common><accessor source="#{gid}-pa" count="8" stride="3">'
            '<param name="X" type="float"/><param name="Y" type="float"/>'
            '<param name="Z" type="float"/></accessor></technique_common></source>'
            f'{normals}<vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-p"/>'
            f'</vertices>{body}</mesh></geometry>')


def _dae(path: Path, *, geometries: str, nodes: str, library_nodes: str = "",
         unit: str | None = '<unit name="millimeter" meter="0.001"/>', up: str | None = "Z_UP",
         prolog: str = '<?xml version="1.0" encoding="utf-8"?>\n') -> Path:
    asset = "<asset><contributor><authoring_tool>test</authoring_tool></contributor>"
    asset += unit or ""
    asset += f"<up_axis>{up}</up_axis>" if up else ""
    asset += "</asset>"
    lib = f"<library_nodes>{library_nodes}</library_nodes>" if library_nodes else ""
    path.write_text(
        f'{prolog}<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        f'{asset}<library_geometries>{geometries}</library_geometries>{lib}'
        f'<library_visual_scenes><visual_scene id="S">{nodes}</visual_scene>'
        '</library_visual_scenes><scene><instance_visual_scene url="#S"/></scene></COLLADA>')
    return path


def _one_box(path: Path, **kw) -> Path:
    return _dae(path, geometries=_geometry("g"),
                nodes='<node id="n"><instance_geometry url="#g"/></node>', **kw)


def _read(src: Path, tmp_path: Path):
    canonical = canonicalise(src, tmp_path / "out", stem="source")
    return canonical, stats(read_stl(canonical.path))


def _assert_box(st: dict, lo, hi, *, volume: float, n: int = 1) -> None:
    assert st["watertight"], st
    assert np.allclose(st["bounds_min"], lo, atol=1e-6), st["bounds_min"]
    assert np.allclose(st["bounds_max"], hi, atol=1e-6), st["bounds_max"]
    # positive: every box still faces outward, mirrored or not
    assert st["volume"] == pytest.approx(n * volume, rel=1e-6)


VOLUME = SIZE[0] * SIZE[1] * SIZE[2]


# the format: found by its content, read as the triangles its scene places

@pytest.mark.parametrize("prim", ["triangles", "polylist", "polygons", "trifans"])
def test_a_collada_box_becomes_the_canonical_stl_whatever_its_primitives(tmp_path, prim):
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g", prim=prim),
               nodes='<node id="n"><instance_geometry url="#g"/></node>')
    assert sniff_format(src) == "dae"
    canonical, st = _read(src, tmp_path)
    assert canonical.source_format == "dae" and canonical.converted
    assert st["triangles"] == 12
    _assert_box(st, (0, 0, 0), SIZE, volume=VOLUME)


def test_a_collada_file_named_as_something_else_is_read_as_what_it_is(tmp_path):
    src = _one_box(tmp_path / "part.obj")
    assert sniff_format(src) == "dae"
    canonical, st = _read(src, tmp_path)
    assert canonical.source_format == "dae"
    _assert_box(st, (0, 0, 0), SIZE, volume=VOLUME)


# the unit: the file's <unit>, never the spec's default metre when the file says nothing

@pytest.mark.parametrize("unit,expected", [
    ('<unit name="millimeter" meter="0.001"/>', LengthUnit.millimetre),
    ('<unit name="centimeter" meter="0.01"/>', LengthUnit.centimetre),
    ('<unit name="inch" meter="0.0254"/>', LengthUnit.inch),
    ('<unit name="meter" meter="1"/>', LengthUnit.metre),
    ('<unit name="meter"/>', LengthUnit.metre),            # meter defaults to 1.0
    (None, None)])                                          # no <unit>: asked
def test_the_unit_the_file_states_travels_with_the_converted_stl(tmp_path, unit, expected):
    from meshpipeline.cad.unit_evidence import read_declared_unit

    src = _one_box(tmp_path / "box.dae", unit=unit)
    canonical, st = _read(src, tmp_path)
    # the coordinates stay the file's own numbers; the unit says what they mean
    _assert_box(st, (0, 0, 0), SIZE, volume=VOLUME)
    for path in (src, canonical.path):
        evidence = read_declared_unit(path)
        assert (evidence.unit if evidence.resolved else None) is expected


def test_a_unit_outside_the_vocabulary_is_converted_exactly_into_millimetres(tmp_path):
    from meshpipeline.cad.unit_evidence import read_declared_unit

    src = _one_box(tmp_path / "box.dae", unit='<unit name="foot" meter="0.3048"/>')
    canonical, st = _read(src, tmp_path)
    k = 304.8
    _assert_box(st, (0, 0, 0), np.asarray(SIZE) * k, volume=VOLUME * k ** 3)
    evidence = read_declared_unit(canonical.path)
    assert evidence.resolved and evidence.unit is LengthUnit.millimetre
    assert read_declared_unit(src).unit is LengthUnit.millimetre
    assert any("millimetres" in n for n in canonical.notes)


# the up axis: turned so +Z is up, as every engine and the check read it

def test_a_y_up_file_is_turned_so_its_up_is_z(tmp_path):
    canonical, st = _read(_one_box(tmp_path / "box.dae", up="Y_UP"), tmp_path)
    # (x, y, z) -> (x, -z, y): the file's height (y, 20) becomes z, its depth (z) runs along -y
    _assert_box(st, (0, -30, 0), (10, 0, 20), volume=VOLUME)
    assert any("up is +Z" in n for n in canonical.notes)


def test_an_x_up_file_is_turned_so_its_up_is_z(tmp_path):
    _c, st = _read(_one_box(tmp_path / "box.dae", up="X_UP"), tmp_path)
    # (x, y, z) -> (-z, y, x)
    _assert_box(st, (-30, 0, 0), (0, 20, 10), volume=VOLUME)


def test_a_file_that_states_no_up_axis_is_y_up_as_collada_says(tmp_path):
    _c, st = _read(_one_box(tmp_path / "box.dae", up=None), tmp_path)
    _assert_box(st, (0, -30, 0), (10, 0, 20), volume=VOLUME)


def test_the_up_turn_and_the_unit_conversion_compose(tmp_path):
    _c, st = _read(_one_box(tmp_path / "box.dae", up="Y_UP",
                            unit='<unit name="foot" meter="0.3048"/>'), tmp_path)
    k = 304.8
    _assert_box(st, (0, -30 * k, 0), (10 * k, 0, 20 * k), volume=VOLUME * k ** 3)


# the scene: node transforms, instances and several geometries, all placed and merged

def test_node_transforms_are_applied_in_the_order_the_file_lists_them(tmp_path):
    # translate then rotate 90 deg about z then scale x2 in x: p' = T R S p
    node = ('<node id="n"><translate>100 0 0</translate><rotate>0 0 1 90</rotate>'
            '<scale>2 1 1</scale><instance_geometry url="#g"/></node>')
    _c, st = _read(_dae(tmp_path / "box.dae", geometries=_geometry("g"), nodes=node), tmp_path)
    # S: 20 x 20 x 30; R (x -> y, y -> -x): x in [-20, 0], y in [0, 20]; T: x + 100
    _assert_box(st, (80, 0, 0), (100, 20, 30), volume=2 * VOLUME)


def test_a_matrix_is_read_row_by_row(tmp_path):
    node = ('<node id="n"><matrix>1 0 0 5  0 1 0 6  0 0 1 7  0 0 0 1</matrix>'
            '<instance_geometry url="#g"/></node>')
    _c, st = _read(_dae(tmp_path / "box.dae", geometries=_geometry("g"), nodes=node), tmp_path)
    _assert_box(st, (5, 6, 7), (15, 26, 37), volume=VOLUME)


def test_one_geometry_instanced_twice_is_placed_twice(tmp_path):
    library = '<node id="part" name="part"><instance_geometry url="#g"/></node>'
    nodes = ('<node id="a"><translate>0 0 0</translate><instance_node url="#part"/></node>'
             '<node id="b"><translate>50 0 0</translate><instance_node url="#part"/></node>')
    _c, st = _read(_dae(tmp_path / "box.dae", geometries=_geometry("g"), nodes=nodes,
                        library_nodes=library), tmp_path)
    assert st["triangles"] == 24
    _assert_box(st, (0, 0, 0), (60, 20, 30), volume=VOLUME, n=2)


def test_nested_nodes_compose_their_transforms(tmp_path):
    nodes = ('<node id="outer"><translate>0 0 100</translate>'
             '<node id="inner"><scale>0.5 0.5 0.5</scale><instance_geometry url="#g"/></node>'
             '</node>')
    _c, st = _read(_dae(tmp_path / "box.dae", geometries=_geometry("g"), nodes=nodes), tmp_path)
    _assert_box(st, (0, 0, 100), (5, 10, 115), volume=VOLUME / 8)


def test_a_mirroring_instance_still_faces_outward(tmp_path):
    nodes = '<node id="m"><scale>-1 1 1</scale><instance_geometry url="#g"/></node>'
    _c, st = _read(_dae(tmp_path / "box.dae", geometries=_geometry("g"), nodes=nodes), tmp_path)
    _assert_box(st, (-10, 0, 0), (0, 20, 30), volume=VOLUME)


def test_several_geometries_are_merged_and_named_nodes_become_regions(tmp_path):
    geoms = _geometry("g1") + _geometry("g2", size=(5, 5, 5))
    nodes = ('<node id="a" name="fuselage"><instance_geometry url="#g1"/></node>'
             '<node id="b" name="pod"><translate>0 0 -20</translate>'
             '<instance_geometry url="#g2"/></node>')
    canonical, st = _read(_dae(tmp_path / "box.dae", geometries=geoms, nodes=nodes), tmp_path)
    assert st["triangles"] == 24
    assert set(canonical.regions) == {"fuselage", "pod"}
    assert st["volume"] == pytest.approx(VOLUME + 125.0, rel=1e-6)


def test_a_skinned_mesh_is_read_in_its_bind_pose(tmp_path):
    ctrl = ('<library_controllers><controller id="c"><skin source="#g">'
            '<bind_shape_matrix>1 0 0 0  0 1 0 0  0 0 1 40  0 0 0 1</bind_shape_matrix>'
            '</skin></controller></library_controllers>')
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g") + "</library_geometries>" + ctrl
               + "<library_geometries>",
               nodes='<node id="n"><instance_controller url="#c"/></node>')
    _c, st = _read(src, tmp_path)
    _assert_box(st, (0, 0, 40), (10, 20, 70), volume=VOLUME)


def test_a_file_without_a_scene_places_each_geometry_once(tmp_path):
    src = _one_box(tmp_path / "box.dae")
    text = src.read_text()
    cut = text[:text.index("<library_visual_scenes>")] + "</COLLADA>"
    src.write_text(cut)
    _c, st = _read(src, tmp_path)
    _assert_box(st, (0, 0, 0), SIZE, volume=VOLUME)


# the bounds: nothing outside the file, no loops, no expansion without end

def test_geometry_kept_in_another_file_is_refused_with_the_way_on(tmp_path):
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g"),
               nodes='<node id="n"><instance_geometry url="parts.dae#g"/></node>')
    with pytest.raises(IngestError, match="separate files"):
        canonicalise(src, tmp_path / "out")


def test_a_node_that_places_itself_is_refused(tmp_path):
    library = '<node id="loop"><instance_geometry url="#g"/><instance_node url="#loop"/></node>'
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g"),
               nodes='<node id="n"><instance_node url="#loop"/></node>', library_nodes=library)
    with pytest.raises(IngestError, match="inside itself"):
        canonicalise(src, tmp_path / "out")


def test_a_scene_that_asks_for_too_many_placements_stops(tmp_path, monkeypatch):
    from meshpipeline.cad.ingest import collada

    monkeypatch.setattr(collada, "MAX_PLACEMENTS", 10)
    # each level places the next twice: 2^8 placements from a few lines of XML
    library = '<node id="l0"><instance_geometry url="#g"/></node>' + "".join(
        f'<node id="l{i}"><instance_node url="#l{i - 1}"/><instance_node url="#l{i - 1}"/></node>'
        for i in range(1, 9))
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g"),
               nodes='<node id="n"><instance_node url="#l8"/></node>', library_nodes=library)
    with pytest.raises(IngestError, match="places more than"):
        canonicalise(src, tmp_path / "out")


def test_an_xml_entity_declaration_is_refused_at_upload_and_at_read(tmp_path):
    prolog = ('<?xml version="1.0"?>\n<!DOCTYPE COLLADA [<!ENTITY a "aaaaaaaaaa">]>\n')
    src = _one_box(tmp_path / "input.dae", prolog=prolog)
    verdict = check_upload(src, ".dae")
    assert not verdict.ok and "entities" in verdict.refusal
    with pytest.raises(IngestError, match="entities"):
        canonicalise(src, tmp_path / "out")


def test_a_dae_whose_bytes_are_not_collada_is_refused_at_upload(tmp_path):
    src = tmp_path / "input.dae"
    src.write_text('<?xml version="1.0"?><scene><thing/></scene>')
    verdict = check_upload(src, ".dae")
    assert not verdict.ok and "COLLADA" in verdict.refusal


def test_a_good_dae_passes_the_upload_check(tmp_path):
    verdict = check_upload(_one_box(tmp_path / "input.dae"), ".dae")
    assert verdict.ok and verdict.suffix == ".dae"


# polygons are split so the triangles cover each one exactly

def _signed_areas(pts, tris, normal=(0.0, 0.0, 1.0)):
    a, b, c = (pts[tris[:, k]] for k in range(3))
    return np.cross(b - a, c - a) @ np.asarray(normal) / 2.0


def _shoelace(xy):
    x, y = np.asarray(xy).T
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


@pytest.mark.parametrize("ring", [
    [(0, 0), (4, 0), (4, 4), (2, 1), (0, 4)],                    # a notch: fanned, it overlaps
    [(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)],            # an L
    [(0, 0), (2, 1), (4, 0), (2, 4)],                            # a dart (a concave quad)
    [(1, 0), (0, 0), (0, 1), (1, 1)][::-1],                      # a convex quad
])
def test_a_concave_polygon_is_covered_exactly_with_no_triangle_outside_it(ring):
    from meshpipeline.cad.ingest.collada import _polygons

    pts = np.array([(x, y, 0.0) for x, y in ring], dtype=float)
    tris = _polygons(np.arange(len(ring)), np.array([len(ring)]), pts)
    assert len(tris) == len(ring) - 2
    signed = _signed_areas(pts, tris)
    # every triangle faces the same way as the polygon, and together they are its area exactly
    assert (signed > 0).all()
    assert signed.sum() == pytest.approx(_shoelace(ring))


def test_a_negative_corner_count_is_refused_before_anything_is_built():
    from meshpipeline.cad.ingest.collada import _polygons
    from meshpipeline.cad.ingest.surface import SurfaceError

    pts = np.zeros((3, 3))
    with pytest.raises(SurfaceError, match="negative"):
        _polygons(np.array([0, 1, 2]), np.array([1_000_000_000, -1_000_000_000]), pts)
    with pytest.raises(SurfaceError, match="shorter"):
        _polygons(np.array([0, 1, 2]), np.array([2_000_000_000]), pts)


def test_polygons_with_holes_are_refused_with_the_way_on(tmp_path):
    geom = _geometry("g", prim="polygons").replace(
        "</polygons>", "<ph><p>0 0 1 0 3 0</p><h>0 0 1 0 3 0</h></ph></polygons>")
    src = _dae(tmp_path / "box.dae", geometries=geom,
               nodes='<node id="n"><instance_geometry url="#g"/></node>')
    with pytest.raises(IngestError, match="triangulated"):
        canonicalise(src, tmp_path / "out")


def test_an_assembly_with_one_part_in_another_file_is_refused_not_read_short(tmp_path):
    nodes = ('<node id="a"><instance_geometry url="#g"/></node>'
             '<node id="b"><instance_geometry url="wheel.dae#w"/></node>')
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g"), nodes=nodes)
    with pytest.raises(IngestError, match="separate files"):
        canonicalise(src, tmp_path / "out")


def test_a_skin_built_from_itself_is_refused(tmp_path):
    ctrl = ('<library_controllers><controller id="c"><skin source="#c"/></controller>'
            '</library_controllers>')
    src = _dae(tmp_path / "box.dae", geometries=_geometry("g") + "</library_geometries>" + ctrl
               + "<library_geometries>",
               nodes='<node id="n"><instance_controller url="#c"/></node>')
    with pytest.raises(IngestError, match="built from itself"):
        canonicalise(src, tmp_path / "out")


def test_a_huge_concave_face_is_refused_before_the_quadratic_split(monkeypatch):
    from meshpipeline.cad.ingest import collada
    from meshpipeline.cad.ingest.surface import SurfaceError

    # a 6,000-corner circle with one corner pushed in: concave, and past the corner limit
    n = 6000
    a = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    r = np.ones(n)
    r[10] = 0.5
    pts = np.c_[r * np.cos(a), r * np.sin(a), np.zeros(n)]
    with pytest.raises(SurfaceError, match="triangulated"):
        collada._polygons(np.arange(n), np.array([n]), pts)
    # ... and many small concave faces together stop at the work budget
    monkeypatch.setattr(collada, "MAX_EAR_CLIP_WORK", 100)
    ring = np.array([(0, 0, 0), (4, 0, 0), (4, 4, 0), (2, 1, 0), (0, 4, 0)], dtype=float)
    pts = np.vstack([ring] * 10)
    with pytest.raises(SurfaceError, match="triangulated"):
        collada._polygons(np.arange(50), np.full(10, 5), pts)
