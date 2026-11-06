# Responsibility: Verify the worker fleet's warm floor is a PRODUCTION default, not a universal one.
# Boundaries: it RUNS bootstrap-env.sh's emit_env as a program and reads the two scripts; it deploys nothing.
"""The floor is a standing bill, so it is tested like one.

`WORKER_MIG_MIN_REPLICAS` is the number of e2-standard-4 VMs the autoscaler holds against an EMPTY
queue. One of them is roughly 24 core-hours and 96 GiB-hours a day, charged whether or not anybody
is simulating anything - and a floor of 1 defaulted for every environment meant shared dev plus one
per `dev-<slug>` all billed a warm worker in perpetuity. Production wants the warm worker; a dev
environment that is idle most of the day does not, and the off-fleet queue-depth publisher exists
precisely so a group sitting at zero can still be woken.

So: prod defaults to 1, everything else defaults to 0, and an explicitly stated value wins either
way. These tests execute the generator rather than grep it, because the thing that matters is the
number that lands in generated.env.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap-env.sh"
PUBLISHER = SCRIPTS / "create-queue-depth-publisher.sh"
FLEET = SCRIPTS / "create-worker-fleet.sh"
PROD_ENV = REPO / "deploy" / "gcp" / "generated.prod.env"


def _emit_env_body() -> str:
    """`emit_env` alone, sliced the way test_personal_environments.py slices it."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    return text[text.index("emit_env() {"):text.index("\nENVFILE\n}")] + "\nENVFILE\n}\n"


def _generated(tmp_path: Path, **env: str) -> dict[str, str]:
    """Run emit_env with the given environment and parse what it wrote.

    Every other variable the heredoc interpolates is left unset, which expands to empty - this
    asks one question (what floor does this target get?) without standing up discovery, and
    deliberately runs WITHOUT `set -u` for that reason.
    """
    script = tmp_path / "emit.sh"
    script.write_text(_emit_env_body() + "\nemit_env\n", encoding="utf-8")
    p = subprocess.run(
        ["bash", str(script)],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **env},
        capture_output=True, text=True,
    )
    assert p.returncode == 0, f"emit_env failed: {p.stderr}"
    out: dict[str, str] = {}
    for line in p.stdout.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k] = v.strip('"')
    return out


# ---------------------------------------------------------------------------------------------
# 1. the default follows APP_ENV


def test_shared_dev_gets_no_warm_worker(tmp_path: Path):
    """An empty slug is shared dev and the picker pins APP_ENV=dev for it."""
    got = _generated(tmp_path, APP_ENV="dev")["WORKER_MIG_MIN_REPLICAS"]
    assert got == "0", (
        f"shared dev would hold {got} worker VM(s) against an empty queue. The floor is billed "
        f"around the clock whether or not anything is queued, and the queue-depth publisher runs "
        f"off the fleet so a group at zero can still be woken - there is nothing to keep warm for.")


def test_a_personal_environment_gets_no_warm_worker(tmp_path: Path):
    """A `dev-<slug>` run shares APP_ENV=dev with shared dev, and there is one per developer -
    this is the default that multiplies."""
    assert _generated(tmp_path, APP_ENV="dev")["WORKER_MIG_MIN_REPLICAS"] == "0"


def test_an_unstated_app_env_is_treated_as_dev(tmp_path: Path):
    """A standalone `make bootstrap` on a laptop states no APP_ENV. The cheap reading is the safe
    one: the expensive default must be asked for, never inherited by omission."""
    assert _generated(tmp_path)["WORKER_MIG_MIN_REPLICAS"] == "0"


def test_production_keeps_its_warm_worker(tmp_path: Path):
    """The first job of the morning should not wait for a VM to boot and an image to pull. Prod is
    the one environment where the standing cost buys something, so it is the one that defaults to
    a floor - and a v* tag is the only thing that sets APP_ENV=prod."""
    got = _generated(tmp_path, APP_ENV="prod")["WORKER_MIG_MIN_REPLICAS"]
    assert got == "1", (
        f"production's warm worker floor is {got}. Lowering dev to zero must not have taken prod "
        f"with it: the saving being chased is the idle dev fleets, not prod's first-job latency.")


# ---------------------------------------------------------------------------------------------
# 2. an explicit value still wins, in both directions


def test_a_stated_floor_survives_in_a_dev_environment(tmp_path: Path):
    """This is what "unless explicitly set" means: a dev environment that genuinely wants a warm
    worker asks for one, and the new default does not overrule the asking."""
    got = _generated(tmp_path, APP_ENV="dev", WORKER_MIG_MIN_REPLICAS="2")
    assert got["WORKER_MIG_MIN_REPLICAS"] == "2"


def test_a_stated_zero_survives_in_production(tmp_path: Path):
    """`${VAR:-default}` treats an EMPTY value as absent but a stated 0 as stated, so a prod
    deployment can still be taken to zero deliberately."""
    got = _generated(tmp_path, APP_ENV="prod", WORKER_MIG_MIN_REPLICAS="0")
    assert got["WORKER_MIG_MIN_REPLICAS"] == "0", (
        "a deliberate 0 was overwritten with prod's default of 1, which makes the floor "
        "impossible to lower from the deploy that owns it")


def test_a_stated_floor_round_trips_a_regeneration(tmp_path: Path):
    """generated.env is re-sourced with `set -a` and regenerated on every deploy (bootstrap-env.sh
    reuses an existing file), so a floor already recorded has to survive the next run or the
    setting silently reverts to the default on an unrelated deploy."""
    first = _generated(tmp_path, APP_ENV="dev", WORKER_MIG_MIN_REPLICAS="3")
    second = _generated(tmp_path, APP_ENV="dev", **{
        "WORKER_MIG_MIN_REPLICAS": first["WORKER_MIG_MIN_REPLICAS"]})
    assert second["WORKER_MIG_MIN_REPLICAS"] == "3"


# ---------------------------------------------------------------------------------------------
# 3. the two consumers agree with the generator


def test_no_consumer_falls_back_to_a_warm_floor():
    """Both the fleet stage and the publisher stage substitute their own default when the env
    states none. Either one defaulting to 1 reintroduces the standing charge behind the
    generator's back - the publisher's did, which is why this test exists."""
    for script in (PUBLISHER, FLEET):
        text = script.read_text(encoding="utf-8")
        bad = [ln.strip() for ln in text.splitlines()
               if "WORKER_MIG_MIN_REPLICAS:-" in ln and "WORKER_MIG_MIN_REPLICAS:-0}" not in ln
               and not ln.strip().startswith("#")]
        assert not bad, (
            f"{script.name} defaults the warm floor to something other than 0: {bad}. An unstated "
            f"floor is no floor; a warm pool is stated by the deployment that wants it.")


def test_production_state_still_records_its_floor():
    """prod's committed state file is the record of what prod actually runs. The default moved, so
    this is the file that has to keep saying 1 out loud rather than relying on it."""
    recorded = dict(
        ln.split("=", 1) for ln in PROD_ENV.read_text(encoding="utf-8").splitlines()
        if "=" in ln and not ln.startswith("#"))
    assert recorded.get("WORKER_MIG_MIN_REPLICAS") == "1", (
        "generated.prod.env no longer records prod's warm floor, so prod's standing worker is "
        "carried only by a default that dev deliberately differs from")
