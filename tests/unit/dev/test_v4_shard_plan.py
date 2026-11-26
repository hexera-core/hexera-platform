"""Cost-balanced shards: a wave may be cut by a SHARD PLAN (largest-first by predicted cost) instead of the hash.

The plan is one file; every place that cuts or checks the partition reads the same one, and a shard records the plan's
digest so a check against a different plan (or none) is refused rather than silently passing.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devtools" / "validation" / "v4_eval"))
import run_arm  # noqa: E402

evalspec = run_arm.evalspec
rp = run_arm.reprobe_mod
def _load_collect():
    import importlib.util

    path = Path(run_arm.__file__).resolve().parent / "collect.py"
    spec = importlib.util.spec_from_file_location("collect", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["collect"] = mod
    spec.loader.exec_module(mod)
    return mod


collect = _load_collect()


def _sha(i: int) -> str:
    return hashlib.sha256(f"f{i}".encode()).hexdigest()


def _entry(i: int) -> SimpleNamespace:
    return SimpleNamespace(name=f"f{i}.step", sha256=_sha(i), source="abc_fail")


def _row(sha: str) -> rp.ProbeRow:
    return rp.ProbeRow(f"{sha[:6]}.step", sha, "abc_fail", {"tier": "full", "clean": True}, None, "")


# a skewed population: a few giants and a long tail, the shape the measurement found
COSTS = {_sha(i): (900.0 if i < 6 else 60.0 if i < 60 else 5.0) for i in range(600)}


def test_the_plan_assigns_every_file_once_and_is_deterministic() -> None:
    plan = evalspec.build_shard_plan(COSTS, 8)
    assert plan["n"] == 8 and set(plan["assign"]) == set(COSTS)
    assert set(plan["assign"].values()) == set(range(1, 9))
    assert evalspec.build_shard_plan(dict(reversed(list(COSTS.items()))), 8) == plan
    assert evalspec.plan_digest(plan) == evalspec.plan_digest(json.loads(json.dumps(plan)))


def test_largest_first_beats_the_hash_on_the_slowest_shard() -> None:
    plan = evalspec.build_shard_plan(COSTS, 8)

    def worst(of) -> float:
        return max(sum(c for h, c in COSTS.items() if of(h) == i) for i in range(1, 9))

    lpt = worst(lambda h: plan["assign"][h])
    hashed = worst(lambda h: next(i for i in range(1, 9) if evalspec.shard_of(h, i, 8)))
    assert lpt <= hashed
    assert lpt <= sum(COSTS.values()) / 8 + max(COSTS.values())  # the LPT bound


def test_predicted_cost_grows_with_size() -> None:
    assert evalspec.predicted_cost_s(10_000_000) > evalspec.predicted_cost_s(100_000) > 0


def test_run_arm_cuts_a_shard_from_the_plan_and_refuses_what_the_plan_lacks(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(40)]
    plan = evalspec.build_shard_plan({e.sha256: float(i) for i, e in enumerate(entries)}, 4)
    parts = [run_arm.shard_entries(entries, f"{i}/4", plan) for i in range(1, 5)]
    assert sorted(e.sha256 for p in parts for e in p) == sorted(e.sha256 for e in entries)
    assert all(plan["assign"][e.sha256] == i for i, p in enumerate(parts, start=1) for e in p)
    with pytest.raises(ValueError, match="not in the shard plan"):
        run_arm.shard_entries([*entries, _entry(99)], "1/4", plan)
    with pytest.raises(ValueError, match="plan has 4 shards"):
        run_arm.shard_entries(entries, "1/5", plan)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert evalspec.load_shard_plan(path) == plan


def _shards(assign_of, n: int, prov_extra=None) -> list[dict]:
    shas = [_sha(i) for i in range(30)]
    out = []
    for i in range(1, n + 1):
        mine = [h for h in shas if assign_of(h) == i]
        out.append({"i": i, "n": n, "dir": Path(f"shard-{i}-of-{n}"), "rows": [_row(h) for h in mine],
                    "excluded": [], "prov": dict(prov_extra or {})})
    return out


def test_collect_checks_plan_cut_shards_against_the_plan() -> None:
    plan = evalspec.build_shard_plan({_sha(i): float(i) for i in range(30)}, 3)
    digest = evalspec.plan_digest(plan)
    shards = _shards(lambda h: plan["assign"][h], 3, {"shard_plan": digest})
    collect.check_partition(shards, plan)  # accepted
    with pytest.raises(collect.CollectRefused, match="shard plan"):
        collect.check_partition(shards)  # plan-cut shards checked by hash: refused, not a hash mismatch hunt
    other = evalspec.build_shard_plan({_sha(i): float(-i) for i in range(30)}, 3)
    with pytest.raises(collect.CollectRefused, match="shard plan"):
        collect.check_partition(shards, other)
    hashed = _shards(lambda h: next(i for i in range(1, 4) if evalspec.shard_of(h, i, 3)), 3)
    collect.check_partition(hashed)  # the hash path is unchanged
    with pytest.raises(collect.CollectRefused, match="shard plan"):
        collect.check_partition(hashed, plan)


def test_the_vm_hands_the_plan_to_run_arm() -> None:
    sh = (Path(run_arm.__file__).resolve().parents[1] / "v3_eval" / "vm_startup.sh").read_text()
    assert "v4-shard-plan" in sh and "planflag=(--shard-plan /eval/shard-plan.json)" in sh
    assert '${planflag[@]+"${planflag[@]}"} --shard' in sh and '${planmount[@]+"${planmount[@]}"}' in sh
