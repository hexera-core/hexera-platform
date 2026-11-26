# Responsibility: Prove the v4 launcher and VM startup can LAUNCH an arms wave (the four arms over a split) with
#                 exactly the flags run_arm takes, and that a model arm cannot be launched without a named secret.
# Boundaries: hermetic; reuses test_v4_fleet_launcher's gcloud-off-PATH dry run and its stub-docker startup run. Nothing
#             is created, nothing reaches a bucket or a model.
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("fleet_launcher_tests_helpers_arms",
                                               Path(__file__).with_name("test_v4_fleet_launcher.py"))
assert _spec is not None and _spec.loader is not None
base = importlib.util.module_from_spec(_spec)
sys.modules["fleet_launcher_tests_helpers_arms"] = base
_spec.loader.exec_module(base)

SPLIT_URI = "gs://dev-crsv3-transfer-224734058693/eval/v4/splits/dev.json"
ARMS_FLAGS = ["--dry-run", "--files", "100", "--arms", "search_det,search_ai", "--wave", "dev_search",
              "--split-uri", SPLIT_URI, "--h-mm", "0.5"]


def test_a_model_arm_cannot_be_launched_without_a_named_secret(tmp_path):
    r = base._v4(*ARMS_FLAGS, tmp_path=tmp_path)
    assert r.returncode == 2 and "--model-secret" in r.stderr and "no model key" in r.stderr
    assert base._creates(r.stdout) == []


def test_the_secret_is_named_by_the_operator_and_reaches_the_vm_metadata(tmp_path):
    r = base._v4(*ARMS_FLAGS, "--model-secret", "operator-chosen-secret", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    [create] = base._creates(r.stdout)[:1]
    for needle in ("v4-model-secret=operator-chosen-secret", "v4-wave=dev_search", "v4-arms=search_det,search_ai",
                   "v4-split=dev", f"v4-split-uri={SPLIT_URI}", "v4-h-mm=0.5", "v4-units=arms_w_test:pool:1/96"):
        assert needle in create, needle


def test_a_deterministic_only_arms_wave_needs_no_secret_and_names_none(tmp_path):
    r = base._v4("--dry-run", "--files", "100", "--arms", "input_only,staged", "--wave", "dev_deterministic",
                 "--split-uri", SPLIT_URI, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "v4-model-secret" not in base._creates(r.stdout)[0] and "v4-arms=input_only,staged" in r.stdout


def test_arms_flags_without_arms_are_refused_and_the_reprobe_is_unchanged(tmp_path):
    r = base._v4("--dry-run", "--files", "100", "--wave", "dev_search", tmp_path=tmp_path)
    assert r.returncode == 2 and "need --arms" in r.stderr
    plain = base._v4("--dry-run", "--files", "100", tmp_path=tmp_path)
    assert plain.returncode == 0 and "v4-arms" not in plain.stdout and "reprobe:pool:1/96" in plain.stdout


def test_an_arm_outside_the_wave_or_the_wrong_type_is_refused(tmp_path):
    r = base._v4("--dry-run", "--files", "100", "--arms", "search_ai", "--wave", "dev_deterministic",
                 "--split-uri", SPLIT_URI, "--h-mm", "1", "--model-secret", "s", tmp_path=tmp_path)
    assert r.returncode == 2 and "dev_deterministic" in r.stderr
    r = base._v4("--dry-run", "--files", "100", "--arms", "staged", "--wave", "dev_checkpoint",
                 "--split-uri", SPLIT_URI, tmp_path=tmp_path)
    assert r.returncode == 2 and "disagrees" in r.stderr           # dev_checkpoint is a checkpoint wave, this one normal


def _meta(**extra: str) -> dict[str, str]:
    return {"v4-image": "IMG@sha256:x", "v4-commit": "c" * 40, "v4-image-commit": "abc1234",
            "v4-units": "arms_w_test:pool:1/1", "v4-bucket": "gs://B", "v4-prefix": "gs://B/eval/v4-reprobe",
            "v4-code": "gs://B/eval/v4-reprobe/code-cccccccccccc/code.tgz", "v4-machine-type": "c2d-standard-8",
            "v4-run-arm": "devtools/validation/v4_eval/run_arm.py", "v4-pool": "gs://B/eval/v4-reprobe/pool.json",
            "v4-cache-uri": "gs://B/c", "v4-sources": "abc_fail", "v4-wave-id": "w-test", "v4-wave-type": "normal",
            "v4-n-shards": "1", "v4-marker-bucket": "B", "v4-wave": "dev_search", "v4-arms": "search_det,search_ai",
            "v4-split": "dev", "v4-split-uri": SPLIT_URI, "v4-h-mm": "0.5",
            # the plan the launcher claimed (plan_wave(...).provenance()): the startup refuses an arms wave without it
            "v4-plan-provenance": '{"wave": "dev_search", "slot": null}', **extra}


def test_startup_runs_run_arm_in_arms_mode_with_the_split_mounted(tmp_path):
    log = base._startup(tmp_path, _meta())
    [run] = [ln for ln in log if ln.startswith("docker run")]
    assert "--mode arms --wave dev_search --arms search_det,search_ai --split dev --split-file /eval/split.json " \
           "--h-mm 0.5" in run
    assert "/split.json:/eval/split.json:ro" in run and "--mode reprobe" not in run
    assert "--ledger-path /eval/ledger/log.jsonl" in run and "--wave-type normal" in run
    assert any(ln.startswith(f"storage cp {SPLIT_URI}") for ln in log)
    assert "secret.env" not in run                    # no secret named: no key mounted
    assert "--plan-provenance" in run and '"wave": "dev_search"' in run  # the claimed plan rides on the SAME command


def test_startup_refuses_an_arms_wave_whose_plan_was_not_claimed(tmp_path):
    for i, bad in enumerate(("", "UNCLAIMED-dry-run")):
        sub = tmp_path / str(i)
        sub.mkdir()
        log = base._startup(sub, {**_meta(), "v4-plan-provenance": bad}, expect_rc=1)
        assert not [ln for ln in log if ln.startswith("docker run")], bad
        assert any(ln.endswith("/FAILED") for ln in log), bad  # reported as the unit's failure, not a silent skip


def test_startup_reads_the_secret_only_when_the_launcher_named_one(tmp_path):
    # (the env-file MOUNT is not asserted here: the shared stub's `docker ps` makes the startup delete secret.env
    # before the unit reaches its `[ -f ]` check, which a real `docker ps` that waits for the containers does not)
    log = base._startup(tmp_path, _meta(**{"v4-model-secret": "operator-chosen-secret"}))
    assert any("secrets versions access" in ln and "operator-chosen-secret" in ln for ln in log)
