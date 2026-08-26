# The layer-coverage criterion silently never evaluated: the manifest reports
# layer_coverage_pct (engines/snappy/finalize.py) while the criterion key said
# layer_coverage, so measured/passed were null on every manifest ever judged and the layer
# call fell entirely to the LLM reviewer (which failed 41.9% and passed 46.1% on
# same-purpose parts). These tests pin the key alignment so the row can never go dark again.
from meshpipeline.engines.quality_criteria import evaluate, measurements_from_manifest
from meshpipeline.engines.snappy.criteria import CRITERIA_ROWS


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_layer_coverage_criterion_reads_the_manifest_key():
    manifest = {"quality": {"layer_coverage_pct": 46.1, "rc": 0, "timed_out": False},
                "patch_face_counts": {"body": 100}, "patch_types": {"body": "wall"}}
    rows = evaluate("snappy", measurements_from_manifest(manifest))
    row = _row(rows, "layer_coverage_pct")
    assert row["measured"] == 46.1
    assert row["passed"] is True


def test_zero_coverage_now_measurably_fails_the_advisory():
    manifest = {"quality": {"layer_coverage_pct": 0.0},
                "patch_face_counts": {}, "patch_types": {}}
    rows = evaluate("snappy", measurements_from_manifest(manifest))
    row = _row(rows, "layer_coverage_pct")
    assert row["measured"] == 0.0
    assert row["passed"] is False            # advisory, but it now SAYS so with a number


def test_every_snappy_criterion_key_is_a_manifest_quality_key():
    # The generalized lesson: a criterion whose key matches no manifest field is a check
    # that never runs. wall_faces is manifest-DERIVED and rc/timed_out are build-time keys;
    # every other criterion must read a key the snappy finalize path actually writes.
    manifest_quality_keys = {"rc", "timed_out", "fatal", "skew_fraction", "skew_faces",
                             "max_non_ortho", "layer_coverage_pct", "cells"}
    derived = {"wall_faces"}
    for c in CRITERIA_ROWS:
        assert c.key in manifest_quality_keys | derived, (
            f"criterion {c.key!r} reads a key the manifest never carries - it can never "
            "evaluate")
