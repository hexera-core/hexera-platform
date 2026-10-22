# checkMesh counted the mesh's disconnected pieces all along and nothing read the number.
#
# `foam_exec.check_mesh` has parsed "Number of regions: N" into quality["regions"] since it was
# written, but no criterion and no review axis cited the key, so it never reached a report card.
# That matters because checkMesh prints the count with its own warning asterisk and then still
# says "Mesh OK" - disconnection is not one of its fatal checks. A mesh can therefore pass kernel
# validity, volume, non-orthogonality and skewness and still be unsolvable: an island of cells has
# no face path to the pressure reference cell, so the pressure matrix is singular and the solver
# dies inside the preconditioner, nowhere near anything that names the cause.
#
# Measured on a microchannel case: a fluid domain came back as 15 pieces, one body of 5,386 cells
# plus 14 islands of 24 to 32 cells, because 100 um cells could not hold a 119 um channel together.
# Every other bar was green.
#
# These tests pin the row so the count can never go dark again, and pin it as ADVISORY so a
# conjugate mesh - solid and fluid, two regions by design - is not failed for being correct.
from meshpipeline.engines.cfmesh.criteria import CRITERIA_ROWS
from meshpipeline.engines.quality_criteria import evaluate, measurements_from_manifest


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def _manifest(**quality):
    return {"quality": {"rc": 0, "timed_out": False, **quality},
            "patch_face_counts": {}, "patch_types": {}}


def test_one_connected_body_passes():
    rows = evaluate("cfmesh", measurements_from_manifest(_manifest(regions=1)))
    row = _row(rows, "regions")
    assert row["measured"] == 1
    assert row["passed"] is True


def test_the_fifteen_island_mesh_now_says_so_with_a_number():
    # The real case. Before this row, every bar on this mesh was green and the solver still
    # crashed; the only trace was an asterisk in a log nobody parsed.
    rows = evaluate("cfmesh", measurements_from_manifest(_manifest(regions=15)))
    row = _row(rows, "regions")
    assert row["measured"] == 15
    assert row["passed"] is False


def test_a_conjugate_mesh_is_reported_not_failed():
    # Solid plus fluid is two regions BY DESIGN. The row must state the number without the
    # delivery gate rejecting a mesh that is exactly what the request asked for.
    rows = evaluate("cfmesh", measurements_from_manifest(_manifest(regions=2)))
    row = _row(rows, "regions")
    assert row["measured"] == 2
    assert row["passed"] is False            # advisory: says so, does not block
    assert _row([c.__dict__ for c in CRITERIA_ROWS], "regions")["gating"] is False


def test_an_unmeasured_count_is_not_a_clean_bill_of_health():
    # checkMesh only prints the line when it ran. An absent count must read as not-measured
    # rather than as "one region", which is the failure mode this whole row exists to stop.
    rows = evaluate("cfmesh", measurements_from_manifest(_manifest()))
    row = _row(rows, "regions")
    assert row["measured"] is None
    assert row["passed"] is None


def test_the_key_matches_what_check_mesh_actually_writes():
    # The generalized lesson from the layer-coverage row: a criterion whose key matches no
    # measurement is a check that never runs. This asserts against the parser itself, so a
    # rename in foam_exec cannot silently take the row dark again.
    import inspect

    from meshpipeline.engines.cfmesh import foam_exec
    src = inspect.getsource(foam_exec)
    assert '"regions"' in src or "q[\"regions\"]" in src, (
        "foam_exec no longer writes the 'regions' key the criterion reads")
    assert any(c.key == "regions" for c in CRITERIA_ROWS)
