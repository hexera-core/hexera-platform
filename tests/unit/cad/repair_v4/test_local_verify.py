# Responsibility: lane C17 - the published shape of search_seq's local verification (the result's trace words).
# Boundaries: hermetic; no kernel.
from __future__ import annotations

from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult, PairResult


def test_the_outcome_and_trace_line_say_what_was_measured() -> None:
    assert LocalResult(True, [], {}, 1.24).line() == "gone (1.2 s)"
    assert LocalResult(False, [], {}, 0.5).outcome == "still_there"
    broke = LocalResult(True, [{"code": "open_shell", "entities": ["B3"]}], {}, 0.8)
    assert broke.outcome == "new_defect" and broke.line() == "new defect: open_shell on B3 (0.8 s)"


def test_a_cluster_defect_carries_its_pairs_and_the_result_reports_each() -> None:
    d = Defect("s1", "validity", "body_overlap", ("B1", "B2", "B3"), pairs=(("B1", "B2"), ("B2", "B3")))
    r = LocalResult(False, [], {}, 2.0, pairs=[PairResult(("B1", "B2"), True), PairResult(("B2", "B3"), False)])
    assert d.pairs[0] == ("B1", "B2") and [p.gone for p in r.pairs] == [True, False]
