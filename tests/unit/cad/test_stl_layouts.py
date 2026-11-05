# Responsibility: Verify every STL reader in the staging path reads the file's real layout - binary
# or ASCII from the bytes, never from the header's first word - and that scaling one to metres
# never writes an empty surface.
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.cad.normalise import scale_stl_file
from meshpipeline.cad.stl_io import (
    StlReadError,
    inspect_stl,
    is_ascii_stl,
    read_stl_solids,
    read_stl_triangles,
    stl_layout,
)
from meshpipeline.contracts.coordinate_state import from_source_file
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)

#: Two triangles of a unit square at z=0 and one at z=2: extent 1 x 1 x 2 in the file's units.
TRIS = (
    ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
    ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
    ((0.0, 0.0, 2.0), (1.0, 0.0, 2.0), (0.0, 1.0, 2.0)),
)


def _prepared(unit: LengthUnit):
    return from_source_file(GeometryInterpretation("i-1", "owner-a", "src-1", unit,
                                                   scale_to_metres(unit), ResolutionBasis.user_confirmed))


def _binary(path: Path, tris=TRIS, *, header: bytes = b"", count: int | None = None,
            tail: bytes = b"", drop_bytes: int = 0) -> Path:
    body = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in tris)
    data = header.ljust(80, b" ")[:80] + struct.pack("<I", len(tris) if count is None else count) + body + tail
    path.write_bytes(data[:len(data) - drop_bytes] if drop_bytes else data)
    return path


def _ascii(path: Path, solids: dict, *, first_line: str | None = None, upper: bool = False,
           bom: bool = False, newline: str = "\n", indent: str = "  ") -> Path:
    lines: list[str] = []
    for name, tris in solids.items():
        lines.append(first_line if (first_line is not None and not lines) else f"solid {name}".rstrip())
        for tri in tris:
            lines += [f"{indent}facet normal 0 0 1", f"{indent * 2}outer loop"]
            lines += [f"{indent * 3}vertex {v[0]} {v[1]} {v[2]}" for v in tri]
            lines += [f"{indent * 2}endloop", f"{indent}endfacet"]
        lines.append(f"endsolid {name}".rstrip())
    text = newline.join(lines) + newline
    if upper:
        text = text.upper()
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("ascii"))
    return path


def _extent(path: Path) -> np.ndarray:
    c = np.asarray(read_stl_triangles(path), dtype=float).reshape(-1, 3)
    return c.max(axis=0) - c.min(axis=0)


# ----------------------------------------------------------------------------- ASCII --------
def test_a_bare_solid_line_with_no_name_is_read_and_scaled(tmp_path):
    # the OpenFOAM cyclone tutorial's surface opens "solid\n": the named reader skipped every
    # facet, the scaled copy came out empty and configure_mesh died on "unpack requires a buffer
    # of 4 bytes"
    raw = _ascii(tmp_path / "cyclone.stl", {"": TRIS}, first_line="solid", indent="    ")
    assert is_ascii_stl(raw)
    assert len(read_stl_triangles(raw)) == 3
    assert sum(len(t) for t in read_stl_solids(raw).values()) == 3

    out = scale_stl_file(raw, tmp_path / "input.stl", _prepared(LengthUnit.centimetre))
    assert len(read_stl_triangles(out)) == 3
    assert _extent(out) == pytest.approx([0.01, 0.01, 0.02])
    assert inspect_stl(tmp_path, "input.stl")["n_triangles"] == 3


@pytest.mark.parametrize("variant", [
    {"upper": True}, {"bom": True}, {"newline": "\r\n"}, {"indent": "\t"},
    {"first_line": "\n\nsolid part"},
])
def test_ascii_spellings_every_writer_uses_read_alike(tmp_path, variant):
    raw = _ascii(tmp_path / "part.stl", {"part": TRIS}, **variant)
    assert is_ascii_stl(raw)
    assert len(read_stl_triangles(raw)) == 3
    out = scale_stl_file(raw, tmp_path / "scaled.stl", _prepared(LengthUnit.millimetre))
    assert _extent(out) == pytest.approx([0.001, 0.001, 0.002])


def test_solid_names_survive_scaling(tmp_path):
    raw = _ascii(tmp_path / "named.stl", {"inlet": TRIS[:1], "wall": TRIS[1:]})
    out = scale_stl_file(raw, tmp_path / "scaled.stl", _prepared(LengthUnit.millimetre))
    solids = read_stl_solids(out)
    assert {n: len(t) for n, t in solids.items()} == {"inlet": 1, "wall": 2}


def test_a_facet_loop_of_four_vertices_is_fanned_into_two_triangles(tmp_path):
    p = tmp_path / "quad.stl"
    p.write_text("solid q\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
                 "vertex 1 1 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid q\n")
    assert len(read_stl_triangles(p)) == 2


def test_an_ascii_file_with_no_triangles_is_never_scaled_to_an_empty_surface(tmp_path):
    p = tmp_path / "empty_solid.stl"
    p.write_text("solid nothing\nendsolid nothing\n")
    with pytest.raises(StlReadError):
        scale_stl_file(p, tmp_path / "scaled.stl", _prepared(LengthUnit.millimetre))
    assert not (tmp_path / "scaled.stl").exists()


# ----------------------------------------------------------------------------- binary -------
def test_a_binary_file_whose_header_says_solid_is_read_as_binary(tmp_path):
    raw = _binary(tmp_path / "sw.stl", header=b"solid SolidWorks export facet normal vertex")
    assert not is_ascii_stl(raw)
    assert len(read_stl_triangles(raw)) == 3
    assert {k: len(v) for k, v in read_stl_solids(raw).items()} == {"sw": 3}

    out = scale_stl_file(raw, tmp_path / "input.stl", _prepared(LengthUnit.centimetre))
    assert not is_ascii_stl(out)
    assert _extent(out) == pytest.approx([0.01, 0.01, 0.02])


@pytest.mark.parametrize("case", ["count_zero", "count_too_big", "cut_short", "padded"])
def test_a_binary_count_that_disagrees_with_the_length_reads_the_records_the_file_holds(tmp_path, case):
    kw = {"count_zero": {"count": 0}, "count_too_big": {"count": 1000},
          "cut_short": {"drop_bytes": 20}, "padded": {"tail": b"\0" * 7}}[case]
    raw = _binary(tmp_path / f"{case}.stl", **kw)
    expected = 2 if case == "cut_short" else 3       # a record cut in half is not a triangle
    assert len(read_stl_triangles(raw)) == expected

    out = scale_stl_file(raw, tmp_path / "scaled.stl", _prepared(LengthUnit.millimetre))
    assert len(read_stl_triangles(out)) == expected


def test_read_stl_in_ingest_reads_a_binary_file_cut_short(tmp_path):
    from meshpipeline.cad.ingest.surface import read_stl

    raw = _binary(tmp_path / "cut.stl", header=b"solid x", drop_bytes=20)
    assert read_stl(raw).corners().shape == (2, 3, 3)


def test_binary_scaling_keeps_float32_values_exactly(tmp_path):
    tris = (((0.1, 0.2, 0.3), (1.7, 0.0, 0.0), (0.0, 2.9, 0.0)),)
    raw = _binary(tmp_path / "f.stl", tris)
    out = scale_stl_file(raw, tmp_path / "scaled.stl", _prepared(LengthUnit.millimetre))
    got = np.asarray(read_stl_triangles(out), dtype=float)
    want = (np.asarray(tris, dtype=np.float32).astype(np.float64) * 0.001).astype(np.float32)
    assert np.array_equal(got, want.astype(np.float64))


# ----------------------------------------------------------------------------- not an STL ---
def test_an_empty_file_is_said_in_words_not_as_a_struct_error(tmp_path):
    p = tmp_path / "empty.stl"
    p.write_bytes(b"")
    with pytest.raises(StlReadError, match="empty"):
        read_stl_triangles(p)
    assert "error" in inspect_stl(tmp_path, "empty.stl")


def test_short_garbage_is_said_in_words(tmp_path):
    p = tmp_path / "junk.stl"
    p.write_bytes(b"\x01\x02junk" * 4)
    with pytest.raises(StlReadError):
        read_stl_triangles(p)


def test_layout_rules_on_bytes_alone():
    rec = b"\0" * 50
    exact = b"solid header".ljust(80) + struct.pack("<I", 2) + rec * 2
    assert stl_layout(exact[:4096], len(exact)).binary
    assert stl_layout(exact[:4096], len(exact)).triangles == 2
    text = b"solid\n facet normal 0 0 1\n  outer loop\n"
    assert not stl_layout(text, len(text)).binary
