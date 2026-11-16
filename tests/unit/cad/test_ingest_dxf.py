# Responsibility: Verify a DXF (.dxf) file becomes the canonical STL at its true size, with one group per layer, whichever mesh entity the drawing used.
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.cad.ingest import IngestError, canonicalise, check_upload, sniff_format
from meshpipeline.cad.ingest.dxf import BINARY_SENTINEL
from meshpipeline.cad.ingest.surface import read_stl, stats

#: The shape every fixture holds: a 10 x 10 x 10 box at the origin.
EDGE = 10.0
AREA, VOLUME = 6 * EDGE ** 2, EDGE ** 3
_CORNERS = [(x * EDGE, y * EDGE, z * EDGE)
            for x in (0, 1) for y in (0, 1) for z in (0, 1)]
#: The 12 outward-facing triangles of that box (corner index = 4x + 2y + z).
_TRIS = [[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
         [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]]


def _tags(*pairs) -> str:
    """Group code / value lines, right-aligned in three columns as AutoCAD writes them."""
    return "".join(f"{int(code):>3}\n{value}\n" for code, value in pairs)


def _drawing(entities: str, *, section: str = "ENTITIES") -> str:
    return (_tags((0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, "0"), (0, "ENDSEC"))
            + _tags((0, "SECTION"), (2, section)) + entities + _tags((0, "ENDSEC"), (0, "EOF")))


def _polyface(layer: str = "box", tris=None) -> str:
    """A POLYLINE polyface mesh: free vertices (flag 192), then one face record each (flag 128),
    numbering corners from 1 and leaving the fourth slot unused on a triangle."""
    tris = _TRIS if tris is None else tris
    out = _tags((0, "POLYLINE"), (8, layer), (66, "1"), (70, "64"),
                (71, str(len(_CORNERS))), (72, str(len(tris))))
    for x, y, z in _CORNERS:
        out += _tags((0, "VERTEX"), (8, layer), (10, f"{x:g}"), (20, f"{y:g}"), (30, f"{z:g}"),
                     (70, "192"))
    for a, b, c in tris:
        out += _tags((0, "VERTEX"), (8, layer), (10, "0"), (20, "0"), (30, "0"), (70, "128"),
                     (71, str(a + 1)), (72, str(b + 1)), (73, str(c + 1)), (74, "0"))
    out += _tags((0, "SEQEND"), (8, layer))
    return out


def _3dfaces(layer: str = "box") -> str:
    """The same box as 3DFACE entities, each triangle written with its last corner repeated -
    the way a three-sided 3DFACE states its unused fourth slot."""
    out = ""
    for a, b, c in _TRIS:
        p = [_CORNERS[i] for i in (a, b, c, c)]
        out += _tags((0, "3DFACE"), (8, layer),
                     *[(10 + k, f"{p[k][0]:g}") for k in range(4)],
                     *[(20 + k, f"{p[k][1]:g}") for k in range(4)],
                     *[(30 + k, f"{p[k][2]:g}") for k in range(4)])
    return out


def _write(tmp_path: Path, text: str, name: str = "model.dxf") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="latin-1")
    return p


def _canonical(src: Path, tmp_path: Path):
    out = canonicalise(src, tmp_path / "out")
    return out, stats(read_stl(Path(out.path)))


@pytest.mark.parametrize("entities", [_polyface(), _3dfaces()], ids=["polyface", "3dface"])
def test_box_becomes_the_canonical_stl_at_its_true_size(tmp_path, entities):
    out, measured = _canonical(_write(tmp_path, _drawing(entities)), tmp_path)

    assert out.source_format == "dxf"
    assert out.converted is True
    assert measured["triangles"] == 12
    assert measured["area"] == pytest.approx(AREA)
    assert measured["watertight"] is True
    assert measured["volume"] == pytest.approx(VOLUME)


def test_each_layer_becomes_its_own_named_group(tmp_path):
    #: two bodies on two layers: the regions a CFD case puts its boundary conditions on
    text = _drawing(_polyface(layer="hub") + _polyface(layer="shroud"))

    out, measured = _canonical(_write(tmp_path, text), tmp_path)

    assert set(out.regions) == {"hub", "shroud"}
    assert measured["triangles"] == 24            # both bodies, neither merged into the other


def test_a_drawing_on_the_default_layer_is_left_unnamed(tmp_path):
    out = canonicalise(_write(tmp_path, _drawing(_polyface(layer="0"))), tmp_path / "out")

    assert out.regions == ()


def test_a_polygon_mesh_grid_is_read_as_its_quads(tmp_path):
    #: a 2 x 3 open grid in the z=0 plane: 1 x 2 = 2 quads = 4 triangles
    verts = [(float(i) * EDGE, float(j) * EDGE, 0.0) for i in range(2) for j in range(3)]
    out = _tags((0, "POLYLINE"), (8, "deck"), (66, "1"), (70, "16"), (71, "2"), (72, "3"))
    for x, y, z in verts:
        out += _tags((0, "VERTEX"), (8, "deck"), (10, f"{x:g}"), (20, f"{y:g}"), (30, f"{z:g}"),
                     (70, "192"))
    out += _tags((0, "SEQEND"), (8, "deck"))

    _, measured = _canonical(_write(tmp_path, _drawing(out)), tmp_path)

    assert measured["triangles"] == 4
    assert measured["area"] == pytest.approx(2 * EDGE * EDGE)


def test_a_subd_mesh_entity_is_read_as_its_faces(tmp_path):
    #: one MESH (AcDbSubDMesh): four vertices, one quad, written as a flat face list
    verts = [(0.0, 0.0, 0.0), (EDGE, 0.0, 0.0), (EDGE, EDGE, 0.0), (0.0, EDGE, 0.0)]
    out = _tags((0, "MESH"), (8, "lid"), (71, "2"), (72, "0"), (91, "0"),
                (92, str(len(verts))))
    for x, y, z in verts:
        out += _tags((10, f"{x:g}"), (20, f"{y:g}"), (30, f"{z:g}"))
    out += _tags((93, "5"), (90, "4"), (90, "0"), (90, "1"), (90, "2"), (90, "3"))

    _, measured = _canonical(_write(tmp_path, _drawing(out)), tmp_path)

    assert measured["triangles"] == 2
    assert measured["area"] == pytest.approx(EDGE * EDGE)


def test_a_dxf_is_recognised_by_its_contents_whatever_it_is_named(tmp_path):
    src = _write(tmp_path, _drawing(_polyface()), name="survey.obj")

    assert sniff_format(src) == "dxf"
    assert check_upload(src, ".obj").suffix == ".dxf"


def test_a_file_named_dxf_that_is_not_one_is_refused_at_upload(tmp_path):
    src = tmp_path / "survey.dxf"
    src.write_text("just some notes about the survey\n")

    verdict = check_upload(src, ".dxf")

    assert "not a DXF" in verdict.refusal


def test_geometry_in_a_block_definition_is_not_read_as_model_space(tmp_path):
    #: the faces live in BLOCKS; model space only references them, and a reference is not expanded
    text = (_drawing(_polyface(), section="BLOCKS").removesuffix(_tags((0, "EOF")))
            + _tags((0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (8, "box"), (2, "BOXBLOCK"),
                    (10, "0"), (20, "0"), (30, "0"), (0, "ENDSEC"), (0, "EOF")))

    with pytest.raises(IngestError) as caught:
        canonicalise(_write(tmp_path, text), tmp_path / "out")

    assert "INSERT" in str(caught.value)


def test_a_drawing_with_no_mesh_entity_says_so(tmp_path):
    lines = _tags((0, "LINE"), (8, "centreline"), (10, "0"), (20, "0"), (30, "0"),
                  (11, "1"), (21, "1"), (31, "1"))

    with pytest.raises(IngestError) as caught:
        canonicalise(_write(tmp_path, _drawing(lines)), tmp_path / "out")

    assert "no mesh faces" in str(caught.value)


def test_a_binary_dxf_is_refused_with_the_way_on(tmp_path):
    src = tmp_path / "binary.dxf"
    src.write_bytes(BINARY_SENTINEL + b"\x00" * 512)

    with pytest.raises(IngestError) as caught:
        canonicalise(src, tmp_path / "out")

    assert "binary DXF" in str(caught.value)
    assert "ASCII" in str(caught.value)


def test_a_hidden_edge_and_an_unused_slot_do_not_change_the_surface(tmp_path):
    """DXF negates a corner index to hide that edge when drawn, and leaves 0 in a slot it does not
    use; neither says anything about the surface."""
    plain = _polyface(tris=[[0, 1, 3]])
    hidden = plain.replace(_tags((71, "1")), _tags((71, "-1")))

    _, measured = _canonical(_write(tmp_path, _drawing(hidden)), tmp_path)

    assert measured["triangles"] == 1
    corners = read_stl(tmp_path / "out" / "model.stl").corners()[0]
    assert np.allclose(np.sort(corners, axis=0),
                       np.sort(np.array([_CORNERS[i] for i in (0, 1, 3)]), axis=0))
