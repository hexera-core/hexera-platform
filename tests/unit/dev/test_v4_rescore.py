"""rescore.py: re-score recorded arm rows under the 2026-11-24 definition (clean = valid + panel), from the
layers the rows already carry. Hermetic: rows are dicts in tmp_path; no checker, no wave, no bucket."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
_SPEC = importlib.util.spec_from_file_location("v4_rescore_under_test", V4 / "rescore.py")
assert _SPEC is not None and _SPEC.loader is not None
rs = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rs
_SPEC.loader.exec_module(rs)


def _row(i: int, arm: str, **over) -> dict:
    row = {"name": f"f{i}.step", "sha256": f"{i:064x}", "source": "abc_fail", "arm": arm, "inconclusive": False,
           "model_usd": 0.0, "wall_s": 1.0, "panel_size": 4, "out_tier": "full",
           "in_valid": True, "in_faithful_ok": True, "in_panel_passes": 2, "in_clean": False,
           "out_valid": True, "out_faithful_ok": True, "out_panel_passes": 4, "out_clean": True}
    row.update(over)
    return row


def _write(tmp_path: Path, rows: list[dict], name: str = "w") -> Path:
    root = tmp_path / name
    d = root / "shard-1-of-1"
    d.mkdir(parents=True)
    (d / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "EXCLUDED.json").write_text("[]")
    return root


def test_clean_is_recomputed_from_valid_and_panel_never_from_the_stored_flag(tmp_path) -> None:
    rows = [
        # the interesting class: the old rule called it unclean ONLY because layer 3 failed
        _row(1, "staged", out_faithful_ok=False, out_clean=False),
        _row(2, "staged", out_valid=False, out_clean=False),                       # invalid: unclean either way
        _row(3, "staged", out_panel_passes=3, out_clean=False),                    # panel short: unclean either way
        _row(4, "staged"),                                                         # clean under both
    ]
    out = rs.rescore(_write(tmp_path, rows))
    arm = out["arms"]["staged"]
    assert arm["clean_old"] == 1 and arm["clean_new"] == 2
    assert [f["sha256"] for f in arm["newly_clean"]] == [f"{1:064x}"]
    assert arm["newly_clean"][0]["why"] == "layer 3 only"
    assert arm["no_longer_clean"] == []


def test_a_row_missing_a_layer_is_refused_not_guessed(tmp_path) -> None:
    with pytest.raises(rs.RescoreRefused, match="out_valid"):
        rs.rescore(_write(tmp_path, [_row(1, "staged", out_valid=None)], "a"))
    with pytest.raises(rs.RescoreRefused, match="panel"):
        rs.rescore(_write(tmp_path, [_row(1, "staged", out_panel_passes=None)], "b"))


def test_an_inconclusive_row_is_counted_apart_and_re_scored_as_nothing(tmp_path) -> None:
    out = rs.rescore(_write(tmp_path, [_row(1, "staged", inconclusive=True), _row(2, "staged")]))
    arm = out["arms"]["staged"]
    assert arm["files"] == 2 and arm["conclusive"] == 1 and arm["inconclusive"] == 1
    assert arm["clean_new"] == 1


def test_the_input_is_re_scored_by_the_same_rule_so_the_gain_is_consistent(tmp_path) -> None:
    rows = [_row(1, "staged", in_valid=True, in_panel_passes=4, in_faithful_ok=False, in_clean=False)]
    arm = rs.rescore(_write(tmp_path, rows))["arms"]["staged"]
    assert arm["clean_in_new"] == 1 and arm["clean_new"] == 1
    assert arm["clean_gain_new"] == 0          # it was already clean under the new rule: the arm gained nothing
    assert arm["clean_gain_old"] == 1          # the old rule credited a gain that was only layer 3


def test_the_gaming_review_cannot_be_rebuilt_from_rows_that_never_measured_it(tmp_path) -> None:
    out = rs.rescore(_write(tmp_path, [_row(1, "staged", out_faithful_ok=False, out_clean=False)]))
    g = out["arms"]["staged"]["gaming"]
    assert g["measurable"] is False and g["unmeasured"] == 1 and g["flagged"] == []
    assert "volume" in g["note"] and "bbox" in g["note"]


def test_rows_that_carry_the_ratios_are_flagged(tmp_path) -> None:
    rows = [_row(1, "staged", out_volume_ratio=3.0, out_bbox_ratio=1.0),
            _row(2, "staged", out_volume_ratio=1.0, out_bbox_ratio=0.5),
            _row(3, "staged", out_volume_ratio=1.1, out_bbox_ratio=0.95)]
    g = rs.rescore(_write(tmp_path, rows))["arms"]["staged"]["gaming"]
    assert g["measurable"] is True and g["unmeasured"] == 0
    assert sorted(f["sha256"] for f in g["flagged"]) == [f"{1:064x}", f"{2:064x}"]


def test_the_markdown_says_it_is_a_re_score_and_names_the_rule(tmp_path) -> None:
    out = rs.rescore(_write(tmp_path, [_row(1, "staged", out_faithful_ok=False, out_clean=False)]))
    md = rs.render(out, title="dev snapshot v1", source="snap-v1/results/snap-v1-dev")
    assert "RE-SCORE" in md and "clean = valid + panel" in md
    assert "not a re-run" in md.lower() and "snap-v1/results/snap-v1-dev" in md
    assert "| staged |" in md
