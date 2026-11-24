"""Self-check for divergence.py: a real divergence, and the ACCESS / JUDGEMENT / DUPLICATE call.
Run: python -m pytest -q test_divergence.py"""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("dv", Path(__file__).with_name("divergence.py"))
dv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dv)

START = {"kind": "expand", "state": "S0", "depth": 0, "tier": "full", "score": [0, 1, 0.0, -0.0, 0],
         "sites": [{"kind": "overlap"}, {"kind": "invalid"}]}


def det_trace():
    return [START,
            {"kind": "candidate", "state": "S0", "outcome": "evaluated", "op": "defeature", "source": "det",
             "params": {"source": "site"}, "child": "S1", "parent_score": [0, 1, 0.0, -0.0, 0],
             "score": [0, 1, 0.0, -0.1, 0]},
            {"kind": "candidate", "state": "S0", "outcome": "rejected", "op": "conformalise", "source": "det",
             "params": {"mode": "fragment"}, "stage": "declined", "errors": ["unresolved_overlap: volume 3"]},
            {"kind": "confirm", "state": "S1", "score": [0, 1, 0.0, -0.1, 0]},
            {"kind": "stop", "best": "S1", "best_path": ["S0", "S1"], "best_score": [0, 1, 0.0, -0.1, 0],
             "clean": False, "reason": "patience"}]


def ai_trace():
    return [START,
            # det offers the same defeature here, and the AI's fuse wins: JUDGEMENT is not it - det never named fuse
            {"kind": "candidate", "state": "S0", "outcome": "evaluated", "op": "defeature", "source": "det",
             "params": {"source": "site"}, "child": "S1", "parent_score": [0, 1, 0.0, -0.0, 0],
             "score": [0, 1, 0.0, -0.1, 0]},
            {"kind": "candidate", "state": "S0", "outcome": "evaluated", "op": "fuse", "source": "ai",
             "params": {}, "child": "S2", "parent_score": [0, 1, 0.0, -0.0, 0], "score": [1, 4, 0.3, -0.5, 0],
             "rationale": "the two bodies overlap; fuse them into one"},
            # the AI also names an op det had right here: JUDGEMENT
            {"kind": "candidate", "state": "S0", "outcome": "evaluated", "op": "defeature", "source": "ai",
             "params": {"source": "site"}, "child": "S3", "parent_score": [0, 1, 0.0, -0.0, 0],
             "score": [0, 2, 0.1, -0.2, 0]},
            {"kind": "confirm", "state": "S2", "score": [1, 4, 0.3, -0.5, 0]},
            {"kind": "stop", "best": "S2", "best_path": ["S0", "S2"], "best_score": [1, 4, 0.3, -0.5, 0],
             "clean": True, "reason": "clean", "ended_by": "clean"}]


def _rows(sha):
    return {("search_det", sha): {"arm": "search_det", "sha256": sha, "name": "p.step", "inconclusive": False,
                                  "out_clean": False, "out_panel_passes": 1, "panel_size": 4},
            ("search_ai", sha): {"arm": "search_ai", "sha256": sha, "name": "p.step", "inconclusive": False,
                                 "out_clean": True, "out_panel_passes": 4, "panel_size": 4}}


def test_the_first_divergence_its_ops_and_which_path_ended_better():
    sha = "a" * 64
    rep = dv.run(_rows(sha), {("search_det", sha[:12]): det_trace(), ("search_ai", sha[:12]): ai_trace()})
    (f,) = rep["files"]
    assert f["same_start"] is True
    d = f["first_divergence"]
    assert d["depth"] == 1
    assert d["det"]["op"] == 'defeature{"source": "site"}' and d["ai"]["op"] == "fuse"
    assert d["ai"]["source"] == "ai"
    assert d["ai"]["other_arm_had_this_op_here"] is False      # det never named fuse at S0: ACCESS
    assert f["better_end"] == "ai" and f["end_ai"]["clean"] is True and f["end_det"]["clean"] is False


def test_access_judgement_and_duplicate_are_told_apart():
    sha = "b" * 64
    rep = dv.run(_rows(sha), {("search_det", sha[:12]): det_trace(), ("search_ai", sha[:12]): ai_trace()})
    (f,) = rep["files"]
    assert f["ai_turn"]["verdicts"] == {"ACCESS": 1, "JUDGEMENT": 1}
    assert f["ops_only_ai_proposed"] == ["fuse"]
    assert f["ai_turn"]["ai_ops_on_best_path"] == ["fuse"]
    assert rep["aggregate"]["ai_ops_on_a_delivered_path"] == 1


def test_a_pair_is_skipped_unless_both_arms_are_conclusive_and_both_traces_exist():
    sha = "c" * 64
    rows = _rows(sha)
    rows[("search_ai", sha)]["inconclusive"] = True
    rep = dv.run(rows, {("search_det", sha[:12]): det_trace(), ("search_ai", sha[:12]): ai_trace()})
    assert rep["files"] == [] and "inconclusive" in rep["missing"][0]["why"]
    rep2 = dv.run(_rows(sha), {("search_det", sha[:12]): det_trace()})
    assert rep2["files"] == [] and "no trace for search_ai" in rep2["missing"][0]["why"]
