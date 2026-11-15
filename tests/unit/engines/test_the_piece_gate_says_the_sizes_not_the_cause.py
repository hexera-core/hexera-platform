"""The piece gate asserted a cause it never measured, and threw away the sizes that disprove it.

WHY THIS EXISTS. Job 6ddb58aa-2224-4210-a2ac-926bc2e7ac42 attempt 1 built a 283,410,955-cell
conjugate mesh in 3.94 h on an n2-highmem-64 and the `quality_floor` gate refused it, in part,
with this sentence:

    fluid is in 6 disconnected piece(s), expected 2. Islands mean the cells are too coarse to
    hold the passage together; this mesh will not solve even though checkMesh calls it OK.

Nothing in the run measured that cause. What the run DID measure, and print, was the size of every
piece - and the sizes say the opposite. The text below is verbatim from
`<workspace>/checkMesh_fluid.log` of that job (captured 2026-11-14), and it names four islands of
6,669 cells between them, three of which are single background cells. A piece of one cell is not a
passage the mesher failed to hold together. Three further agents then measured those four pieces
to be metal - winding number 0.0000 against both components of fluid.stl, ray parity even in six
directions - so the cause the message asserted was not merely unmeasured, it was wrong.

The six sizes sum to 91,329,913, which is the region's own cell count on the line above them, so
the reading is self-consistent in the same text it is read from. The solid's log of the same run
says "Number of regions: 1 (OK)." and prints no sizes at all, which is why an absent reading has
to be empty rather than invented.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate.conjugate import read_region
from meshpipeline.engines.cfmesh_conjugate.foam_exec import parse_check_mesh

#: Verbatim from job 6ddb58aa's checkMesh_fluid.log, lines 60-83 and its verdict.
REAL_FLUID = """\
Mesh stats
    points:           94324547
    faces:            274600172
    internal faces:   266340113
    cells:            91329913

Checking topology...
    Boundary definition OK.
    Cell to face addressing OK.
    Point usage OK.
    Upper triangular ordering OK.
    Face vertices OK.
   *Number of regions: 6
    The mesh has multiple regions which are not connected by any face.
  <<Writing region information to "constant/cellToRegion"
  <<Writing region 0 with 91226027 cells to cellSet region0
  <<Writing region 1 with 6666 cells to cellSet region1
  <<Writing region 2 with 1 cells to cellSet region2
  <<Writing region 3 with 1 cells to cellSet region3
  <<Writing region 4 with 1 cells to cellSet region4
  <<Writing region 5 with 97217 cells to cellSet region5

Checking geometry...
    Min volume = 1.3e-14. Max volume = 6.5e-14.  Total volume = 5.08983901e-05.
    Mesh non-orthogonality Max: 84.08 average: 5.1

Failed 5 mesh checks.

End
"""

#: And the metal region of the same run, which is one body and prints no sizes.
REAL_SOLID = """\
Mesh stats
    cells:            192081042

Checking topology...
    Number of regions: 1 (OK).

Failed 5 mesh checks.

End
"""


# -- the sizes checkMesh already printed ---------------------------------------------------

def test_every_pieces_size_is_read_from_the_same_text_the_count_is():
    """`Number of regions: 6` was kept and the six sizes beside it were dropped. One reader."""
    q = parse_check_mesh(REAL_FLUID)
    assert q["regions"] == 6
    assert q["region_cells"] == (91226027, 6666, 1, 1, 1, 97217)
    assert sum(q["region_cells"]) == q["cells"], (
        "the pieces have to account for every cell, or the reading is of a different mesh")


def test_the_region_information_line_is_not_mistaken_for_a_piece():
    """checkMesh prints `Writing region information to "constant/cellToRegion"` first, in the
    same shape as the six lines that follow it. It is not a piece and has no size."""
    q = parse_check_mesh(REAL_FLUID)
    assert len(q["region_cells"]) == 6


def test_the_sizes_are_ordered_by_region_and_not_by_where_they_appear():
    """region0 is the passage and region5 the second shell; an order taken from the text is the
    same order today and is not the thing being read."""
    shuffled = REAL_FLUID.replace(
        "  <<Writing region 1 with 6666 cells to cellSet region1\n", ""
    ).replace(
        "  <<Writing region 5 with 97217 cells to cellSet region5\n",
        "  <<Writing region 5 with 97217 cells to cellSet region5\n"
        "  <<Writing region 1 with 6666 cells to cellSet region1\n")
    q = parse_check_mesh(shuffled)
    assert q["region_cells"] == (91226027, 6666, 1, 1, 1, 97217)


def test_one_body_reads_as_no_sizes_rather_than_an_invented_one():
    """The metal region of this run. checkMesh prints the sizes only when it writes the sets,
    and a reading that was never taken is empty, not (192081042,)."""
    q = parse_check_mesh(REAL_SOLID)
    assert q["regions"] == 1
    assert q.get("region_cells", ()) == ()


# -- and that they survive the trip into the report ----------------------------------------

def test_the_report_carries_the_sizes_beside_the_count():
    f = read_region(parse_check_mesh(REAL_FLUID), "fluid")
    assert f.pieces == 6
    assert f.piece_cells == (91226027, 6666, 1, 1, 1, 97217)
    s = read_region(parse_check_mesh(REAL_SOLID), "solid")
    assert s.pieces == 1 and s.piece_cells == ()


def test_a_region_that_was_never_measured_has_no_piece_sizes():
    r = read_region({"mesh_ok": False, "unmeasured": True}, "fluid")
    assert r.pieces == 1 and r.piece_cells == ()


# -- the message -----------------------------------------------------------------------------

def _report(**kw):
    """This run's fluid region against its solid, in the shape `verify` reads."""
    base = dict(
        fluid=C.RegionReport("fluid", cells=91329913, volume_m3=5.08983901e-05, pieces=6,
                             piece_cells=(91226027, 6666, 1, 1, 1, 97217), mesh_ok=True),
        solid=C.RegionReport("solid", cells=192081042, volume_m3=1.3e-04, pieces=1,
                             mesh_ok=True),
        interface_faces_fluid=16590533, interface_faces_solid=16590533)
    base.update(kw)
    return C.ConjugateReport(**base)


def _piece_problem(report):
    hits = [p for p in report.problems if "disconnected piece" in p]
    assert len(hits) == 1, report.problems
    return hits[0]


def test_the_refusal_states_every_pieces_size():
    """The one reading that would have ended this investigation on sight: three of the four
    extra pieces are a single cell."""
    msg = _piece_problem(C.verify(_report(), expect_fluid_pieces=2))
    assert "6 disconnected piece(s), expected 2" in msg
    for size in ("91,226,027", "97,217", "6,666"):
        assert size in msg, f"{size} is missing from: {msg}"
    assert "1 cell" in msg or " 1," in msg or "/ 1 " in msg, (
        f"a one-cell piece has to be legible as one cell: {msg}")


def test_the_refusal_does_not_assert_a_cause_it_did_not_measure():
    """Nothing in the run measured why the pieces are there. Three agents later measured them to
    be metal, so the asserted cause was wrong as well as unmeasured."""
    msg = _piece_problem(C.verify(_report(), expect_fluid_pieces=2))
    assert "Islands mean" not in msg, (
        "the resolution was never varied on the mesh that printed this")
    assert "will not solve" not in msg, (
        "no solve was run on this mesh, and the gate is not entitled to the claim")


def test_the_refusal_names_both_candidate_causes():
    """A refusal that says nothing about why sends the next reader to guess. Both candidates are
    real - a passage the cells could not hold, or cells tagged as coolant that are not coolant -
    and this check cannot tell them apart, so it says so."""
    msg = _piece_problem(C.verify(_report(), expect_fluid_pieces=2)).lower()
    assert "coarse" in msg, "the resolution candidate is not named"
    assert "tagg" in msg or "tagged" in msg, "the mis-tagging candidate is not named"


def test_a_refusal_with_no_sizes_available_says_so_rather_than_inventing_them():
    """An older log, or one this parser could not read the sizes out of. The count still refuses;
    the sentence must not imply sizes it does not have."""
    msg = _piece_problem(C.verify(
        _report(fluid=C.RegionReport("fluid", cells=91329913, volume_m3=5.08983901e-05,
                                     pieces=6, mesh_ok=True)),
        expect_fluid_pieces=2))
    assert "6 disconnected piece(s), expected 2" in msg
    assert "91,226,027" not in msg
    assert "not in" in msg.lower() or "unavailable" in msg.lower() or "did not" in msg.lower(), (
        f"an absent reading has to be stated as absent: {msg}")


def test_the_expected_number_of_pieces_still_passes():
    """The gate's own job is untouched: this is a message change, not a tolerance."""
    r = C.verify(_report(fluid=C.RegionReport("fluid", cells=91329913, volume_m3=5.08983901e-05,
                                              pieces=2, piece_cells=(91226027, 97217),
                                              mesh_ok=True)),
                 expect_fluid_pieces=2)
    assert not [p for p in r.problems if "disconnected piece" in p], r.problems


def test_four_islands_of_6669_cells_are_still_refused():
    """A tolerance was considered and refused. 6,669 cells of 91,329,913 is 0.0073% by count and
    0.487% by volume, and a piece with no face path to a named patch has nothing to fix its
    pressure whatever its size. The gate still says no; it just says why differently."""
    r = C.verify(_report(), expect_fluid_pieces=2)
    assert any("disconnected piece" in p for p in r.problems)
    assert r.ok is False


@pytest.mark.parametrize("sizes", [(10, 1), (10, 1, 1, 1), (5, 5)])
def test_the_sentence_is_built_for_any_number_of_pieces(sizes):
    r = C.verify(_report(fluid=C.RegionReport("fluid", cells=sum(sizes), volume_m3=1e-06,
                                              pieces=len(sizes), piece_cells=sizes,
                                              mesh_ok=True)),
                 expect_fluid_pieces=1)
    msg = _piece_problem(r)
    assert f"{len(sizes)} disconnected piece(s), expected 1" in msg


# -- and it stays one sentence when the count does not ---------------------------------------

def test_367_pieces_do_not_become_367_numbers():
    """This engine has really produced a 367-piece coolant - the first customer cold plate,
    99,172,391 cells, recorded in finalize.py. Listing them would be a wall nobody reads."""
    sizes = tuple([40_000_000, 50_000, 900, 120, 60, 40, 7] + [1] * 360)
    said = C.piece_sizes_said(sizes)
    assert len(said) < 220, said
    assert "40,000,000" in said and "50,000" in said
    assert "the smallest is 1" in said
    assert "360 of the 367 are a single cell" in said, said


def test_up_to_eight_pieces_are_listed_one_by_one():
    assert C.piece_sizes_said((91226027, 6666, 1, 1, 1, 97217)) == (
        "The pieces are 91,226,027 / 6,666 / 1 / 1 / 1 / 97,217 cells.")
    assert C.PIECES_LISTED == 8


def test_no_sizes_says_so():
    assert "did not print" in C.piece_sizes_said(())


# -- the DURABLE record, which is where the count arrived alone -----------------------------
#
# The measured shape of the loss, read off job 6ddb58aa's own mesh_manifest.json on 2026-11-14:
# quality.regions[fluid] carries 23 readings of the coolant, down to a 329,153,282-byte
# concaveCells.vtp, and `pieces: 6` with no sizes. The one reading that identifies the defect is
# the only one the chain drops. There are three hops and the sizes have to survive all of them:
# native._as_dict -> conjugate_report.json -> finalize.quality_of -> mesh_manifest.json.

def test_the_sizes_cross_the_container_boundary(tmp_path):
    """`native._as_dict` lists by hand what finalize reads on the other side, so a field that is
    not named there does not exist to the record however well it was measured."""
    from meshpipeline.engines.cfmesh_conjugate import native as N
    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    plan = PL.ConjugatePlan(background_cell_m=4.025248e-04, levels=1,
                            expect_fluid_volume_m3=5.3637e-05, expect_fluid_pieces=2,
                            volume_tol=0.05, narrowest_m=9.82422e-05)
    body = N._as_dict(_report(), plan)
    assert body["regions"]["fluid"]["pieces"] == 6
    assert body["regions"]["fluid"]["piece_cells"] == [91226027, 6666, 1, 1, 1, 97217]
    assert body["regions"]["solid"]["piece_cells"] == []
    import json
    json.dumps(body)       # it is JSON on the other side


def test_the_manifests_quality_carries_the_sizes(tmp_path):
    """The gating criterion is `coolant_pieces_short` and it is built from this dict."""
    from meshpipeline.engines.cfmesh_conjugate.finalize import quality_of

    report = {
        "regions": {
            "fluid": {"cells": 91329913, "volume_m3": 5.26429e-05, "pieces": 6,
                      "piece_cells": [91226027, 6666, 1, 1, 1, 97217], "mesh_ok": False},
            "solid": {"cells": 192081042, "volume_m3": 2.23555e-04, "pieces": 1,
                      "piece_cells": [], "mesh_ok": False}},
        "interface_faces_fluid": 16590533, "interface_faces_solid": 16590533,
        "plan": {"expect_fluid_pieces": 2, "expect_fluid_volume_m3": 5.3637e-05},
    }
    q = quality_of(tmp_path, report)
    assert q["coolant_pieces"] == 6
    assert q["coolant_piece_cells"] == [91226027, 6666, 1, 1, 1, 97217]
    fluid = next(r for r in q["regions"] if r["name"] == "fluid")
    assert fluid["piece_cells"] == [91226027, 6666, 1, 1, 1, 97217]
    # AND IN THE SENTENCE, which is the criterion's `measured` value - the line a reader of the
    # report is given. "6 piece(s) where the part's geometry has 2" is true and names no pieces.
    (said,) = q["coolant_pieces_short"]
    assert "6 piece(s)" in said and "has 2" in said
    assert "6,666" in said and "91,226,027" in said, said
    # ONE SPELLING of these numbers across the three places that say them.
    assert C.piece_sizes_said((91226027, 6666, 1, 1, 1, 97217)) in said


def test_the_customer_facing_piece_gate_states_sizes_and_asserts_no_cause():
    """`gates._gate_coolant_pieces` ended with "Passages that should join have been cut apart by
    the mesh." - the same unmeasured cause, in the sentence that actually reaches a user. On this
    job it would have been wrong: the four extra pieces were metal."""
    from meshpipeline.engines.cfmesh_conjugate.gates import _gate_coolant_pieces

    class _Ctx:
        def __init__(self, q): self._q = q
        def manifest_or_load(self): return {"quality": self._q}

    q = {"coolant_pieces": 6, "coolant_pieces_expected": 2,
         "coolant_piece_cells": [91226027, 6666, 1, 1, 1, 97217]}
    ok, said = _gate_coolant_pieces(_Ctx(q))
    assert ok is False
    assert "6 separate piece(s)" in said and "has 2" in said
    assert "6,666" in said, said
    assert C.piece_sizes_said((91226027, 6666, 1, 1, 1, 97217)) in said
    assert "cut apart by the mesh" not in said, (
        "the cause is asserted again in the one sentence a user reads")
    assert _gate_coolant_pieces(_Ctx({"coolant_pieces": 2, "coolant_pieces_expected": 2}))[0]
