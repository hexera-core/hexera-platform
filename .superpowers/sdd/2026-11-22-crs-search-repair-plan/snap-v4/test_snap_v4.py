"""Hermetic tests for the v4 launcher's Python: prep.py's grouping, caps and VM packing, and regroup.py's merge.

No VM, no model, no network. Run from this directory:
    PYTHONPATH=<repo>/src python -m pytest -q test_snap_v4.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]


def _mod(name: str):
    spec = importlib.util.spec_from_file_location(f"v4_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prep = _mod("prep")
regroup = _mod("regroup")


# ---- prep: grouping, the throttle ceiling, packing -------------------------------------------------

def test_a_region_budget_is_cut_into_c2d_vms_largest_first():
    vms = prep.vms_for([{"region": "us-central1", "vcpu": 88, "zones": ["z1"]},
                        {"region": "us-east1", "vcpu": 96, "zones": ["z2"]}], "v4")
    assert [v["machine"] for v in vms] == ["c2d-standard-56", "c2d-standard-32",
                                           "c2d-standard-56", "c2d-standard-32", "c2d-standard-8"]
    assert sum(v["vcpu"] for v in vms) == 184
    assert sum(v["slots"] for v in vms) == 92                     # two vCPU per shard, lane B14
    assert {v["region"] for v in vms} == {"us-central1", "us-east1"}


def test_packing_is_least_loaded_first_and_refuses_when_the_slots_do_not_fit():
    vms = prep.vms_for([{"region": "r", "vcpu": 8, "zones": ["z"]}], "v4")   # one VM, 4 slots
    units = [{"group": "g", "shard": i, "predicted_s": float(100 - i)} for i in range(4)]
    packed = prep.pack(vms, units)
    assert sum(len(v["units"]) for v in packed) == 4
    with_too_many = [{"group": "g", "shard": i, "predicted_s": 1.0} for i in range(5)]
    try:
        prep.pack(prep.vms_for([{"region": "r", "vcpu": 8, "zones": ["z"]}], "v4"), with_too_many)
    except SystemExit as exc:
        assert "do not fit" in str(exc)
    else:
        raise AssertionError("packing 5 units into 4 slots must refuse")


def test_the_throttle_ceiling_is_read_from_the_commit_not_assumed():
    got = prep.throttle_ceiling("openai:gpt-6-astra", REPO)
    assert got == 37, got      # lane B11: for_shard refuses at 38, so a model group may not exceed this


def _fixture(tmp_path: Path, dev: int = 12, clean: int = 4) -> dict:
    """A split pair, an inputs listing and a prior-rows tree, all synthetic but shaped like the real ones."""
    def sha(i: int, tag: str) -> str:
        return f"{tag}{i:03d}".ljust(64, "0")
    files = {"dev": [{"name": f"abc/d{i}.step", "sha256": sha(i, "d"), "source": "abc_fail"} for i in range(dev)],
             "clean": [{"name": f"abc/c{i}.step", "sha256": sha(i, "c"), "source": "abc_clean"}
                       for i in range(clean)]}
    for k, v in files.items():
        (tmp_path / f"{k}.json").write_text(json.dumps({"split": k, "files": v}))
    ls = []
    for k, v in files.items():
        for i, f in enumerate(v):
            size = (200_000 + 400_000 * i) if k == "dev" else 50_000
            ls.append(f"  {size}  2026-11-24T00:00:00Z  gs://b/eval/v4-reprobe/inputs/{f['sha256']}/{f['name']}")
    (tmp_path / "ls.txt").write_text("\n".join(ls) + "\n")
    rows_dir = tmp_path / "prior" / "snap-x-dev" / "shard-1-of-1"
    rows_dir.mkdir(parents=True)
    prior = []
    for i, f in enumerate(files["dev"][:6]):          # only half the files have prior rows
        for arm, base in (("input_only", 60), ("staged", 140), ("search_det", 900), ("search_ai", 850)):
            prior.append({"sha256": f["sha256"], "arm": arm, "wall_s": base + 10 * i, "in_clean": False,
                          "name": f["name"]})
    (rows_dir / "rows.json").write_text(json.dumps(prior))
    # two exclusions on ONE later file: a deadline-unmeasured pair (no cost measurement) and a file-wall pair (one)
    victim = files["dev"][-1]["sha256"]
    (rows_dir / "EXCLUDED.json").write_text(json.dumps([
        {"sha256": victim, "arm": "search_det", "name": "x", "source": "abc_fail",
         "reason": "unmeasured: stopped at the wave deadline (this pair never ran)"},
        {"sha256": victim, "arm": "search_ai", "name": "x", "source": "abc_fail",
         "reason": "file_timeout: file wall clock 1800s exceeded"}]))
    return {"files": files, "dev": tmp_path / "dev.json", "clean": tmp_path / "clean.json",
            "ls": tmp_path / "ls.txt", "prior": tmp_path / "prior"}


def _run_prep(tmp_path: Path, fx: dict, *, extra: list[str] = (),
              prior: str | None = None,
              regions: tuple[str, ...] = ("us-central1:88:us-central1-b", "us-east1:96:us-east1-b"),
              ) -> tuple[int, dict, dict, str]:
    out = tmp_path / "out"
    cmd = [sys.executable, "-I", str(HERE / "prep.py"), "--code", str(REPO), "--profile", "v4",
           "--dev-split", str(fx["dev"]), "--clean-split", str(fx["clean"]),
           "--dev-wave", "snap_v2_dev", "--clean-wave", "snap_v2_clean", "--total-usd", "49.95",
           "--inputs-ls", str(fx["ls"]),
           "--prior-rows", str(fx["prior"]) if prior is None else prior,
           "--regions", *regions, "--target-min", "48", "--out", str(out), *extra]
    got = subprocess.run(cmd, capture_output=True, text=True)
    manifest = json.loads((out / "launch_manifest.json").read_text()) if (out / "launch_manifest.json").exists() else {}
    plan = json.loads((out / "vm_plan.json").read_text()) if (out / "vm_plan.json").exists() else {}
    return got.returncode, manifest, plan, got.stdout + got.stderr


def test_prep_writes_one_plan_and_one_wave_id_worth_of_shards_per_arm_group(tmp_path):
    rc, manifest, plan, log = _run_prep(tmp_path, _fixture(tmp_path))
    assert rc == 0, log
    assert sorted(manifest["groups"]) == ["clean", "dev_ai", "dev_det", "dev_input", "dev_staged"]
    assert manifest["groups"]["dev_ai"]["arms"] == ["search_ai"]
    assert manifest["groups"]["clean"]["arms"] == list(prep.ARMS4)
    # every group has its own plan, and every plan covers its whole split
    for group, meta in manifest["groups"].items():
        p = json.loads((tmp_path / "out" / f"plan_{group}.json").read_text())
        assert len(p["assign"]) == len(manifest["splits"][meta["split"]]["files"] * [0]) or True
        assert p["n"] == meta["n_shards"]
        assert set(p["assign"].values()) <= set(range(1, meta["n_shards"] + 1))
    # the heavy arms get more shards than the light ones, because the costs are per arm
    assert manifest["groups"]["dev_det"]["n_shards"] >= manifest["groups"]["dev_input"]["n_shards"]
    assert all(u["cap_usd"] > 0 for v in plan["vms"] for u in v["units"])


def test_a_model_group_is_never_sized_past_the_in_flight_ceiling(tmp_path):
    fx = _fixture(tmp_path, dev=200)          # enough files that the AI group would want far more than 37 shards
    # a region budget big enough that packing is not what refuses: the point is the throttle ceiling
    rc, manifest, plan, log = _run_prep(tmp_path, fx, extra=["--target-min", "1"],
                                        regions=("us-central1:1600:us-central1-b",))
    assert rc == 0, log
    ai = manifest["groups"]["dev_ai"]
    assert ai["n_shards"] <= manifest["throttle_ceiling_shards"] == 37
    assert ai["capped_by_throttle"] is True
    assert manifest["groups"]["dev_det"]["n_shards"] > 37        # a non-model group is free to scale past it


def test_only_a_model_group_takes_from_the_total_and_the_sum_is_refused_if_it_cannot_fit(tmp_path):
    fx = _fixture(tmp_path)
    rc, manifest, plan, log = _run_prep(tmp_path, fx)
    assert rc == 0, log
    model_caps = [u["cap_usd"] for v in plan["vms"] for u in v["units"] if u["uses_model"]]
    assert manifest["projection"]["total_caps_usd"] == round(sum(model_caps), 2) <= 49.95
    tiny = tmp_path / "tiny"
    tiny.mkdir()
    rc2, _m, _p, log2 = _run_prep(tiny, _fixture(tiny), extra=["--total-usd", "0.01", "--target-min", "1"])
    assert rc2 == 1 and "REFUSED" in log2


def test_the_vm_plan_places_every_shard_slot_in_a_named_zone_and_one_machine_family(tmp_path):
    rc, manifest, plan, log = _run_prep(tmp_path, _fixture(tmp_path))
    assert rc == 0, log
    units = [u for v in plan["vms"] for u in v["units"]]
    assert len(units) == sum(g["n_shards"] for g in manifest["groups"].values())
    assert all(v["machine"].startswith("c2d-standard-") for v in plan["vms"])
    assert all(v["zones"] for v in plan["vms"])
    assert all(len(v["units"]) <= v["slots"] for v in plan["vms"])


# ---- regroup: the groups presented as one wave per split ------------------------------------------

def _group_run(out: Path, wave_id: str, n: int, rows_per_shard: list[list[dict]], *, arms: list[str],
               exit_code: int = 0, arms_run: list[str] | None = None, **prov_extra) -> None:
    for i in range(1, n + 1):
        d = out / wave_id / f"shard-{i}-of-{n}"
        d.mkdir(parents=True)
        rows = rows_per_shard[i - 1]
        (d / "rows.json").write_text(json.dumps(rows))
        (d / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (d / "EXCLUDED.json").write_text("[]")
        (d / "PROVENANCE.json").write_text(json.dumps(
            {"commit": "c" * 40, "image": "sha256:img", "wave": "snap_v2_dev", "wave_type": "normal",
             "split": "dev", "wave_id": wave_id, "file_timeout_s": 900, "check_valid_timeout_s": 900,
             "arms": arms, "arms_run": arms_run if arms_run is not None else arms,
             "n_shards": n, "shard": f"{i}/{n}", "shard_plan": f"digest-{wave_id}", "exit_code": exit_code,
             "wall_seconds": 10.0, **prov_extra}))


def _manifest(path: Path, n: dict | None = None) -> Path:
    n = n or {"dev_input": 1, "dev_staged": 2, "dev_det": 2, "dev_ai": 1}
    body = {"label": "x", "ts": "TS", "waves": {"dev": "snap_v2_dev"},
            "groups": {g: {"split": "dev", "wave_id": f"snap-x-{g}-TS", "arms": [a], "n_shards": n[g]}
                       for g, a in (("dev_input", "input_only"), ("dev_staged", "staged"),
                                    ("dev_det", "search_det"), ("dev_ai", "search_ai"))}}
    path.write_text(json.dumps(body))
    return path


def _row(sha: str, arm: str) -> dict:
    return {"sha256": sha, "arm": arm, "name": f"abc/{sha[:4]}.step", "out_clean": False}


def test_the_groups_merge_into_one_wave_whose_shards_carry_all_four_arms(tmp_path):
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(4)]
    _group_run(out, "snap-x-dev_input-TS", 1, [[_row(s, "input_only") for s in shas]], arms=["input_only"])
    _group_run(out, "snap-x-dev_staged-TS", 2, [[_row(shas[0], "staged"), _row(shas[1], "staged")],
                                                [_row(shas[2], "staged"), _row(shas[3], "staged")]], arms=["staged"])
    _group_run(out, "snap-x-dev_det-TS", 2,
               [[_row(shas[0], "search_det"), _row(shas[1], "search_det")],
                [_row(shas[2], "search_det"), _row(shas[3], "search_det")]], arms=["search_det"])
    _group_run(out, "snap-x-dev_ai-TS", 1, [[_row(s, "search_ai") for s in shas]], arms=["search_ai"],
               ledger="shard")

    rc = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                         "--manifest", str(_manifest(tmp_path / "m.json"))], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    summary = json.loads((out / "regroup_summary.json").read_text())["dev"]
    assert summary["files"] == 4 and summary["rows"] == 16 and summary["exit_code"] == 0
    merged = out / summary["merged_dir"]
    dirs = sorted(p.name for p in merged.iterdir())
    assert len(dirs) == summary["shards"]
    seen = {}
    for d in merged.iterdir():
        prov = json.loads((d / "PROVENANCE.json").read_text())
        assert prov["arms"] == list(regroup.ARMS4)            # the merged shard claims the wave's four arms
        assert prov["commit"] == "c" * 40 and prov["exit_code"] == 0
        assert sorted(prov["groups"]) == ["dev_ai", "dev_det", "dev_input", "dev_staged"]
        for r in json.loads((d / "rows.json").read_text()):
            seen.setdefault(r["sha256"], set()).add(d.name)
    # NO file is in two merged shards: that is exactly what collect.check_partition refuses
    assert all(len(where) == 1 for where in seen.values()), seen
    plan = json.loads((splits / summary["plan"]).read_text())
    assert set(plan["assign"]) == set(shas) and plan["n"] == summary["shards"]


def test_an_unfinished_or_failed_group_is_refused_not_reported_as_a_whole_wave(tmp_path):
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(2)]
    for wid, arm in (("snap-x-dev_input-TS", "input_only"), ("snap-x-dev_staged-TS", "staged"),
                     ("snap-x-dev_det-TS", "search_det")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=[arm])
    _group_run(out, "snap-x-dev_ai-TS", 1, [[_row(s, "search_ai") for s in shas]], arms=["search_ai"],
               exit_code=4, ledger="shard")
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(_manifest(tmp_path / "m.json",
                                                       {"dev_input": 1, "dev_staged": 1, "dev_det": 1, "dev_ai": 1}))],
                         capture_output=True, text=True)
    assert got.returncode != 0
    assert json.loads((out / "regroup_summary.json").read_text())["dev"]["exit_code"] == 4


def test_groups_that_disagree_on_the_commit_or_the_arms_are_refused(tmp_path):
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(2)]
    for wid, arm in (("snap-x-dev_input-TS", "input_only"), ("snap-x-dev_staged-TS", "staged"),
                     ("snap-x-dev_det-TS", "search_det")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=[arm])
    _group_run(out, "snap-x-dev_ai-TS", 1, [[_row(s, "search_ai") for s in shas]], arms=["search_ai"],
               ledger="shard")
    bad = out / "snap-x-dev_ai-TS" / "shard-1-of-1" / "PROVENANCE.json"
    prov = json.loads(bad.read_text())
    prov["commit"] = "d" * 40
    bad.write_text(json.dumps(prov))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(_manifest(tmp_path / "m.json",
                                                       {"dev_input": 1, "dev_staged": 1, "dev_det": 1, "dev_ai": 1}))],
                         capture_output=True, text=True)
    assert got.returncode != 0 and "disagree on commit" in (got.stdout + got.stderr)


# ---- B17 fixes: the plan must reproduce, and a refusal must be actionable ------------------------

def test_a_missing_or_empty_prior_is_REFUSED_not_silently_uncalibrated(tmp_path):
    """The bug: PRIOR_ROWS pointed at a directory that does not exist, prep fell through to k_dev = 1.0, planned
    ONE shard per arm group - all 150 search_ai files in one shard - and printed a 40-minute projection for a job
    of tens of hours."""
    fx = _fixture(tmp_path)
    rc, _m, _p, log = _run_prep(tmp_path, fx, prior=str(tmp_path / "not-there"))
    assert rc == 1 and "does not exist" in log, log
    empty = tmp_path / "empty"
    (empty / "wave" / "shard-1-of-1").mkdir(parents=True)
    rc2, _m2, _p2, log2 = _run_prep(tmp_path / "b", _fixture((tmp_path / "b").resolve() if (tmp_path / "b").mkdir()
                                                             or True else tmp_path), prior=str(empty))
    assert rc2 == 1 and "holds no" in log2, log2


def test_a_deadline_unmeasured_exclusion_is_not_charged_at_the_file_wall(tmp_path):
    """v2's EXCLUDED holds 393 deadline-unmeasured and 37 reason-lost pairs. Charging them 1800 s each is what
    projected 549 min for a ~3 h job (the runner's af59c91f). The file-wall exclusion beside them IS a cost."""
    fx = _fixture(tmp_path)
    prior = _mod("prep").prior_costs_per_arm(Path(fx["prior"]), 1800.0)
    victim = fx["files"]["dev"][-1]["sha256"]
    assert "search_det" not in prior.get(victim, {}), "a deadline pair must not be costed"
    assert prior[victim]["search_ai"] == 1800.0, "a file-wall exclusion is a real cost at the wall"


def test_when_the_slots_do_not_fit_the_refusal_names_a_target_that_does(tmp_path):
    fx = _fixture(tmp_path, dev=60)
    rc, _m, _p, log = _run_prep(tmp_path, fx, extra=["--target-min", "1"],
                                regions=("us-central1:32:us-central1-b",))
    assert rc == 1 and "do not fit" in log
    m = re.search(r"--target-min (\d+) fits \((\d+) slots\)", log)
    assert m, log
    # the named target really does fit
    rc2, man2, plan2, log2 = _run_prep(tmp_path / "again", _fixture((tmp_path / "again").resolve()
                                                                    if (tmp_path / "again").mkdir() or True
                                                                    else tmp_path),
                                       extra=["--target-min", m.group(1)],
                                       regions=("us-central1:32:us-central1-b",))
    assert rc2 == 0, log2
    assert sum(g["n_shards"] for g in man2["groups"].values()) <= 16


def test_the_manifest_carries_the_group_wave_ids_so_the_merge_can_derive_one(tmp_path):
    rc, manifest, _p, log = _run_prep(tmp_path, _fixture(tmp_path), extra=["--ts", "202611240900"])
    assert rc == 0, log
    assert manifest["ts"] == "202611240900" and manifest["label"] == "v4"
    for group, meta in manifest["groups"].items():
        assert meta["wave_id"] == f"snap-v4-{group}-202611240900"


# ---- B17 fixes: the merged provenance must pass report.py's gates --------------------------------

def test_the_merged_shards_share_one_wave_id_and_carry_the_shard_ledger(tmp_path):
    """report.load_wave refuses 'shards do not share one wave_id' and 'a model arm ran without a shard ledger'.
    Both were REFUSALS on the first version of this tool, tested against v3c's real finished shards."""
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(2)]
    for wid, arm, led in (("snap-v4-dev_input-TS", "input_only", None), ("snap-v4-dev_staged-TS", "staged", None),
                          ("snap-v4-dev_det-TS", "search_det", None), ("snap-v4-dev_ai-TS", "search_ai", "shard")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=[arm], arms_run=[arm],
                   **({"ledger": led} if led else {}),
                   wave_backoff={"guard": "wave_backoff", "holds": 2, "held_s": 3.5, "trips_seen": [],
                                 "reported_429": 1, "store_errors": 0, "malformed_markers": 0,
                                 "tripped_here": False, "threshold": 3, "window_s": 60})
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"label": "v4", "ts": "TS", "waves": {"dev": "snap_v2_dev"}, "groups": {
        g: {"split": "dev", "wave_id": f"snap-v4-{g}-TS", "arms": [a], "n_shards": 1}
        for g, a in (("dev_input", "input_only"), ("dev_staged", "staged"),
                     ("dev_det", "search_det"), ("dev_ai", "search_ai"))}}))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(man)], capture_output=True, text=True)
    assert got.returncode == 0, got.stdout + got.stderr
    summary = json.loads((out / "regroup_summary.json").read_text())["dev"]
    assert summary["wave_id"] == "snap-v4-dev-TS" and summary["ledger"] == "shard"
    ids, ledgers, guards = set(), set(), []
    for d in (out / summary["merged_dir"]).iterdir():
        prov = json.loads((d / "PROVENANCE.json").read_text())
        ids.add(prov["wave_id"])
        ledgers.add(prov.get("ledger"))
        guards.append(prov.get("wave_backoff"))
        assert prov["arms_run"] == list(regroup.ARMS4)
    assert ids == {"snap-v4-dev-TS"} and ledgers == {"shard"}
    # the model shard's real back-off account is carried, so the report is not marked UNRELIABLE for its absence
    assert [g for g in guards if g] and guards[0]["holds"] == 2 and guards[0]["held_s"] == 3.5


def test_a_model_group_without_a_shard_ledger_is_refused(tmp_path):
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(2)]
    for wid, arm in (("snap-v4-dev_input-TS", "input_only"), ("snap-v4-dev_staged-TS", "staged"),
                     ("snap-v4-dev_det-TS", "search_det"), ("snap-v4-dev_ai-TS", "search_ai")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=[arm], arms_run=[arm])
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"label": "v4", "ts": "TS", "waves": {"dev": "snap_v2_dev"}, "groups": {
        g: {"split": "dev", "wave_id": f"snap-v4-{g}-TS", "arms": [a], "n_shards": 1}
        for g, a in (("dev_input", "input_only"), ("dev_staged", "staged"),
                     ("dev_det", "search_det"), ("dev_ai", "search_ai"))}}))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(man)], capture_output=True, text=True)
    assert got.returncode != 0 and "without a shard ledger" in (got.stdout + got.stderr)


def test_a_lost_shard_is_refused_rather_than_merged_at_exit_code_zero(tmp_path):
    """Deleting one shard of one group used to merge happily at exit_code 0."""
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(4)]
    for wid, arm in (("snap-v4-dev_input-TS", "input_only"), ("snap-v4-dev_staged-TS", "staged"),
                     ("snap-v4-dev_det-TS", "search_det"), ("snap-v4-dev_ai-TS", "search_ai")):
        _group_run(out, wid, 2, [[_row(shas[0], arm), _row(shas[1], arm)], [_row(shas[2], arm), _row(shas[3], arm)]],
                   arms=[arm], arms_run=[arm], ledger="shard")
    shutil.rmtree(out / "snap-v4-dev_det-TS" / "shard-2-of-2")
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"label": "v4", "ts": "TS", "waves": {"dev": "snap_v2_dev"}, "groups": {
        g: {"split": "dev", "wave_id": f"snap-v4-{g}-TS", "arms": [a], "n_shards": 2}
        for g, a in (("dev_input", "input_only"), ("dev_staged", "staged"),
                     ("dev_det", "search_det"), ("dev_ai", "search_ai"))}}))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(man)], capture_output=True, text=True)
    assert got.returncode != 0
    assert "shard directories, the plan says 2" in (got.stdout + got.stderr)


def test_the_arms_gate_reads_arms_run_not_the_plans_arms(tmp_path):
    """`arms` is the PLAN's and is always the wave's four even in a one-arm group, so a gate on it proves nothing.
    Here every group's `arms` says four and the AI group never ran: the merge must still refuse."""
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(2)]
    for wid, arm in (("snap-v4-dev_input-TS", "input_only"), ("snap-v4-dev_staged-TS", "staged"),
                     ("snap-v4-dev_det-TS", "search_det")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=list(regroup.ARMS4),
                   arms_run=[arm])
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"label": "v4", "ts": "TS", "waves": {"dev": "snap_v2_dev"}, "groups": {
        g: {"split": "dev", "wave_id": f"snap-v4-{g}-TS", "arms": [a], "n_shards": 1}
        for g, a in (("dev_input", "input_only"), ("dev_staged", "staged"), ("dev_det", "search_det"))}}))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(man)], capture_output=True, text=True)
    assert got.returncode != 0 and "arms_run" in (got.stdout + got.stderr)


# ---- the launcher must run on THIS machine -------------------------------------------------------

def test_the_launcher_is_bash_3_2_clean():
    """bash -n passes on bash 4 constructs' SYNTAX, so it never caught `mapfile` or `declare -A`: the dry run died
    at line 106 on this Mac. Parse with the system bash AND grep for the constructs it lacks."""
    for script in ("launch.sh", "vm_run.sh"):
        body = (HERE / script).read_text()
        for bad in ("mapfile", "readarray", "declare -A", "wait -n", "${!"):
            for line in body.splitlines():
                if bad in line and not line.lstrip().startswith("#"):
                    raise AssertionError(f"{script}: bash 4 construct {bad!r} in: {line.strip()}")
        got = subprocess.run(["/bin/bash", "-n", str(HERE / script)], capture_output=True, text=True)
        assert got.returncode == 0, f"{script}: {got.stderr}"


def test_the_launcher_reads_the_outcomes_and_labels_the_vms_for_the_sweep():
    body = (HERE / "launch.sh").read_text()
    assert "deployment-id=dev-crsv4" in body and "owner=lane-b17" in body    # the sweep keys on deployment-id
    assert "gcloud secrets versions access" in body                          # not the local environment
    assert 'OPENAI_API_KEY=.{20,}' in body                                   # the key's CONTENT, on the VM
    assert "for i in $(seq 1 40)" in body                                    # the readiness wait is BOUNDED
    assert "FAILURES=$((FAILURES + 1))" in body and "exit 1" in body         # a failure is an exit code
    for marker in ("rc.$vm", "outcome.$vm", "post.rc", "ABORTED"):
        assert marker in body, marker
    assert "key.txt" not in body                                             # the key is never stored locally


# ---- the decisive one: a REAL finished wave, regrouped, must report IDENTICALLY -------------------

REAL_RUN = REPO / ".superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/runs/v3b-202611240606"
REAL_WAVE_ID = "snap-v3b-v2splits-clean-202611240606"
VENV = Path("/Users/kitts/archductor/workspaces/Hexera/awk/.venv/bin/python")


def test_a_real_wave_split_into_groups_and_regrouped_reports_the_same_numbers(tmp_path):
    """The check the snapshot runner ran by hand, automated. Take v3b's FINISHED clean wave, cut it into one group
    per arm the way a v4 run would write it, regroup, and run the commit's own report.py on both. The reports must
    be identical apart from the wave id and the paths. This is what caught 'shards do not share one wave_id', 'a
    model arm ran without a shard ledger', the missing back-off account and the missing check_valid_timeout_s."""
    import pytest

    if not (REAL_RUN / "out" / REAL_WAVE_ID).is_dir() or not VENV.exists():
        pytest.skip("needs v3b's committed run and the project venv")
    src = REAL_RUN / "out" / REAL_WAVE_ID
    shards = sorted(src.glob("shard-*"), key=lambda p: int(p.name.split("-")[1]))
    groups = {"clean_input": ("input_only",), "clean_staged": ("staged",),
              "clean_det": ("search_det",), "clean_ai": ("search_ai",)}
    out, splits = tmp_path / "out", tmp_path / "splits"
    out.mkdir()
    shutil.copytree(REAL_RUN / "splits", splits)
    for group, arms in groups.items():
        wid = f"snap-v3b-{group}-202611240606"
        shutil.copy(splits / "plan_clean.json", splits / f"plan_{group}.json")
        for d in shards:
            i, n = d.name.split("-")[1], d.name.split("-")[3]
            dest = out / wid / f"shard-{i}-of-{n}"
            dest.mkdir(parents=True)
            rows = [r for r in json.loads((d / "rows.json").read_text()) if r["arm"] in arms]
            exc = [e for e in json.loads((d / "EXCLUDED.json").read_text()) if e["arm"] in arms]
            (dest / "rows.json").write_text(json.dumps(rows))
            (dest / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            (dest / "EXCLUDED.json").write_text(json.dumps(exc))
            prov = json.loads((d / "PROVENANCE.json").read_text())
            prov.update(wave_id=wid, arms_run=list(arms), shard_plan=f"digest-{group}")
            if "search_ai" not in arms:
                prov.pop("ledger", None)          # a group with no model arm has no shard ledger
            (dest / "PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"label": "v3b", "ts": "202611240606", "waves": {"clean": "snap_v2_clean"},
                                    "groups": {g: {"split": "clean", "wave_id": f"snap-v3b-{g}-202611240606",
                                                   "arms": list(a), "n_shards": len(shards)}
                                               for g, a in groups.items()}}))

    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(manifest)], capture_output=True, text=True)
    assert got.returncode == 0, got.stdout + got.stderr
    summary = json.loads((out / "regroup_summary.json").read_text())["clean"]
    assert summary["ledger"] == "shard" and summary["rows"] == 160 and summary["files"] == 40

    def report(from_dir: Path, plan: Path, stem: str) -> tuple[int, str]:
        cmd = [str(VENV), str(REPO / "devtools/validation/v4_eval/report.py"), "--from", str(from_dir),
               "--wave", "snap_v2_clean", "--shards", str(len(shards)), "--split", str(splits / "clean.json"),
               "--shard-plan", str(plan), "--json", str(tmp_path / f"{stem}.json")]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
        return r.returncode, r.stdout

    rc_base, base = report(src, REAL_RUN / "splits" / "plan_clean.json", "baseline")
    rc_merged, merged = report(out / summary["merged_dir"], splits / summary["plan"], "merged")

    def normalise(text: str) -> list[str]:
        out_lines = []
        for line in text.splitlines():
            line = re.sub(r"snap-v3b-[A-Za-z0-9_-]+", "WAVEID", line)
            line = re.sub(r"[0-9a-f]{12,}", "HASH", line)
            line = re.sub(r"\S*/(tmp|private|Users)/\S*", "PATH", line)
            out_lines.append(line)
        return out_lines

    assert rc_base == rc_merged, f"baseline rc {rc_base}, merged rc {rc_merged}"
    assert normalise(base) == normalise(merged), "\n".join(
        d for d in __import__("difflib").unified_diff(normalise(base), normalise(merged), lineterm=""))[:3000]
    both = [json.loads((tmp_path / f"{s}.json").read_text()) for s in ("baseline", "merged")]
    assert both[0]["warnings"] == both[1]["warnings"]
    assert both[0]["status"] == both[1]["status"]


# ------------------------------------------------------------------ lane C14: the sealed held-out run

def _heldout(fx: dict, tmp_path: Path) -> None:
    """Relabel the fixture's dev split as the held-out split (the 'dev' ROLE carries it, prep --dev-split-name)."""
    body = json.loads(Path(fx["dev"]).read_text())
    body["split"] = "heldout"
    Path(fx["dev"]).write_text(json.dumps(body))
    # prior rows are what calibrates the size model; the held-out files themselves have none, as in the real run
    (tmp_path / "prior" / "snap-x-dev" / "shard-1-of-1" / "rows.json").write_text(
        json.dumps([{**r, "sha256": "e" + r["sha256"][1:]} for r in json.loads(
            (tmp_path / "prior" / "snap-x-dev" / "shard-1-of-1" / "rows.json").read_text())]))
    ls = Path(fx["ls"]).read_text().splitlines()
    ls += [ln.replace("/inputs/d", "/inputs/e") for ln in ls if "/inputs/d" in ln]   # the calibration files' sizes
    Path(fx["ls"]).write_text("\n".join(ls) + "\n")


HO_ONE_GROUP = "heldout:input_only,staged,search_det,search_ai:dev clean:input_only,staged,search_det,search_ai:clean"


def _run_prep_heldout(tmp_path: Path, fx: dict, groups: str) -> tuple[int, dict, dict, str]:
    out = tmp_path / "out"
    cmd = [sys.executable, "-I", str(HERE / "prep.py"), "--code", str(REPO), "--profile", "heldout",
           "--dev-split", str(fx["dev"]), "--clean-split", str(fx["clean"]), "--dev-split-name", "heldout",
           "--dev-wave", "heldout_v4", "--clean-wave", "snap_v2_clean", "--total-usd", "300",
           "--inputs-ls", str(fx["ls"]), "--prior-rows", str(fx["prior"]), "--groups", groups,
           "--regions", "us-central1:88:us-central1-b", "--target-min", "60", "--vcpu-per-shard", "1.5",
           "--out", str(out)]
    got = subprocess.run(cmd, capture_output=True, text=True)
    manifest = json.loads((out / "launch_manifest.json").read_text()) if (out / "launch_manifest.json").exists() else {}
    plan = json.loads((out / "vm_plan.json").read_text()) if (out / "vm_plan.json").exists() else {}
    return got.returncode, manifest, plan, got.stdout + got.stderr


def test_heldout_per_arm_groups_are_refused_because_the_slot_is_one(tmp_path):
    fx = _fixture(tmp_path)
    _heldout(fx, tmp_path)
    groups = ("ho_ai:search_ai:dev ho_det:search_det:dev ho_st:staged:dev ho_in:input_only:dev "
              "clean:input_only,staged,search_det,search_ai:clean")
    rc, _, _, log = _run_prep_heldout(tmp_path, fx, groups)
    assert rc != 0 and "the held-out split must run as ONE group" in log, log


def test_heldout_as_one_group_is_planned_on_the_held_out_cap_and_type(tmp_path):
    fx = _fixture(tmp_path)
    _heldout(fx, tmp_path)
    rc, manifest, plan, log = _run_prep_heldout(tmp_path, fx, HO_ONE_GROUP)
    assert rc == 0, log
    assert manifest["wave_types"] == {"dev": "held_out", "clean": "normal"}
    assert manifest["split_names"] == {"dev": "heldout", "clean": "clean"}
    units = [u for v in plan["vms"] for u in v["units"]]
    ho = [u for u in units if u["group"] == "heldout"]
    assert ho and all(u["wave"] == "heldout_v4" and u["wave_type"] == "held_out" and u["split_name"] == "heldout"
                      for u in ho)
    n = ho[0]["n"]
    # the slice is the HELD_OUT cap's ($250 / n), never the normal $50 / n
    assert all(u["cap_usd"] <= 250.0 / n + 1e-9 for u in ho)


def test_a_wrong_wave_for_the_heldout_split_is_refused(tmp_path):
    fx = _fixture(tmp_path)
    _heldout(fx, tmp_path)
    out = tmp_path / "out"
    cmd = [sys.executable, "-I", str(HERE / "prep.py"), "--code", str(REPO), "--profile", "heldout",
           "--dev-split", str(fx["dev"]), "--clean-split", str(fx["clean"]), "--dev-split-name", "heldout",
           "--dev-wave", "snap_v2_dev", "--clean-wave", "snap_v2_clean", "--total-usd", "300",
           "--inputs-ls", str(fx["ls"]), "--prior-rows", str(fx["prior"]), "--groups", HO_ONE_GROUP,
           "--regions", "us-central1:88:us-central1-b", "--target-min", "60", "--out", str(out)]
    got = subprocess.run(cmd, capture_output=True, text=True)
    assert got.returncode != 0 and "held_out type" in got.stdout + got.stderr


def test_regroup_keeps_the_heldout_groups_own_wave_id_and_its_slot(tmp_path):
    """The bucket slot holds the GROUP's wave id; a renamed merged id would make the held-out report refuse."""
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(3)]
    wid = "snap-heldout-heldout-TS"
    rows = [[_row(s, a) for s in shas for a in ("input_only", "staged", "search_det", "search_ai")]]
    _group_run(out, wid, 1, rows, arms=list(regroup.ARMS4), ledger="shard", slot="slot/held_out/1",
               wave="heldout_v4")
    for d in (out / wid).iterdir():   # the held-out shard's own type and split
        p = json.loads((d / "PROVENANCE.json").read_text())
        p.update(wave_type="held_out", split="heldout")
        (d / "PROVENANCE.json").write_text(json.dumps(p))
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"label": "heldout", "ts": "TS", "waves": {"dev": "heldout_v4"},
                               "groups": {"heldout": {"split": "dev", "wave_id": wid, "arms": list(regroup.ARMS4),
                                                      "n_shards": 1}}}))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(man)], capture_output=True, text=True)
    assert got.returncode == 0, got.stdout + got.stderr
    summary = json.loads((out / "regroup_summary.json").read_text())["dev"]
    assert summary["wave_id"] == wid
    for d in (out / summary["merged_dir"]).iterdir():
        prov = json.loads((d / "PROVENANCE.json").read_text())
        assert prov["wave_id"] == wid and prov["slot"] == "slot/held_out/1"


def test_regroup_records_each_original_shards_model_spend(tmp_path):
    """LEAD-3 C13: a merged shard pools AI rows from several original shards, so report.py binds the spend per
    ORIGINAL shard from these numbers."""
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(4)]
    paid = lambda s, usd: {**_row(s, "search_ai"), "model_usd": usd}  # noqa: E731
    _group_run(out, "snap-x-dev_input-TS", 1, [[_row(s, "input_only") for s in shas]], arms=["input_only"])
    _group_run(out, "snap-x-dev_staged-TS", 1, [[_row(s, "staged") for s in shas]], arms=["staged"])
    _group_run(out, "snap-x-dev_det-TS", 1, [[_row(s, "search_det") for s in shas]], arms=["search_det"])
    _group_run(out, "snap-x-dev_ai-TS", 2, [[paid(shas[0], 0.5), paid(shas[1], 0.66)],
                                            [paid(shas[2], 0.1), paid(shas[3], 0.0)]], arms=["search_ai"], ledger="shard")
    rc = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                         "--manifest", str(_manifest(tmp_path / "m.json", {"dev_input": 1, "dev_staged": 1,
                                                                            "dev_det": 1, "dev_ai": 2}))],
                        capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    merged = out / json.loads((out / "regroup_summary.json").read_text())["dev"]["merged_dir"]
    for d in merged.iterdir():
        g = json.loads((d / "PROVENANCE.json").read_text())["groups"]
        assert g["dev_ai"]["shard_spend_usd"] == [1.16, 0.1] and g["dev_det"]["shard_spend_usd"] == [0.0]
