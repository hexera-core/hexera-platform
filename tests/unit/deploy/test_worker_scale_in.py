# Responsibility: Verify the fleet is sized on the work it is RUNNING as well as the work queued, shrinks at most one
#                 instance per window, and that every worker VM stops its containers on shutdown so its job is handed back.
# Boundaries: the publisher program, the two provisioning stages against a fake gcloud, and static reads of the VM
#             scripts; nothing reaches a cloud.
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
WORKER = REPO / "deploy" / "gcp" / "worker"
PUBLISHER = WORKER / "queue_depth_publisher.py"

DEMAND = "custom.googleapis.com/hexera/worker_demand"
DEPTH = "custom.googleapis.com/hexera/queue_depth"

# THE DEFECT THIS PINS. Shared dev, 2026-09-29. The autoscaler scaled on queue DEPTH, which a
# running job does not count, so every time a burst of queued jobs drained it cut the group to its
# floor of 1 - at 05:41, 06:39 and 07:32 UTC ("recommended size changed from 5 to 1 because the
# minimum number of instances set in the autoscaling policy is 1") - and deleted the VMs three mesh
# jobs were running on. With no shutdown script the VMs went down in under a minute and the jobs
# were failed as worker_lost half an hour later. Nothing limited how fast the group could shrink,
# so it churned 1 -> 5 -> 1 every time the intake soak queued a burst.


# ---------------------------------------------------------------------------------------------
# the publisher: demand = queued + running
# ---------------------------------------------------------------------------------------------

def _publisher(monkeypatch, env: dict[str, str] | None = None):
    for key in ("QUEUE_NAME", "QUEUE_NAMES", "METRIC_NAMESPACE", "METRIC_LOCATION",
                "REDIS_KEY_PREFIX"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("queue_depth_publisher_scale_in", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prefix", ["", "dev-pranav:"])
def test_the_running_count_reads_the_key_every_owned_job_holds(monkeypatch, prefix):
    # The publisher may not import the application, so it restates the fence key's shape; this is
    # what holds the two together. A drift would publish "0 running" forever - the old bug.
    import fnmatch

    import meshpipeline.settings.providers as provcfg
    from meshpipeline.events.channels import fence_key_for
    monkeypatch.setattr(provcfg, "REDIS_KEY_PREFIX", prefix)
    mod = _publisher(monkeypatch)
    key = fence_key_for("4c536ce5-a83f-4e5c-95ea-e278964f38d1")
    assert fnmatch.fnmatchcase(key, mod.fence_pattern(prefix)), (key, mod.fence_pattern(prefix))
    # ...and only this deployment's: another environment's fences on the same instance are not ours
    other = "dev-other:" + key[len(prefix):]
    assert not fnmatch.fnmatchcase(other, mod.fence_pattern(prefix))


def test_a_prefix_is_matched_literally_not_as_a_glob(monkeypatch):
    mod = _publisher(monkeypatch)
    assert mod.fence_pattern("a*[b]?:") == r"a\*\[b\]\?:jobs:*:eventfence"


class _FakeClient:
    def __init__(self, depths: dict[str, int], fences: list[str]):
        self.depths, self.fences, self.calls = depths, fences, []

    def llen(self, queue):
        self.calls.append(("llen", queue))
        return self.depths.get(queue, 0)

    def scan_iter(self, match=None, count=None):
        self.calls.append(("scan", match))
        import fnmatch
        return iter([f for f in self.fences if fnmatch.fnmatchcase(f, match.replace("\\", ""))])

    def close(self):
        self.calls.append(("close",))


def test_one_connection_measures_the_queues_and_the_running_jobs(monkeypatch):
    mod = _publisher(monkeypatch, {"QUEUE_NAME": "simulation_jobs",
                                   "QUEUE_NAMES": "simulation_jobs,geometry_checks"})
    client = _FakeClient({"simulation_jobs": 1, "geometry_checks": 2},
                         ["jobs:a:eventfence", "jobs:b:eventfence", "jobs:b:eventfence",
                          "dev-x:jobs:c:eventfence", "jobs:a:eventops"])

    class _Err(Exception):
        pass
    monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(
        from_url=lambda *a, **k: client,
        exceptions=types.SimpleNamespace(TimeoutError=_Err, ConnectionError=_Err)))

    depths, running = mod.measure("redis://broker", prefix="")
    assert depths == {"simulation_jobs": 1, "geometry_checks": 2}
    # two jobs owned in this keyspace; a key seen twice by SCAN is one job; another
    # environment's fence and a non-fence key are not counted
    assert running == 2
    # SCAN, never KEYS, on the one connection the cold VPC attach was paid for
    assert [c[0] for c in client.calls] == ["llen", "llen", "scan", "close"]


def test_demand_is_queued_plus_running_on_the_series_the_autoscaler_filters(monkeypatch):
    mod = _publisher(monkeypatch)
    end = "2026-09-29T06:39:00Z"
    demand = mod.build_demand_series(0, 3, queue="simulation_jobs", project_id="p",
                                     namespace="dev", location="us-central1-a", end_time=end)
    (depth,) = mod.build_time_series({"simulation_jobs": 0}, project_id="p", namespace="dev",
                                     location="us-central1-a", end_time=end)
    # 06:39 on shared dev: nothing queued, three jobs running. Depth said 0 and the group went to
    # 1; demand says 3.
    assert demand["points"][0]["value"]["doubleValue"] == 3.0
    assert depth["points"][0]["value"]["doubleValue"] == 0.0
    assert demand["metric"]["type"] == DEMAND and depth["metric"]["type"] == DEPTH
    # the same resource labels as the depth series: the autoscaler's filter changes metric, not shape
    assert demand["resource"] == depth["resource"]


def test_one_write_carries_the_depths_and_the_demand(monkeypatch):
    mod = _publisher(monkeypatch, {"QUEUE_NAME": "simulation_jobs"})
    sent: list[dict] = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def _urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp()
    monkeypatch.setattr(mod, "_access_token", lambda: "t")
    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    mod.publish({"simulation_jobs": 2, "geometry_checks": 0}, project_id="p", namespace="dev",
                location="z", running=1)
    (body,) = sent
    by_type = {}
    for s in body["timeSeries"]:
        by_type.setdefault(s["metric"]["type"], []).append(s)
    assert len(by_type[DEPTH]) == 2
    (demand,) = by_type[DEMAND]
    assert demand["points"][0]["value"]["doubleValue"] == 3.0
    assert demand["resource"]["labels"]["task_id"] == "simulation_jobs"


# ---------------------------------------------------------------------------------------------
# the provisioning stages, against a fake gcloud
# ---------------------------------------------------------------------------------------------

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 0; fi
if has "instance-templates describe"; then exit 1; fi
if has "scheduler jobs describe"; then exit 1; fi
if has "storage objects describe"; then exit 0; fi
if has "storage buckets describe"; then exit 0; fi
if has "instance-groups managed describe"; then
  if has "status.autoscaler"; then printf '%s\n' "${FAKE_AUTOSCALER:-}"; exit 0; fi
  if has "autoscaler.name"; then printf '%s\n' "${FAKE_AUTOSCALER:-}"; exit 0; fi
  if has "customMetricUtilizations[].metric"; then printf '%s\n' "${FAKE_AS_METRICS:-}"; exit 0; fi
  if has "maxScaledInReplicas.fixed"; then printf '%s\n' "${FAKE_SI_FIXED:-}"; exit 0; fi
  if has "maxScaledInReplicas.percent"; then printf '%s\n' "${FAKE_SI_PERCENT:-}"; exit 0; fi
  if has "timeWindowSec"; then printf '%s\n' "${FAKE_SI_WINDOW:-}"; exit 0; fi
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

_LIVE = {"FAKE_MIG_RC": "0", "FAKE_AUTOSCALER": "zones/us-central1-a/autoscalers/t-workers-eyms",
         "FAKE_AS_MIN": "1", "FAKE_AS_MAX": "5", "FAKE_AS_COOLDOWN": "180",
         "FAKE_AS_ASSIGNMENT": "1", "FAKE_AS_METRICS": DEPTH}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", _FAKE_GCLOUD), ("envsubst", _FAKE_ENVSUBST),
                       ("curl", "#!/usr/bin/env bash\nexit 0\n")):
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
        return done, (log.read_text(encoding="utf-8") if log.exists() else ""), tmp_path / f"rendered{counter['n']}"
    return _run


def _set_autoscaling(calls: str) -> str:
    lines = [ln for ln in calls.splitlines() if "set-autoscaling" in ln]
    assert len(lines) == 1, calls
    return lines[0]


def test_the_live_autoscaler_is_repointed_at_demand_and_the_depth_metric_retired(run):
    done, calls, _ = run("create-queue-depth-publisher.sh", fake=_LIVE)
    assert done.returncode == 0, done.stderr
    line = _set_autoscaling(calls)
    assert f"--update-stackdriver-metric {DEMAND}" in line
    # set-autoscaling MERGES custom metrics; without this the group would scale on both, and the
    # admin console reads the first entry as "jobs per instance"
    assert f"--remove-stackdriver-metric {DEPTH}" in line
    assert 'resource.labels.task_id = "simulation_jobs"' in line


def test_a_group_with_no_scale_in_control_gets_one_instance_per_half_hour(run):
    done, calls, _ = run("create-queue-depth-publisher.sh", fake=_LIVE)
    assert done.returncode == 0, done.stderr
    assert "--scale-in-control max-scaled-in-replicas=1,time-window=1800" in _set_autoscaling(calls)


def test_a_scale_in_control_set_in_the_console_survives_the_deploy(run):
    # set-autoscaling replaces the whole policy: a control not passed through is wiped
    done, calls, _ = run("create-queue-depth-publisher.sh",
                         fake={**_LIVE, "FAKE_SI_FIXED": "2", "FAKE_SI_WINDOW": "600"})
    assert done.returncode == 0, done.stderr
    assert "--scale-in-control max-scaled-in-replicas=2,time-window=600" in _set_autoscaling(calls)


def test_a_percentage_set_in_the_console_stays_a_percentage(run):
    done, calls, _ = run("create-queue-depth-publisher.sh",
                         fake={**_LIVE, "FAKE_SI_PERCENT": "20", "FAKE_SI_WINDOW": "900"})
    assert done.returncode == 0, done.stderr
    line = _set_autoscaling(calls)
    assert "--scale-in-control max-scaled-in-replicas-percent=20,time-window=900" in line


def test_an_autoscaler_already_on_demand_is_not_asked_to_remove_anything(run):
    done, calls, _ = run("create-queue-depth-publisher.sh", fake={**_LIVE, "FAKE_AS_METRICS": DEMAND})
    assert done.returncode == 0, done.stderr
    assert "--remove-stackdriver-metric" not in _set_autoscaling(calls)


def test_the_publisher_is_told_the_keyspace_it_counts_running_jobs_in(run):
    done, _calls, rendered = run("create-queue-depth-publisher.sh", fake=_LIVE,
                                 extra={"REDIS_KEY_PREFIX": "dev-pranav:"})
    assert done.returncode == 0, done.stderr
    spec = (rendered / "queue-depth-job.yaml").read_text(encoding="utf-8")
    assert re.search(r'- name: REDIS_KEY_PREFIX\s+value: "dev-pranav:"', spec), spec


def test_a_new_fleet_is_sized_on_demand_with_a_scale_in_control(run):
    done, calls, _ = run("create-worker-fleet.sh", fake={"FAKE_MIG_RC": "1", "FAKE_AUTOSCALER": ""})
    assert done.returncode == 0, done.stderr
    line = _set_autoscaling(calls)
    assert f"--update-stackdriver-metric {DEMAND}" in line
    assert "--scale-in-control max-scaled-in-replicas=1,time-window=1800" in line


def test_every_worker_vm_gets_the_shutdown_script(run):
    done, calls, _ = run("create-worker-fleet.sh", fake={"FAKE_MIG_RC": "1", "FAKE_AUTOSCALER": ""})
    assert done.returncode == 0, done.stderr
    (create,) = [ln for ln in calls.splitlines() if "instance-templates create" in ln]
    assert re.search(r"shutdown-script=\S*deploy/gcp/worker/shutdown\.sh", create), create
    assert re.search(r"startup-script=\S*deploy/gcp/worker/startup\.sh", create), create


def test_a_change_to_the_shutdown_script_rolls_the_fleet():
    # The template name carries a hash of what a worker runs; a shutdown script left out of it
    # would change in git and never reach a VM.
    body = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
    assert 'cat - "${STARTUP}" "${SHUTDOWN}"' in body


# ---------------------------------------------------------------------------------------------
# the VM's own shutdown
# ---------------------------------------------------------------------------------------------

def _seconds(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    assert m, pattern
    return int(m.group(1))


def test_the_shutdown_script_stops_both_workers_inside_the_vms_budget():
    s = (WORKER / "shutdown.sh").read_text(encoding="utf-8")
    sim = _seconds(r"docker stop --time (\d+) hexera-worker\b", s)
    geo = _seconds(r"docker stop --time (\d+) hexera-geometry-worker\b", s)
    # a VM being deleted gets about ninety seconds; the job needs its SIGTERM well inside that
    assert sim < 90 and geo <= sim
    # ...and long enough for the hand-back after the graph's own grace to unwind
    from meshpipeline.application.worker_handoff import CANCEL_GRACE_SECONDS, DRAIN_POLL_SECONDS
    assert sim > CANCEL_GRACE_SECONDS + DRAIN_POLL_SECONDS + 30
    # both at once: the geometry worker's stop costs the simulation worker nothing
    assert "&" in s and "wait" in s


def test_the_containers_carry_the_same_budget_when_docker_stops_them_first():
    # Docker's own shutdown on a stopping VM may reach the containers before the script does, and
    # its default is ten seconds, then SIGKILL.
    startup = (WORKER / "startup.sh").read_text(encoding="utf-8")
    shutdown = (WORKER / "shutdown.sh").read_text(encoding="utf-8")
    for name in ("hexera-worker", "hexera-geometry-worker"):
        run_line = next(ln for ln in startup.splitlines()
                        if ln.startswith(f"docker run -d --name {name} "))
        given = _seconds(r"--stop-timeout (\d+)", run_line)
        assert given == _seconds(rf"docker stop --time (\d+) {name}\b", shutdown), name
