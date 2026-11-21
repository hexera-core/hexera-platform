# Responsibility: Pin a worker's database address to the instance metadata the deploy writes, from the same variables the API gets.
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

# THE DEFECT THIS PINS. `database-url` is deliberately empty (a DSN carries the password), so the
# worker composes its connection from POSTGRES_* in the env object - which, in a personal
# environment, is .env.example with POSTGRES_HOST=localhost. dev-raymond's worker took a job off the
# queue, failed on 127.0.0.1:5432, could not record the failure, and the job stayed "pending"
# (2026-11-21). The API had the right database all along, from MIGRATE_DB_* in the deployment env.

#: metadata key -> (the worker's variable, the deployment variable the API's line is built from)
WORKER_KEYS = {
    "postgres-host": ("POSTGRES_HOST", "MIGRATE_DB_HOST"),
    "postgres-port": ("POSTGRES_PORT", "MIGRATE_DB_PORT"),
    "postgres-db": ("POSTGRES_DB", "MIGRATE_DB_NAME"),
    "postgres-user": ("POSTGRES_USER", "MIGRATE_DB_USER"),
}


def _metadata_entries() -> dict[str, str]:
    start = FLEET.index("WORKER_METADATA=(")
    block = FLEET[start:FLEET.index("\n)\n", start)]
    out = {}
    for line in block.splitlines()[1:]:
        found = re.match(r'\s*"([a-z0-9-]+)=(.*)"\s*$', line)
        if found:
            out[found.group(1)] = found.group(2)
    return out


def _append_loop() -> str:
    start = STARTUP.index('for pair in "deployment-id:DEPLOYMENT_ID"')
    return STARTUP[start:STARTUP.index("\ndone\n", start) + len("\ndone\n")]


def test_the_template_carries_the_database_address_from_the_apis_variables():
    entries = _metadata_entries()
    for key, (_, deploy_var) in WORKER_KEYS.items():
        assert key in entries, f"create-worker-fleet.sh writes no `{key}`, so the worker dials localhost"
        assert f"${{{deploy_var}" in entries[key], f"`{key}` is not built from {deploy_var}, as the API's is"
        assert f'{WORKER_KEYS[key][0]}=${{{deploy_var}' in API, f"the API no longer reads {deploy_var}"


def test_no_database_credential_enters_metadata():
    for key, value in _metadata_entries().items():
        assert "POSTGRES_PASSWORD}" not in value and "POSTGRES_PASSWORD:-" not in value, (
            f"`{key}` puts the database password in the template; it travels as a Secret Manager name")


def test_startup_turns_every_key_into_the_workers_environment():
    loop = _append_loop()
    for key, (var, _) in WORKER_KEYS.items():
        assert f'"{key}:{var}"' in loop, f"startup.sh never reads `{key}` into {var}"


def _run_loop(attributes: dict[str, str], tmp_path: Path) -> dict[str, str]:
    env_file = tmp_path / "worker.env"
    env_file.write_text("POSTGRES_HOST=localhost\nPOSTGRES_DB=meshpipeline\n", encoding="utf-8")
    cases = "".join(f"    {k}) printf %s {v!r} ;;\n" for k, v in attributes.items())
    script = ("set -euo pipefail\n"
              "md() {\n  case \"$1\" in\n" + cases + "    *) return 22 ;;\n  esac\n}\n"
              + _append_loop().replace("/etc/hexera/worker.env", str(env_file)))
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    last = {}  # docker's --env-file takes the LAST line for a name
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, value = line.split("=", 1)
        last[name] = value
    return last


def test_a_stated_database_overrides_the_env_objects_localhost(tmp_path):
    last = _run_loop({"postgres-host": "10.1.2.3", "postgres-port": "5432",
                      "postgres-db": "meshpipeline_x", "postgres-user": "meshpipeline"}, tmp_path)
    assert last["POSTGRES_HOST"] == "10.1.2.3"
    assert last["POSTGRES_DB"] == "meshpipeline_x"


def test_an_unstated_database_leaves_the_env_object_in_force(tmp_path):
    """A template made before these keys existed (or a deployment with no MIGRATE_DB_HOST) adds nothing."""
    last = _run_loop({}, tmp_path)
    assert last["POSTGRES_HOST"] == "localhost"
