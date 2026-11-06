"""Hermetic tests for the lab's pure logic. No kernel: geometry is exercised by the sweep itself.

    PYTHONPATH=src:. .venv/bin/python -m pytest labs/defect_cad_rag/tests -q
"""

from __future__ import annotations

from types import SimpleNamespace

from labs.defect_cad_rag.knn import Row, _auc, evaluate
from labs.defect_cad_rag.patch import DIMENSIONS, _log_ratio
from labs.defect_cad_rag.sweep import _stratified, family_of


def test_family_takes_first_token_and_joins_a_known_family_named_later():
    assert family_of("bm2_ec_t4-A.stp") == "bm2"
    assert family_of("bug25013_25013face.stp") == "bug25013"
    assert family_of("bug21802_as1-oc-214.stp", {"as1", "bm2"}) == "as1"
    assert family_of("bug21802_as1-oc-214.stp") == "bug21802"


def test_stratified_sampling_round_robins_codes_so_a_rare_code_is_kept():
    def d(code, fp):
        return SimpleNamespace(code=SimpleNamespace(value=code), fingerprint=fp)

    defects = [d("small_edge", f"e{i}") for i in range(50)] + [d("open_shell", "o1")]
    chosen = _stratified(defects, 4)
    assert len(chosen) == 4
    assert 50 in chosen  # the one open_shell survives a 50:1 imbalance
    assert chosen == _stratified(defects, 4)  # deterministic


def test_log_ratio_is_clipped_not_infinite():
    assert _log_ratio(0.0, 1.0) == -6.0
    assert _log_ratio(1e12, 1.0) == 6.0
    assert _log_ratio(10.0, 1.0) == 1.0
    assert DIMENSIONS == 3 * 19 + 4 + 4


def test_auc_counts_ties_as_half():
    assert _auc([(0.9, 1), (0.1, 0)]) == 1.0
    assert _auc([(0.5, 1), (0.5, 0)]) == 0.5
    assert _auc([(0.5, 1)]) is None


def _rows(geometry_matters: bool) -> list[Row]:
    """Two ops on one code; which one is kept depends on the first coordinate when it matters."""
    rows = []
    for fam in range(8):
        for i in range(6):
            x = float(i % 2)
            good = ("a" if x == 0 else "b") if geometry_matters else "a"
            rows.append(Row(family=f"f{fam}", file=f"f{fam}-{i}", code="open_shell",
                            vector=(x, 0.0), labels={"a": int(good == "a"), "b": int(good == "b")},
                            rules_op="a"))
    return rows


def test_knn_beats_code_prior_only_when_geometry_carries_the_answer():
    signal = evaluate(_rows(True))["top1_kept_rate"]["multi_op"]
    assert signal["knn"]["rate"] == 1.0
    assert signal["prior"]["rate"] < 0.75
    assert signal["rules"]["rate"] == 0.5

    flat = evaluate(_rows(False))["top1_kept_rate"]["multi_op"]
    assert flat["knn"]["rate"] == flat["prior"]["rate"] == flat["rules"]["rate"] == 1.0


def test_leave_family_out_never_retrieves_the_query_family():
    # One family holds the only examples of a pattern. Held out, nothing else has it, so the
    # neighbours cannot know it and knn falls back to the training families' answer.
    rows = _rows(False)
    rows += [Row("lonely", f"l{i}", "open_shell", (5.0, 5.0), {"a": 0, "b": 1}, "a")
             for i in range(3)]
    lonely = evaluate(rows)["top1_kept_rate"]["multi_op"]["knn"]
    assert lonely["rate"] < 1.0


def test_threshold_binds_on_raw_scores_despite_calibration_ties():
    from labs.defect_cad_rag.policy import TARGET_KEEP_RECALL, _calibrator, _threshold

    # 40 keeps at high score, 2 keeps stranded low among 100 hopeless attempts. The two stranded
    # keeps are 4.8% of all keeps - inside the 5% loss allowance - so the largest valid cutoff
    # sits at the high-score mass and sacrifices them.
    scored = [(0.9, 1)] * 40 + [(0.05, 1)] * 2 + [(0.04, 0)] * 100
    t = _threshold(scored)
    assert t == 0.9
    lost = sum(y for s, y in scored if s < t)
    assert lost / 42 <= 1 - TARGET_KEEP_RECALL + 0.03
    # the binned calibrator would have tied 0.05 and 0.04 into one bin
    cal = _calibrator(scored)
    assert cal(0.05) == cal(0.04)
    assert sum(1 for s, _ in scored if s < t) >= 100  # the hopeless mass is skippable


def test_replay_budget_counts_keeps_within_op_and_time_limits():
    from labs.defect_cad_rag.policy import MAX_OPS, R, _replay

    rows = [R("f", "one.step", "open_shell", (0.0,), {"sew_faces": i % 2}, "sew_faces",
              kept=i % 2, seconds=30.0, order=i) for i in range(8)]
    probs = {i: float(r.kept) for i, r in enumerate(rows)}
    out = _replay(rows, probs)
    # 120 s / 30 s = 4 attempts fit. Scan order alternates 0,1,0,1 -> 2 keeps;
    # ranked order puts the four keeps first -> 4 keeps.
    assert out == {"scan": 2, "ranked": 4, "possible": 4, "files": 1,
                   "files_improved": 1, "files_worsened": 0}
    assert MAX_OPS == 6
