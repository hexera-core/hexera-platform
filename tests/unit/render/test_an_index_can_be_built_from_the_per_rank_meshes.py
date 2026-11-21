# Responsibility: an index built from the per-rank meshes must describe the same surface as one
# built from the same mesh reconstructed.
# Boundaries: ASCII and binary polyMesh files, and the sidecar on disk. No OpenFOAM, no subprocess.
"""The boundary index can be built without a merged mesh, and draws the same surface.

WHY THIS EXISTS. With `CONJUGATE_MERGE_REGIONS=0` the chain never reconstructs, so
`constant/<region>/polyMesh` holds the patch table `_publish_decomposed_boundaries` wrote and no
cells at all. `write_index` reads `faces` and `points` from beside that boundary file and there
are none, so the detail window would refuse every region of a decomposed delivery by name - which
is what `detail_at` was taught to say in an earlier commit. This is the other half: the index gets
built from `processor*/` instead, and nothing downstream can tell.

WHAT HAS TO BE TRUE for that to be safe, and what the arms here pin:

  - THE FACES ARE THE SAME FACES. A region's real boundary is the union of its non-`processor`
    patches across the ranks, and the faces on a cut between ranks are excluded - each was an
    INTERNAL face of the whole mesh and is a boundary face on both ranks that share it, so taking
    them in draws walls through the middle of the part. Three arms compare the assembled surface
    against a serial index of the same geometry, centroid for centroid.
  - THE PATCH TABLE IS THE PUBLISHED ONE. `open_index` cross-checks the index's patch table
    against the `boundary` file beside it on every open, so the two have to come from one producer
    (`decomposed_region_boundary`) rather than from two readings of the same ranks.
  - THE GATE RUNS ON EVERY REGION. The hand-run script this replaces gated on three hardcoded
    face counts and skipped the gate for any region not among those three names, so on a new case
    the check that made it trustworthy did not run at all.
  - EITHER FORMAT IS READ. Measured on the real four-rank case: its reconstructed copy is ascii
    and its processor directories are binary, from the same run. An ascii-only reader would have
    refused the only real decomposed mesh there is to check any of this against.

THE REAL-CASE ARM IS THE ONE THAT MATTERS. `runs/unit_cell_100um-4across` is a genuine four-rank
case carrying BOTH the per-rank meshes and the reconstructed ones, so neither side's answer is
anybody's opinion. It is skipped when the directory is absent so the suite stays hermetic, and
the hand-built fixtures - in both formats - cover the same ground where it is.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from foam_fixtures import write_decomposed_row_of_hexes, write_row_of_hexes
from meshpipeline.engines.cfmesh_conjugate.conjugate import _publish_decomposed_boundaries
from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import (
    decomposed_region_boundary,
    processor_dirs,
    read_boundary_patches,
)
from meshpipeline.render.boundary_index import (
    INDEX_VERSION,
    IndexUnavailable,
    _foam_format,
    index_dir,
    index_is_decomposed,
    open_index,
    write_index,
    write_index_decomposed,
)

REAL = Path(__file__).resolve().parents[3] / "runs" / "unit_cell_100um-4across"
needs_real = pytest.mark.skipif(not (REAL / "processor0").is_dir(),
                                reason="the real 4-rank case is not on this machine")

#: The three files of a rank's polyMesh this builder reads, and the only ones a test needs to copy.
READ_FILES = ("boundary", "faces", "points")


def _real_case(dest: Path, region: str) -> Path:
    """The real case's ranks at `dest`, with the boundary file the chain would publish beside them.

    Copied rather than read in place because the sidecar is written beside the region's boundary,
    and a test does not write into `runs/`.
    """
    for d in processor_dirs(REAL):
        out = dest / d.name / "constant" / region / "polyMesh"
        out.mkdir(parents=True, exist_ok=True)
        for name in READ_FILES:
            shutil.copy2(d / "constant" / region / "polyMesh" / name, out / name)
    # THE PRODUCT'S OWN PUBLISHER, not a boundary file this test wrote. It is the other reader of
    # `decomposed_region_boundary`, and the whole point of the patch-table arms below is that the
    # two agree, which a hand-written file here would quietly stop testing.
    assert _publish_decomposed_boundaries(dest) == [region]
    return dest


def _canon(a):
    """`a`'s rows in a canonical order, so two surfaces can be compared as sets of faces.

    THE ORDER IS NOT EXPECTED TO MATCH. The serial index numbers faces as the mesh's boundary
    file does; this builder assembles them patch-major over the ranks, and on the real case the
    two patch orders differ already - the reconstructed fluid lists ymin, ymax, fluid_to_solid and
    rank 0 lists them in that order while the faces themselves are spread over four ranks. What
    has to match is WHICH faces are there, so the comparison is made order-independent on purpose
    rather than by accident.
    """
    a = np.asarray(a, dtype=np.float64)
    return a[np.lexsort((a[:, 2], a[:, 1], a[:, 0]))]


def _corner_histogram(sizes):
    return np.bincount(np.asarray(sizes, dtype=np.int64), minlength=9).tolist()


# -- the real four-rank case ----------------------------------------------------------------

@needs_real
def test_the_real_case_is_the_mixed_pair_this_was_built_for():
    """Said as a measurement rather than left implicit: the reconstructed copy is ascii and the
    ranks are binary, in one case from one run. This is why `_foam_format` asks the file."""
    assert _foam_format(REAL / "constant" / "fluid" / "polyMesh" / "faces") == "ascii"
    assert _foam_format(REAL / "processor0" / "constant" / "fluid" / "polyMesh" / "faces") \
        == "binary"


@needs_real
@pytest.mark.parametrize("region", ["fluid", "solid"])
def test_the_decomposed_index_draws_the_same_surface_as_the_reconstructed_one(tmp_path, region):
    """The acceptance test for indexing without a merge, on a mesh nobody hand-wrote."""
    case = _real_case(tmp_path / "dec", region)
    dec = write_index_decomposed(case, region=region)

    ser_pm = tmp_path / "ser" / "polyMesh"
    ser_pm.mkdir(parents=True)
    for name in READ_FILES:
        shutil.copy2(REAL / "constant" / region / "polyMesh" / name, ser_pm / name)
    ser = write_index(ser_pm, region=region)

    assert dec["n_faces"] == ser["n_faces"], (
        f"{region}: the ranks assemble {dec['n_faces']:,} boundary faces and the reconstructed "
        f"mesh has {ser['n_faces']:,}")
    assert ({p["name"]: p["n_faces"] for p in dec["patches"]}
            == {p["name"]: p["n_faces"] for p in ser["patches"]})

    d_idx = open_index(case / "constant" / region / "polyMesh")
    s_idx = open_index(ser_pm)
    # A TOLERANCE AND NOT EQUALITY, because the two sides read two files in two formats: the
    # ranks' packed float64 and the reconstructed mesh's ascii print of the same points. The
    # arithmetic after them is identical, so this is tight enough to catch a face that moved and
    # loose enough not to fail on the last digit of OpenFOAM's own output.
    assert np.allclose(_canon(d_idx.centroids), _canon(s_idx.centroids), rtol=0, atol=1e-9), (
        f"{region}: the assembled surface's face centres are not the reconstructed mesh's")
    assert _corner_histogram(d_idx.sizes) == _corner_histogram(s_idx.sizes), (
        f"{region}: the assembled faces do not have the same corner counts as the mesh's")


@needs_real
@pytest.mark.parametrize("region", ["fluid", "solid"])
def test_the_faces_on_a_cut_between_ranks_are_not_in_the_delivered_surface(tmp_path, region):
    """Rank-summed boundary faces are the real ones plus twice the cut; only the real ones ship."""
    real = sum(int(r["n_faces"]) for r in decomposed_region_boundary(REAL, region))
    summed = sum(int(p["n_faces"])
                 for d in processor_dirs(REAL)
                 for p in read_boundary_patches(d / "constant" / region / "polyMesh"))
    assert summed > real, "this case has no processor faces, so it cannot test their exclusion"
    meta = write_index_decomposed(_real_case(tmp_path, region), region=region)
    assert meta["n_faces"] == real
    assert meta["n_faces"] != summed, (
        f"{region}: the index carries every patch the ranks list, {summed - real:,} of which are "
        "faces on a cut between ranks and are interior to the mesh")


@needs_real
def test_a_decomposed_index_opens_against_the_boundary_the_chain_published(tmp_path):
    """`open_index` checks the index's patch table against the boundary file beside it, on every
    open. Two readings of the same ranks that disagreed would make every detail request refuse,
    with nothing wrong with the mesh or the index."""
    case = _real_case(tmp_path, "fluid")
    write_index_decomposed(case, region="fluid")
    pm = case / "constant" / "fluid" / "polyMesh"
    idx = open_index(pm)                                    # the cross-check runs in here
    published = read_boundary_patches(pm)
    assert [str(p["name"]) for p in idx.patches] == [str(p["name"]) for p in published], (
        "the published order is the index's order, so a reader walking both sees one surface")
    assert ({str(p["name"]): int(p["n_faces"]) for p in idx.patches}
            == {str(p["name"]): int(p["n_faces"]) for p in published})


# -- the same geometry written both ways ----------------------------------------------------

@pytest.mark.parametrize("binary", [False, True], ids=["ascii", "binary"])
def test_three_hexes_split_three_ways_index_as_three_hexes_whole(tmp_path, binary):
    """`write_decomposed_row_of_hexes(ranks=3, n_cells=3)` is `write_row_of_hexes`' own geometry:
    three unit hexes along x, inlet at x=0, outlet at x=3, twelve wall faces. Cut three ways it
    has one processor face on each side of two cuts, and must still index as the same surface -
    written either way, because the real decomposed mesh is binary and the serial one is not."""
    dec = tmp_path / "dec"
    counts = write_decomposed_row_of_hexes(dec, ranks=3, n_cells=3, binary=binary)
    assert counts["processor_faces"] == 4, "two cuts, one face each side"
    _publish_decomposed_boundaries(dec)
    d_meta = write_index_decomposed(dec)

    ser_pm = tmp_path / "ser" / "polyMesh"
    write_row_of_hexes(ser_pm)
    s_meta = write_index(ser_pm)

    assert d_meta["n_faces"] == s_meta["n_faces"] == 14
    assert ({p["name"]: p["n_faces"] for p in d_meta["patches"]}
            == {p["name"]: p["n_faces"] for p in s_meta["patches"]}
            == {"inlet": 1, "outlet": 1, "wall": 12})
    d_idx, s_idx = open_index(dec / "constant" / "polyMesh"), open_index(ser_pm)
    # EXACTLY, here: both sides are the same integer coordinates, so any difference is a face that
    # moved or a face that is not the same face.
    assert np.array_equal(_canon(d_idx.centroids), _canon(s_idx.centroids))
    assert _corner_histogram(d_idx.sizes) == _corner_histogram(s_idx.sizes)


def test_the_two_formats_index_to_the_same_bytes(tmp_path):
    """The format is how the numbers are written down and nothing else, so the sidecar built from
    one must be the sidecar built from the other - not merely close."""
    metas, arrays = {}, {}
    for form in (False, True):
        case = tmp_path / ("bin" if form else "asc")
        write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid", binary=form)
        _publish_decomposed_boundaries(case)
        metas[form] = write_index_decomposed(case, region="fluid")
        idx = open_index(case / "constant" / "fluid" / "polyMesh")
        arrays[form] = (np.asarray(idx.centroids), np.asarray(idx.sizes), np.asarray(idx.conn),
                        np.asarray(idx.points))
    assert metas[False]["patches"] == metas[True]["patches"]
    assert metas[False]["grid"] == metas[True]["grid"]
    assert metas[False]["n_faces"] == metas[True]["n_faces"]
    assert metas[False]["n_points"] == metas[True]["n_points"]
    for a, b in zip(arrays[False], arrays[True], strict=True):
        assert np.array_equal(a, b)


def test_the_points_are_not_welded_across_the_ranks(tmp_path):
    """A point on a cut appears once per rank that touches it, and that is the trade taken.

    It costs a few per cent of vertices and changes no face, no centroid and no bucket. Welding
    would mean comparing tens of millions of float triples for equality on the meshes this is for.
    """
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6)
    _publish_decomposed_boundaries(case)
    meta = write_index_decomposed(case)
    idx = open_index(case / "constant" / "polyMesh")
    pts = np.asarray(idx.points)
    assert meta["n_points"] == pts.shape[0]
    assert np.unique(pts, axis=0).shape[0] < pts.shape[0], (
        "three ranks of one row share points on two cuts, so some point is carried twice")
    # AND IT CHANGES NO FACE. Every face still names points that exist and keeps its own corners.
    conn = np.asarray(idx.conn)
    assert int(conn.max()) < pts.shape[0]
    assert int(np.asarray(idx.sizes, dtype=np.int64).sum()) == conn.shape[0]


def test_the_sidecar_lands_beside_the_published_boundary(tmp_path):
    """Where `open_index` and `detail_at` look, which is the region's own polyMesh - not a scratch
    directory the hand-run script chose with an environment variable."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    write_index_decomposed(case, region="fluid")
    pm = case / "constant" / "fluid" / "polyMesh"
    assert sorted(p.name for p in index_dir(pm).iterdir()) == [
        "bucket_face.u32", "bucket_start.i64", "centroids.f32", "conn.u32", "meta.json",
        "patch_of.u16", "points.f32", "sizes.u8"]


def test_the_meta_says_it_is_not_a_reconstruct(tmp_path):
    """A later reader must not take this sidecar for one built from a serial mesh, because there is
    no serial mesh. The `source` stamp is the record of that and the only one there will be."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=4, n_cells=8, region="fluid")
    _publish_decomposed_boundaries(case)
    meta = write_index_decomposed(case, region="fluid")
    assert meta["version"] == INDEX_VERSION
    assert meta["source"]["built_from"] == "decomposed"
    assert meta["source"]["n_processors"] == 4
    assert meta["source"]["ranks"] == ["processor0", "processor1", "processor2", "processor3"]
    assert "no serial volume mesh" in meta["source"]["note"]
    # THE PUBLISHED BOUNDARY IS STILL STAMPED, and it is the one file of the mesh that sits beside
    # the sidecar. `open_index` compares it on every open, so a boundary file rewritten under a
    # standing index is still caught - which is the only stamp a decomposed delivery can have.
    assert "sha256" in meta["source"]["boundary"]
    assert "faces" not in meta["source"] and "points" not in meta["source"]


def test_a_rewritten_boundary_under_a_standing_index_is_still_caught(tmp_path):
    """The stamp is not decoration: the patch table is what a reader is answered from."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    write_index_decomposed(case, region="fluid")
    pm = case / "constant" / "fluid" / "polyMesh"
    open_index(pm)
    (pm / "boundary").write_text((pm / "boundary").read_text() + "\n// touched\n")
    with pytest.raises(IndexUnavailable, match="changed since"):
        open_index(pm)


def test_the_meta_first_face_is_zero_because_there_is_no_global_numbering(tmp_path):
    """On the meta this key is the first boundary face's id in the mesh's own global face
    numbering, and each rank numbers from zero, so the mesh has none. Every reader of the sidecar
    uses the patch-local `first_face` instead (`index_surface` says so where it reads them)."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    meta = write_index_decomposed(case, region="fluid")
    assert meta["first_face"] == 0
    assert [p["first_face"] for p in meta["patches"]] == [0, 1, 2]


def test_an_extra_rank_with_no_cells_of_this_region_is_not_an_error(tmp_path):
    """An interior rank of a big decomposition can hold cells whose every outside face is a cut to
    another rank, so a rank with no real boundary is normal and contributes nothing."""
    case = tmp_path / "c"
    counts = write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    (case / "processor2" / "constant" / "fluid" / "polyMesh").mkdir(parents=True)
    _publish_decomposed_boundaries(case)
    meta = write_index_decomposed(case, region="fluid")
    assert meta["n_faces"] == sum(counts["patches"].values())
    assert meta["source"]["n_processors"] == 3


# -- what it refuses, and by name -----------------------------------------------------------

def test_a_case_with_no_ranks_is_refused_by_name(tmp_path):
    (tmp_path / "constant" / "fluid" / "polyMesh").mkdir(parents=True)
    with pytest.raises(IndexUnavailable, match="no processor"):
        write_index_decomposed(tmp_path, region="fluid")


def test_a_region_none_of_the_ranks_holds_is_refused_by_name(tmp_path):
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    with pytest.raises(IndexUnavailable, match="solid"):
        write_index_decomposed(case, region="solid")


def test_real_patches_that_are_not_contiguous_are_refused(tmp_path):
    """One read covers the real patches and nothing else only because the processor patches come
    after them. A hole takes faces on a cut into the delivered surface, where they draw as a wall
    that is not there - so it is checked rather than assumed."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    pm = case / "processor0" / "constant" / "fluid" / "polyMesh"
    text = (pm / "boundary").read_text()
    # The wall is the one patch with eight faces, so this names it without naming a line number.
    # Moving its start leaves a face in the middle of the span that no patch claims.
    assert text.count("nFaces 8;\n        startFace 2;") == 1
    (pm / "boundary").write_text(
        text.replace("nFaces 8;\n        startFace 2;", "nFaces 8;\n        startFace 3;", 1))
    with pytest.raises(IndexUnavailable, match="not contiguous"):
        write_index_decomposed(case, region="fluid")


def test_a_rank_carrying_a_patch_the_regions_own_table_does_not_have_is_refused(tmp_path,
                                                                                monkeypatch):
    """The two readings of the ranks have to agree.

    `decomposed_region_boundary` reads them for the patch table and this builder reads them again
    for the faces, and nothing makes those two reads atomic. A patch present in the second and
    absent from the first would be assembled into a surface the published boundary does not
    describe, and `open_index` would then refuse the sidecar on every request with nothing wrong
    with either file - so it is caught here, where it can be named.
    """
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    real = decomposed_region_boundary

    def without_the_wall(c, region=""):
        return [dict(r) for r in real(c, region) if r["name"] != "wall"]

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.polymesh_surface.decomposed_region_boundary",
        without_the_wall)
    with pytest.raises(IndexUnavailable, match="wall"):
        write_index_decomposed(case, region="fluid")


def test_a_patch_that_does_not_assemble_to_its_declared_count_is_refused(tmp_path, monkeypatch):
    """THE GATE THE HAND-RUN SCRIPT HARDCODED. It held three face counts from one case and skipped
    the check for any other region name, so the one thing that made it trustworthy did not run
    where it was needed. Here the declaration is the ranks' own and every region has one."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    real = decomposed_region_boundary

    def one_too_many(c, region=""):
        rows = [dict(r) for r in real(c, region)]
        rows[0]["n_faces"] += 1
        return rows

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.polymesh_surface.decomposed_region_boundary",
        one_too_many)
    with pytest.raises(IndexUnavailable, match="declare"):
        write_index_decomposed(case, region="fluid")


# -- the binary form, read from the file rather than assumed --------------------------------

def test_a_format_this_does_not_read_is_refused_by_its_own_name(tmp_path):
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    pm = case / "processor0" / "constant" / "fluid" / "polyMesh"
    (pm / "points").write_text(
        (pm / "points").read_text().replace("format      ascii;", "format      gzip;"))
    with pytest.raises(IndexUnavailable, match="gzip"):
        write_index_decomposed(case, region="fluid")


def test_a_binary_mesh_with_no_arch_line_is_refused_rather_than_guessed_at(tmp_path):
    """The guess that is wrong produces a face table rather than an error, which is why there is
    no default here. Every polyMesh OpenFOAM writes carries the line."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid", binary=True)
    _publish_decomposed_boundaries(case)
    pm = case / "processor0" / "constant" / "fluid" / "polyMesh"
    raw = (pm / "faces").read_bytes()
    assert b"arch" in raw
    (pm / "faces").write_bytes(raw.replace(b'arch        "LSB;label=32;scalar=64";\n', b"", 1))
    with pytest.raises(IndexUnavailable, match="arch"):
        write_index_decomposed(case, region="fluid")


def test_the_arch_line_is_read_and_not_assumed(tmp_path):
    """A label64 mesh read as label32 comes back as every other number interleaved with zeros - a
    face table that parses cleanly and describes nothing. So the widths come off the file, and a
    file whose line disagrees with its bytes is refused by a gate rather than answered."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid", binary=True)
    _publish_decomposed_boundaries(case)
    pm = case / "processor0" / "constant" / "fluid" / "polyMesh"
    raw = (pm / "faces").read_bytes()
    (pm / "faces").write_bytes(raw.replace(b"label=32", b"label=64", 1))
    with pytest.raises(IndexUnavailable):
        write_index_decomposed(case, region="fluid")
    assert not (index_dir(case / "constant" / "fluid" / "polyMesh") / "meta.json").is_file(), (
        "a mesh this could not read must leave no sidecar at all, because a sidecar is what a "
        "later reader is answered from")


def test_a_width_pair_this_cannot_size_is_refused(tmp_path):
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid", binary=True)
    _publish_decomposed_boundaries(case)
    pm = case / "processor0" / "constant" / "fluid" / "polyMesh"
    raw = (pm / "faces").read_bytes()
    (pm / "faces").write_bytes(raw.replace(b"scalar=64", b"scalar=16", 1))
    with pytest.raises(IndexUnavailable, match="width pair"):
        write_index_decomposed(case, region="fluid")


def test_a_big_endian_mesh_is_refused(tmp_path):
    """Every byte would be the wrong way round, and nothing downstream would say so."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid", binary=True)
    _publish_decomposed_boundaries(case)
    pm = case / "processor0" / "constant" / "fluid" / "polyMesh"
    raw = (pm / "faces").read_bytes()
    (pm / "faces").write_bytes(raw.replace(b"LSB;", b"MSB;", 1))
    with pytest.raises(IndexUnavailable, match="little-endian"):
        write_index_decomposed(case, region="fluid")


# -- which builder a delivery needs ---------------------------------------------------------

def test_a_region_whose_cells_sit_beside_its_boundary_is_not_decomposed(tmp_path):
    """A MERGED delivery keeps its processor directories until the reconstruct has finished, so
    "are there ranks" is true in both cases and says nothing. The file the index reads is the
    question worth asking."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=4, region="fluid")
    _publish_decomposed_boundaries(case)
    pm = case / "constant" / "fluid" / "polyMesh"
    assert index_is_decomposed(case, pm) is True
    shutil.copy2(case / "processor0" / "constant" / "fluid" / "polyMesh" / "faces", pm / "faces")
    assert index_is_decomposed(case, pm) is False, (
        "a reconstructed region must be indexed by write_index, which reads the whole mesh")


def test_a_serial_delivery_with_no_ranks_at_all_is_not_decomposed(tmp_path):
    pm = tmp_path / "ser" / "polyMesh"
    write_row_of_hexes(pm)
    assert index_is_decomposed(tmp_path, pm) is False


def test_the_sidecar_on_disk_is_the_meta_that_was_returned(tmp_path):
    """The caller's report and the file a later reader opens are the same measurement."""
    case = tmp_path / "c"
    write_decomposed_row_of_hexes(case, ranks=2, n_cells=8, region="fluid")
    _publish_decomposed_boundaries(case)
    meta = write_index_decomposed(case, region="fluid")
    on_disk = json.loads(
        (index_dir(case / "constant" / "fluid" / "polyMesh") / "meta.json").read_text())
    assert on_disk == meta
    assert sum(meta["bytes"].values()) > 0
