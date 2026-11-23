"""corpus_harvest: classification, manifest merge and summary logic, with no gmsh."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "corpus_harvest", Path(__file__).resolve().parents[3] / "devtools/validation/corpus_harvest.py"
)
harvest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harvest)


def _result(**kw):
    base = {"solids": 1, "faces": 6, "diag_mm": 100.0, "volume_elements": 10, "error": None}
    base.update(kw)
    return "STAGE:import\nSTAGE:2d\nSTAGE:3d\nRESULT:" + json.dumps(base) + "\n"


def test_pass_when_volume_elements():
    row = harvest.interpret_child(_result(), 0, False)
    assert row["status"] == "pass"
    assert row["family"] is None
    assert row["solids"] == 1 and row["diag_mm"] == 100.0


def test_edge_through_face_is_a_3d_failure():
    err = "Invalid boundary mesh (segment and a facet intersect) at point (1, 2, 3)"
    row = harvest.interpret_child(_result(volume_elements=0, error=err, stage="3d"), 0, False)
    assert row["status"] == "fail"
    assert row["family"] == "edge_through_face"
    assert row["stage"] == "3d"
    assert row["gmsh_error"].startswith("FAIL: ") and "segment and a facet" in row["gmsh_error"]


def test_overlapping_facets_family_from_the_2d_stage():
    err = "2 overlapping facets on surface 4 surface 9"
    row = harvest.interpret_child(_result(volume_elements=0, error=err, stage="2d"), 0, False)
    assert (row["family"], row["stage"]) == ("overlapping_facets", "2d")


def test_unrecognised_gmsh_error_is_other_and_import_stage_kept():
    row = harvest.interpret_child(_result(volume_elements=0, error="boom", stage="import"), 0, False)
    assert (row["family"], row["stage"]) == ("other", "import")


def test_no_solids_is_not_a_mesh_failure():
    row = harvest.interpret_child(_result(solids=0, volume_elements=0), 0, False)
    assert row["status"] == "no_solids"
    assert row["family"] is None


def test_zero_volume_with_solids_is_no_volume():
    row = harvest.interpret_child(_result(volume_elements=0), 0, False)
    assert (row["status"], row["family"], row["stage"]) == ("fail", "no_volume", "3d")


def test_timeout_takes_stage_from_last_marker():
    row = harvest.interpret_child("STAGE:import\nSTAGE:2d\n", None, True)
    assert (row["status"], row["family"], row["stage"]) == ("timeout", "timeout", "2d")
    assert row["gmsh_error"] == "FAIL: timeout rc=124"


def test_crash_without_result_names_signal_and_stage():
    row = harvest.interpret_child("STAGE:import\nSTAGE:2d\nSTAGE:3d\n", -11, False)
    assert (row["status"], row["family"], row["stage"]) == ("fail", "crash", "3d")
    assert row["gmsh_error"] == "FAIL: crash rc=-11"


def test_crash_before_any_stage_has_empty_stage():
    row = harvest.interpret_child("", 1, False)
    assert row["family"] == "crash" and row["stage"] == ""


def test_manifest_entry_prefixes_chunk_and_keeps_schema():
    row = {"name": "x/00510001_a_step_000.step", "sha256": "ab" * 32, "solids": 2, "faces": 9,
           "diag_mm": 12.5, "gmsh_error": "FAIL: no volume elements"}
    entry = harvest.track_entry(row, "0051")
    assert entry == {
        "name": "abc/0051_00510001_a_step_000.step", "source": "abc-chunk-0051",
        "sha256": "ab" * 32, "solids": 2, "faces": 9, "diag_mm": 12.5,
        "gmsh_error": "FAIL: no volume elements", "diag_source": "gmsh",
    }
    corpus = harvest.corpus_entry(row, "0051", "https://example/x.7z")
    assert corpus["file"] == "abc/0051_00510001_a_step_000.step"
    assert corpus["archive_url"] == "https://example/x.7z"
    assert "name" not in corpus and "diag_source" not in corpus


def test_merge_is_idempotent_and_bumps_version_once_per_source():
    track = {"track": "t", "files": [{"name": "a", "sha256": "1"}], "sources": []}
    src = {"name": "abc-chunk-0051", "archive_url": "u", "archive_sha256": "s"}
    entries = [{"name": "b", "sha256": "2"}, {"name": "dup", "sha256": "1"}]
    out = harvest.merge_track(track, entries, src, note="added 0051")
    assert [f["name"] for f in out["files"]] == ["a", "b"]
    assert out["version"] == 2 and out["sources"] == [src]
    again = harvest.merge_track(out, entries, src, note="added 0051")
    assert again["version"] == 2 and len(again["files"]) == 2 and len(again["sources"]) == 1


def test_merge_corpus_manifest_dedupes_by_sha():
    manifest = {"probe": "p", "files": [{"file": "a", "sha256": "1"}]}
    out = harvest.merge_corpus(manifest, [{"file": "a2", "sha256": "1"}, {"file": "b", "sha256": "2"}])
    assert [f["file"] for f in out["files"]] == ["a", "b"]


def test_summary_counts_families_stages_and_samples_clean():
    rows = [
        {"name": f"p{i}", "sha256": f"{i:064x}", "status": "pass", "family": None, "stage": ""}
        for i in range(300)
    ]
    rows += [
        {"name": "f1", "sha256": "f1", "status": "fail", "family": "no_volume", "stage": "3d"},
        {"name": "f2", "sha256": "f2", "status": "fail", "family": "overlapping_facets", "stage": "2d"},
        {"name": "f3", "sha256": "f3", "status": "fail", "family": "no_volume", "stage": "3d"},
        {"name": "t", "sha256": "t", "status": "timeout", "family": "timeout", "stage": "2d"},
        {"name": "n", "sha256": "n", "status": "no_solids", "family": None, "stage": ""},
    ]
    s = harvest.summarise(rows, "0051", sample_fraction=0.01, seed=7)
    assert s["total"] == 305 and s["passed"] == 300 and s["failed"] == 3
    assert s["timeouts"] == 1 and s["no_solids"] == 1
    assert s["by_family"] == {"no_volume": 2, "overlapping_facets": 1, "timeout": 1}
    assert s["by_stage"] == {"3d": 2, "2d": 2}
    assert len(s["clean_sample"]) == 3
    assert all(set(c) == {"name", "sha256"} for c in s["clean_sample"])
    assert s == harvest.summarise(rows, "0051", sample_fraction=0.01, seed=7)


def test_chunk_url_table_and_name_validation():
    assert harvest.chunk_url("0051").endswith("/retrieve/88834/abc_0051_step_v00.7z")
    assert harvest.chunk_url("0052").endswith("/retrieve/88838/abc_0052_step_v00.7z")


def test_resume_skips_names_already_in_results(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"name": "a", "status": "pass"}) + "\n{broken\n"
                       + json.dumps({"name": "b", "status": "fail"}) + "\n")
    done = harvest.load_results(results)
    assert set(done) == {"a", "b"}


def test_family_from_legacy_manifest_error_text():
    f = harvest.legacy_family
    assert f("FAIL: PLC Error:  A segment and a facet intersect at point") == "edge_through_face"
    assert f("FAIL: no volume elements") == "no_volume"
    assert f("FAIL: timeout/crash rc=124") == "timeout"
    assert f("FAIL: Invalid boundary mesh (overlapping facets) on surface 1 surface 1") == (
        "overlapping_facets")
    assert f("FAIL: something odd") == "other"


def test_aggregate_family_stage_solids_median_faces():
    rows = [
        {"family": "no_volume", "stage": "3d", "solids": 1, "faces": 10},
        {"family": "no_volume", "stage": "3d", "solids": 3, "faces": 30},
        {"family": "no_volume", "stage": "", "solids": None, "faces": None},
        {"family": "timeout", "stage": "2d", "solids": 1, "faces": 5},
    ]
    agg = harvest.aggregate(rows)
    nv = agg["families"]["no_volume"]
    assert nv["total"] == 3
    assert nv["stage"] == {"2d": 0, "3d": 2, "unknown": 1}
    assert nv["solids"] == {"single": 1, "multi": 1, "unknown": 1}
    assert nv["median_faces"] == 20
    assert agg["total"] == 4
    assert agg["families"]["timeout"]["stage"]["2d"] == 1
