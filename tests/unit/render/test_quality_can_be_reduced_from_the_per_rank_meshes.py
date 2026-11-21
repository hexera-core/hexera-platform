# Responsibility: per-face quality reduced from the per-rank meshes must be numbered against the
# decomposed index, face for face, and must be checkMesh's own measurement.
# Boundaries: ASCII and binary polyMesh files, checkMesh's written fields and its log. No OpenFOAM.
"""The quality sidecar can be reduced without a merged mesh, and lands on the right faces.

WHY THIS EXISTS. The index is what the detail window cuts with; this is what colours it. With
`CONJUGATE_MERGE_REGIONS=0` there is no serial mesh to reduce - checkMesh ran across the ranks and
wrote its fields into `processor*/constant/<region>/` - so a decomposed delivery would have opened
in the viewer with no colours at all, which is the half of the deliverable a customer looks at.

THE FAILURE THIS FILE IS MOSTLY ABOUT. A quality array that walked the ranks in a different order
than the index did would be exactly the right length, would sum to exactly the right patch totals,
and would pass every gate there is to write - and it would paint the metal's numbers on the
coolant. The hand-run scripts this replaces had the rank loop twice and said so in their own
docstring. The product has it once (`polymesh_surface.decomposed_rank_spans`), and
`test_every_value_lands_on_the_face_that_produced_it` is what holds that: the fixture's values
encode which rank and which patch they came from, so a misalignment is not a tolerance away, it
is a different array.

AND THE GATE THAT MAKES ANY OF IT TRUSTWORTHY is checkMesh's own printed summary, which comes out
of a walk this code has no part in. For the two owner-cell metrics the maximum over all cells of
all ranks must BE the maximum it printed - the same cells, so the same number - and the boundary's
skewness must not exceed the whole mesh's, because the boundary is a subset of the faces. A
disagreement loses the colours and says why, which beats a plausible wrong number on a picture a
customer is looking at.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from foam_fixtures import (
    write_decomposed_row_of_hexes,
    write_quality_fields,
    write_row_of_hexes,
)
from meshpipeline.engines.cfmesh_conjugate.conjugate import _publish_decomposed_boundaries
from meshpipeline.engines.cfmesh_conjugate.foam_exec import CHECKMESH_LOG
from meshpipeline.render.boundary_index import (
    IndexUnavailable,
    open_index,
    write_index,
    write_index_decomposed,
)
from meshpipeline.render.boundary_quality import (
    QUALITY_VERSION,
    QualityUnavailable,
    _all_patch_values,
    open_quality,
    quality_dir,
    write_quality,
    write_quality_decomposed,
)

#: One cell per rank for a three-rank cut of a three-cell row, so rank r holds cell r and the
#: owner-cell metrics have a known answer on every face.
CELLS = [1.5, 2.5, 3.5]

#: Each rank's real patches and their face counts, from `_write_one_rank`'s own shape: the row is
#: cut along x, so rank 0 carries the inlet, rank 2 the outlet, and every rank four wall faces.
RANK_PATCHES = {0: {"inlet": 1, "wall": 4}, 1: {"wall": 4}, 2: {"outlet": 1, "wall": 4}}

#: What a value says about where it came from: `rank * 1000 + patch_base + i`. A rank order that
#: flipped, or a patch cut in the wrong place, then produces a DIFFERENT array rather than a close
#: one. This is the only check there is on the alignment, so it has to be exact.
PATCH_BASE = {"inlet": 0, "outlet": 100, "wall": 200}

#: Above every coded value the fixture writes, so the whole-mesh skewness gate is satisfied by a
#: fixture whose numbers were chosen to be readable rather than realistic.
CODED_CEILING = 3000.0


def _coded(rank: int, patch: str, n: int) -> list:
    return [float(rank * 1000 + PATCH_BASE[patch] + i) for i in range(n)]


def _decomposed_case(root: Path, *, region="fluid", binary=False, fields=True) -> Path:
    """Three unit hexes cut three ways, with checkMesh's fields written on each rank."""
    case = root / "dec"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region=region, binary=binary)
    if fields:
        for r, patches in RANK_PATCHES.items():
            write_quality_fields(
                case / f"processor{r}" / "constant" / region,
                cells=[CELLS[r]],
                per_face={"skewness": {p: _coded(r, p, n) for p, n in patches.items()}},
                uniform={"nonOrthoAngle": dict.fromkeys(patches, 0.0)},
                patches=tuple(patches))
    assert _publish_decomposed_boundaries(case) == [region]
    write_index_decomposed(case, region=region)
    return case


def _log(case: Path, region: str, *, aspect=max(CELLS), non_ortho=max(CELLS),
         skew=CODED_CEILING) -> None:
    """checkMesh's summary lines, in the file `foam_exec` names and nothing else spells."""
    (case / CHECKMESH_LOG.format(suffix=f"_{region}" if region else "")).write_text(
        "Checking geometry...\n"
        f"    Mesh non-orthogonality Max: {non_ortho} average: 0.1\n"
        f"    Max skewness = {skew} OK.\n"
        f"    Max aspect ratio = {aspect} OK.\n"
        "Mesh OK.\nEnd\n")


def _pm(case: Path, region: str = "fluid") -> Path:
    return (case / "constant" / region / "polyMesh") if region else (case / "constant" / "polyMesh")


def _by_patch(polymesh: Path, metric: str) -> dict:
    """One metric's values, cut up the way the index says the surface is cut up."""
    idx = open_index(polymesh)
    q = open_quality(polymesh)
    vals = q.values(metric, np.arange(idx.n_faces))
    return {str(p["name"]): np.asarray(vals[int(p["first_face"]):
                                            int(p["first_face"]) + int(p["n_faces"])],
                                       dtype=np.float64).tolist()
            for p in idx.patches}


# -- the alignment, which is what has no other check ----------------------------------------

def test_every_value_lands_on_the_face_that_produced_it(tmp_path):
    """The fixture's skewness encodes its own rank and patch, so this is an exact statement about
    which face each number ended up on - not a statement about lengths and totals, which a rank
    order that flipped would also satisfy."""
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    meta = write_quality_decomposed(case, region="fluid")
    assert meta["n_faces"] == 14
    assert _by_patch(_pm(case), "skewness") == {
        "inlet": _coded(0, "inlet", 1),
        "outlet": _coded(2, "outlet", 1),
        # RANK ORDER WITHIN THE PATCH, which is the index's own assembly order. Reversed, these
        # twelve are the same twelve numbers in a different place on the mesh.
        "wall": _coded(0, "wall", 4) + _coded(1, "wall", 4) + _coded(2, "wall", 4),
    }


def test_the_owner_cell_metrics_are_the_owner_cells_values(tmp_path):
    """Rank r holds cell r, so every boundary face of rank r must carry cell r's number. A face
    taking the wrong cell's value is the quiet version of this whole file's failure."""
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    write_quality_decomposed(case, region="fluid")
    for key in ("aspect_ratio", "non_ortho"):
        assert _by_patch(_pm(case), key) == {
            "inlet": [CELLS[0]], "outlet": [CELLS[2]],
            "wall": [CELLS[0]] * 4 + [CELLS[1]] * 4 + [CELLS[2]] * 4}, key


# -- the same geometry reduced both ways ----------------------------------------------------

def test_the_decomposed_reduction_is_the_serial_one(tmp_path):
    """The three-cell row written whole and written in three ranks: the same cells, the same
    faces, the same checkMesh output, so the sidecar has to come out the same patch by patch."""
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    dec = write_quality_decomposed(case, region="fluid")

    ser = tmp_path / "ser"
    pm = ser / "polyMesh"
    write_row_of_hexes(pm)
    write_quality_fields(
        ser, cells=CELLS,
        per_face={"skewness": {"inlet": _coded(0, "inlet", 1),
                               "outlet": _coded(2, "outlet", 1),
                               "wall": (_coded(0, "wall", 4) + _coded(1, "wall", 4)
                                        + _coded(2, "wall", 4))}},
        uniform={"nonOrthoAngle": {"inlet": 0.0, "outlet": 0.0, "wall": 0.0}})
    write_index(pm)
    ser_meta = write_quality(pm)

    assert sorted(dec["basis"]) == sorted(ser_meta["basis"])
    for key in sorted(dec["basis"]):
        assert _by_patch(_pm(case), key) == _by_patch(pm, key), key
    assert dec["figures"] == ser_meta["figures"]


@pytest.mark.parametrize("binary", [False, True], ids=["ascii", "binary"])
def test_the_owner_file_is_read_in_either_format(tmp_path, binary):
    """A binary labelList is two seeks and an ascii one is a walk, and the real decomposed mesh is
    binary. A reader that assumed binary and met ascii read four digit bytes as one int32 -
    0x39393939, "9999", 960,051,513, the same handful of values on every rank - and the
    cell-range gate is the only thing that caught it, which is why it is not optional."""
    case = _decomposed_case(tmp_path, binary=binary)
    _log(case, "fluid")
    write_quality_decomposed(case, region="fluid")
    assert _by_patch(_pm(case), "aspect_ratio") == {
        "inlet": [CELLS[0]], "outlet": [CELLS[2]],
        "wall": [CELLS[0]] * 4 + [CELLS[1]] * 4 + [CELLS[2]] * 4}


# -- the gate against checkMesh's own summary -----------------------------------------------

def test_the_cell_maxima_are_checkmeshs_own(tmp_path):
    """The reduction is believed because it reproduces a number this code had no part in making."""
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    gate = write_quality_decomposed(case, region="fluid")["checked_against_checkMesh"]
    assert gate["aspect_ratio"] == {"cells_max": max(CELLS), "checkMesh_said": max(CELLS),
                                    "agree": True}
    assert gate["non_ortho"]["agree"] is True
    assert gate["skewness"]["within"] is True
    assert "not_checked" not in gate


def test_a_parse_that_does_not_reproduce_checkmeshs_maximum_is_refused(tmp_path):
    """Losing the colours and saying why beats a plausible wrong number on a customer's picture."""
    case = _decomposed_case(tmp_path)
    _log(case, "fluid", aspect=99.0)
    with pytest.raises(QualityUnavailable, match="not its measurement"):
        write_quality_decomposed(case, region="fluid")


def test_a_boundary_skewness_above_the_whole_meshs_cannot_be(tmp_path):
    """The boundary is a subset of the faces, so its maximum cannot exceed the mesh's."""
    case = _decomposed_case(tmp_path)
    _log(case, "fluid", skew=1.0)          # the fixture's coded values go well above this
    with pytest.raises(QualityUnavailable, match="cannot be"):
        write_quality_decomposed(case, region="fluid")


def test_no_log_is_recorded_as_not_checked_rather_than_left_absent(tmp_path):
    """An empty gate reads exactly like a gate that passed, and the difference between "checked
    and agreed" and "there was nothing to check against" is the difference between a measurement
    and a hope."""
    case = _decomposed_case(tmp_path)
    meta = write_quality_decomposed(case, region="fluid")
    assert "not_checked" in meta["checked_against_checkMesh"]
    assert "no checkMesh log" in meta["checked_against_checkMesh"]["not_checked"]


# -- what it refuses, and by name -----------------------------------------------------------

def test_without_an_index_there_is_nothing_to_be_numbered_against(tmp_path):
    case = tmp_path / "dec"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region="fluid")
    _publish_decomposed_boundaries(case)
    with pytest.raises(IndexUnavailable):
        write_quality_decomposed(case, region="fluid")


def test_a_region_with_no_fields_at_all_is_refused_by_name(tmp_path):
    case = _decomposed_case(tmp_path, fields=False)
    with pytest.raises(QualityUnavailable, match="none of checkMesh's fields"):
        write_quality_decomposed(case, region="fluid")


def test_a_field_whose_cells_are_not_this_meshs_cells_is_refused(tmp_path):
    """`cells[owner]` on a field from a different mesh is an answer rather than an error, so the
    cell-range check is what stands between a wrong colour and a named refusal. Six cells over
    three ranks, and one rank handed a field that claims to hold one cell."""
    case = tmp_path / "dec"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region="fluid")
    for r in range(3):
        write_quality_fields(case / f"processor{r}" / "constant" / "fluid",
                             cells=[1.0] if r == 1 else [1.0, 2.0],
                             per_face={"skewness": {}},
                             uniform={"nonOrthoAngle": {}},
                             patches=())
    _publish_decomposed_boundaries(case)
    write_index_decomposed(case, region="fluid")
    with pytest.raises(QualityUnavailable, match="not one mesh"):
        write_quality_decomposed(case, region="fluid")


def test_a_patch_whose_value_count_disagrees_is_named_and_painted_nothing(tmp_path):
    """NaN and a note, not a dropped patch: the array has to stay the index's own length or the
    gates cannot tell a short patch from a short file, and a NaN draws as no colour."""
    case = tmp_path / "dec"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region="fluid")
    for r, patches in RANK_PATCHES.items():
        bad = {p: _coded(r, p, n) for p, n in patches.items()}
        if r == 1:
            bad["wall"] = bad["wall"][:2]          # checkMesh wrote two values for four faces
        write_quality_fields(case / f"processor{r}" / "constant" / "fluid",
                             cells=[CELLS[r]], per_face={"skewness": bad},
                             uniform={"nonOrthoAngle": dict.fromkeys(patches, 0.0)},
                             patches=tuple(patches))
    _publish_decomposed_boundaries(case)
    write_index_decomposed(case, region="fluid")
    _log(case, "fluid")
    meta = write_quality_decomposed(case, region="fluid")
    assert "processor1/wall" in meta["notes"]["skewness"]
    assert "2 for 4 faces" in meta["notes"]["skewness"]
    skew = np.asarray(_by_patch(_pm(case), "skewness")["wall"], dtype=np.float64)
    assert int(np.isnan(skew).sum()) == 4, "rank 1's four wall faces, and nothing else"
    assert meta["figures"]["skewness"]["measured"] == 10
    # AND THE OTHER METRICS ARE UNHARMED. One field checkMesh wrote badly is one colour lost.
    assert "aspect_ratio" in meta["basis"] and "non_ortho" in meta["basis"]


# -- the one-read patch parser --------------------------------------------------------------

def test_a_patch_name_with_punctuation_in_it_still_has_values(tmp_path):
    """`\\w+` drops a patch whose name carries a dot, a colon or a hyphen, and a dropped patch is a
    patch painted NaN. The charset here is `read_boundary_patches`' own."""
    d = tmp_path / "r"
    d.mkdir()
    write_quality_fields(d, cells=[1.0], patches=("inlet.1", "a-b", "c:d"),
                         per_face={"skewness": {"inlet.1": [1.0], "a-b": [2.0], "c:d": [3.0]}},
                         uniform={"nonOrthoAngle": {"inlet.1": 0.0}})
    got = _all_patch_values(d / "skewness")
    assert sorted(got) == ["a-b", "c:d", "inlet.1"]
    assert got["c:d"].tolist() == [3.0]


def test_what_is_not_a_value_is_not_returned_as_one(tmp_path):
    """`uniform 0` and `zeroGradient` are not measurements, and returning them as if they were is
    how a boundary ends up painted with a number nobody took."""
    d = tmp_path / "r"
    d.mkdir()
    write_quality_fields(d, cells=[1.0], per_face={"skewness": {"wall": [1.0]}},
                         uniform={"nonOrthoAngle": {"wall": 0.0}}, patches=("wall",))
    assert _all_patch_values(d / "nonOrthoAngle") == {}
    assert _all_patch_values(d / "aspectRatio") == {}          # zeroGradient
    assert list(_all_patch_values(d / "skewness")) == ["wall"]


def test_it_is_one_read_of_the_file_and_not_one_per_patch(tmp_path):
    """On the customer plate's metal side `skewness` is 288 MB PER RANK, and this question is
    asked once per rank per field, so three patches must not mean three full reads of it."""
    d = tmp_path / "r"
    d.mkdir()
    write_quality_fields(d, cells=[1.0], patches=("a", "b", "c"),
                         per_face={"skewness": {"a": [1.0], "b": [2.0], "c": [3.0]}},
                         uniform={"nonOrthoAngle": {"a": 0.0}})
    reads = {"n": 0}
    real = Path.read_text

    def counted(self, *a, **k):
        if self.name == "skewness":
            reads["n"] += 1
        return real(self, *a, **k)

    try:
        Path.read_text = counted
        got = _all_patch_values(d / "skewness")
    finally:
        Path.read_text = real
    assert sorted(got) == ["a", "b", "c"]
    assert reads["n"] == 1


# -- the record it leaves -------------------------------------------------------------------

def test_the_meta_says_it_was_reduced_without_a_reconstruct(tmp_path):
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    meta = write_quality_decomposed(case, region="fluid")
    assert meta["version"] == QUALITY_VERSION
    assert "no reconstruct" in meta["source"]
    assert meta["ranks"] == 3
    assert meta["index"]["n_faces"] == 14
    assert [p["name"] for p in meta["index"]["patches"]] == ["inlet", "outlet", "wall"]
    on_disk = json.loads((quality_dir(_pm(case)) / "meta.json").read_text())
    assert on_disk == meta


def test_the_sidecar_lands_beside_the_index_it_is_numbered_against(tmp_path):
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    write_quality_decomposed(case, region="fluid")
    d = quality_dir(_pm(case))
    assert sorted(p.name for p in d.iterdir()) == [
        "aspect_ratio.f32", "meta.json", "non_ortho.f32", "skewness.f32"]
    assert d.parent == _pm(case)

# -- the gaps a mutation pass found ---------------------------------------------------------

#: SIX CELLS OVER THREE RANKS, so each rank holds TWO and its boundary faces do not all share one
#: owner. The three-cell fixture above cannot see an owner slice that starts in the wrong place:
#: with one cell per rank every owner is 0, so reading the span from face 0 instead of from the
#: rank's own first boundary face gives the same answer. Here it does not.
CELLS6 = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def _two_cells_per_rank(root: Path, *, region="fluid") -> Path:
    case = root / "dec6"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=6, region=region)
    for r in range(3):
        write_quality_fields(case / f"processor{r}" / "constant" / region,
                             cells=CELLS6[2 * r:2 * r + 2],
                             per_face={"skewness": {}}, uniform={"nonOrthoAngle": {}},
                             patches=())
    assert _publish_decomposed_boundaries(case) == [region]
    write_index_decomposed(case, region=region)
    _log(case, region, aspect=max(CELLS6), non_ortho=max(CELLS6))
    return case


def test_the_owner_slice_starts_at_the_ranks_own_first_boundary_face(tmp_path):
    """A rank's `faces` file holds its INTERNAL faces first, so a slice of `owner` taken from
    face 0 is a slice of internal-face owners shifted into the boundary's place. Every value is a
    real cell id and every array is the right length, so nothing but the arithmetic says so.

    Rank r holds cells 2r and 2r+1. Each rank's four -y/+y/-z/+z faces per cell mean the wall
    carries cell 2r on its first four and cell 2r+1 on its next four, which is the pattern a
    one-face shift destroys.
    """
    case = _two_cells_per_rank(tmp_path)
    write_quality_decomposed(case, region="fluid")
    assert _by_patch(_pm(case), "aspect_ratio") == {
        "inlet": [CELLS6[0]],
        "outlet": [CELLS6[5]],
        "wall": ([CELLS6[0]] * 4 + [CELLS6[1]] * 4
                 + [CELLS6[2]] * 4 + [CELLS6[3]] * 4
                 + [CELLS6[4]] * 4 + [CELLS6[5]] * 4),
    }


def test_a_patch_no_rank_wrote_a_value_for_is_dropped_with_a_reason(tmp_path):
    """THE ASSEMBLY GATE, which the per-rank short-count arm above never reaches. A rank whose
    field file is simply absent contributes nothing at all, so the patch comes to fewer values
    than the index declares faces - and a short array written anyway would be read as the first
    N faces' colours, which is every face after it painted with somebody else's number.
    """
    case = tmp_path / "dec"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region="fluid")
    for r, patches in RANK_PATCHES.items():
        write_quality_fields(
            case / f"processor{r}" / "constant" / "fluid",
            cells=[CELLS[r]],
            per_face={"skewness": {p: _coded(r, p, n) for p, n in patches.items()}},
            uniform={"nonOrthoAngle": dict.fromkeys(patches, 0.0)},
            patches=tuple(patches))
    # checkMesh wrote nothing at all on rank 2 - a measurement that stopped, not one that lied
    for name in ("skewness", "nonOrthoAngle", "aspectRatio"):
        (case / "processor2" / "constant" / "fluid" / name).unlink()
    _publish_decomposed_boundaries(case)
    write_index_decomposed(case, region="fluid")
    _log(case, "fluid")
    with pytest.raises(QualityUnavailable, match="none of checkMesh's fields"):
        write_quality_decomposed(case, region="fluid")


def test_the_gate_names_the_patch_and_the_two_counts(tmp_path):
    """One field short of a rank and the others whole: the short one is dropped by name and the
    rest are still written, because one missing colour is not a reason to have none."""
    case = tmp_path / "dec"
    write_decomposed_row_of_hexes(case, ranks=3, n_cells=3, region="fluid")
    for r, patches in RANK_PATCHES.items():
        write_quality_fields(
            case / f"processor{r}" / "constant" / "fluid",
            cells=[CELLS[r]],
            per_face={"skewness": {p: _coded(r, p, n) for p, n in patches.items()}},
            uniform={"nonOrthoAngle": dict.fromkeys(patches, 0.0)},
            patches=tuple(patches))
    (case / "processor1" / "constant" / "fluid" / "skewness").unlink()
    _publish_decomposed_boundaries(case)
    write_index_decomposed(case, region="fluid")
    _log(case, "fluid")
    meta = write_quality_decomposed(case, region="fluid")
    assert "skewness" not in meta["basis"], "eight values for twelve faces is not the wall"
    assert "wall: assembled 8 values for 12 faces" in meta["notes"]["skewness"]
    assert sorted(meta["basis"]) == ["aspect_ratio", "non_ortho"]


def test_an_index_whose_patches_are_not_a_contiguous_run_is_refused(tmp_path):
    """`write_index` leaves a hole when a caller passes `skip_names` - those faces are read
    because one pass is cheaper than two and no patch names them - so a patch table with a gap in
    it is a real shape, not an impossible one. Reduced against it, every patch after the gap would
    sit one block early and carry the previous patch's numbers.
    """
    case = _decomposed_case(tmp_path)
    _log(case, "fluid")
    meta_path = _pm(case) / "boundaryIndex" / "meta.json"
    meta = json.loads(meta_path.read_text())
    for p in meta["patches"]:
        if p["name"] == "wall":
            p["first_face"] += 3                 # a hole where no patch claims the faces
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(QualityUnavailable, match="the index puts it at"):
        write_quality_decomposed(case, region="fluid")
