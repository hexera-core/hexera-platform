"""D21: the oracle harness's own guarantees - the count, the per-file kill, the gaming flags, the summary of a
killed file's partial evidence. The sweep itself is native (it runs the engine)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval/oracle.py"
_spec = importlib.util.spec_from_file_location("v4_oracle_under_test", _PATH)
oracle = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(oracle)  # type: ignore[union-attr]


def _entry(i: int) -> dict:
    return {"sha256": (f"{i:x}" * 64)[:64], "name": f"f{i}.step"}


def test_the_collector_refuses_unless_every_input_has_exactly_one_row(tmp_path: Path) -> None:
    manifest = [_entry(i) for i in range(3)]
    for e in manifest[:2]:
        (tmp_path / f"{e['sha256'][:16]}{oracle.ROW_SUFFIX}").write_text(json.dumps({"sha256": e["sha256"]}))
    rows, problems = oracle.collect(manifest, str(tmp_path))
    assert len(rows) == 2 and problems and "1 of 3 inputs have NO row" in problems[0]
    (tmp_path / f"{manifest[2]['sha256'][:16]}{oracle.ROW_SUFFIX}").write_text(json.dumps(manifest[2]))
    rows, problems = oracle.collect(manifest, str(tmp_path))
    assert len(rows) == 3 and problems == []
    (tmp_path / "stray.row.json").write_text(json.dumps(_entry(9)))
    assert any("not in the manifest" in p for p in oracle.collect(manifest, str(tmp_path))[1])


def test_the_collect_command_exits_non_zero_on_a_short_count(tmp_path: Path) -> None:
    m = tmp_path / "m.json"
    m.write_text(json.dumps([_entry(1)]))
    assert oracle.main(["collect", "--manifest", str(m), "--rows", str(tmp_path), "--out", str(tmp_path / "t")]) == 2


def test_shards_partition_the_manifest() -> None:
    manifest = [_entry(i) for i in range(10)]
    parts = [oracle.shard_entries(manifest, f"{i}/3") for i in (1, 2, 3)]
    assert sorted(e["sha256"] for p in parts for e in p) == sorted(e["sha256"] for e in manifest)


def test_the_parent_kills_a_child_at_the_file_wall_and_still_writes_a_row(tmp_path: Path) -> None:
    e = _entry(5)
    odir = tmp_path / e["sha256"][:16]
    odir.mkdir()
    (odir / "candidates.jsonl").write_text(json.dumps({"outcome": "applied"}) + "\n" + '{"outcome": "decl')  # cut
    (odir / "progress.json").write_text(json.dumps({"stage": "depth1 3/20 split_angle"}))
    row = oracle.run_file(e, str(tmp_path), str(tmp_path), wall_s=1.0,
                          _cmd=[sys.executable, "-c", "import time; time.sleep(60)"])
    assert row["status"] == "killed" and row["wall_s"] < 30
    assert row["stopped_at"]["stage"].startswith("depth1") and row["recorded_lines"] == 1
    assert json.loads((tmp_path / f"{e['sha256'][:16]}{oracle.ROW_SUFFIX}").read_text())["status"] == "killed"


def test_a_child_that_dies_without_a_row_is_an_error_row(tmp_path: Path) -> None:
    row = oracle.run_file(_entry(6), str(tmp_path), str(tmp_path), wall_s=30, _cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    assert row["status"] == "error" and "exited 3" in row["error"]


@pytest.mark.parametrize(("metrics", "flagged"), [
    ({"volume_ratio": 1.1, "bbox_min_axis_ratio": 0.99}, False),
    ({"volume_ratio": 0.7}, True),                 # > 25% volume change
    ({"volume_ratio": 1.0, "bbox_min_axis_ratio": 0.85}, True),   # > 10% bbox shrink on an axis
])
def test_the_owners_gaming_rule(metrics: dict, flagged: bool) -> None:
    assert bool(oracle.gamed(metrics)) is flagged


def test_winners_are_full_verdict_improvements_over_the_root(tmp_path: Path) -> None:
    lines = [{"outcome": "applied", "full": {"clean": True, "passes": 4, "valid": True}, "own": {"volume_ratio": 1.0},
              "gaming": [], "op": "sew"},
             {"outcome": "applied", "full": {"clean": False, "passes": 2, "valid": True}, "op": "fill_loop"},
             {"outcome": "applied", "full": {"clean": False, "passes": 3, "valid": True}, "op": "fuse"},
             {"outcome": "declined"}]
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    row = oracle.summarise({"staged": {"passes": 2, "valid": True}}, p)
    assert [w["op"] for w in row["winners"]] == ["sew", "fuse"] and row["clean_found"] and row["clean_ungamed"]
    assert row["outcomes"] == {"applied": 3, "declined": 1}
