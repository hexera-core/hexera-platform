# The reviewer judged a 279-million-cell mesh without ever seeing one of the three files it is
# told to judge the recipe from.
#
# `run_policy.required_files` for cfmesh_conjugate is conjugate_plan.json,
# constant/triSurface/block.stl and constant/triSurface/fluid.stl, and the reviewer reads them so
# it judges "from the ACTUAL config, not by eyeballing the render". The reader was
# `read_text(encoding="utf-8")` inside a try that logged and skipped. MEASURED on job cba7ac69
# (2026-11-25): "Reviewer: could not read constant/triSurface/fluid.stl: 'utf-8' codec can't decode
# byte 0xcc in position 81" - the engine writes that one as BINARY STL (2,616,784 bytes, 52,334
# triangles, 84 + 50n exactly), so it was simply absent from the evidence.
#
# Its sibling block.stl IS ascii and was read all along - as four thousand characters of vertex
# soup out of a 7.1 MB file, which is noise in a prompt that asks a model to judge a mesh. What is
# evidence about a surface is how finely it was tessellated and over what span.
import struct

import pytest

from meshpipeline.agents.reviewer.context import RECIPE_TEXT_BUDGET, recipe_text


def _binary_stl(path, tris):
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\0\0")
    return path


def _ascii_stl(path, tris):
    out = ["solid test"]
    for tri in tris:
        out.append("facet normal 0 0 0")
        out.append(" outer loop")
        out += [f"  vertex {v[0]} {v[1]} {v[2]}" for v in tri]
        out.append(" endloop")
        out.append("endfacet")
    out.append("endsolid test")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


#: One triangle per unit, spanning 0 to 0.01 m on x, 0 to 0.002 m on y, 0 on z.
TRIS = [
    [(0.0, 0.0, 0.0), (0.01, 0.0, 0.0), (0.0, 0.002, 0.0)],
    [(0.01, 0.002, 0.0), (0.01, 0.0, 0.0), (0.0, 0.002, 0.0)],
]


def test_a_binary_stl_is_described_instead_of_vanishing(tmp_path):
    got = recipe_text(_binary_stl(tmp_path / "fluid.stl", TRIS))
    assert "binary STL" in got, got
    assert "2 triangles" in got, got
    assert "10.0 x 2.0 x 0.0 mm" in got, got


def test_an_ascii_stl_is_described_too_instead_of_pasting_its_triangles(tmp_path):
    got = recipe_text(_ascii_stl(tmp_path / "block.stl", TRIS))
    assert "ascii STL" in got, got
    assert "2 triangles" in got, got
    assert "10.0 x 2.0 x 0.0 mm" in got, got
    assert "vertex" not in got, "the triangle soup is back in the prompt"


def test_the_two_forms_of_the_same_surface_report_the_same_shape(tmp_path):
    """The same two triangles, written both ways, must give the same count and the same span."""
    a = recipe_text(_binary_stl(tmp_path / "a.stl", TRIS))
    b = recipe_text(_ascii_stl(tmp_path / "b.stl", TRIS))
    assert "2 triangles" in a and "2 triangles" in b
    assert "10.0 x 2.0 x 0.0 mm" in a and "10.0 x 2.0 x 0.0 mm" in b
    assert "from (0.0, 0.0, 0.0) mm" in a and "from (0.0, 0.0, 0.0) mm" in b
    # and they are honest about which form they are, because the byte sizes differ
    assert "binary STL" in a and "ascii STL" in b


def test_a_real_config_file_is_still_read_as_text(tmp_path):
    p = tmp_path / "conjugate_plan.json"
    p.write_text('{"cells_across": 10, "expect_fluid_pieces": 2}', encoding="utf-8")
    assert recipe_text(p) == '{"cells_across": 10, "expect_fluid_pieces": 2}'


def test_a_long_config_file_is_still_bounded(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text("x" * (RECIPE_TEXT_BUDGET * 3), encoding="utf-8")
    assert len(recipe_text(p)) == RECIPE_TEXT_BUDGET


def test_a_binary_file_that_is_not_an_stl_says_so_rather_than_raising(tmp_path):
    p = tmp_path / "something.bin"
    p.write_bytes(b"\xcc\xdd\xee" * 100)
    got = recipe_text(p)
    assert "binary file" in got and "300 bytes" in got, got


def test_an_stl_that_cannot_be_parsed_never_raises(tmp_path):
    p = tmp_path / "truncated.stl"
    p.write_bytes(b"\0" * 80 + struct.pack("<I", 99999))   # claims 99,999 triangles, has none
    got = recipe_text(p)
    assert got.startswith("("), got
    assert "STL" in got, got


def test_a_missing_file_never_raises(tmp_path):
    got = recipe_text(tmp_path / "not_here.json")
    assert "could not be read" in got, got


@pytest.mark.parametrize("name", ["fluid.STL", "fluid.Stl"])
def test_the_suffix_is_matched_whatever_its_case(tmp_path, name):
    assert "STL," in recipe_text(_binary_stl(tmp_path / name, TRIS))
