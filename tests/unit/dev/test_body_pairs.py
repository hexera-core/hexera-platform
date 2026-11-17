"""classify_pairs: touching vs overlapping bodies, with the kernel behind injected callables."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "body_pairs", Path(__file__).resolve().parents[3] / "devtools/validation/body_pairs.py"
)
body_pairs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(body_pairs)


def _run(solids, volumes, **kw):
    kw.setdefault("eps_mm3", 1e-6)
    kw.setdefault("pair_cap", 40)
    kw.setdefault("deadline_s", 120.0)
    return body_pairs.classify_pairs(
        solids,
        boxes_meet=lambda a, b: True,
        common_volume=lambda a, b: volumes[(a, b)],
        **kw,
    )


def test_classify_counts_overlap_and_touch():
    vols = {(0, 1): 5.0, (0, 2): 0.0, (1, 2): 1e-9}
    out = _run([0, 1, 2], vols)
    assert out["overlapping"] == 1
    assert out["touching"] == 2
    assert out["pairs_checked"] == 3
    assert out["complete"] is True


def test_bbox_prefilter_skips_kernel():
    calls = []
    out = body_pairs.classify_pairs(
        [0, 1],
        eps_mm3=1e-6,
        pair_cap=40,
        deadline_s=10,
        boxes_meet=lambda a, b: False,
        common_volume=lambda a, b: calls.append((a, b)) or 1.0,
    )
    assert calls == [] and out["overlapping"] == 0 and out["touching"] == 0
    assert out["complete"] is True


def test_pair_cap_marks_incomplete():
    solids = list(range(5))
    vols = {(i, j): 1.0 for i in solids for j in solids if i < j}
    out = _run(solids, vols, pair_cap=3)
    assert out["pairs_checked"] == 3
    assert out["complete"] is False


def test_deadline_marks_incomplete():
    ticks = iter([0.0, 0.0, 200.0, 200.0, 200.0, 200.0])
    vols = {(0, 1): 1.0, (0, 2): 1.0, (1, 2): 1.0}
    out = _run([0, 1, 2], vols, deadline_s=120.0, clock=lambda: next(ticks))
    assert out["complete"] is False
    assert out["pairs_checked"] < 3


def test_defaults_match_the_binding_signature():
    out = body_pairs.classify_pairs([0, 1], boxes_meet=lambda a, b: True, common_volume=lambda a, b: 1.0)
    assert out["overlapping"] == 1 and out["complete"] is True


def test_meeting_pairs_equal_to_cap_stays_complete():
    vols = {(0, 1): 1.0, (0, 2): 1.0, (1, 2): 0.0}
    out = _run([0, 1, 2], vols, pair_cap=3)
    assert out["pairs_checked"] == 3 and out["complete"] is True


def test_failed_common_is_not_a_touch_and_marks_incomplete():
    vols = {(0, 1): None, (0, 2): 0.0, (1, 2): 1.0}
    out = _run([0, 1, 2], vols)
    assert out["failed"] == 1
    assert out["touching"] == 1 and out["overlapping"] == 1
    assert out["complete"] is False
