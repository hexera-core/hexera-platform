# Responsibility: Verify the deploy hands scale-in to the idle workers - the autoscaler only scales out, the workers
#                 are granted the right to remove their own VM (or told the one command that grants it), and a fleet
#                 deploy moves workers that can move themselves without replacing them mid-job.
# Boundaries: the two provisioning stages against a fake gcloud; nothing reaches a cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"

# THE DEFECT THIS PINS. Shared dev, 2026-09-30, rehearsal job bc5ddb08 moved three times:
#   08:29:52  autoscaler 3 -> 2 deleted the VM running it (solver check)
#   08:57:57  a fleet deploy's PROACTIVE roll replaced the VM running it (meshing)
#   09:00:13  autoscaler 2 -> 1 deleted the VM that had picked it up two minutes earlier
# Scale-in control (1 per 30 min) was in place; it only spaced the removals out. A group cannot be
# told which VM is idle, so the autoscaler no longer removes any, and a roll no longer replaces a
# worker that can move itself.

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 0; fi
if has "iam roles create"; then exit "${FAKE_ROLE_CREATE_RC:-0}"; fi
if has "iam roles update"; then exit "${FAKE_ROLE_UPDATE_RC:-0}"; fi
if has "projects add-iam-policy-binding"; then exit "${FAKE_BIND_RC:-0}"; fi
if has "instance-templates describe"; then
  if has "properties.labels.self-retire"; then printf '%s\n' "${FAKE_CURRENT_LABEL:-}"; exit 0; fi
  exit 1
fi
if has "scheduler jobs describe"; then exit 1; fi
if has "storage objects describe"; then exit 0; fi
if has "storage buckets describe"; then exit 0; fi
if has "update-autoscaling"; then exit "${FAKE_UPDATE_AS_RC:-0}"; fi
if has "instance-groups managed describe"; then
  if has "autoscalingPolicy.mode"; then printf '%s\n' "${FAKE_MODE:-}"; exit 0; fi
  if has "status.autoscaler"; then printf '%s\n' "${FAKE_AUTOSCALER:-}"; exit 0; fi
  if has "autoscaler.name"; then printf '%s\n' "${FAKE_AUTOSCALER:-}"; exit 0; fi
  if has "customMetricUtilizations[].metric"; then printf '%s\n' "${FAKE_AS_METRICS:-}"; exit 0; fi
  if has "maxScaledInReplicas.fixed"; then exit 0; fi
  if has "maxScaledInReplicas.percent"; then exit 0; fi
  if has "timeWindowSec"; then exit 0; fi
  if has "minNumReplicas"; then printf '%s\n' "${FAKE_AS_MIN:-}"; exit 0; fi
  if has "maxNumReplicas"; then printf '%s\n' "${FAKE_AS_MAX:-}"; exit 0; fi
  if has "coolDownPeriodSec"; then printf '%s\n' "${FAKE_AS_COOLDOWN:-}"; exit 0; fi
  if has "singleInstanceAssignment"; then printf '%s\n' "${FAKE_AS_ASSIGNMENT:-}"; exit 0; fi
  if has "instanceTemplate"; then printf '%s\n' "${FAKE_LIVE_TEMPLATE:-}"; exit 0; fi
  exit "${FAKE_MIG_RC:-0}"
fi
if has "auth print-access-token"; then printf 'fake-token\n'; exit 0; fi
exit 0
"""

# The Compute API read-then-PATCH of updatePolicy.minReadySec that precedes every roll.
_FAKE_CURL = r"""#!/usr/bin/env bash
case "$*" in
  *"-X PATCH"*) exit 0 ;;
  *instanceGroupManagers*)
    printf '%s' '{"updatePolicy":{"type":"PROACTIVE","maxSurge":{"fixed":1},"maxUnavailable":{"fixed":0}}}'
    exit 0 ;;
esac
exit 0
"""

_FAKE_ENVSUBST = r"""#!/usr/bin/env python3
import os, re, sys
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\$(\w+)",
                        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                        sys.stdin.read()))
"""

_ENV = {
    "APP_IMAGE": "us-central1-docker.pkg.dev/fake-proj/mesh/app@sha256:c0ffee",
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "us-central1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "REDIS_URL": "redis://10.0.0.3:6379",
    "WORKER_ENV_URI": "gs://fake-bucket/worker.env",
    "WORKER_MIG": "t-workers",
    "WORKER_MIG_ZONE": "us-central1-a",
    "WORKER_SERVICE_ACCOUNT": "t-worker",
    "QUEUE_NAME": "simulation_jobs",
}
_SA = "t-worker@fake-proj.iam.gserviceaccount.com"
_ROLE = "projects/fake-proj/roles/hexeraWorkerSelfRetire"

#: a new group: no group, no autoscaler
_NEW = {"FAKE_MIG_RC": "1", "FAKE_AUTOSCALER": ""}
#: a live group with an autoscaler that still scales in, on a template from before this change
_LIVE = {"FAKE_MIG_RC": "0", "FAKE_AUTOSCALER": "zones/us-central1-a/autoscalers/t-workers-eyms",
         "FAKE_LIVE_TEMPLATE": "t-workers-tpl-old", "FAKE_MODE": "ON"}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", _FAKE_GCLOUD), ("curl", _FAKE_CURL), ("envsubst", _FAKE_ENVSUBST)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    counter = {"n": 0}

    def _run(script: str, fake: dict | None = None, extra: dict | None = None):
        counter["n"] += 1
        state = tmp_path / f"state{counter['n']}"
        env_file = tmp_path / f"generated{counter['n']}.env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in {**_ENV, **(extra or {})}.items())
                            + "\n", encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPTS / script)], capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file),
                 "RENDER_DIR": str(tmp_path / f"rendered{counter['n']}"), "ASSUME_YES": "1",
                 **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")
    return _run


def _lines(calls: str, needle: str) -> list[str]:
    return [ln for ln in calls.splitlines() if needle in ln]


# ---------------------------------------------------------------------------------------------
# the autoscaler scales OUT only
# ---------------------------------------------------------------------------------------------

def test_a_new_fleet_only_scales_out_and_its_vms_are_marked_as_self_retiring(run):
    done, calls = run("create-worker-fleet.sh", fake=_NEW)
    assert done.returncode == 0, done.stderr
    (line,) = _lines(calls, "set-autoscaling")
    assert "--mode only-scale-out" in line
    (create,) = _lines(calls, "instance-templates create")
    assert "self-retire=v1" in create


def test_a_live_autoscaler_that_scales_in_is_switched_to_scale_out_only(run):
    done, calls = run("create-worker-fleet.sh", fake=_LIVE)
    assert done.returncode == 0, done.stderr
    (line,) = _lines(calls, "update-autoscaling")
    assert "--mode only-scale-out" in line
    # ...and ONLY the mode: the floor, ceiling and the rest are the admin console's
    assert not _lines(calls, "set-autoscaling")
    for knob in ("--min-num-replicas", "--max-num-replicas", "--cool-down-period",
                 "--scale-in-control"):
        assert knob not in line


@pytest.mark.parametrize("mode", ["ONLY_SCALE_OUT", "OFF"])
def test_a_mode_already_right_or_switched_off_by_hand_is_left_alone(run, mode):
    done, calls = run("create-worker-fleet.sh", fake={**_LIVE, "FAKE_MODE": mode})
    assert done.returncode == 0, done.stderr
    assert not _lines(calls, "update-autoscaling")


@pytest.mark.parametrize("live,expected", [("ONLY_SCALE_OUT", "only-scale-out"), ("ON", "on"),
                                           ("OFF", "off"), ("", "on")])
def test_the_publisher_stage_carries_the_mode_through_its_rewrite(run, live, expected):
    # set-autoscaling replaces the whole policy and writes mode ON unless told otherwise - which
    # would hand scale-in back to the autoscaler on every deploy of the queue stage
    done, calls = run("create-queue-depth-publisher.sh",
                      fake={"FAKE_MIG_RC": "0", "FAKE_AUTOSCALER": _LIVE["FAKE_AUTOSCALER"],
                            "FAKE_AS_MIN": "1", "FAKE_AS_MAX": "5", "FAKE_AS_COOLDOWN": "180",
                            "FAKE_AS_ASSIGNMENT": "1", "FAKE_MODE": live})
    assert done.returncode == 0, done.stderr
    (line,) = _lines(calls, "set-autoscaling")
    assert f"--mode {expected} " in line + " "


# ---------------------------------------------------------------------------------------------
# the grant
# ---------------------------------------------------------------------------------------------

def test_the_workers_are_granted_exactly_the_right_to_manage_their_own_group(run):
    done, calls = run("create-worker-fleet.sh", fake=_NEW)
    assert done.returncode == 0, done.stderr
    (role,) = _lines(calls, "iam roles create hexeraWorkerSelfRetire")
    from meshpipeline.runtime.idle_retire import REQUIRED_PERMISSIONS
    assert f"--permissions {','.join(REQUIRED_PERMISSIONS)}" in role
    (bind,) = _lines(calls, "projects add-iam-policy-binding")
    assert f"--member serviceAccount:{_SA}" in bind and f"--role {_ROLE}" in bind
    assert "instanceAdmin" not in calls


def test_a_deployer_that_cannot_grant_prints_the_one_command_and_carries_on(run):
    # shared dev's github-deployer holds neither iam.roles.create nor the project's setIamPolicy
    done, calls = run("create-worker-fleet.sh",
                      fake={**_NEW, "FAKE_ROLE_CREATE_RC": "1", "FAKE_ROLE_UPDATE_RC": "1",
                            "FAKE_BIND_RC": "1"})
    assert done.returncode == 0, done.stderr
    from meshpipeline.runtime.idle_retire import Identity
    worker_says = Identity(project="fake-proj", zone="us-central1-a", instance="i", group="t-workers",
                           template=None, service_account=_SA).grant_command()
    # the deploy and the worker name the SAME command, so whichever log an owner reads, it works
    assert worker_says in done.stderr
    # the rest of the stage still ran
    assert _lines(calls, "set-autoscaling")


def test_a_refused_binding_is_reported_too(run):
    done, _calls = run("create-worker-fleet.sh", fake={**_NEW, "FAKE_BIND_RC": "1"})
    assert done.returncode == 0, done.stderr
    # `;` - the role this run may already have created must not stop the owner's binding
    assert "--stage GA ; gcloud projects add-iam-policy-binding fake-proj" in done.stderr


def test_the_binding_is_the_verdict_not_the_role(run):
    # an owner created the role once; the deployer can neither create nor update it, but the binding
    # is what grants anything - made, it is a success and nothing is printed
    done, calls = run("create-worker-fleet.sh",
                      fake={**_NEW, "FAKE_ROLE_CREATE_RC": "1", "FAKE_ROLE_UPDATE_RC": "1"})
    assert done.returncode == 0, done.stderr
    assert _lines(calls, "projects add-iam-policy-binding")
    assert "could not grant" not in done.stderr


# ---------------------------------------------------------------------------------------------
# the roll
# ---------------------------------------------------------------------------------------------

def _roll_type(calls: str) -> str:
    (update,) = _lines(calls, "--update-policy-type")
    (start,) = _lines(calls, "rolling-action start-update")
    kind = update.split("--update-policy-type ", 1)[1].split()[0]
    assert f"--type {kind}" in start
    return kind


def test_the_first_roll_onto_self_retiring_workers_is_proactive(run):
    # the workers running now were built before they could move themselves: nobody would ever
    # move them, so this one roll replaces them
    done, calls = run("create-worker-fleet.sh", fake=_LIVE)
    assert done.returncode == 0, done.stderr
    assert _roll_type(calls) == "proactive"


def test_once_the_workers_move_themselves_a_roll_replaces_none_of_them(run):
    done, calls = run("create-worker-fleet.sh",
                      fake={**_LIVE, "FAKE_MODE": "ONLY_SCALE_OUT", "FAKE_CURRENT_LABEL": "v1"})
    assert done.returncode == 0, done.stderr
    assert _roll_type(calls) == "opportunistic"
    assert "none is replaced mid-job" in done.stdout


def test_a_forced_roll_type_wins(run):
    done, calls = run("create-worker-fleet.sh",
                      fake={**_LIVE, "FAKE_CURRENT_LABEL": "v1"},
                      extra={"WORKER_ROLLING_TYPE": "proactive"})
    assert done.returncode == 0, done.stderr
    assert _roll_type(calls) == "proactive"


def test_a_misspelt_roll_type_is_refused_before_anything_is_created(run):
    done, calls = run("create-worker-fleet.sh", fake=_LIVE,
                      extra={"WORKER_ROLLING_TYPE": "gentle"})
    assert done.returncode != 0
    assert not _lines(calls, "instance-templates create")


# ---------------------------------------------------------------------------------------------
# the escape hatch
# ---------------------------------------------------------------------------------------------

def test_self_retire_off_is_exactly_the_old_fleet(run):
    done, calls = run("create-worker-fleet.sh", fake=_NEW, extra={"WORKER_SELF_RETIRE": "false"})
    assert done.returncode == 0, done.stderr
    (line,) = _lines(calls, "set-autoscaling")
    assert "--mode on" in line and "--scale-in-control max-scaled-in-replicas=1" in line
    assert not _lines(calls, "iam roles")
    (create,) = _lines(calls, "instance-templates create")
    assert "self-retire" not in create


def test_self_retire_off_hands_scale_in_back_and_rolls_proactively(run):
    done, calls = run("create-worker-fleet.sh",
                      fake={**_LIVE, "FAKE_MODE": "ONLY_SCALE_OUT", "FAKE_CURRENT_LABEL": "v1"},
                      extra={"WORKER_SELF_RETIRE": "false"})
    assert done.returncode == 0, done.stderr
    (line,) = _lines(calls, "update-autoscaling")
    assert "--mode on" in line
    assert _roll_type(calls) == "proactive"


def test_turning_self_retire_off_or_on_is_a_new_template():
    # the label is in the specification hash, so it can never be flipped on a template in place
    body = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
    spec = body[body.index('SPEC_HASH="$('):]
    assert '"${SELF_RETIRE_LABEL}"' in spec[: spec.index("hashlib")]
