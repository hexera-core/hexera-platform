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


# ------------------------------------------------------------------ a wave still running: rows.partial.jsonl

def _write_partial(tmp_path: Path, rows: list[dict], name: str = "p", shard: str = "shard-1-of-6") -> Path:
    root = tmp_path / name
    d = root / shard
    d.mkdir(parents=True)
    (d / "rows.partial.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return root


def test_a_running_wave_is_re_scored_from_its_streamed_partial_rows(tmp_path) -> None:
    out = rs.rescore(_write_partial(tmp_path, [_row(1, "staged", out_faithful_ok=False, out_clean=False),
                                               _row(2, "staged")]))
    assert out["partial"] is True and out["partial_shards"] == ["shard-1-of-6"]
    arm = out["arms"]["staged"]
    assert arm["conclusive"] == 2 and arm["clean_old"] == 1 and arm["clean_new"] == 2


def test_a_finished_shard_is_preferred_over_its_own_partial_file(tmp_path) -> None:
    root = _write_partial(tmp_path, [_row(1, "staged", out_clean=False, out_valid=False)], "x", "shard-1-of-1")
    (root / "shard-1-of-1" / "rows.jsonl").write_text(json.dumps(_row(1, "staged")) + "\n")
    out = rs.rescore(root)
    assert out["partial"] is False and out["partial_shards"] == []
    assert out["arms"]["staged"]["clean_new"] == 1      # the final file, not the streamed one


def test_the_markdown_says_a_partial_table_is_provisional(tmp_path) -> None:
    out = rs.rescore(_write_partial(tmp_path, [_row(1, "staged")]))
    md = rs.render(out, title="dev snapshot v1r", source="snap-v1r/partial")
    assert "PROVISIONAL" in md and "still running" in md and "shard-1-of-6" in md
    done = rs.rescore(_write(tmp_path, [_row(1, "staged")], "done"))
    assert "still running" not in rs.render(done, title="t", source="s")


# ------------------------------------------------------------------ files no arm comparison can use

def _excluded(i: int, arm: str, reason: str = "file_timeout: file wall clock 1800s exceeded") -> dict:
    return {"name": f"f{i}.step", "sha256": f"{i:064x}", "source": "abc_fail", "arm": arm, "reason": reason}


def test_a_file_most_arms_were_excluded_on_is_named_as_unusable(tmp_path) -> None:
    """LEAD-2's dev3 case: 3 of 4 arms hit the file wall, so the file contributes to no arm comparison."""
    root = tmp_path / "w"
    d = root / "shard-1-of-1"
    d.mkdir(parents=True)
    arms = ("input_only", "staged", "search_det", "search_ai")
    # file 1: only input_only got a verdict. file 2: every arm did, so it stays usable
    (d / "rows.jsonl").write_text(json.dumps(_row(1, "input_only")) + "\n"
                                  + "".join(json.dumps(_row(2, a)) + "\n" for a in arms))
    (d / "EXCLUDED.json").write_text(json.dumps([_excluded(1, a) for a in ("staged", "search_det", "search_ai")]))
    out = rs.rescore(root, arms=arms)

    assert out["arms"]["staged"]["excluded"] == 1 and out["arms"]["input_only"]["excluded"] == 0
    (unusable,) = out["unpaired_files"]
    assert unusable["sha256"] == f"{1:064x}"
    assert unusable["measured_arms"] == ["input_only"]
    assert unusable["excluded_arms"] == ["search_ai", "search_det", "staged"]
    assert "file_timeout" in unusable["reasons"][0]
    md = rs.render(out, title="t", source="s")
    assert "contribute to no arm comparison" in md and "f1.step" in md


def test_a_file_every_arm_measured_is_not_called_unpaired(tmp_path) -> None:
    root = tmp_path / "ok"
    d = root / "shard-1-of-1"
    d.mkdir(parents=True)
    (d / "rows.jsonl").write_text("".join(json.dumps(_row(1, a)) + "\n" for a in ("input_only", "staged")))
    (d / "EXCLUDED.json").write_text("[]")
    out = rs.rescore(root, arms=("input_only", "staged"))
    assert out["unpaired_files"] == []
    assert "contribute to no arm comparison" not in rs.render(out, title="t", source="s")


def test_a_pending_arm_is_not_reported_as_an_exclusion(tmp_path) -> None:
    """In a running wave an arm with no row may simply not have reached the file yet. Only an EXCLUDED entry is a
    final "no verdict"; calling the rest unusable would overstate what the wave lost."""
    root = tmp_path / "mix"
    final, running = root / "shard-1-of-2", root / "shard-2-of-2"
    final.mkdir(parents=True)
    running.mkdir(parents=True)
    (final / "rows.jsonl").write_text(json.dumps(_row(1, "input_only")) + "\n")
    (final / "EXCLUDED.json").write_text(json.dumps([_excluded(1, "staged")]))
    (running / "rows.partial.jsonl").write_text(json.dumps(_row(2, "input_only")) + "\n")
    out = rs.rescore(root, arms=("input_only", "staged"))

    killed = next(f for f in out["unpaired_files"] if f["sha256"] == f"{1:064x}")
    pending = next(f for f in out["unpaired_files"] if f["sha256"] == f"{2:064x}")
    assert killed["final"] is True and pending["final"] is False
    md = rs.render(out, title="t", source="s")
    assert "contribute to no arm comparison" in md and "f1.step" in md
    assert "still running" in md and "f2.step" in md
    # the pending file is NOT listed under the unusable heading
    unusable = md.split("contribute to no arm comparison")[1].split("##")[0]
    assert "f1.step" in unusable and "f2.step" not in unusable
