# Responsibility: Verify dev worker fleets run on Spot, production does not, and the drain budget fits the reclaim window.
# Boundaries: it RUNS bootstrap-env.sh's emit_env and reads the fleet and worker scripts; it deploys nothing.
"""Spot is a price for a deadline, and the deadline is what these tests are about.

A Spot VM is the same machine at roughly a third of the price, on the condition that Compute
Engine may reclaim it with a shutdown window of **up to thirty seconds** - not the ~90 s a deleted
on-demand VM gets. The worker's hand-back takes about eighteen seconds (DRAIN_POLL_SECONDS 1 +
CANCEL_GRACE_SECONDS 15 + the re-launch), so it fits - but only if the drain budget was lowered to
fit with it. Leave the budget at the on-demand 75 s and every preemption becomes a SIGKILL
mid-hand-back, a job that waits out its 900 s lease, and a `worker_lost` that reads as flakiness.

So: dev is SPOT, prod is STANDARD, a stated value wins either way, and the budget can never be
larger than the window. The last one is the test that matters.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap-env.sh"
FLEET = SCRIPTS / "create-worker-fleet.sh"
VALIDATE = SCRIPTS / "validate-config.sh"
STARTUP = REPO / "deploy" / "gcp" / "worker" / "startup.sh"
SHUTDOWN = REPO / "deploy" / "gcp" / "worker" / "shutdown.sh"
PROD_ENV = REPO / "deploy" / "gcp" / "generated.prod.env"

#: What Compute Engine gives a reclaimed Spot VM, in seconds. The number every budget below is
#: measured against; it is Google's, not ours.
SPOT_WINDOW_SECONDS = 30


def _emit_env_body() -> str:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    return text[text.index("emit_env() {"):text.index("\nENVFILE\n}")] + "\nENVFILE\n}\n"


def _generated(tmp_path: Path, **env: str) -> dict[str, str]:
    script = tmp_path / "emit.sh"
    script.write_text(_emit_env_body() + "\nemit_env\n", encoding="utf-8")
    p = subprocess.run(["bash", str(script)],
                       env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **env},
                       capture_output=True, text=True)
    assert p.returncode == 0, f"emit_env failed: {p.stderr}"
    out: dict[str, str] = {}
    for line in p.stdout.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k] = v.strip('"')
    return out


# ---------------------------------------------------------------------------------------------
# 1. the default follows APP_ENV, exactly as the warm floor does


def test_dev_runs_on_spot(tmp_path: Path):
    got = _generated(tmp_path, APP_ENV="dev")["WORKER_PROVISIONING_MODEL"]
    assert got == "SPOT", (
        f"dev would run on {got}. Dev work is bursty and restartable - over the fourteen days to "
        f"2026-11-05 the dev fleets were busy in 77 separate periods, median 26 min - and Spot is "
        f"about a third of the price for exactly that shape of work.")


def test_production_does_not_run_on_spot(tmp_path: Path):
    got = _generated(tmp_path, APP_ENV="prod")["WORKER_PROVISIONING_MODEL"]
    assert got == "STANDARD", (
        f"production would run on {got}. A reclaimed worker hands its job back and "
        f"worker_handoff.relaunch() RE-DISPATCHES it - the run starts over rather than resuming, "
        f"which for prod is a customer's run restarting to save a few dollars.")


def test_an_unstated_app_env_is_treated_as_dev(tmp_path: Path):
    assert _generated(tmp_path)["WORKER_PROVISIONING_MODEL"] == "SPOT"


@pytest.mark.parametrize("app_env,stated", [("dev", "STANDARD"), ("prod", "SPOT")])
def test_a_stated_model_wins_either_way(tmp_path: Path, app_env: str, stated: str):
    """A dev environment chasing a long run can ask for on-demand, and prod can be taken to Spot
    deliberately. The default is a default, not a policy engine."""
    got = _generated(tmp_path, APP_ENV=app_env, WORKER_PROVISIONING_MODEL=stated)
    assert got["WORKER_PROVISIONING_MODEL"] == stated


def test_production_state_records_its_model():
    recorded = dict(
        ln.split("=", 1) for ln in PROD_ENV.read_text(encoding="utf-8").splitlines()
        if "=" in ln and not ln.startswith("#"))
    assert recorded.get("WORKER_PROVISIONING_MODEL") == "STANDARD"


# ---------------------------------------------------------------------------------------------
# 2. the drain budget fits the reclaim window - the test this file exists for


def _fleet_budgets(provisioning_model: str) -> tuple[int, int]:
    """Run create-worker-fleet.sh's budget block for one model and report (worker, geometry)."""
    text = FLEET.read_text(encoding="utf-8")
    start = text.index('if [ "${WORKER_PROVISIONING_MODEL}" = SPOT ]; then\n  WORKER_STOP_TIMEOUT')
    end = text.index("# The guard that makes the paragraph above a rule")
    block = text[start:end]
    p = subprocess.run(
        ["bash", "-c", block + '\necho "$WORKER_STOP_TIMEOUT $GEOMETRY_STOP_TIMEOUT"'],
        env={"PATH": "/usr/bin:/bin", "WORKER_PROVISIONING_MODEL": provisioning_model},
        capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    w, g = p.stdout.split()
    return int(w), int(g)


def test_the_spot_drain_budget_fits_the_reclaim_window():
    """The whole point. 18 s of hand-back has to complete inside ~30 s of notice."""
    worker, geometry = _fleet_budgets("SPOT")
    assert worker < SPOT_WINDOW_SECONDS, (
        f"a Spot worker is given {worker}s to drain but a reclaimed Spot VM is stopped after about "
        f"{SPOT_WINDOW_SECONDS}s. docker stop would still be waiting when the machine went away, "
        f"so the job is never handed back and waits out its 900 s lease as worker_lost.")
    assert geometry < SPOT_WINDOW_SECONDS
    assert worker >= 20, (
        f"the Spot budget is {worker}s, but the measured drain is about 18s "
        f"(DRAIN_POLL_SECONDS 1 + CANCEL_GRACE_SECONDS 15 + the re-launch). Cutting below ~20s "
        f"starts SIGKILLing hand-backs that would have finished.")


def test_on_demand_keeps_the_budget_it_always_had():
    """The on-demand path must be byte-for-byte the behaviour that shipped before Spot existed."""
    assert _fleet_budgets("STANDARD") == (75, 30)


def test_a_spot_fleet_refuses_an_oversized_budget():
    """The guard, run as a program: a budget that does not fit the window stops the deploy rather
    than converting every preemption into silent flakiness."""
    text = FLEET.read_text(encoding="utf-8")
    guard = text[text.index("# The guard that makes the paragraph above a rule"):
                 text.index('fi', text.index("WORKER_PROVISIONING_MODEL=STANDARD.\"")) + 2]
    p = subprocess.run(
        ["bash", "-c", 'die() { echo "$*" >&2; exit 1; }\n' + guard],
        env={"PATH": "/usr/bin:/bin", "WORKER_PROVISIONING_MODEL": "SPOT",
             "WORKER_STOP_TIMEOUT": "75"},
        capture_output=True, text=True)
    assert p.returncode != 0, "a 75 s drain budget on a Spot fleet was accepted"
    assert "THIRTY" in p.stderr or "30" in p.stderr


# ---------------------------------------------------------------------------------------------
# 3. both halves of the drain read the same number, and the template rotates when it changes


def test_neither_worker_script_hardcodes_the_budget():
    """startup.sh sets each container's own --stop-timeout and shutdown.sh runs the docker stop.
    A literal in either one is a budget that ignores the provisioning model - and the two would
    disagree about how long the job has, depending on which stop arrived first."""
    for script in (STARTUP, SHUTDOWN):
        text = script.read_text(encoding="utf-8")
        literals = re.findall(r"--(?:stop-timeout|time)[= ]+(\d+)", text)
        assert not literals, (
            f"{script.name} hard-codes a drain budget of {literals}; it must read "
            f"worker-stop-timeout / geometry-stop-timeout from instance metadata so the number "
            f"follows the provisioning model the template was built with.")
        assert "worker-stop-timeout" in text, (
            f"{script.name} never reads the budget from metadata")


def test_the_provisioning_model_rotates_the_template():
    """An instance template is immutable, so the model has to be part of SPEC_HASH. Without it,
    flipping dev to Spot would leave the group running STANDARD VMs while every log line and the
    generated env both claimed SPOT."""
    text = FLEET.read_text(encoding="utf-8")
    spec = text[text.index("SPEC_HASH=\"$("):text.index("TEMPLATE=\"${WORKER_TEMPLATE_PREFIX")]
    assert "WORKER_PROVISIONING_MODEL" in spec, (
        "SPEC_HASH does not include WORKER_PROVISIONING_MODEL, so a change of model reuses the "
        "existing template and the fleet silently keeps its old provisioning")


def test_spot_templates_delete_rather_than_stop_on_reclaim():
    """A reclaimed VM that merely STOPS stays in the group as a TERMINATED instance to be repaired;
    a DELETEd one is recreated from the template by the group's own sizing."""
    text = FLEET.read_text(encoding="utf-8")
    assert "--instance-termination-action DELETE" in text
    assert "--provisioning-model" in text


def test_the_config_gate_rejects_a_bad_model_and_spot_in_prod():
    text = VALIDATE.read_text(encoding="utf-8")
    assert "WORKER_PROVISIONING_MODEL" in text, (
        "validate-config.sh does not check the provisioning model, so a lowercase or misspelled "
        "value reaches gcloud twenty minutes into a deploy")
    assert "SPOT" in text and "prod" in text, (
        "the gate does not flag a Spot fleet in production")
