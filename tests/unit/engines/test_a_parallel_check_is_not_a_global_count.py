# Responsibility: a checkMesh run across ranks must not have its rank-summed counts published as
# totals of the mesh.
# Boundaries: pure parsing. No OpenFOAM, no subprocess, no workspace.
"""checkMesh run on eight ranks counts some things eight times, and the parser could not tell.

WHY THIS EXISTS. `read_region` ships `faces` and `points` straight into the customer's quality
report, and `parse_check_mesh` is a sweep of regexes with no idea whether it is reading one
process or eight. A face on a processor boundary was ONE internal face of the whole mesh and
becomes a BOUNDARY face on each of the two ranks that share it, so a parallel run's `faces` is
larger than the truth and its `internal faces` smaller, by exactly the same amount.

THE TWO FIXTURES BELOW ARE REAL AND THEY ARE THE SAME MESH. Both are verbatim from
`cbox2b_record/logs`, run on 6 November 2026 against the same 504,715,948-cell coupled plate with
the same binary (OpenFOAM 2412, `_b8cf4d35-20260127`): `checkMesh -constant -region fluid` at
`nProcs : 1` and `checkMesh -parallel -constant -region fluid` at `nProcs : 8`. The difference is
not a different mesh and not a different build, it is only the rank count:

                   serial         8 ranks        difference
    faces          263,003,681    263,310,321    +306,640
    internal       237,313,600    237,006,960    -306,640
    points          98,268,345     98,598,852    +330,507
    cells           82,529,515     82,529,515    IDENTICAL

`cells` is the count with no duplication in it, which is why it is the one left alone.

Recovering the true totals needs the processor-face count, which lives only in the patch table
this parser does not read, so the three affected numbers are REMOVED rather than corrected: a
reader that wants them gets a missing key, loudly, instead of a plausible wrong one. They are
kept under `*_rank_summed` so nothing measured is thrown away.

UNREACHABLE ON TODAY'S PRODUCT PATH, deliberately. `check_mesh` builds a bare command with no
mpirun and `conjugate.build` deletes the per-rank copies before it runs, so every checkMesh this
engine has ever made is one process. This exists so the FIRST parallel check cannot quietly
publish eight ranks' arithmetic as a measurement of a mesh.
"""
from meshpipeline.engines.cfmesh_conjugate.foam_exec import parse_check_mesh

_HEAD = """\
Build  : _b8cf4d35-20260127 OPENFOAM=2412 patch=260127 version=2412
Arch   : "LSB;label=32;scalar=64"
Exec   : checkMesh{flags} -constant -region fluid
Case   : /case
nProcs : {nprocs}
"""

SERIAL = _HEAD.format(flags="", nprocs=1) + """\
Mesh stats fluid
    points:           98268345
    faces:            263003681
    internal faces:   237313600
    cells:            82529515
    faces per cell:   6.06228
    boundary patches: 2
    point zones:      0
    face zones:       0
    cell zones:       4

Mesh OK.
"""

PARALLEL = _HEAD.format(flags=" -parallel", nprocs=8) + """\
Mesh stats fluid
    points:           98598852
    faces:            263310321
    internal faces:   237006960
    cells:            82529515
    faces per cell:   6.06228
    boundary patches: 2 (5 8)
    point zones:      0
    face zones:       0
    cell zones:       4

 ***1312 highly skew faces detected
Mesh OK.
"""

#: What the two runs agree on, because it is the one count a decomposition does not duplicate.
CELLS = 82_529_515
#: The processor-boundary faces, which is the whole of the discrepancy, in both directions.
P = 306_640


def test_a_serial_run_is_read_exactly_as_before():
    """The guard must be invisible to every run this engine has actually made."""
    q = parse_check_mesh(SERIAL)
    assert q["nprocs"] == 1
    assert q["faces"] == 263_003_681
    assert q["points"] == 98_268_345
    assert q["internal_faces"] == 237_313_600
    assert q["cells"] == CELLS
    assert "faces_rank_summed" not in q, "a one-process run has nothing summed over ranks"
    assert q["mesh_ok"] is True


def test_a_parallel_run_does_not_publish_its_rank_sums_as_totals():
    """The defect: 263,310,321 is eight ranks' boundary faces, not the mesh's."""
    q = parse_check_mesh(PARALLEL)
    assert q["nprocs"] == 8
    for key in ("faces", "points", "internal_faces"):
        assert key not in q, (
            f"{key} was published as a total of the mesh from an 8-rank run, which is what "
            "reaches the customer's quality report")
    assert q["faces_rank_summed"] == 263_310_321
    assert q["points_rank_summed"] == 98_598_852
    assert q["internal_faces_rank_summed"] == 237_006_960


def test_the_cell_count_is_the_same_mesh_either_way():
    """Proves the two fixtures are one mesh, so every other difference is the rank count."""
    assert parse_check_mesh(SERIAL)["cells"] == parse_check_mesh(PARALLEL)["cells"] == CELLS


def test_the_discrepancy_is_one_processor_face_count_in_both_directions():
    """faces = true + P and internal = true - P, so the two moves are equal and opposite.

    This is the identity that says the numbers are duplicated rather than merely different, and
    it is what makes removing them the right answer: knowing the SIZE of the error needs P, and P
    is not on any line this parser reads.
    """
    s, p = parse_check_mesh(SERIAL), parse_check_mesh(PARALLEL)
    assert p["faces_rank_summed"] - s["faces"] == P
    assert s["internal_faces"] - p["internal_faces_rank_summed"] == P


def test_the_skew_fraction_survives_the_rename():
    """It is computed BEFORE the rank-summed keys are renamed away, and the order is the point.

    Taken after the rename there is no `faces` left, the else-branch fires, and every parallel
    check publishes `skew_fraction` 0.0. That is not "unknown": it is a customer-facing quality
    figure asserting the mesh has no skew faces. The parallel fixture carries 1,312 of them, so
    this test cannot pass by the numerator being zero.

    The denominator is the rank sum, which is too large by the processor-face count - 306,640 of
    263,310,321, so the fraction reads 0.12% low. A figure 0.12% optimistic is a measurement; a
    zero on a mesh with 1,312 skew faces is a false claim of a clean mesh.
    """
    q = parse_check_mesh(PARALLEL)
    assert q["skew_faces"] == 1312, "the numerator must be real or this test proves nothing"
    assert q["skew_fraction"] == 1312 / 263_310_321
    assert q["skew_fraction"] > 0.0, "a parallel check reported a mesh with skew faces as clean"
    # and the error from using the rank sum is the 0.12% claimed above, not something larger
    assert abs(q["skew_fraction"] / (1312 / (263_310_321 - P)) - 1.0) < 0.002


def test_a_log_with_no_header_is_treated_as_one_process():
    """Absence must not mean parallel: a trimmed or truncated log still reads as serial."""
    q = parse_check_mesh("Mesh stats\n    faces:            1000\n    cells:  10\nMesh OK.\n")
    assert q["nprocs"] == 1 and q["faces"] == 1000
