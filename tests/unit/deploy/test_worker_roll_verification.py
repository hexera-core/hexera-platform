# Responsibility: Verify a fleet deploy is only green once every running worker VM is on the new template, runs the new
#                 digest in both containers and reports healthy - that VMs which did not move themselves are replaced
#                 with a surge, and that a roll that cannot land fails the stage loudly.
# Boundaries: the fleet stage against a fake gcloud that holds the group's instances and their guest attributes;
#             nothing reaches a cloud.
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
FLEET = REPO / "deploy" / "gcp" / "scripts" / "create-worker-fleet.sh"

# THE DEFECT THIS PINS. Shared dev, 2026-10-17 to 10-19: four `deploy.yml -f fleet=true` runs (one
# of them 39192614101) went green while dev-workers-tjj1 kept running image 09518cf25b23 from
# 10-12. The roll was opportunistic - it only set the template NEW VMs start on, and relied on the
# idle worker recycling itself - and that worker's consumer was stuck, so it never did. Nothing
# checked. The VM moved only when it was recreated by hand.

_APP_IMAGE = "us-central1-docker.pkg.dev/fake-proj/mesh/app@sha256:c0ffee"

_FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys, time
state = os.environ["FAKE_GCP_STATE"]; os.makedirs(state, exist_ok=True)
args = " ".join(sys.argv[1:])
with open(os.path.join(state, "calls.log"), "a") as fh:
    fh.write(args + "\n")
def has(s): return s in args
def out(s): sys.stdout.write(s + "\n"); sys.exit(0)
forced = os.path.join(state, "forced")
recreated = os.path.join(state, "recreated")
def after_recreate(key): return key + "_RECREATED" if os.path.exists(recreated) and key + "_RECREATED" in os.environ else key
if has("recreate-instances"):
    open(recreated, "w").write("1"); sys.exit(0)
if has("get-serial-port-output"):
    out(os.environ.get(after_recreate("FAKE_SERIAL"), ""))
if has("add-iam-policy-binding") and (has("artifacts repositories") or has("storage buckets")) \
        and os.environ.get("FAKE_BOOT_GRANTS_REFUSED"):
    sys.exit(1)
if has("get-ancestors-iam-policy") or has("artifacts repositories get-iam-policy") or has("storage buckets get-iam-policy"):
    key = "FAKE_ANCESTORS" if has("ancestors") else "FAKE_REPO_POLICY" if has("artifacts") else "FAKE_BUCKET_POLICY"
    if key not in os.environ: sys.exit(1)
    out(os.environ[key])
if has("instance-templates create"):
    name = sys.argv[sys.argv.index("create") + 1]
    open(os.path.join(state, "template"), "w").write(name); sys.exit(0)
if has("instance-templates describe"):
    if has("properties.labels.self-retire"): out(os.environ.get("FAKE_CURRENT_LABEL", ""))
    sys.exit(1)
if has("instance-groups managed update") and has("--update-policy-type proactive") \
        and not has("--update-policy-max-surge"):
    open(forced, "w").write("1"); sys.exit(0)
if has("instance-groups managed list-instances"):
    spec = os.environ.get(after_recreate("FAKE_INSTANCES"), "[]")
    if "FAKE_INSTANCES_SEQ" in os.environ:                     # the group, one entry per look
        seq = json.loads(os.environ["FAKE_INSTANCES_SEQ"])
        looks = os.path.join(state, "looks")
        n = int(open(looks).read()) if os.path.exists(looks) else 0
        open(looks, "w").write(str(n + 1))
        spec = json.dumps(seq[min(n, len(seq) - 1)])
    elif os.path.exists(forced):
        spec = os.environ.get("FAKE_INSTANCES_AFTER", spec)     # unset: the replacement changed nothing
    tpl = open(os.path.join(state, "template")).read() if os.path.exists(os.path.join(state, "template")) else "?"
    rows = [{"name": i["name"], "instanceStatus": i.get("status", "RUNNING"),
             "currentAction": i.get("action", "NONE"), "id": i.get("id", ""),
             "version": {"instanceTemplate": "https://x/global/instanceTemplates/" + (tpl if i["tpl"] == "NEW" else i["tpl"])}}
            for i in json.loads(spec)]
    out(json.dumps(rows))
if has("get-guest-attributes"):
    spec = os.environ.get("FAKE_GA_AFTER" if os.path.exists(forced) and "FAKE_GA_AFTER" in os.environ
                          else after_recreate("FAKE_GA"), "{}")
    values = {k: v.replace("IMAGE", os.environ["FAKE_APP_IMAGE"]).replace("NOW", str(int(time.time())))
              for k, v in json.loads(spec).items()}
    out(json.dumps([{"namespace": "hexera", "key": k, "value": v} for k, v in values.items()]))
if has("instance-groups managed describe"):
    if has("autoscalingPolicy.mode"): out("ONLY_SCALE_OUT")
    if has("status.autoscaler"): out("zones/us-central1-a/autoscalers/t-workers-eyms")
    if has("instanceTemplate"): out(os.environ.get("FAKE_LIVE_TEMPLATE", "t-workers-tpl-old"))
    sys.exit(0)
if has("auth print-access-token"): out("fake-token")
if has("storage objects describe") or has("storage buckets describe"): sys.exit(0)
sys.exit(0)
'''

_FAKE_CURL = r"""#!/usr/bin/env bash
case "$*" in
  *"-X PATCH"*) exit 0 ;;
  *instanceGroupManagers*) printf '%s' '{"updatePolicy":{"type":"OPPORTUNISTIC"}}'; exit 0 ;;
esac
exit 0
"""

_ENV = {
    "APP_IMAGE": _APP_IMAGE, "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693", "GCP_REGION": "us-central1", "MESH_SERVICE_ACCOUNT": "t-mesh", "REDIS_URL": "redis://10.0.0.3:6379",
    "WORKER_ENV_URI": "gs://fake-bucket/worker.env", "WORKER_MIG": "t-workers",
    "WORKER_MIG_ZONE": "us-central1-a", "WORKER_SERVICE_ACCOUNT": "t-worker",
    # a fast clock for the test: replace after 0 s, give up after 3 s, look every second
    "WORKER_ROLL_FORCE_AFTER_SECONDS": "0", "WORKER_ROLL_TIMEOUT_SECONDS": "3",
    "WORKER_ROLL_POLL_SECONDS": "1",
}

_HEALTHY = {"worker-image": "IMAGE", "geometry-image": "IMAGE", "worker-health": "healthy",
            "geometry-health": "healthy", "heartbeat": "NOW"}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", _FAKE_GCLOUD), ("curl", _FAKE_CURL)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    counter = {"n": 0}

    def _run(instances, ga=None, *, after=None, ga_after=None, extra=None, seq=None, fake_env=None):
        counter["n"] += 1
        state = tmp_path / f"state{counter['n']}"
        env_file = tmp_path / f"generated{counter['n']}.env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in {**_ENV, **(extra or {})}.items()) + "\n",
                            encoding="utf-8")
        fake = {"FAKE_INSTANCES": json.dumps(instances), "FAKE_GA": json.dumps(ga or {}),
                "FAKE_APP_IMAGE": _APP_IMAGE, "FAKE_CURRENT_LABEL": "v1"}
        if after is not None:
            fake["FAKE_INSTANCES_AFTER"] = json.dumps(after)
        if ga_after is not None:
            fake["FAKE_GA_AFTER"] = json.dumps(ga_after)
        if seq is not None:
            fake["FAKE_INSTANCES_SEQ"] = json.dumps(seq)
        fake.update(fake_env or {})
        done = subprocess.run(
            ["bash", str(FLEET)], capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GCP_STATE": str(state),
                 "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1", **fake})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")
    return _run


def _forced(calls: str) -> list[str]:
    return [ln for ln in calls.splitlines()
            if "rolling-action start-update" in ln and "--type proactive" in ln]


def test_a_group_at_zero_is_done_at_once(run):
    done, calls = run([])
    assert done.returncode == 0, done.stderr
    assert "no VM is running" in done.stdout
    assert not _forced(calls)


def test_a_vm_on_the_new_digest_and_healthy_is_done_without_replacing_anything(run):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW"}], _HEALTHY)
    assert done.returncode == 0, done.stderr
    assert "t-workers-a1: on the new digest, both containers healthy" in done.stdout
    assert not _forced(calls)


def test_the_2026_10_19_vm_is_replaced_with_a_surge_and_then_verified(run):
    # tjj1: still on the old template, and nothing will ever move it
    done, calls = run([{"name": "t-workers-tjj1", "tpl": "t-workers-tpl-09518cf25b23-231924"}], {},
                      after=[{"name": "t-workers-k2v9", "tpl": "NEW"}], ga_after=_HEALTHY,
                      extra={"WORKER_ROLL_TIMEOUT_SECONDS": "20"})
    assert done.returncode == 0, done.stderr
    (force,) = _forced(calls)
    # the replacement is created first and must be ready before the old VM goes
    assert "--max-surge 1" in force and "--max-unavailable 0" in force
    assert "--replacement-method substitute" in force
    assert "t-workers-tjj1" in done.stderr          # the warning names who is being replaced
    assert "t-workers-k2v9: on the new digest" in done.stdout


def test_a_roll_that_never_lands_fails_the_stage_loudly(run):
    done, calls = run([{"name": "t-workers-tjj1", "tpl": "t-workers-tpl-old"}])
    assert done.returncode != 0
    assert _forced(calls)                             # it tried the replacement first
    assert "did not reach" in done.stderr and "even after a proactive replacement" in done.stderr
    assert "t-workers-tjj1: on t-workers-tpl-old" in done.stderr
    assert "The deploy is NOT done" in done.stderr


@pytest.mark.parametrize("ga,needle", [
    ({**_HEALTHY, "geometry-health": "unhealthy"}, "geometry container is unhealthy"),
    ({**_HEALTHY, "worker-image": "app@sha256:09518cf25b23"}, "worker container runs app@sha256:09518cf25b23"),
    ({**_HEALTHY, "heartbeat": "1000"}, "its watchdog last reported"),
    ({}, "its watchdog has published nothing yet"),
])
def test_a_vm_on_the_new_template_is_not_proof_of_the_new_code(run, ga, needle):
    # a VM made from the new template whose startup failed, whose container is stuck, or whose
    # watchdog stopped - none of them is a finished roll, and replacing it again would not help
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW"}], ga)
    assert done.returncode != 0
    assert needle in done.stderr
    assert not _forced(calls)


def test_a_vm_still_booting_is_waited_for_not_replaced(run):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW", "status": "STAGING",
                        "action": "CREATING"}], _HEALTHY)
    assert done.returncode != 0
    assert "still STAGING/CREATING" in done.stderr
    assert not _forced(calls)


def test_the_deadline_must_leave_room_for_the_replacement(run):
    done, calls = run([], extra={"WORKER_ROLL_FORCE_AFTER_SECONDS": "60",
                                 "WORKER_ROLL_TIMEOUT_SECONDS": "60"})
    assert done.returncode != 0
    assert "not before" in done.stderr
    assert "instance-templates create" not in calls     # refused before anything was made


def test_verification_runs_even_when_the_template_is_reused():
    # the re-run of a deploy whose roll never finished reads "reused" in step 5 - and is exactly
    # the case that must not pass unchecked
    body = FLEET.read_text(encoding="utf-8")
    roll = body.index("# 5) THE GROUP, and the rotation.")
    verify = body.index("# 7) THE ROLL IS VERIFIED, NOT ASSUMED")
    assert roll < verify
    block = body[verify:body.index("FLOOR_STATE=", verify)]
    assert "MIG_DISPOSITION" not in block.split("while :", 1)[0], (
        "the verification is gated on the roll's disposition")


def _rolling(n: int, looks_per_vm: int) -> list[list[dict]]:
    """A fleet of `n` VMs moving onto the new template one at a time, each taking `looks_per_vm` looks."""
    seq = []
    for done in range(n + 1):
        group = [{"name": f"t-workers-v{k}", "tpl": "NEW" if k < done else "t-workers-tpl-old"} for k in range(n)]
        seq.extend([group] * looks_per_vm)
    return seq


def test_a_big_roll_that_keeps_moving_is_never_failed_on_a_total(run):
    """dev-areen run 40152656665: 20 workers took about 45 minutes and reached the target a minute after
    a fixed 2400 s total failed the deploy. The wait is a STALL window: here every VM moves within a look
    of the last, the whole roll takes longer than the 5 s window, and the stage is green."""
    done, _calls = run([], _HEALTHY, seq=_rolling(6, 1),
                       extra={"WORKER_ROLL_FORCE_AFTER_SECONDS": "4", "WORKER_ROLL_TIMEOUT_SECONDS": "5",
                              "WORKER_ROLL_POLL_SECONDS": "1"})
    assert done.returncode == 0, done.stderr
    assert "every VM is on" in done.stdout


def test_a_roll_that_stops_moving_fails_on_the_stall_and_says_so(run):
    seq = _rolling(3, 1)[:2]                             # one VM moves, then nothing does
    done, _calls = run([], _HEALTHY, seq=seq,
                       extra={"WORKER_ROLL_FORCE_AFTER_SECONDS": "4", "WORKER_ROLL_TIMEOUT_SECONDS": "5",
                              "WORKER_ROLL_POLL_SECONDS": "1"})
    assert done.returncode != 0
    assert "no VM moved any closer for" in done.stderr and "The deploy is NOT done" in done.stderr


# THE DEFECT THESE PIN. v0.2.5 in prod, 2026-11-14: prod-workers-qfv0 came up on the new template,
# its startup script died at `docker pull` (the worker identity had lost Artifact Registry read),
# and the roll waited 40 minutes for a watchdog that never started. A rerun waited again: the VM
# was already on the new template, so nothing replaced it, and its startup script never ran again.

_FAILED_BOOT = """[    0.000000] Linux version 6.1.0-cloud-amd64
startup-script: + docker pull us-central1-docker.pkg.dev/fake-proj/mesh/app@sha256:c0ffee
startup-script: Error response from daemon: Permission 'artifactregistry.repositories.downloadArtifacts' denied on resource
startup-script: Error response from daemon: Permission 'artifactregistry.repositories.downloadArtifacts' denied on resource
Script "startup-script" failed with error: exit status 1"""
_CLEAN_BOOT = "[    0.000000] Linux version 6.1.0-cloud-amd64\nstartup-script: + docker pull x"
_SLOW = {"WORKER_ROLL_FORCE_AFTER_SECONDS": "300", "WORKER_ROLL_TIMEOUT_SECONDS": "600"}


def _recreates(calls: str) -> list[str]:
    return [ln for ln in calls.splitlines() if "recreate-instances" in ln]


def test_a_vm_whose_startup_failed_is_recreated_once_and_then_verified(run):
    # the rerun after the grant: the VM's console still shows the old failure, from before the fix
    done, calls = run([{"name": "t-workers-qfv0", "tpl": "NEW", "id": "1"}], {}, extra=_SLOW, fake_env={
        "FAKE_SERIAL": _FAILED_BOOT, "FAKE_SERIAL_RECREATED": _CLEAN_BOOT,
        "FAKE_INSTANCES_RECREATED": json.dumps([{"name": "t-workers-qfv0", "tpl": "NEW", "id": "2"}]),
        "FAKE_GA_RECREATED": json.dumps(_HEALTHY)})
    assert done.returncode == 0, done.stderr
    (recreate,) = _recreates(calls)
    assert "--instances t-workers-qfv0" in recreate
    assert "its startup script failed" in done.stderr and "downloadArtifacts' denied" in done.stderr
    assert "t-workers-qfv0: on the new digest" in done.stdout
    assert not _forced(calls)


def test_a_startup_that_fails_again_after_the_recreate_stops_the_stage_at_once(run):
    started = time.monotonic()
    done, calls = run([{"name": "t-workers-qfv0", "tpl": "NEW", "id": "1"}], {}, extra=_SLOW, fake_env={
        "FAKE_SERIAL": _FAILED_BOOT,
        "FAKE_INSTANCES_RECREATED": json.dumps([{"name": "t-workers-qfv0", "tpl": "NEW", "id": "2"}])})
    assert done.returncode != 0
    assert time.monotonic() - started < 60, "the stage waited out the stall window"
    assert len(_recreates(calls)) == 1
    assert "failed again after this deploy" in done.stderr and "The deploy is NOT done" in done.stderr
    assert "downloadArtifacts' denied" in done.stderr
    assert ("gcloud artifacts repositories add-iam-policy-binding mesh --project fake-proj --location us-central1"
            " --member serviceAccount:t-worker@fake-proj.iam.gserviceaccount.com --role roles/artifactregistry.reader"
            in done.stderr)


def test_the_vm_before_the_recreate_took_effect_is_not_judged_as_its_replacement(run):
    # same instance id after the recreate call: the old VM, still showing the old failure
    done, calls = run([{"name": "t-workers-qfv0", "tpl": "NEW", "id": "1"}], {},
                      extra={"WORKER_ROLL_FORCE_AFTER_SECONDS": "1", "WORKER_ROLL_TIMEOUT_SECONDS": "3"},
                      fake_env={"FAKE_SERIAL": _FAILED_BOOT})
    assert done.returncode != 0
    assert len(_recreates(calls)) == 1
    assert "failed again" not in done.stderr and "no VM moved any closer" in done.stderr


def test_a_silent_vm_with_a_clean_console_is_recreated_after_the_force_window(run):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW", "id": "1"}], {},
                      extra={"WORKER_ROLL_TIMEOUT_SECONDS": "20"}, fake_env={
        "FAKE_SERIAL": _CLEAN_BOOT,
        "FAKE_INSTANCES_RECREATED": json.dumps([{"name": "t-workers-a1", "tpl": "NEW", "id": "2"}]),
        "FAKE_GA_RECREATED": json.dumps(_HEALTHY)})
    assert done.returncode == 0, done.stderr
    assert len(_recreates(calls)) == 1 and "has reported nothing for" in done.stderr


def test_a_booting_vm_inside_the_force_window_is_left_alone(run):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW", "id": "1"}], {},
                      extra={"WORKER_ROLL_FORCE_AFTER_SECONDS": "300", "WORKER_ROLL_TIMEOUT_SECONDS": "400",
                             "WORKER_ROLL_SECONDS_PER_VM": "0"},
                      fake_env={"FAKE_SERIAL": _CLEAN_BOOT, "FAKE_INSTANCES_SEQ": json.dumps(
                          [[{"name": "t-workers-a1", "tpl": "NEW", "id": "1"}]] * 3 + [[]])})
    assert done.returncode == 0, done.stderr
    assert not _recreates(calls)


_SA = "serviceAccount:t-worker@fake-proj.iam.gserviceaccount.com"
_EMPTY = json.dumps({"bindings": []})


def test_a_worker_identity_proven_unable_to_boot_stops_the_deploy_before_the_roll(run):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW"}], _HEALTHY, fake_env={
        "FAKE_BOOT_GRANTS_REFUSED": "1", "FAKE_REPO_POLICY": _EMPTY, "FAKE_BUCKET_POLICY": _EMPTY,
        "FAKE_ANCESTORS": json.dumps([{"type": "project", "policy": {"bindings": [
            {"role": "roles/monitoring.metricWriter", "members": [_SA]}]}}])})
    assert done.returncode != 0
    assert "Nothing was rolled" in done.stderr
    assert "--role roles/artifactregistry.reader" in done.stderr
    assert "gcloud storage buckets add-iam-policy-binding gs://fake-bucket" in done.stderr
    assert "instance-templates create" not in calls


def test_a_grant_already_held_passes_the_check(run):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW"}], _HEALTHY, fake_env={
        "FAKE_BOOT_GRANTS_REFUSED": "1",
        "FAKE_REPO_POLICY": json.dumps({"bindings": [{"role": "roles/artifactregistry.reader", "members": [_SA]}]}),
        "FAKE_BUCKET_POLICY": _EMPTY,
        "FAKE_ANCESTORS": json.dumps([{"type": "project", "policy": {"bindings": [
            {"role": "roles/storage.objectViewer", "members": [_SA]}]}}])})
    assert done.returncode == 0, done.stderr
    assert "may pull" in done.stdout and "may read gs://fake-bucket/worker.env" in done.stdout


@pytest.mark.parametrize("policies", [
    {},                                                                     # unreadable
    {"FAKE_REPO_POLICY": json.dumps({"bindings": [{"role": "roles/artifactregistry.reader",
                                                   "members": ["group:workers@example.com"]}]}),
     "FAKE_BUCKET_POLICY": json.dumps({"bindings": [{"role": "roles/storage.objectViewer",
                                                     "members": ["group:workers@example.com"]}]}),
     "FAKE_ANCESTORS": "[]"},                                               # held through a group, maybe
])
def test_what_the_policies_cannot_prove_only_warns(run, policies):
    done, calls = run([{"name": "t-workers-a1", "tpl": "NEW"}], _HEALTHY,
                      fake_env={"FAKE_BOOT_GRANTS_REFUSED": "1", **policies})
    assert done.returncode == 0, done.stderr
    assert "could not read" in done.stderr and "--role roles/artifactregistry.reader" in done.stderr
