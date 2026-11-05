# Responsibility: Pin a worker's object store and deployment names to the instance metadata the deploy writes, never to the env object alone.
# Boundaries: static reads of create-worker-fleet.sh, create-api-service.sh and startup.sh, plus one run of startup.sh's
# append loop under bash with a fake metadata server; nothing in the cloud is touched.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
FLEET = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
API = (SCRIPTS / "create-api-service.sh").read_text(encoding="utf-8")
STARTUP = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text(encoding="utf-8")

# THE DEFECT THIS PINS. A personal environment's env object (gs://dev-<slug>-transfer-.../worker.env)
# is published ONCE, from .env.example, when its fleet is first created, and never refreshed.
# startup.sh took only the database and the broker from metadata, so dev-areen's workers pointed
# the object store at localhost:9000 / mesh-artifacts: each geometry check was taken off the queue,
# could not fetch the upload, could not store its own failure, and the check sat "pending" until the
# API gave up with "the drawing took too long" (2026-10-11). The API service had the right store
# all along - from the deployment env, which the fleet stage loads too.

#: metadata key -> the worker's environment variable it becomes
WORKER_KEYS = {
    "deployment-id": "DEPLOYMENT_ID",
    "minio-endpoint": "MINIO_ENDPOINT",
    "minio-public-endpoint": "MINIO_PUBLIC_ENDPOINT",
    "minio-access-key": "MINIO_ACCESS_KEY",
    "minio-bucket": "MINIO_BUCKET",
    "minio-region": "MINIO_REGION",
    "minio-secure": "MINIO_SECURE",
    "gcp-mesh-bucket": "GCP_MESH_BUCKET",
    "cloudrun-job": "CLOUDRUN_JOB",
}


def _metadata_block() -> str:
    start = FLEET.index("WORKER_METADATA=(")
    return FLEET[start:FLEET.index("\n)\n", start)]


def _metadata_entries() -> dict[str, str]:
    """`key=value-expression` entries of the WORKER_METADATA array, comments dropped."""
    out = {}
    for line in _metadata_block().splitlines()[1:]:
        found = re.match(r'\s*"([a-z0-9-]+)=(.*)"\s*$', line)
        if found:
            out[found.group(1)] = found.group(2)
    return out


def _append_loop() -> str:
    start = STARTUP.index('for pair in "deployment-id:DEPLOYMENT_ID"')
    return STARTUP[start:STARTUP.index("\ndone\n", start) + len("\ndone\n")]


def test_the_fleet_template_carries_the_store_and_the_deployment_names():
    entries = _metadata_entries()
    for key in WORKER_KEYS:
        assert key in entries, (
            f"create-worker-fleet.sh writes no `{key}` into the template, so a personal environment's "
            "worker keeps whatever .env.example says (localhost:9000 for the store)")
    # from the variables the API stage uses - one deployment env, two consumers that must agree
    assert "${MINIO_ENDPOINT" in entries["minio-endpoint"]
    assert "${MINIO_BUCKET" in entries["minio-bucket"]
    assert "${MINIO_ACCESS_KEY" in entries["minio-access-key"]
    assert "${GCP_MESH_BUCKET" in entries["gcp-mesh-bucket"]
    assert "${CLOUDRUN_MESH_JOB" in entries["cloudrun-job"]
    assert "${DEPLOYMENT_ID" in entries["deployment-id"]


def test_startup_turns_every_key_into_the_workers_environment():
    loop = _append_loop()
    for key, var in WORKER_KEYS.items():
        assert f'"{key}:{var}"' in loop, f"startup.sh never reads `{key}` into {var}"
    # after the env object is fetched, so the metadata's line is the one that wins
    assert STARTUP.index("gcloud storage cp \"${ENV_URI}\" /etc/hexera/worker.env") < STARTUP.index(loop)


def test_the_worker_gets_every_plain_store_setting_the_api_gets():
    """Parity with create-api-service.sh: whatever non-secret object-store setting the API is given,
    the worker is given too - a store the API writes to and the worker cannot read is this bug."""
    api_store = set(re.findall(r'"(MINIO_[A-Z_]+)=\$\{', API))
    secret = {"MINIO_SECRET_KEY"}
    assert api_store, "found no MINIO_* settings in create-api-service.sh - the scan subject is empty"
    missing = (api_store - secret) - set(WORKER_KEYS.values())
    assert not missing, f"the API is given {sorted(missing)} and the worker is not"


def test_the_secret_half_never_enters_metadata():
    entries = _metadata_entries()
    for key, value in entries.items():
        assert "MINIO_SECRET_KEY}" not in value and "MINIO_SECRET_KEY:-" not in value, (
            f"`{key}` puts the store's secret in the template, where anyone who can describe the "
            "instance reads it; it travels as a Secret Manager name (minio-secret-key-secret)")


def _run_loop(attributes: dict[str, str], tmp_path: Path) -> list[str]:
    env_file = tmp_path / "worker.env"
    env_file.write_text("MINIO_ENDPOINT=localhost:9000\nMINIO_BUCKET=mesh-artifacts\n", encoding="utf-8")
    cases = "".join(f'    {k}) printf %s {v!r} ;;\n' for k, v in attributes.items())
    script = ("set -euo pipefail\n"
              "md() {\n  case \"$1\" in\n" + cases + "    *) return 22 ;;\n  esac\n}\n"
              + _append_loop().replace("/etc/hexera/worker.env", str(env_file)))
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return env_file.read_text(encoding="utf-8").splitlines()


def test_a_stated_store_overrides_the_env_object(tmp_path):
    lines = _run_loop({"minio-endpoint": "storage.googleapis.com", "minio-bucket": "dev-x-artifacts",
                       "minio-secure": "true", "deployment-id": "dev-x", "cloudrun-job": "dev-x-mesh"},
                      tmp_path)
    # docker's --env-file takes the LAST line for a name, so the appended value is the one in force
    last = {}
    for line in lines:
        name, value = line.split("=", 1)
        last[name] = value
    assert last["MINIO_ENDPOINT"] == "storage.googleapis.com"
    assert last["MINIO_BUCKET"] == "dev-x-artifacts"
    assert last["MINIO_SECURE"] == "true"
    assert last["DEPLOYMENT_ID"] == "dev-x"
    assert last["CLOUDRUN_JOB"] == "dev-x-mesh"


def test_an_unstated_or_absent_key_leaves_the_env_object_alone(tmp_path):
    # empty values (a deployment that states no store) and keys a pre-existing template does not
    # have (the metadata server's 404) both append nothing - and neither stops the boot
    lines = _run_loop({"minio-endpoint": "", "minio-bucket": ""}, tmp_path)
    assert lines == ["MINIO_ENDPOINT=localhost:9000", "MINIO_BUCKET=mesh-artifacts"]
