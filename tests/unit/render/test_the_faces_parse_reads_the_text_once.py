# Responsibility: the ascii faces stream is read in ONE pass, and reads the same arrays as before.
# Boundaries: `render/boundary_index._stream_boundary_faces` only - no index is written here.
# Collaborates with: the polyMesh ascii faceList format.
"""The corner counts were found by a second pass over the same bytes.

`np.fromstring` already yields every digit run in a chunk, in order - the translate table turns
the brackets into spaces and a face id is never negative - so the counts were ALREADY in the
array the function builds. A regex pass was reading the same text again to find them, and it was
98.6% of the chunk body's time.

A count is the digit run a '(' follows, which `searchsorted` resolves for every bracket at once.
Measured on a 29.0 MiB chunk of 900,000 quad records: 12.6 MB/s before, 49.8 MB/s after, 3.96x,
worth about 144 s off this plate's 380 s index at its 71,851,833 boundary faces. The sizes and
connectivity arrays were byte-identical to the old code's on 348 MiB spanning several chunk
boundaries.

THE INDEX DECIDES WHICH FACES A READER IS SHOWN when they zoom past what the merged drawing can
carry, so these tests are about the arrays and not about the clock.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.render import boundary_index as BI

HEADER = (b"FoamFile\n{\n    version 2.0;\n    format ascii;\n"
          b"    class faceList;\n    object faces;\n}\n\n")


def faces_file(path, n, *, corners=4, sep=b" ", before=b"", trunc=False):
    with path.open("wb") as fh:
        fh.write(HEADER)
        fh.write(b"%d\n(\n" % n)
        pid = 0
        for i in range(n):
            ids = sep.join(b"%d" % (pid + j) for j in range(corners))
            if trunc and i == n - 1:
                ids = sep.join(b"%d" % (pid + j) for j in range(corners - 1))
            fh.write(b"%d" % corners + before + b"(" + ids + b")\n")
            pid += corners
        fh.write(b")\n")
    return path


def expected(n, corners=4):
    sizes = np.full(n, corners, dtype=np.uint8)
    conn = np.arange(n * corners, dtype=np.uint32)
    return sizes, conn


# ------------------------------------------------------------------ one pass, not two

def test_the_module_no_longer_carries_a_corner_count_regex():
    """The regex had exactly one reader and it was the second pass over the chunk."""
    assert not hasattr(BI, "_SIZE_RE")


def test_the_counts_are_classified_out_of_the_integer_pass():
    import inspect

    src = inspect.getsource(BI._stream_boundary_faces)
    assert "searchsorted" in src
    # the code, not the prose: a comment may legitimately mention the pass it replaced
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert ".findall(" not in code, "a second pass over the chunk is back"
    # and the interleaving arithmetic the regex path needed is gone with it
    assert "prefix_at" not in code and "np.delete" not in code


# ------------------------------------------------------------------ the arrays

@pytest.mark.parametrize("corners", [3, 4, 5, 8])
def test_it_reads_the_declared_faces(tmp_path, corners):
    p = faces_file(tmp_path / "faces", 500, corners=corners)
    sizes, conn = BI._stream_boundary_faces(p, 0, 500)
    es, ec = expected(500, corners)
    assert np.array_equal(sizes, es)
    assert np.array_equal(conn, ec)
    assert sizes.dtype == np.uint8 and conn.dtype == np.uint32


@pytest.mark.parametrize("sep,before", [
    (b" ", b""),            # what cfMesh writes
    (b"   ", b""),          # a run of spaces inside a record
    (b"\n", b""),           # a record spread over lines
    (b" ", b" "),           # the `\s*` the old regex allowed before the bracket
    (b" ", b"\t"),
    (b" ", b"  "),
])
def test_the_shapes_the_format_allows_all_read_the_same(tmp_path, sep, before):
    p = faces_file(tmp_path / "faces", 300, sep=sep, before=before)
    sizes, conn = BI._stream_boundary_faces(p, 0, 300)
    es, ec = expected(300)
    assert np.array_equal(sizes, es) and np.array_equal(conn, ec)


@pytest.mark.parametrize("want", [1, 2, 17, 299, 300])
def test_it_stops_at_the_face_count_it_was_given(tmp_path, want):
    """The declared count is the authority; a chunk holding more records must not add them."""
    p = faces_file(tmp_path / "faces", 300)
    sizes, conn = BI._stream_boundary_faces(p, 0, want)
    assert sizes.size == want
    assert conn.size == want * 4
    assert np.array_equal(conn, np.arange(want * 4, dtype=np.uint32))


# ------------------------------------------------------------------ what it must refuse

def test_a_face_with_more_corners_than_the_index_can_hold_is_refused(tmp_path):
    """The sizes array is uint8, so 256 corners would wrap round into a plausible small face."""
    p = tmp_path / "faces"
    with p.open("wb") as fh:
        fh.write(HEADER + b"1\n(\n")
        fh.write(b"300(" + b" ".join(b"%d" % j for j in range(300)) + b")\n")
        fh.write(b")\n")
    with pytest.raises(BI.IndexUnavailable, match="corners"):
        BI._stream_boundary_faces(p, 0, 1)


def test_a_record_with_fewer_ids_than_it_declared_is_refused(tmp_path):
    """Counting the ids rather than trusting the count is the whole point of the check."""
    p = faces_file(tmp_path / "faces", 50, trunc=True)
    with pytest.raises(BI.IndexUnavailable, match="connectivity|counts"):
        BI._stream_boundary_faces(p, 0, 50)


def test_a_file_with_fewer_faces_than_declared_is_refused(tmp_path):
    """Caught before a byte is parsed, off the list's own declared count."""
    p = faces_file(tmp_path / "faces", 10)
    with pytest.raises(BI.IndexUnavailable, match="exceeds the faces file's own count"):
        BI._stream_boundary_faces(p, 0, 99)


# ------------------------------------------------------------------ across a chunk boundary

def test_a_record_split_by_a_chunk_boundary_is_still_one_record(tmp_path, monkeypatch):
    """CHUNK_BYTES is 64 MiB in production; a test that needed 64 MiB to cover the carry would
    never be run, so the chunk is shrunk instead of the file grown."""
    monkeypatch.setattr(BI, "CHUNK_BYTES", 97)
    p = faces_file(tmp_path / "faces", 400)
    sizes, conn = BI._stream_boundary_faces(p, 0, 400)
    es, ec = expected(400)
    assert np.array_equal(sizes, es) and np.array_equal(conn, ec)


@pytest.mark.parametrize("chunk", [41, 64, 97, 128, 1021])
def test_every_small_chunk_size_reads_the_same_arrays(tmp_path, monkeypatch, chunk):
    monkeypatch.setattr(BI, "CHUNK_BYTES", chunk)
    p = faces_file(tmp_path / "faces", 250, corners=3)
    sizes, conn = BI._stream_boundary_faces(p, 0, 250)
    es, ec = expected(250, 3)
    assert np.array_equal(sizes, es) and np.array_equal(conn, ec)
