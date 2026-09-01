# The multiregion layer-coverage criterion silently never evaluated - the same dark-row bug
# class tests/unit/engines/test_layer_coverage_criterion_evaluates.py pins for snappy: the
# manifest quality dict came from check_mesh() (per-region checkMesh aggregation, which carries
# NO layer key) while the criterion key said layer_coverage, so measured/passed were null on
# every multiregion manifest ever judged. The fix threads the carve log's parsed coverage into
# the quality dict (multiregion_runner.attach_layer_coverage, the engines/snappy/finalize.py
# pattern: layer_coverage_pct + a layer_coverage_source provenance key) and renames the
# criterion to match. These tests pin the alignment so the row can never go dark again.
from pathlib import Path

from meshpipeline.engines.quality_criteria import evaluate, measurements_from_manifest
from meshpipeline.engines.snappy_multiregion.criteria import CRITERIA_ROWS
from meshpipeline.engines.snappy_multiregion.multiregion_runner import (
    LAYER_LOG,
    attach_layer_coverage,
)

# a carve-stage log in the real OpenFOAM v2412 shape: the areal cells-added headline plus the
# per-patch layer summary. The background box face carries no layers BY DESIGN (target 0);
# only the fluid-side interface walls are layered.
_CARVE_LOG = """\
Added 4610 out of 10000 cells (46.1%)

patch              faces    layers        overall thickness
                        target   mesh     [m]       [%]
-----              -----    -----    ----     ---       ---
fluid_to_heatsink  1200     3        2.4      0.00071   58.2
fluid_to_casing    800      3        2.9      0.00082   71.5
background_box     400      0        0        0         0

Layer mesh : cells:10000 faces:41000 points:22000
"""


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_layer_coverage_criterion_reads_the_manifest_key():
    manifest = {"quality": {"layer_coverage_pct": 46.1, "rc": 0, "timed_out": False},
                "patch_face_counts": {}, "patch_types": {}}
    rows = evaluate("snappy_multiregion", measurements_from_manifest(manifest))
    row = _row(rows, "layer_coverage_pct")
    assert row["measured"] == 46.1
    assert row["passed"] is True


def test_zero_coverage_now_measurably_fails_the_advisory():
    manifest = {"quality": {"layer_coverage_pct": 0.0},
                "patch_face_counts": {}, "patch_types": {}}
    rows = evaluate("snappy_multiregion", measurements_from_manifest(manifest))
    row = _row(rows, "layer_coverage_pct")
    assert row["measured"] == 0.0
    assert row["passed"] is False            # advisory, but it now SAYS so with a number


def test_every_multiregion_criterion_key_is_a_manifest_quality_key():
    # The generalized lesson: a criterion whose key matches no manifest field is a check that
    # never runs. rc/timed_out are build-time keys write_manifest records from the native run;
    # every other criterion must read a key check_mesh() or attach_layer_coverage() writes.
    manifest_quality_keys = {"rc", "timed_out",
                             # check_mesh()
                             "cells", "fatal", "skew_fraction", "max_non_ortho", "regions",
                             "regions_missing", "regions_undeclared", "interface_ok",
                             "interfaces", "interface_mismatch", "mesh_ok",
                             # attach_layer_coverage()
                             "layer_coverage_pct", "layer_coverage_source",
                             "layer_cells_with", "layer_cells_targeted", "per_patch_layers"}
    for c in CRITERIA_ROWS:
        assert c.key in manifest_quality_keys, (
            f"criterion {c.key!r} reads a key the manifest never carries - it can never "
            "evaluate")


def test_attach_layer_coverage_threads_the_carve_log_into_the_quality_dict(tmp_path):
    (tmp_path / LAYER_LOG).write_text(_CARVE_LOG)
    q = attach_layer_coverage(tmp_path, {"cells": 10000, "fatal": []})
    assert q["layer_coverage_pct"] == 46.1
    assert q["layer_coverage_source"] == "overall"     # the AREAL headline, stated provenance
    assert q["layer_cells_with"] == 4610
    assert q["layer_cells_targeted"] == 10000
    # only the LAYERED patches are reported; the un-layered background box must not appear
    assert set(q["per_patch_layers"]) == {"fluid_to_heatsink", "fluid_to_casing"}
    assert q["per_patch_layers"]["fluid_to_heatsink"] == {
        "layers": 2.4, "target": 3, "coverage_pct": 58.2}
    # and the threaded dict is exactly what the criterion evaluates
    rows = evaluate("snappy_multiregion", measurements_from_manifest({"quality": q}))
    assert _row(rows, "layer_coverage_pct")["passed"] is True


def test_fallback_min_is_taken_over_layered_patches_only(tmp_path):
    # no areal headline -> the thickness-percent fallback. The background box's 0% row targets
    # no layers and must not drag the minimum to zero (that would fabricate a failure).
    log = "\n".join(ln for ln in _CARVE_LOG.splitlines() if not ln.startswith("Added"))
    (tmp_path / LAYER_LOG).write_text(log)
    q = attach_layer_coverage(tmp_path, {})
    assert q["layer_coverage_pct"] == 58.2
    assert q["layer_coverage_source"] == "per_patch_min"   # a DIFFERENT measurement, named


def test_a_missing_carve_log_leaves_the_quality_dict_untouched(tmp_path):
    # absent evidence must read as not-measured (row n/a), never as an invented number
    q = attach_layer_coverage(Path(tmp_path), {"cells": 5})
    assert q == {"cells": 5}
    rows = evaluate("snappy_multiregion", measurements_from_manifest({"quality": q}))
    row = _row(rows, "layer_coverage_pct")
    assert row["measured"] is None and row["passed"] is None
