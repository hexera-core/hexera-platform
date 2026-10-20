# Responsibility: Verify the corpus tool's publication bundle path: rows streamed during a run
#                 round-trip through the shared loader, a recorded rows file can be rebundled with
#                 its summary recomputed from the rows, and a refused redistribution status never
#                 produces a loadable bundle.
# Boundaries: hermetic - the kernel is a stub and no CAD is read; git is the repo's own.
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "devtools" / "validation" / filename)
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: benchmark_bundle defines a dataclass, and dataclasses resolves
    # string field annotations through sys.modules - an unregistered module raises on import.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load("repair_corpus_bundle_under_test", "repair_corpus.py")
bb = _load("benchmark_bundle_for_bundle_test", "benchmark_bundle.py")


@pytest.fixture
def kernel(monkeypatch, tmp_path):
    """A stub kernel: one repairable part whose re-inspection reports the defect gone."""
    import meshpipeline.cad.repair.conservative as cons
    import meshpipeline.cad.repair.contracts as contracts
    import meshpipeline.cad.repair.triage as triage

    def repair(source, destination, *, profile=None, directives=()):
        Path(destination).write_bytes(b"repaired")
        return SimpleNamespace(
            operations=[{"name": "fix_small_edges", "status": "kept", "targets": ["fa"]}],
            measurements=[SimpleNamespace(name="operations_attempted", value=1),
                          SimpleNamespace(name="operations_kept", value=1),
                          SimpleNamespace(name="stopped_because", value="resolved"),
                          SimpleNamespace(name="resolved", value=True)])

    def inspect(path):
        fixed = Path(path).name.endswith("-repaired.step")
        located = {} if fixed else {"small_edge": 2}
        entities = [] if fixed else [
            {"code": "small_edge", "fingerprint": "fa", "entity_type": "edge",
             "entity_index": 1, "severity": "error", "message": "m", "measurements": {}}]
        return {"report": SimpleNamespace(defects=[], to_dict=lambda: {}),
                "defects": [], "entities": entities, "valid": True,
                "counts": {}, "located": {"by_code": located}}

    monkeypatch.setattr(cons, "repair_step_file", repair)
    monkeypatch.setattr(contracts, "status_from_defects",
                        lambda defects: SimpleNamespace(value="repairable"))
    monkeypatch.setattr(triage, "recommend", lambda **kw: SimpleNamespace(
        route=triage.ROUTE_CONSERVATIVE_REPAIR, confidence=1.0, targets=[], abstain_reason=""))
    monkeypatch.setattr(rc, "_inspect", inspect)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    part = corpus / "part.step"
    part.write_text("solid part")
    return SimpleNamespace(corpus=corpus, part=part)


def test_a_bundle_written_during_a_run_round_trips_through_load_bundle(kernel, tmp_path, capsys):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    writer = rc.benchmark_bundle.BundleWriter(tmp_path / "bundle")
    rows = rc.run_files([kernel.part], scratch, mode="off", bundle=writer)
    sealed = rc.seal_corpus_bundle(
        writer, rows, roots=[kernel.corpus], files=[kernel.part],
        settings={"limit": 0}, track="occt-real-defect", corpus_name="stub corpus",
        provenance="authored by this test", redistribution="internal")

    manifest, loaded_rows, summary, settings = bb.load_bundle(sealed)
    assert manifest["benchmark"] == "cad-repair"
    assert manifest["track"] == "occt-real-defect"
    assert manifest["corpus_name"] == "stub corpus"
    assert manifest["corpus_provenance"] == "authored by this test"
    assert manifest["redistribution_status"] == "internal"
    # input hashes cover exactly the files processed, keyed relative to the corpus root
    assert list(manifest["input_hashes"]) == ["part.step"]
    assert len(manifest["input_hashes"]["part.step"]) == 64
    assert manifest["engine_commit"]  # a commit hash, or "unknown" when git is absent
    assert manifest["hardware_class"]
    # the rows in the bundle are the rows the run produced, in order
    assert [r["file"] for r in loaded_rows] == [r["file"] for r in rows] == ["part.step"]
    assert loaded_rows[0]["outcome"] == "repaired"
    # the sealed summary is the recomputed aggregate, not anything the repair claimed
    assert summary["row_count"] == 1
    assert summary["outcomes"] == {"repaired": 1}
    assert summary["repaired_by_code"] == {"small_edge": 2}
    assert settings == {"limit": 0}


_RECORDED_ROWS = [
    {"file": "a.step", "outcome": "repaired", "route": "conservative-repair",
     "repaired_defects": {"small_edge": 3}, "stopped_because": "resolved",
     "operations": [{"op": "fix_small_edges", "status": "kept"}]},
    {"file": "b.step", "outcome": "not_attempted:needs-review", "route": "needs-review"},
]


def test_rebundle_recomputes_the_summary_from_the_rows(tmp_path):
    rows_json = tmp_path / "rows.json"
    # a stored summary that lies: rebundling must ignore it and recount from the rows
    rows_json.write_text(json.dumps(
        {"rows": _RECORDED_ROWS, "summary": {"outcomes": {"repaired": 99}}}))
    sealed = rc.bundle_from_rows(
        rows_json, tmp_path / "bundle", track="occt-real-defect",
        corpus_name="recorded run", provenance="rows recorded by a previous --json run",
        redistribution="review-pending")

    manifest, loaded_rows, summary, settings = bb.load_bundle(sealed)
    assert summary["row_count"] == 2
    assert summary["outcomes"] == {"repaired": 1, "not_attempted": 1}
    assert summary["repaired_by_code"] == {"small_edge": 3}
    assert summary["routes"] == {"conservative-repair": 1, "needs-review": 1}
    assert summary["catalogue"]["tally"]["fix_small_edges"]["kept"] == 1
    assert [r["file"] for r in loaded_rows] == ["a.step", "b.step"]
    assert manifest["redistribution_status"] == "review-pending"
    assert settings["rebundled_from"] == str(rows_json)


def test_rebundle_accepts_the_plain_list_the_json_flag_writes(tmp_path):
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(_RECORDED_ROWS))
    sealed = rc.bundle_from_rows(rows_json, tmp_path / "bundle")
    _, loaded_rows, summary, _ = bb.load_bundle(sealed)
    assert summary["row_count"] == len(loaded_rows) == 2


def test_a_refused_redistribution_status_leaves_no_loadable_bundle(tmp_path):
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(_RECORDED_ROWS))
    with pytest.raises(ValueError, match="redistribution_status"):
        rc.bundle_from_rows(rows_json, tmp_path / "bundle", redistribution="probably-fine")
    # nothing was sealed, so the partial directory is refused by the loader too
    with pytest.raises(FileNotFoundError, match="missing"):
        bb.load_bundle(tmp_path / "bundle")


def test_seal_corpus_bundle_refuses_an_unknown_status_too(tmp_path):
    writer = rc.benchmark_bundle.BundleWriter(tmp_path / "bundle")
    with pytest.raises(ValueError, match="redistribution_status"):
        rc.seal_corpus_bundle(writer, [], roots=[], files=[], settings={},
                              redistribution="probably-fine")
