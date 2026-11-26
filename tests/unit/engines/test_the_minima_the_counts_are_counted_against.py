# checkMesh printed six figures and this engine read none of them, and one is the number that says
# whether a cell is folded.
#
# The engine published "71,529 cells under determinant 0.001" and "81,703 faces under interpolation
# weight 0.05" and never the minima those counts are taken against. A mesh one part in a thousand
# under the line and a mesh with genuinely folded cells publish the identical count. The shipped
# meshQualityDict's own scale settles it: "1 = hex, <= 0 = folded or flattened illegal cell".
#
# EVERY STRING BELOW IS COPIED OUT OF JOB 22ac7054's OWN checkMesh LOGS, not invented, because a
# regex tested against a string someone wrote to match it is tested against nothing.
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate.foam_exec import parse_check_mesh

#: Verbatim from checkMesh_fluid.log of job 22ac7054 (279,281,786-cell Corintis cold plate).
FLUID = """
    Overall number of cells of each type:
      hexahedra:     81104142
      prisms:        2366268
      wedges:        50158
      pyramids:      0
      tet wedges:    88489
      tetrahedra:    0
      polyhedra:     3585018

Checking topology...
    cells:            87194075
    Max aspect ratio = 108.362 OK.
    Minimum face area = 6.29307e-11. Maximum face area = 4.92506e-07.  Face area magnitudes OK.
    Min volume = 1.62477e-15. Max volume = 2.16054e-10.  Total volume = 5.2395e-05.  Cell volumes OK.
    Mesh non-orthogonality Max: 61.0276 average: 6.37189
    Non-orthogonality check OK.
    Face pyramids OK.
    Face tets OK.
    Max skewness = 3.53913 OK.
    Cell determinant (wellposedness) : minimum: 0 average: 0.382067
 ***Cells with small determinant (< 0.001) found, number of cells: 71529
    Face interpolation weight : minimum: 0.0170438 average: 0.473058
 ***Faces with small interpolation weight (< 0.05) found, number of faces: 81703
    Face volume ratio : minimum: 0.0100117 average: 0.905281
    Face volume ratio check OK.
 ***Concave cells (using face planes) found, number of cells: 1340185
Failed 3 mesh checks.
"""

#: Verbatim from checkMesh_solid.log of the same job. Note the solid has NO layers and still reads
#: a determinant minimum of 0, which is why that figure cannot be read as "the layers folded a cell".
SOLID = """
Checking topology...
    cells:            192087711
    Max aspect ratio = 15.2027 OK.
    Minimum face area = 4.9535e-10. Maximum face area = 8.44205e-07.  Face area magnitudes OK.
    Min volume = 1.41038e-14. Max volume = 3.12879e-10.  Total volume = 0.000223803.  Cell volumes OK.
    Mesh non-orthogonality Max: 70.8361 average: 3.43371
    Face pyramids OK.
 ***Error in face tets: 8 faces with low quality or negative volume decomposition tets.
    Max skewness = 3.81809 OK.
    Cell determinant (wellposedness) : minimum: 0 average: 0.736294
 ***Cells with small determinant (< 0.001) found, number of cells: 16464
    Face interpolation weight : minimum: 0.0197908 average: 0.49306
 ***Faces with small interpolation weight (< 0.05) found, number of faces: 6
    Face volume ratio : minimum: 0.0018164 average: 0.972064
 ***Faces with small volume ratio (< 0.01) found, number of faces: 11
 ***Concave cells (using face planes) found, number of cells: 401
Failed 5 mesh checks.
"""

#: A clean mesh, from the width rig at nSmoothSurfaceNormals 0: a POSITIVE determinant minimum.
CLEAN = """
    cells:            36432
    Max aspect ratio = 26.7611 OK.
    Min volume = 1.93879e-15. Max volume = 4.86589e-14.  Total volume = 8.2689e-10.  Cell volumes OK.
    Mesh non-orthogonality Max: 45.0182 average: 11.8
    Face pyramids OK.
    Face tets OK.
    Max skewness = 1.88801 OK.
    Cell determinant (wellposedness) : minimum: 0.000522046 average: 0.187495
 ***Cells with small determinant (< 0.001) found, number of cells: 140
    Face interpolation weight : minimum: 0.0608366 average: 0.408275
    Face interpolation weight check OK.
Failed 1 mesh checks.
"""


def test_the_determinant_minimum_is_read_at_all():
    """The whole point. Before this it was absent from every report the product wrote."""
    assert parse_check_mesh(FLUID)["determinant_min"] == 0.0
    assert parse_check_mesh(SOLID)["determinant_min"] == 0.0
    assert parse_check_mesh(CLEAN)["determinant_min"] == pytest.approx(0.000522046)


def test_a_positive_minimum_and_a_zero_one_are_different_numbers():
    """The reading the count cannot give. CLEAN has 140 cells under the 0.001 floor and a minimum
    of 0.000522046, so nothing in it is folded; FLUID has 71,529 under the same floor and a
    minimum of 0. Identical check, identical failure line, different mesh - and only the minimum
    tells them apart."""
    clean, fluid = parse_check_mesh(CLEAN), parse_check_mesh(FLUID)
    assert clean["small_determinant_cells"] == 140
    assert fluid["small_determinant_cells"] == 71529
    assert clean["determinant_min"] > 0.0
    assert fluid["determinant_min"] == 0.0


def test_the_averages_come_through_beside_the_minima():
    assert parse_check_mesh(FLUID)["determinant_avg"] == pytest.approx(0.382067)
    assert parse_check_mesh(SOLID)["determinant_avg"] == pytest.approx(0.736294)


def test_the_interpolation_weight_minimum_is_read():
    """The floor is 0.05. The fluid is under it at 0.0170438 with 81,703 faces failing; the solid
    is over it at 0.0197908 with only 6 - so the minimum alone does not predict the count, and
    both are worth publishing."""
    assert parse_check_mesh(FLUID)["face_weight_min"] == pytest.approx(0.0170438)
    assert parse_check_mesh(FLUID)["face_weight_avg"] == pytest.approx(0.473058)
    assert parse_check_mesh(SOLID)["face_weight_min"] == pytest.approx(0.0197908)
    assert parse_check_mesh(CLEAN)["face_weight_min"] == pytest.approx(0.0608366)


def test_the_volume_ratio_minimum_is_read():
    """0.01 is the floor. The fluid clears it at 0.0100117 and reports check OK; the solid is
    under at 0.0018164 with 11 faces failing. Two meshes, one line apart in the log."""
    assert parse_check_mesh(FLUID)["face_vol_ratio_min"] == pytest.approx(0.0100117)
    assert parse_check_mesh(FLUID)["face_vol_ratio_avg"] == pytest.approx(0.905281)
    assert parse_check_mesh(SOLID)["face_vol_ratio_min"] == pytest.approx(0.0018164)


def test_a_log_without_the_lines_leaves_the_keys_absent_rather_than_zero():
    """ABSENT, NOT ZERO, and the distinction is the whole reason the counts were misleading. A
    determinant minimum of 0.0 means folded cells; a missing key means checkMesh was not asked.
    These three lines only print under -allGeometry, so a plain run must not publish 0.0."""
    bare = parse_check_mesh("    cells:            100\n    Mesh OK.\n")
    for k in ("determinant_min", "determinant_avg", "face_weight_min",
              "face_weight_avg", "face_vol_ratio_min", "face_vol_ratio_avg"):
        assert k not in bare, k


def test_the_figures_already_published_did_not_move():
    """Six keys added to a shared regex table, so the ones every other cfMesh path reads are
    asserted here too."""
    q = parse_check_mesh(FLUID)
    assert q["cells"] == 87194075
    assert q["max_aspect_ratio"] == pytest.approx(108.362)
    assert q["max_skewness"] == pytest.approx(3.53913)
    assert q["small_determinant_cells"] == 71529
    assert q["concave_cells"] == 1340185
    assert q["mesh_ok"] is False
    assert parse_check_mesh(SOLID)["low_quality_tet_faces"] == 8
