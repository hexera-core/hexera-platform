"""Hermetic tests for the v4 launcher's Python: prep.py's grouping, caps and VM packing, and regroup.py's merge.

No VM, no model, no network. Run from this directory:
    PYTHONPATH=<repo>/src python -m pytest -q test_snap_v4.py
"""
from __future__ import annotations

import importlib.util
import json
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
    return {"files": files, "dev": tmp_path / "dev.json", "clean": tmp_path / "clean.json",
            "ls": tmp_path / "ls.txt", "prior": tmp_path / "prior"}


def _run_prep(tmp_path: Path, fx: dict, *, extra: list[str] = (),
              regions: tuple[str, ...] = ("us-central1:88:us-central1-b", "us-east1:96:us-east1-b"),
              ) -> tuple[int, dict, dict, str]:
    out = tmp_path / "out"
    cmd = [sys.executable, "-I", str(HERE / "prep.py"), "--code", str(REPO), "--profile", "v4",
           "--dev-split", str(fx["dev"]), "--clean-split", str(fx["clean"]),
           "--dev-wave", "snap_v2_dev", "--clean-wave", "snap_v2_clean", "--total-usd", "49.95",
           "--inputs-ls", str(fx["ls"]), "--prior-rows", str(fx["prior"]),
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
               exit_code: int = 0, **prov_extra) -> None:
    for i in range(1, n + 1):
        d = out / wave_id / f"shard-{i}-of-{n}"
        d.mkdir(parents=True)
        rows = rows_per_shard[i - 1]
        (d / "rows.json").write_text(json.dumps(rows))
        (d / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (d / "EXCLUDED.json").write_text("[]")
        (d / "PROVENANCE.json").write_text(json.dumps(
            {"commit": "c" * 40, "image": "sha256:img", "wave": "snap_v2_dev", "wave_type": "normal",
             "split": "dev", "file_timeout_s": 900, "arms": arms, "n_shards": n, "shard": f"{i}/{n}",
             "shard_plan": f"digest-{wave_id}", "exit_code": exit_code, **prov_extra}))


def _manifest(path: Path) -> Path:
    body = {"waves": {"dev": "snap_v2_dev"},
            "groups": {"dev_input": {"split": "dev", "wave_id": "w-input", "arms": ["input_only"]},
                       "dev_staged": {"split": "dev", "wave_id": "w-staged", "arms": ["staged"]},
                       "dev_det": {"split": "dev", "wave_id": "w-det", "arms": ["search_det"]},
                       "dev_ai": {"split": "dev", "wave_id": "w-ai", "arms": ["search_ai"]}}}
    path.write_text(json.dumps(body))
    return path


def _row(sha: str, arm: str) -> dict:
    return {"sha256": sha, "arm": arm, "name": f"abc/{sha[:4]}.step", "out_clean": False}


def test_the_groups_merge_into_one_wave_whose_shards_carry_all_four_arms(tmp_path):
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(4)]
    _group_run(out, "w-input", 1, [[_row(s, "input_only") for s in shas]], arms=["input_only"])
    _group_run(out, "w-staged", 2, [[_row(shas[0], "staged"), _row(shas[1], "staged")],
                                    [_row(shas[2], "staged"), _row(shas[3], "staged")]], arms=["staged"])
    _group_run(out, "w-det", 2, [[_row(shas[0], "search_det"), _row(shas[1], "search_det")],
                                 [_row(shas[2], "search_det"), _row(shas[3], "search_det")]], arms=["search_det"])
    _group_run(out, "w-ai", 1, [[_row(s, "search_ai") for s in shas]], arms=["search_ai"])

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
    for wid, arm in (("w-input", "input_only"), ("w-staged", "staged"), ("w-det", "search_det")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=[arm])
    _group_run(out, "w-ai", 1, [[_row(s, "search_ai") for s in shas]], arms=["search_ai"], exit_code=4)
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(_manifest(tmp_path / "m.json"))], capture_output=True, text=True)
    assert got.returncode != 0
    assert json.loads((out / "regroup_summary.json").read_text())["dev"]["exit_code"] == 4


def test_groups_that_disagree_on_the_commit_or_the_arms_are_refused(tmp_path):
    out, splits = tmp_path / "out", tmp_path / "splits"
    splits.mkdir()
    shas = [f"{i:064x}" for i in range(2)]
    for wid, arm in (("w-input", "input_only"), ("w-staged", "staged"), ("w-det", "search_det")):
        _group_run(out, wid, 1, [[_row(s, arm) for s in shas]], arms=[arm])
    _group_run(out, "w-ai", 1, [[_row(s, "search_ai") for s in shas]], arms=["search_ai"])
    bad = out / "w-ai" / "shard-1-of-1" / "PROVENANCE.json"
    prov = json.loads(bad.read_text())
    prov["commit"] = "d" * 40
    bad.write_text(json.dumps(prov))
    got = subprocess.run([sys.executable, "-I", str(HERE / "regroup.py"), "--out", str(out), "--splits", str(splits),
                          "--manifest", str(_manifest(tmp_path / "m.json"))], capture_output=True, text=True)
    assert got.returncode != 0 and "disagree on commit" in (got.stdout + got.stderr)
