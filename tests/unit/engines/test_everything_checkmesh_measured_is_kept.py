"""checkMesh measures a mesh completely and the report used to keep a tenth of it.

WHY THIS EXISTS. The first customer asked, by name, for "mesh count, quality metrics to know if
the mesh is of good quality, number of boundary layers in the fluid, min and max cell sizes, cell
types and count per type, highlight of poor-quality cells" (Corintis, 2026-10-29). Four of those
six were already being measured on every run and thrown away: `_CM` read `hexahedra` and
`polyhedra` out of seven cell types and no sizes at all, and `read_region` kept six fields of
what it was handed. A run paid 204.9 s on the coolant and 584.5 s on the metal to produce this
text and then discarded most of it.

The text below is REAL - the verbatim output of `checkMesh -constant -region fluid -allGeometry`
on runs/murray_ported_4gen-2across, captured 2026-10-29. Writing these regexes against invented
output is how a parser comes to read lines OpenFOAM does not print.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate.conjugate import RegionReport, read_region
from meshpipeline.engines.cfmesh_conjugate.foam_exec import parse_check_mesh

REAL = """\
Mesh stats
    points:           48909
    faces:            112362
    internal faces:   80946
    cells:            32218
    faces per cell:   6
    boundary patches: 3
    point zones:      0
    face zones:       0
    cell zones:       1

Overall number of cells of each type:
    hexahedra:     32218
    prisms:        0
    wedges:        0
    pyramids:      0
    tet wedges:    0
    tetrahedra:    0
    polyhedra:     0

Checking topology...
    Boundary definition OK.
    Cell to face addressing OK.
    Point usage OK.
    Upper triangular ordering OK.
    Face vertices OK.
    Number of regions: 1 (OK).

Checking geometry...
    Overall domain bounding box (-0.00559174 -0.00027 -1.86259e-05) (0.00559174 0.0110509 0.00092)
    Mesh has 3 geometric (non-empty/wedge) directions (1 1 1)
    Mesh has 3 solution (non-empty) directions (1 1 1)
    Boundary openness (1.1e-17 -2.5e-18 3.4e-19) OK.
    Max cell openness = 1.80512e-16 OK.
    Max aspect ratio = 1.52926 OK.
    Min volume = 1.38903e-13. Max volume = 2.12448e-13.  Total volume = 6.75656e-09.  Cell volumes OK.
    Mesh non-orthogonality Max: 3.40253 average: 0.120434
    Non-orthogonality check OK.
    Face pyramids OK.
    Max skewness = 0.0390864 OK.
    Coupled point location match (average 0) OK.
 ***Cells with small determinant (< 0.001) found, number of cells: 134

Failed 1 mesh checks.

End
"""


@pytest.fixture(scope="module")
def q():
    return parse_check_mesh(REAL)


# -- the six readings the customer asked for, in their order -------------------------------

def test_mesh_count(q):
    assert q["cells"] == 32218
    assert q["faces"] == 112362
    assert q["internal_faces"] == 80946
    assert q["points"] == 48909


def test_every_cell_type_is_counted_not_just_two_of_seven(q):
    """The parser read hexahedra and polyhedra. checkMesh prints seven, and a mesh made of
    prisms reported as made of neither."""
    assert q["cell_types"] == {"hexahedra": 32218}, (
        "kinds with no cells are left out - seven rows of which five say 0 reads worse than one")
    assert q["cell_types_total"] == q["cells"], (
        "the types have to account for every cell, or the mesh contains something unnamed")
    for kind in ("prisms", "wedges", "pyramids", "tet_wedges", "tetrahedra", "polyhedra"):
        assert q[kind] == 0, f"{kind} must be read even when it is zero"


def test_tet_wedges_does_not_steal_the_wedges_count():
    """checkMesh prints 'tet wedges:' two lines below 'wedges:'. A bare \\bwedges: matches both,
    and only finds the right one first because of the order they happen to be printed in."""
    swapped = REAL.replace("    wedges:        0", "    wedges:        7").replace(
        "    tet wedges:    0", "    tet wedges:    3")
    got = parse_check_mesh(swapped)
    assert got["wedges"] == 7 and got["tet_wedges"] == 3


def test_min_and_max_cell_size_as_a_length(q):
    """checkMesh reports VOLUMES. A cell size is a length, and the budget is set in lengths."""
    assert q["min_volume"] == pytest.approx(1.38903e-13)
    assert q["max_volume"] == pytest.approx(2.12448e-13)
    assert q["min_cell_m"] == pytest.approx(1.38903e-13 ** (1 / 3))
    assert q["max_cell_m"] == pytest.approx(2.12448e-13 ** (1 / 3))
    assert q["min_cell_m"] < q["max_cell_m"]
    assert q["cell_size_ratio"] == pytest.approx(q["max_cell_m"] / q["min_cell_m"])


def test_poor_quality_cells_are_counted(q):
    """'highlight of poor-quality cells' starts with knowing how many there are. checkMesh names
    these only when it finds some, which is why the absent case must read 0 and not be missing."""
    assert q["small_determinant_cells"] == 134
    clean = parse_check_mesh(REAL.replace(
        " ***Cells with small determinant (< 0.001) found, number of cells: 134", ""))
    assert clean["small_determinant_cells"] == 0


def test_the_quality_numbers_a_gate_reads(q):
    assert q["max_non_ortho"] == pytest.approx(3.40253)
    assert q["avg_non_ortho"] == pytest.approx(0.120434), (
        "the average is on the same line as the max and was never read")
    assert q["max_skewness"] == pytest.approx(0.0390864)
    assert q["max_aspect_ratio"] == pytest.approx(1.52926)
    assert q["max_cell_openness"] == pytest.approx(1.80512e-16)


# -- and that it survives the trip into the report -----------------------------------------

def test_read_region_carries_it_all_through(q):
    """RegionReport kept six fields. Everything measured above died at this boundary."""
    r = read_region(q, "fluid")
    assert isinstance(r, RegionReport)
    assert r.cells == 32218 and r.faces == 112362 and r.points == 48909
    assert r.cell_types == {"hexahedra": 32218}
    assert r.min_cell_m == pytest.approx(q["min_cell_m"])
    assert r.max_cell_m == pytest.approx(q["max_cell_m"])
    assert r.max_aspect_ratio == pytest.approx(1.52926)
    assert r.avg_non_ortho == pytest.approx(0.120434)
    assert r.small_determinant_cells == 134


def test_a_region_that_was_never_measured_reports_zero_not_rubbish():
    """An unmeasured mesh must not read as a perfect one."""
    r = read_region({"mesh_ok": False, "unmeasured": True}, "solid")
    assert r.cells == 0 and r.faces == 0 and r.cell_types == {}
    assert r.min_cell_m is None and r.max_cell_m is None, (
        "a size that was not measured is not zero; a gate reading 0.0 passes everything")
    assert r.max_aspect_ratio is None and r.avg_non_ortho is None
    assert r.small_determinant_cells == 0


def test_the_two_regions_are_described_separately():
    """THE READING THAT MATTERS ON A CONJUGATE MESH. The coolant is the fine region and the metal
    the coarse one; one pair of size numbers is an average of two different meshes."""
    fine = parse_check_mesh(REAL)
    coarse = parse_check_mesh(REAL.replace(
        "Min volume = 1.38903e-13. Max volume = 2.12448e-13.",
        "Min volume = 1.38903e-10. Max volume = 2.12448e-10."))
    f, s = read_region(fine, "fluid"), read_region(coarse, "solid")
    assert f.min_cell_m is not None and s.min_cell_m is not None
    assert s.min_cell_m > f.min_cell_m * 5, "the fixture is meant to be two different scales"
