"""Self-check for newops_triage.py's trace reader on a synthetic trace (no kernel). Run: python -m pytest -q this."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("nt", Path(__file__).with_name("newops_triage.py"))
nt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nt)

EVENTS = [
    {"kind": "expand", "state": "S0", "parent": None, "tier": "full", "score": [1, 1, 0.0, -0.0, 0],
     "sites": [{"kind": "overlap"}, {"kind": "sliver"}]},
    {"kind": "candidate", "state": "S0", "outcome": "evaluated", "op": "conformalise", "params": {"mode": "cut_by:B1"},
     "source": "det", "child": "S1", "parent_score": [1, 1, 0.0, -0.0, 0], "score": [1, 2, 0.1, -0.5, 0]},
    {"kind": "candidate", "state": "S0", "outcome": "rejected", "op": "conformalise", "params": {"mode": "fragment"},
     "source": "det", "stage": "declined", "errors": ["unresolved_overlap: volume 3"]},
    {"kind": "candidate", "state": "S0", "outcome": "rejected", "op": "fuse", "params": {}, "source": "ai",
     "stage": "declined", "errors": ["nothing_to_fuse: the bodies do not touch"]},
    {"kind": "candidate", "state": "S0", "outcome": "evaluated", "op": "defeature",
     "params": {"source": "recognised", "groups": [[0], [1, 2]]}, "source": "det", "child": "S2",
     "parent_score": [1, 1, 0.0, -0.0, 0], "score": [0, 0, 0.0, -2.0, 0],
     "rationale": "remove the recognised features at the site (1 holes, 1 blends) in one defeaturing call"},
    {"kind": "candidate", "state": "S0", "outcome": "duplicate", "op": "defeature", "params": {"source": "site"},
     "source": "det"},
    {"kind": "confirm", "state": "S1", "score": [1, 4, 0.2, -0.5, 0]},
    {"kind": "stop", "best": "S1", "best_path": ["S0", "S1"]},
]


def test_trace_outcomes_split_modes_path_confirm_and_sites():
    got = nt.trace_outcomes(EVENTS)
    ops = {k: dict(v) for k, v in got["ops"].items()}
    # confirmed child, full parent: whole tuples compared, full against full
    assert ops["conformalise:cut_by"] == {"full_vs_full:better": 1, "kept_on_best_path": 1}
    assert ops["conformalise:fragment"] == {"declined:unresolved_overlap": 1}
    assert ops["fuse"] == {"declined:nothing_to_fuse": 1}
    # unconfirmed (cheap) child, full parent: tier-invariant keys only - it made the part invalid
    assert ops["defeature"] == {"cheap_vs_full:made_INVALID": 1, "duplicate": 1}
    assert ops["trim_unbounded"] == {"never_proposed:no_site": 1}
    assert got["extra"]["defeature_source"] == {"recognised": 1, "site": 1}
    assert got["extra"]["defeature_features_per_call"] == {"2": 1}


def test_a_row_uses_its_trace_when_present_and_its_op_table_when_not(tmp_path):
    rows = tmp_path / "snap-v2-dev" / "shard-1-of-1"
    rows.mkdir(parents=True)
    base = {"in_panel_passes": 1, "out_panel_passes": 1, "panel_size": 4, "in_valid": True, "out_valid": True,
            "out_clean": False, "inconclusive": False,
            "triage": {"valid": {"ok": True, "hard": []}, "panel_failing": [{"name": "gmsh_prod", "error_kind": "other"}],
                       "search": {"ops": {"fuse": {"evaluated": 0, "declined": {"nothing_to_fuse: x": 2}}}}}}
    a = {**base, "arm": "search_det", "name": "abc/part_a.step", "sha256": "aaaaaaaaaaaa" + "0" * 52}
    b = {**base, "arm": "search_det", "name": "abc/part_b.step", "sha256": "bbbbbbbbbbbb" + "0" * 52}
    (rows / "rows.jsonl").write_text("\n".join(json.dumps(x) for x in (a, b)))
    tdir = tmp_path / "traces"
    tdir.mkdir()
    (tdir / "search_det__aaaaaaaaaaaa.json").write_text(json.dumps({"events": EVENTS}))
    t = nt.triage(nt.load([str(rows / "rows.jsonl")]), nt.load_traces([str(tdir / "*.json")]))
    by = {r["name"]: r for r in t["rows"]}
    assert by["abc/part_a.step"]["from"] == "trace" and "conformalise:cut_by" in by["abc/part_a.step"]["new_ops"]
    assert by["abc/part_b.step"]["from"] == "row" and by["abc/part_b.step"]["new_ops"]["fuse"] == {
        "declined:nothing_to_fuse": 2}
    assert t["failing_search_rows_from_trace"] == {"search_det": 1}
    assert "conformalise:cut_by" in nt.markdown(t)


def test_rows_compare_only_tier_invariant_keys():
    r = {"triage": {"search": {"ops": {"fuse": {"evaluated": 3}}, "deltas": [
        {"op": "fuse", "parent": [1, 4, 0.3, -0.0, 0], "score": [1, 1, 0.1, -0.2, 0]},   # fewer passes: cheap tier
        {"op": "fuse", "parent": [0, 1, 0.1, -0.0, 0], "score": [1, 0, 0.0, -0.9, 0]}]}}}
    got = dict(nt.op_outcomes(r, "fuse"))
    assert got == {"applied:stays_valid:dev_more": 1, "applied:made_VALID": 1, "applied:unscored(past deltas cap)": 1}
