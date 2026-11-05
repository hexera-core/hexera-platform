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


def test_corpus_summary_reports_cad_simplicity_from_before_after_counts():
    """Catches a summary that says defects were repaired but hides geometry complexity changes.

    A repair can be valid yet make a STEP harder to inspect or mesh by adding many faces/edges; the
    benchmark needs the before/after topology deltas beside the repair yield.
    """
    rows = [
        {"file": "simpler.step", "outcome": "repaired", "route": "conservative-repair",
         "counts_before": {"solids": 1, "faces": 14, "edges": 42, "vertices": 30},
         "counts_after": {"solids": 1, "faces": 10, "edges": 30, "vertices": 24},
         "operations_kept": 1, "repaired_defects": {"small_face": 2}, "operations": []},
        {"file": "busier.step", "outcome": "repaired", "route": "conservative-repair",
         "counts_before": {"solids": 1, "faces": 6, "edges": 18, "vertices": 12},
         "counts_after": {"solids": 1, "faces": 8, "edges": 22, "vertices": 14},
         "operations_kept": 2, "repaired_defects": {"small_edge": 1}, "operations": []},
        {"file": "unmeasured.step", "outcome": "unreadable", "operations": []},
    ]

    simplicity = rc.corpus_summary(rows)["cad_simplicity"]

    assert simplicity["files_measured"] == 2
    assert simplicity["files_with_more_faces"] == 1
    assert simplicity["files_with_fewer_faces"] == 1
    assert simplicity["face_delta"] == {"count": 2, "min": -4.0, "max": 2.0,
                                        "mean": -1.0, "median": -1.0}
    assert simplicity["edge_delta"] == {"count": 2, "min": -12.0, "max": 4.0,
                                        "mean": -4.0, "median": -4.0}
    assert simplicity["vertex_delta"] == {"count": 2, "min": -6.0, "max": 2.0,
                                          "mean": -2.0, "median": -2.0}
    assert simplicity["operations_kept"] == {"count": 2, "min": 1.0, "max": 2.0,
                                             "mean": 1.5, "median": 1.5}
    assert simplicity["solid_count_changed"] == 0


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


def test_the_sealed_settings_name_every_switch_the_settings_module_declares():
    """A flag must not be able to change a published number invisibly.

    CAD_REPAIR_FIX_SMALL_FACES took the generated corpus from 0 to 5 repaired files while the
    sealed settings stayed byte-identical, because the dict was hand-written. This holds the
    sealed settings to the module, so the next flag is covered on the day it is added.
    """
    import meshpipeline.settings.cad_repair as rcfg

    sealed = rc.repair_settings_of(rcfg)
    declared = {name for name in vars(rcfg)
                if name.isupper() and name.startswith(("CAD_REPAIR_", "CAD_INSPECT_"))}
    missing = {name for name in declared
               if name[len("CAD_REPAIR_"):].lower() not in sealed
               and name[len("CAD_"):].lower() not in sealed}
    assert not missing, f"settings the bundle would not record: {sorted(missing)}"
    # the keys the earlier hand-written dict used are unchanged, so a reader of an older bundle
    # is looking at the same names
    assert sealed["enabled"] == rcfg.CAD_REPAIR_ENABLED
    assert sealed["autonomous"] == rcfg.CAD_REPAIR_AUTONOMOUS
    assert sealed["max_deviation_ratio"] == rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO
    assert sealed["inspect_timeout_s"] == rcfg.CAD_INSPECT_TIMEOUT_S
    # and the two that were missing are in
    assert sealed["fix_small_faces"] == rcfg.CAD_REPAIR_FIX_SMALL_FACES
    assert sealed["allow_face_removal"] == rcfg.CAD_REPAIR_ALLOW_FACE_REMOVAL


def test_two_runs_differing_only_in_a_consent_flag_seal_different_digests(tmp_path, monkeypatch):
    """The settings digest is what tells two bundles apart; it has to move when consent does."""
    import meshpipeline.settings.cad_repair as rcfg

    def seal(where: str) -> str:
        writer = rc.benchmark_bundle.BundleWriter(tmp_path / where)
        sealed = rc.seal_corpus_bundle(writer, [], roots=[], files=[],
                                       settings={"cad_repair": rc.repair_settings_of(rcfg)},
                                       redistribution="internal")
        manifest, _, _, _ = bb.load_bundle(sealed)
        return manifest["settings_digest"]

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", False)
    off = seal("off")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", True)
    assert seal("on") != off


def test_rebundle_does_not_name_todays_engine_for_yesterdays_rows(tmp_path):
    """No engine runs in a --rebundle, so the current HEAD is not a fact about these rows.

    Packaging a run recorded at commit A while sitting on commit B would publish B as the tested
    engine - the one thing a bundle must not get wrong. Unknown unless the caller states it.
    """
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(_RECORDED_ROWS))

    sealed = rc.bundle_from_rows(rows_json, tmp_path / "unstated")
    manifest, _, _, _ = bb.load_bundle(sealed)
    assert manifest["engine_commit"] == "unknown"
    assert manifest["hardware_class"] == "unknown"

    # a caller who knows what measured them says so, and that is what is sealed
    sealed = rc.bundle_from_rows(rows_json, tmp_path / "stated",
                                 engine_commit="a" * 40, hardware_class="linux-x86_64")
    manifest, _, _, _ = bb.load_bundle(sealed)
    assert manifest["engine_commit"] == "a" * 40
    assert manifest["hardware_class"] == "linux-x86_64"


def test_a_live_run_still_records_this_host_and_commit(tmp_path):
    """The defaults are only wrong for a rebundle: when the engine just ran here, here is true."""
    writer = rc.benchmark_bundle.BundleWriter(tmp_path / "bundle")
    sealed = rc.seal_corpus_bundle(writer, [], roots=[], files=[], settings={},
                                   redistribution="internal")
    manifest, _, _, _ = bb.load_bundle(sealed)
    assert manifest["engine_commit"] != "unknown"
    assert manifest["hardware_class"] != "unknown"


def test_verify_reads_a_sealed_bundle_and_refuses_an_edited_headline(tmp_path, monkeypatch,
                                                                     capsys):
    """--verify is what gives the loader's recompute a caller outside the tests.

    A summary figure edited in place leaves row_count intact, so only re-deriving the aggregate
    from rows.jsonl catches it.
    """
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(_RECORDED_ROWS))
    bundle = tmp_path / "bundle"
    rc.bundle_from_rows(rows_json, bundle)

    monkeypatch.setattr(sys, "argv", ["repair_corpus.py", "--verify", str(bundle)])
    assert rc.main() == 0
    assert "verified" in capsys.readouterr().out

    summary_path = bundle / "summary.json"
    body = json.loads(summary_path.read_text())
    body["outcomes"] = {**body["outcomes"], "repaired": 99}
    summary_path.write_text(json.dumps(body, indent=2, sort_keys=True))

    monkeypatch.setattr(sys, "argv", ["repair_corpus.py", "--verify", str(bundle)])
    assert rc.main() == 1
    assert "NOT verified" in capsys.readouterr().err


def test_verify_recomputes_under_the_planner_posture_the_bundle_recorded(tmp_path):
    """corpus_summary adds the shadow block only for a shadow/live run, so a verifier assuming
    "off" would reject an honest shadow bundle."""
    assert rc.settings_planner_mode(tmp_path) == "off"  # unreadable settings fail safe
    (tmp_path / "settings.json").write_text(json.dumps({"planner": "shadow"}))
    assert rc.settings_planner_mode(tmp_path) == "shadow"
    (tmp_path / "settings.json").write_text(json.dumps({"planner": "nonsense"}))
    assert rc.settings_planner_mode(tmp_path) == "off"


def test_verification_survives_the_engine_growing_a_new_operation(tmp_path, monkeypatch, capsys):
    """An untouched bundle must not start failing verification because OPERATIONS grew.

    never_exercised is the one summary field the rows cannot re-derive alone - it needs the list
    of operations that existed - so the run's catalogue is sealed and recomputation uses it.
    """
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(_RECORDED_ROWS))
    bundle = tmp_path / "bundle"
    rc.bundle_from_rows(rows_json, bundle,
                        settings={"operations_catalogue": ["fix_small_edges", "sew_faces"]})
    monkeypatch.setattr(sys, "argv", ["repair_corpus.py", "--verify", str(bundle)])
    assert rc.main() == 0

    # the engine of tomorrow carries an operation this run never had
    monkeypatch.setattr(rc, "operation_catalogue",
                        lambda: ["fix_small_edges", "sew_faces", "fix_brand_new_thing"])
    monkeypatch.setattr(sys, "argv", ["repair_corpus.py", "--verify", str(bundle)])
    assert rc.main() == 0, "a grown catalogue made untouched evidence look edited"
    assert "verified" in capsys.readouterr().out


def test_a_bundle_sealing_no_catalogue_is_verified_against_its_own_recorded_one(tmp_path):
    """Bundles sealed before the catalogue was recorded still verify: the run's operation universe
    is recovered from the summary's own catalogue block."""
    summary = rc.corpus_summary(_RECORDED_ROWS, "off", ["fix_small_edges", "sew_faces"])
    recovered = rc.settings_operation_catalogue(tmp_path, summary)
    assert set(recovered) == {"fix_small_edges", "sew_faces"}
    # what has to hold is not the declaration order - that is gone - but that recomputing with the
    # recovered list reproduces the stored catalogue exactly, so verification passes
    assert rc.corpus_summary(_RECORDED_ROWS, "off", recovered)["catalogue"] == summary["catalogue"]
    # nothing to go on at all is honestly None, not a guess
    assert rc.settings_operation_catalogue(tmp_path, {}) is None


def test_a_sealed_catalogue_beats_the_reconstruction(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"operations_catalogue": ["sealed_op"]}))
    summary = {"catalogue": {"tally": {"other_op": {}}, "never_exercised": []}}
    assert rc.settings_operation_catalogue(tmp_path, summary) == ["sealed_op"]


def test_two_corpus_roots_of_the_same_name_do_not_shadow_each_others_inputs(tmp_path):
    """`/runs/a/corpus` and `/runs/b/corpus` are two corpora both named `corpus`. Keyed on the
    directory name alone, a `part.step` in each collides and the second hash replaces the first,
    leaving the bundle unable to name every input behind its totals."""
    files = []
    for side, body in (("a", b"solid A"), ("b", b"solid B")):
        root = tmp_path / side / "corpus"
        root.mkdir(parents=True)
        part = root / "part.step"
        part.write_bytes(body)
        files.append(part)
    roots = [tmp_path / "a" / "corpus", tmp_path / "b" / "corpus"]

    hashes = rc.hash_corpus_inputs(roots, files)

    assert len(hashes) == 2, f"an input was shadowed: {hashes}"
    assert sorted(hashes) == ["a/corpus/part.step", "b/corpus/part.step"]
    assert len(set(hashes.values())) == 2


def test_one_corpus_root_keeps_its_keys_bare(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    part = root / "part.step"
    part.write_bytes(b"solid")
    assert list(rc.hash_corpus_inputs([root], [part])) == ["part.step"]


def test_rebundling_a_shadow_planner_run_seals_a_bundle_that_verifies(tmp_path, monkeypatch,
                                                                      capsys):
    """corpus_summary only adds the shadow block for a shadow/live run. Sealing with "off" while
    the settings say "shadow" wrote a summary that --verify, which reads the posture back out of
    those settings, recomputed differently and rejected - so a recorded model-planner run could
    not be packaged with its own settings."""
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(_RECORDED_ROWS))
    bundle = tmp_path / "bundle"

    rc.bundle_from_rows(rows_json, bundle, settings={"planner": "shadow"})

    monkeypatch.setattr(sys, "argv", ["repair_corpus.py", "--verify", str(bundle)])
    assert rc.main() == 0, capsys.readouterr().err
