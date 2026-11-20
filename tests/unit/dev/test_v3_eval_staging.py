# Responsibility: Hold the CRS v3 verdict staging (T19/T29) to its refusals: arms that do not share
#                 one sample or one commit are not compared, an n2 machine is not launched, a flag
#                 the build does not read is caught before a fleet is paid for, and the 4x rule's
#                 degenerate case is reported as undefined rather than as met.
# Boundaries: hermetic. No gcloud, no docker, no CAD: the launcher is driven in --dry-run with gcloud
#             off PATH, and the collector is fed shard directories written here.
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
V3 = REPO / "devtools" / "validation" / "v3_eval"
MANIFEST = REPO / "devtools" / "validation" / "corpora" / "mesh-failure-real.json"
CLEAN_SAMPLES = (REPO / "devtools" / "validation" / "evidence" / "2026-11-19-corpus-harvest"
                 / "clean-samples.json")
CLEAN_MANIFEST = REPO / "devtools" / "validation" / "corpora" / "clean-control-100.json"
VERDICT = REPO / "devtools" / "validation" / "evidence" / "2026-11-20-crs-v3-verdict"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


spec = _load("v3_evalspec_under_test", V3 / "evalspec.py")


# ------------------------------------------------------------------ the launcher

def test_launcher_refuses_the_n2_family():
    for bad in ("n2-highcpu-8", "n2-standard-4", "n2d-highcpu-8"):
        with pytest.raises(ValueError, match="quota"):
            spec.validate_machine_type(bad)
    for bad in ("e2-standard-4", "c2d", "nonsense"):
        with pytest.raises(ValueError):
            spec.validate_machine_type(bad)
    assert spec.validate_machine_type("c2d-highcpu-4") == "c2d-highcpu-4"
    assert spec.validate_machine_type("c3-highcpu-8") == "c3-highcpu-8"


def _launch(*argv: str, tmp_path: Path):
    # gcloud is taken OFF the PATH: a dry run that reached for it would fail here, not launch.
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(V3 / "launch_fleet.py"), *argv],
                          capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)


COMMIT = "812ae022" + "0" * 32
IMAGE = "us-central1-docker.pkg.dev/hexera-dev/mesh/app@sha256:" + "a" * 64
#: A real commit, so the dry run's local `git diff` (toolchain drift) has something to read.
HEAD = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                      text=True).stdout.strip()


def test_launcher_cli_refuses_n2_before_planning_anything(tmp_path):
    r = _launch("--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD,
                "--machine-type", "n2-highcpu-8", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "n2" in r.stderr and "gcloud compute instances create" not in r.stdout


def test_launcher_refuses_an_unpinned_image_and_a_short_commit(tmp_path):
    r = _launch("--image", "repo/app:latest", "--commit", HEAD, "--image-commit", HEAD,
                "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "digest" in r.stderr
    r = _launch("--image", IMAGE, "--commit", "812ae022", "--image-commit", HEAD,
                "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "40" in r.stderr


def test_launcher_refuses_more_than_100_vcpu(tmp_path):
    r = _launch("--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD,
                "--shards-total", "120", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "100-vCPU" in r.stderr


def test_launcher_dry_run_is_one_wave_of_labelled_self_deleting_vms(tmp_path):
    r = _launch("--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD, "--dry-run",
                "--skip-preflight", tmp_path=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    creates = [ln for ln in r.stdout.splitlines() if "gcloud compute instances create" in ln]
    assert len(creates) == 12        # 96 shards, 8 per c2d-standard-8: 96 vCPU <= 100
    units = []
    for j, line in enumerate(creates, start=1):
        assert f"{j}of12" in line
        assert "deployment-id=dev-crsv3" in line and "purpose=v3-eval" in line
        assert "--instance-termination-action=DELETE" in line and "--max-run-duration=" in line
        assert "--machine-type=c2d-standard-8" in line and "n2-" not in line
        assert f"v3-code=gs://dev-crsv3-transfer-224734058693/eval/v3-verdict/code-{HEAD[:12]}/" \
            in line
        units += line.split("v3-units=")[1].split("|")[0].split(",")
    plan = spec.shard_plan(96)
    assert sorted(units) == sorted(spec.work_units(plan))     # every shard once, across VMs
    assert {u.split(":")[0] for u in units} == set(spec.ARMS)
    assert {(u.split(":")[0], u.split(":")[1]) for u in units} == set(spec.UNITS)
    # the secret is named, never valued
    assert "openai-api-key" in r.stdout and "OPENAI_API_KEY=" not in r.stdout


def test_the_shard_plan_partitions_every_unit_and_balances_wall():
    plan = spec.shard_plan(96)
    assert sum(plan.values()) == 96 and set(plan) == set(spec.UNITS)
    minutes = spec.plan_minutes(plan)
    assert max(minutes.values()) <= 30 and min(minutes.values()) >= 15
    assert spec.shard_plan(96) == plan                         # a resume recomputes the same N
    vms = spec.pack(spec.work_units(plan), 8)
    assert len(vms) == 12 and all(len(v) == 8 for v in vms)


# ------------------------------------------------------------------ the sample

def test_sample_body_is_byte_for_byte_repair_corpus_writer(tmp_path):
    rc = _load("v3_rc_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    manifest = json.loads(MANIFEST.read_text())
    entries = rc.stratified_sample(manifest, spec.SAMPLE_SIZE, seed=spec.SEED)
    written = rc.write_bundle_sample(tmp_path, entries, seed=spec.SEED)
    assert written.read_bytes() == spec.sample_body(entries, seed=spec.SEED)
    assert len(entries) == 120


def test_the_committed_sample_is_the_sampler_s_sample():
    rc = _load("v3_rc_under_test2", REPO / "devtools" / "validation" / "repair_corpus.py")
    manifest = json.loads(MANIFEST.read_text())
    entries = rc.stratified_sample(manifest, spec.SAMPLE_SIZE, seed=spec.SEED)
    assert (VERDICT / "sample.json").read_bytes() == spec.sample_body(entries, seed=spec.SEED)


def _prov(label, *, kind="failing", commit=COMMIT, image=IMAGE, sample="s" * 64):
    return {"label": label, "kind": kind, "commit": commit, "image": image,
            "sample_sha256": sample}


def test_arms_on_different_samples_are_refused():
    ok = spec.check_comparable([_prov("det/1"), _prov("v3/1")])
    assert ok["samples"]["failing"] == "s" * 64
    with pytest.raises(ValueError, match="failing sample"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", sample="t" * 64)])
    # the clean control is its own sample: differing from the failing one is fine
    spec.check_comparable([_prov("det/1"), _prov("det/c", kind="clean", sample="c" * 64)])


def test_mixed_commits_and_images_are_refused():
    with pytest.raises(ValueError, match="commit"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", commit="f" * 40)])
    with pytest.raises(ValueError, match="image"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", image=IMAGE[:-1] + "b")])
    with pytest.raises(ValueError, match="states no"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", commit="")])


# ------------------------------------------------------------------ the collector

def _shard(root: Path, arm: str, kind: str, i: int, n: int, *, commit=COMMIT, rows=None):
    d = root / arm / kind / f"shard-{i}-of-{n}"
    d.mkdir(parents=True)
    prov = {**_prov(f"{arm}/{kind}/{i}", kind=kind, commit=commit,
                    sample=("s" if kind == "failing" else "c") * 64),
            "arm": arm, "shard": f"{i}/{n}", "exit_code": 0, "machine_type": "c2d-highcpu-4",
            "started_at": "2026-11-20T00:00:00Z", "ended_at": "2026-11-20T00:30:00Z",
            "wall_seconds": 1800.0, "operations_catalogue": []}
    (d / "PROVENANCE.json").write_text(json.dumps(prov))
    (d / "rows.json").write_text(json.dumps(rows if rows is not None else [
        {"file": f"{arm}-{kind}-{i}.step", "outcome": "nothing_to_repair",
         "meshes_before": False, "meshes_after": None, "mesh_kind_before": "other",
         "mesh_kind_after": ""}]))
    (d / "DONE").write_text("0\n")
    return d


def test_collector_refuses_mixed_commits(tmp_path):
    collect = _load("v3_collect_under_test", V3 / "collect.py")
    _shard(tmp_path, "t29_deterministic", "failing", 1, 1)
    _shard(tmp_path, "v3_full", "failing", 1, 1, commit="f" * 40)
    with pytest.raises(ValueError, match="commit"):
        collect.load_runs(tmp_path)


def test_collector_refuses_a_missing_shard(tmp_path):
    collect = _load("v3_collect_under_test2", V3 / "collect.py")
    _shard(tmp_path, "t29_deterministic", "failing", 1, 2)
    with pytest.raises(ValueError, match="2/2"):
        collect.load_runs(tmp_path)


def test_collector_merges_shards_through_repair_corpus_aggregate(tmp_path):
    collect = _load("v3_collect_under_test3", V3 / "collect.py")
    _shard(tmp_path, "t29_deterministic", "failing", 1, 2)
    _shard(tmp_path, "t29_deterministic", "failing", 2, 2)
    runs = collect.load_runs(tmp_path)
    merged = runs["checked"]
    assert merged["commit"] == COMMIT
    rows = runs["arms"][("t29_deterministic", "failing")]["rows"]
    assert len(rows) == 2
    summary = collect.summarise(rows)
    assert summary["meshing"]["files"] == 2 and summary["meshing"]["newly_broken"] == 0


# ------------------------------------------------------------------ the posture and the verdict

def test_preflight_catches_a_flag_the_build_does_not_read():
    env = spec.ARMS["v3_full"]["env"]
    settings = {k: v for k, v in env.items() if k.startswith("CAD_REPAIR_")}
    settings["CAD_REPAIR_ENABLED"] = True
    assert spec.preflight(settings, env) == []
    missing = {k: v for k, v in settings.items()
               if k not in ("CAD_REPAIR_TOURNAMENT", "CAD_REPAIR_TOURNAMENT_K")}
    problems = spec.preflight(missing, env)
    assert any("INERT CAD_REPAIR_TOURNAMENT=live" in p for p in problems)
    # asking for exactly what the code falls back to is harmless when the row is absent
    det = spec.ARMS["t29_deterministic"]["env"]
    det_settings = {k: v for k, v in det.items()
                    if k.startswith("CAD_REPAIR_") and k not in ("CAD_REPAIR_TOURNAMENT",
                                                                 "CAD_REPAIR_MEMORY")}
    assert [p for p in spec.preflight(det_settings, det) if "TOURNAMENT" in p] == []
    # a fail-closed posture that read "off" is a mismatch, not a pass
    off = {**settings, "CAD_REPAIR_INVESTIGATOR": "off"}
    assert any("MISMATCH CAD_REPAIR_INVESTIGATOR" in p for p in spec.preflight(off, env))


def test_arms_differ_only_by_investigator_and_tournament():
    base = {k: v for k, v in spec.ARMS["t29_deterministic"]["env"].items()
            if not k.startswith("CAD_REPAIR_TOURNAMENT") and k != "CAD_REPAIR_INVESTIGATOR"}
    for name, a in spec.ARMS.items():
        rest = {k: v for k, v in a["env"].items()
                if not k.startswith("CAD_REPAIR_TOURNAMENT") and k != "CAD_REPAIR_INVESTIGATOR"}
        assert rest == base, name
    assert spec.ARMS["v3_full"]["env"]["CAD_REPAIR_TOURNAMENT_K"] == "3"


def test_t29_degenerate_ratio_is_undefined_not_met():
    zero = {"newly_meshed": 0, "newly_broken": 0}
    out = spec.t29_verdict(zero, {"newly_meshed": 5, "newly_broken": 0},
                           clean_untouched=True, cost_per_fixed_part=0.4)
    assert out["ratio"] is None and "undefined" in out["ratio_rule"] and "= 5" in out["ratio_rule"]
    assert out["accepted"] is True
    out = spec.t29_verdict({"newly_meshed": 2, "newly_broken": 0},
                           {"newly_meshed": 7, "newly_broken": 0},
                           clean_untouched=True, cost_per_fixed_part=0.4)
    assert out["ratio_ok"] is False and out["accepted"] is False
    out = spec.t29_verdict({"newly_meshed": 2, "newly_broken": 0},
                           {"newly_meshed": 8, "newly_broken": 1},
                           clean_untouched=True, cost_per_fixed_part=0.4)
    assert out["ratio_ok"] is True and out["accepted"] is False


# ------------------------------------------------------------------ the clean control

def test_clean_control_manifest_is_100_seeded_passing_parts_with_sources():
    clean = _load("v3_clean_under_test", V3 / "clean_control.py")
    pool = json.loads(CLEAN_SAMPLES.read_text())
    committed = json.loads(CLEAN_MANIFEST.read_text())
    rebuilt = clean.build_manifest(pool, json.loads(MANIFEST.read_text()), n=100, seed=spec.SEED,
                                   metadata={})
    # the draw is the seed's alone: the committed list is exactly what the builder draws today
    # (the probe metadata beside each entry came from the harvest's local results and is not
    # re-derivable here, so only the identity fields are compared)
    ident = ("name", "sha256", "chunk", "archive_url", "archive_sha256")
    assert [{k: f[k] for k in ident} for f in committed["files"]] == \
        [{k: f[k] for k in ident} for f in rebuilt["files"]]
    assert committed["seed"] == spec.SEED
    files = committed["files"]
    assert len(files) == 100 and len({f["sha256"] for f in files}) == 100
    known = {e["sha256"]: (chunk, e["name"]) for chunk, es in pool.items() for e in es}
    for f in files:
        assert known[f["sha256"]] == (f["chunk"], f["name"])
        assert f["archive_url"].startswith("https://archive.nyu.edu/retrieve/")
        assert len(f["archive_sha256"]) == 64
    assert not any(k in f for f in files for k in ("step", "brep", "bytes_b64"))


# ------------------------------------------------------------------ results.json (evidence lane)

#: The required keys of the evidence lane's results.schema.json (schema_version 1). Restated here
#: because that file lives on another lane; a schema change there must be mirrored here.
SCHEMA_ARMS = ("t6_rules_only_local", "t29_deterministic", "t29_v3", "t19_tournament_off",
               "t19_tournament_on", "clean_control")
SCHEMA_ARM_KEYS = {"arm", "label", "commit", "image_digest", "sample_sha256", "n_files",
                   "newly_meshed", "newly_broken", "meshes_before", "meshes_after", "endings",
                   "ops_kept", "ops_rolled_back", "model_calls", "cost_usd", "wall_s",
                   "cost_per_fixed_part"}
SCHEMA_ARM_OPTIONAL = {"same_run_as", "notes"}
SCHEMA_CLEAN_KEYS = {"arm", "label", "commit", "image_digest", "sample_sha256", "n_files",
                     "byte_unchanged", "newly_broken"}
SCHEMA_CLEAN_OPTIONAL = {"arms_covered", "basis", "notes"}


def test_results_json_has_the_evidence_lane_shape(tmp_path):
    collect = _load("v3_collect_under_test4", V3 / "collect.py")
    for arm in spec.ARMS:
        _shard(tmp_path, arm, "failing", 1, 1)
    _shard(tmp_path, "v3_full", "clean", 1, 1)
    runs = collect.load_runs(tmp_path)
    reports = {}
    for key, entry in runs["arms"].items():
        rep = collect.arm_report(entry)
        if key[1] == "clean":
            rep["clean"] = collect.clean_untouched(entry["rows"], rep["summary"])
        reports[key] = rep
    out = collect.results_json(reports, runs["checked"], collect.build_verdicts(reports),
                               source="test")
    assert out["schema_version"] == 1 and set(out) <= {"schema_version", "generated_at",
                                                        "source", "arms", "verdict"}
    assert set(out["arms"]) == set(SCHEMA_ARMS)
    for name in SCHEMA_ARMS[:-1]:
        arm = out["arms"][name]
        assert arm["arm"] == name
        assert SCHEMA_ARM_KEYS <= set(arm) <= SCHEMA_ARM_KEYS | SCHEMA_ARM_OPTIONAL, name
    assert out["arms"]["t6_rules_only_local"]["label"] == "rules-only baseline"
    assert out["arms"]["t19_tournament_on"]["same_run_as"] == "t29_v3"
    assert out["arms"]["t19_tournament_on"]["newly_meshed"] == \
        out["arms"]["t29_v3"]["newly_meshed"]
    clean = out["arms"]["clean_control"]
    assert SCHEMA_CLEAN_KEYS <= set(clean) <= SCHEMA_CLEAN_KEYS | SCHEMA_CLEAN_OPTIONAL
    assert clean["arms_covered"] == ["v3_full"] and clean["byte_unchanged"] == 1
    # nothing measured reads null, never 0
    assert out["arms"]["t6_rules_only_local"]["newly_meshed"] is None
    json.dumps(out, allow_nan=False)


# ------------------------------------------------------------------ review fixes (PR #494)

def _all_units_but(tmp_path: Path, *skip: tuple[str, str]) -> None:
    for arm_name, kind in spec.UNITS:
        if (arm_name, kind) not in skip:
            _shard(tmp_path, arm_name, kind, 1, 1)


def test_collector_refuses_when_a_whole_planned_unit_is_missing(tmp_path, capsys):
    collect = _load("v3_collect_under_test_missing_unit", V3 / "collect.py")
    _all_units_but(tmp_path, ("t29_deterministic", "clean"))
    assert collect.missing_units(collect.load_runs(tmp_path)["arms"]) == \
        ["t29_deterministic/clean"]
    assert collect.main(["--commit", COMMIT, "--from", str(tmp_path), "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "t29_deterministic/clean" in err


def test_t29_is_not_accepted_without_the_deterministic_clean_control(tmp_path):
    collect = _load("v3_collect_under_test_t29_clean", V3 / "collect.py")
    _all_units_but(tmp_path, ("t29_deterministic", "clean"))
    runs = collect.load_runs(tmp_path)
    reports = {}
    for key, entry in runs["arms"].items():
        rep = collect.arm_report(entry)
        if key[1] == "clean":
            rep["clean"] = collect.clean_untouched(entry["rows"], rep["summary"])
        reports[key] = rep
    # force the v3 arm to "win" so only the missing control can block acceptance
    reports[("v3_full", "failing")]["summary"]["meshing"]["newly_meshed"] = 5
    reports[("v3_full", "failing")]["cost_per_fixed_part_usd"] = 0.1
    assert collect.build_verdicts(reports)["T29"]["accepted"] is False


def test_vm_cost_charges_each_shard_its_vcpu_share_not_the_whole_vm():
    collect = _load("v3_collect_under_test_vm_cost", V3 / "collect.py")
    # 8 one-hour shards packed onto ONE c2d-standard-8 (one shard per vCPU) cost one VM-hour
    provs = [{"machine_type": "c2d-standard-8", "wall_seconds": 3600.0} for _ in range(8)]
    hourly = 8 * 0.029563 + 8 * 4 * 0.003959
    assert collect.vm_cost(provs)["container_cost_usd"] == pytest.approx(hourly, abs=1e-4)


def test_a_running_vm_of_another_commit_does_not_block_this_launch(monkeypatch):
    launch = _load("v3_launch_under_test_running", V3 / "launch_fleet.py")
    unit = "v3_full:failing:1/30"

    def vm(name, commit):
        return {"name": name, "metadata": {"items": [
            {"key": "v3-commit", "value": commit}, {"key": "v3-units", "value": unit}]}}

    monkeypatch.setattr(launch, "_gcloud_json", lambda cmd: [vm("old", "a" * 40)])
    claimed, live = launch.running_units(COMMIT)
    assert unit not in claimed and live == ["old"]
    monkeypatch.setattr(launch, "_gcloud_json", lambda cmd: [vm("same", COMMIT)])
    assert unit in launch.running_units(COMMIT)[0]


#: A gcloud stand-in: logs every call, swallows stdin, and fails any call naming $FAIL_ON.
FAKE_GCLOUD = """#!/bin/bash
[ "$3" = "-" ] && cat >/dev/null
echo "$*" >> "$GCLOUD_LOG"
if [ -n "${FAIL_ON:-}" ] && [[ "$*" == *"$FAIL_ON"* ]]; then exit 1; fi
exit 0
"""


def _publish(tmp_path: Path, rc: int, fail_on: str = "") -> tuple[int, list[str]]:
    """Run vm_startup.sh's publish_unit for one finished shard against a stub gcloud."""
    bin_dir, shard = tmp_path / "bin", tmp_path / "shard"
    bin_dir.mkdir()
    shard.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    for name in ("rows.json", "PROVENANCE.json"):
        (shard / name).write_text("{}")
    log = tmp_path / "gcloud.log"
    log.touch()
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GCLOUD_LOG": str(log), "FAIL_ON": fail_on,
           "V3EVAL_SOURCE_ONLY": "1", "V3EVAL_RETRY_SLEEP": "0"}
    out = subprocess.run(["bash", "-c", f'source "{V3 / "vm_startup.sh"}" && '
                          f'publish_unit "{shard}" gs://b/eval/x/failing/shard-1-of-2 {rc}'],
                         env=env, capture_output=True, text=True, timeout=60)
    return out.returncode, log.read_text().splitlines()


def _wrote(calls: list[str], marker: str) -> bool:
    return any(c.startswith("storage cp ") and c.endswith(f"/{marker}") for c in calls)


def test_vm_startup_publishes_done_only_after_rows_and_provenance_upload(tmp_path):
    status, calls = _publish(tmp_path, 0)
    assert status == 0 and _wrote(calls, "DONE") and not _wrote(calls, "FAILED")
    uploads = [c for c in calls if "rows.json" in c or "PROVENANCE.json" in c]
    assert uploads and calls.index(uploads[-1]) < next(
        i for i, c in enumerate(calls) if c.endswith("/DONE"))


@pytest.mark.parametrize("fail_on", ["rows.json", "PROVENANCE.json"])
def test_vm_startup_a_failed_upload_leaves_the_shard_retry_eligible(tmp_path, fail_on):
    status, calls = _publish(tmp_path, 0, fail_on=fail_on)
    assert status == 1 and not _wrote(calls, "DONE"), calls
    assert _wrote(calls, "FAILED")
    assert sum(fail_on in c for c in calls) >= 3   # retried, not given up on the first error


# ------------------------------------------------------------------ the compute SA can write results

def _policy(*bindings):
    return {"bindings": [{"role": r, "members": list(m)} for r, m in bindings]}


def test_a_compute_sa_that_can_only_read_the_bucket_is_refused():
    # The lost launch: the VMs could read the staged inputs but every shard's upload failed.
    launch = _load("v3_launch_under_test_sa_read", V3 / "launch_fleet.py")
    sa = f"serviceAccount:{launch.SERVICE_ACCOUNT}"
    bucket = _policy(("roles/storage.objectViewer", [sa]))
    problem = launch.sa_write_problem(bucket, _policy())
    assert problem and launch.SERVICE_ACCOUNT in problem and "objectAdmin" in problem


@pytest.mark.parametrize("where,role", [
    ("bucket", "roles/storage.objectAdmin"), ("bucket", "roles/storage.objectUser"), ("bucket", "roles/storage.legacyBucketWriter"),
    ("project", "roles/editor"), ("project", "roles/storage.admin"),
])
def test_a_compute_sa_with_a_write_role_on_bucket_or_project_passes(where, role):
    launch = _load(f"v3_launch_under_test_sa_{where}", V3 / "launch_fleet.py")
    granted = _policy((role, [f"serviceAccount:{launch.SERVICE_ACCOUNT}"]))
    bucket, project = (granted, _policy()) if where == "bucket" else (_policy(), granted)
    assert launch.sa_write_problem(bucket, project) is None


def test_a_conditional_or_someone_elses_write_grant_does_not_count():
    launch = _load("v3_launch_under_test_sa_cond", V3 / "launch_fleet.py")
    sa = f"serviceAccount:{launch.SERVICE_ACCOUNT}"
    conditional = {"bindings": [{"role": "roles/storage.objectAdmin", "members": [sa],
                                 "condition": {"expression": "resource.name.startsWith('x')"}}]}
    other = _policy(("roles/storage.objectAdmin", ["serviceAccount:other@x.iam.gserviceaccount.com"]))
    assert launch.sa_write_problem(conditional, other)


def test_every_arm_runs_the_shipping_repair_timeout():
    """At the 120 s settings default the investigator had no time above its reserves and made 0
    tool calls; shared dev runs 420. Every arm gets it, or the arms stop being comparable."""
    for name, arm in spec.ARMS.items():
        assert arm["env"].get("CAD_REPAIR_TIMEOUT_S") == "420", name


def test_create_only_access_is_refused_because_workers_replace_and_delete_objects():
    # publish_unit re-copies rows.json/PROVENANCE.json over the rsync'd copies and a rerun shard
    # deletes its old FAILED marker: objectCreator can do neither, so every shard would end FAILED.
    launch = _load("v3_launch_under_test_sa_creator", V3 / "launch_fleet.py")
    sa = f"serviceAccount:{launch.SERVICE_ACCOUNT}"
    bucket = _policy(("roles/storage.objectCreator", [sa]), ("roles/storage.objectViewer", [sa]))
    assert launch.sa_write_problem(bucket, _policy())


def test_write_access_is_checked_on_the_results_bucket_not_the_input_prefix(monkeypatch):
    launch = _load("v3_launch_under_test_sa_bucket", V3 / "launch_fleet.py")
    asked = []

    def fake_json(cmd):
        asked.append(cmd)
        return {"quotas": []} if "regions" in cmd else {"bindings": []}

    monkeypatch.setattr(launch, "_gcloud_json", fake_json)
    monkeypatch.setattr(launch.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    args = type("A", (), {"image": "img", "image_commit": "a" * 40, "commit": COMMIT,
                          "prefix": "gs://dev-crsv3-other-inputs/eval/v3-verdict",
                          "machine_type": "c2d-standard-8"})()
    launch.live_checks(args, 8)
    buckets = [c[-1] for c in asked if c[:4] == ["gcloud", "storage", "buckets", "get-iam-policy"]]
    assert buckets == [launch.BUCKET]


# ------------------------------------------------------------------ lane D: transcripts per shard

def test_collector_gathers_each_shards_transcripts_and_a_per_file_outcome_table(tmp_path):
    collect = _load("v3_collect_under_test_transcripts", V3 / "collect.py")
    src, out = tmp_path / "src", tmp_path / "out"
    why = {"region": 0, "end": "runaway", "why_no_package": "runaway; no edit kept"}
    d1 = _shard(src, "v3_full", "failing", 1, 2, rows=[
        {"file": "a.step", "outcome": "repair_refused", "meshes_before": False,
         "meshes_after": False, "investigator": {"ends": {"runaway": 1}, "outcomes": [why]}}])
    _shard(src, "v3_full", "failing", 2, 2)  # no transcripts: an arm shard that never asked
    (d1 / "transcripts.jsonl").write_text(
        json.dumps({"file": "a.step", "region": 0, "end": "runaway"}) + "\n"
        + json.dumps({"file": "a.step", "region": 1, "end": "budget_spent"}) + "\n")
    runs = collect.load_runs(src)
    out.mkdir()
    got = collect.gather_transcripts(runs["arms"][("v3_full", "failing")], "v3_full",
                                     "failing", out)
    assert got == {"records": 2, "shards_with_transcripts": 1, "files": 2}
    lines = [json.loads(ln) for ln in
             (out / "transcripts-v3_full-failing.jsonl").read_text().splitlines()]
    assert [(ln["file"], ln["shard"]) for ln in lines] == [("a.step", "1/2"), ("a.step", "1/2")]
    table = json.loads((out / "outcomes-v3_full-failing.json").read_text())
    by_file = {r["file"]: r for r in table}
    # REBUILT FROM THE TRANSCRIPT (one entry per region it names), not the row's baked outcomes.
    assert [r["end"] for r in by_file["a.step"]["regions"]] == ["runaway", "budget_spent"]
    assert by_file["a.step"]["meshes_after"] is False
    assert by_file["v3_full-failing-2.step"]["regions"] == []


def test_collector_recomputes_outcomes_from_transcripts_so_elided_edits_still_count(tmp_path):
    """Greptile: rows.json outcomes made at b04befe8 undercount edits whose results were elided.
    The transcript's `turns` are never elided, so the table is rebuilt from it."""
    collect = _load("v3_collect_under_test_elided", V3 / "collect.py")
    src, out = tmp_path / "src", tmp_path / "out"
    stale = {"region": 0, "end": "runaway", "edits": {"tried": 0, "kept": 0, "rolled_back": {}},
             "why_no_package": "runaway; no edit tried",
             "package": {"status": "reverted", "reason": "r"}}
    d = _shard(src, "v3_full", "failing", 1, 1, rows=[
        {"file": "a.step", "outcome": "repair_refused", "meshes_before": False,
         "meshes_after": False, "investigator": {"ends": {"runaway": 1}, "outcomes": [stale]}}])
    turn = {"tool": "edit", "args": "{\"op\": \"fragment_bodies\"}", "ok": True}
    stub = {"role": "tool", "tool_call_id": "c", "content": json.dumps(
        {"elided": True, "call": 1, "tool": "edit", "sha256": "x"})}
    early = {"file": "a.step", "region": 0, "end": "budget_spent", "turns": [], "edits": [],
             "conclusion": {}, "messages": []}
    late = {"file": "a.step", "region": 0, "end": "runaway", "turns": [turn, turn],
            "edits": [], "conclusion": {"form": "", "reason": "runaway"},
            "messages": [stub, stub]}
    (d / "transcripts.jsonl").write_text(json.dumps(early) + "\n" + json.dumps(late) + "\n")
    runs = collect.load_runs(src)
    out.mkdir()
    collect.gather_transcripts(runs["arms"][("v3_full", "failing")], "v3_full", "failing", out)
    [row] = json.loads((out / "outcomes-v3_full-failing.json").read_text())
    [region] = row["regions"]
    assert region["end"] == "runaway"
    assert region["edits"]["tried"] == 2
    assert "no edit tried" not in region["why_no_package"]
    assert region["package"] == {"status": "reverted", "reason": "r"}
