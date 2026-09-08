# Responsibility: Verify a blank project is provisioned without building an image, a supplied tier only validated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"

# A fake gcloud that logs every call and answers the read paths the scripts use. Discovery reports
# NO mesh job (fresh mode must not need one); resource-describe calls report "absent" so create
# paths run; the digest resolver returns a pinned digest so job/image deploys proceed.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "auth print-access-token"; then echo "fake-token"; exit 0; fi
if has "auth list"; then echo "deployer@example.com"; exit 0; fi
if has "config get-value auth/impersonate_service_account"; then echo "(unset)"; exit 0; fi
if has "config get-value project"; then echo "independent-org-mesh"; exit 0; fi
if has "config get-value run/region"; then echo "europe-west1"; exit 0; fi
if has "projects describe"; then echo "778899"; exit 0; fi
if has "artifacts docker images describe"; then
  if has "fully_qualified_digest"; then echo "REG/app@sha256:deadbeef"; exit 0; fi
  exit 1   # image not present yet -> build path runs
fi
# discovery: report NO mesh job / NO exchange bucket so fresh mode generates, existing mode dies
if has "run jobs list"; then exit 0; fi
if has "run jobs describe"; then exit 1; fi
if has "storage buckets list"; then exit 0; fi
if has "storage buckets describe"; then exit 1; fi
if has "iam service-accounts describe"; then
  # honour a preset "already exists" set for idempotency tests
  for e in ${FAKE_EXISTING_SAS:-}; do case "$ARGS" in *"$e"*) exit 0;; esac; done
  exit 1
fi
if has "secrets describe"; then exit 1; fi
if has "secrets versions list"; then exit 0; fi
if has "builds submit"; then exit 0; fi
exit 0
"""

_FAKE_ENVSUBST = r"""#!/usr/bin/env python3
import os, re, sys
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\$(\w+)",
                        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                        sys.stdin.read()))
"""


def _bindir(tmp_path):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (b / "gcloud").write_text(_FAKE_GCLOUD); (b / "gcloud").chmod(0o755)
    (b / "envsubst").write_text(_FAKE_ENVSUBST); (b / "envsubst").chmod(0o755)
    return b


INDEP = {
    "DEPLOYMENT_ID": "isolated-deploy",
    "GCP_PROJECT_ID": "independent-org-mesh", "GCP_PROJECT_NUMBER": "778899",
    "GCP_REGION": "europe-west1", "ARTIFACT_REGISTRY_REPOSITORY": "isolated-deploy",
    "CLOUDRUN_MESH_JOB": "isolated-deploy-mesh",
    "MESH_SERVICE_ACCOUNT": "isolated-deploy-mesh",
    "GCP_MESH_BUCKET": "isolated-deploy-exchange-778899",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created", "MESH_BUCKET_DISPOSITION": "created",
    "NEON_DATABASE_SECRET": "database-url", "UPSTASH_REDIS_SECRET": "redis-url",
    "TAVILY_API_KEY_SECRET": "tavily-api-key", "DEEPSEEK_API_KEY_SECRET": "deepseek-api-key",
    "DEEPINFRA_API_KEY_SECRET": "deepinfra-api-key", "MESH_API_KEY_SECRET": "mesh-api-key",
    "USER_TOKEN_SECRET_NAME": "user-token-secret",
    "PIPELINE_BACKEND": "celery", "WEB_SEARCH_PROVIDER": "tavily",
    "MESH_IMAGE": "REG/mesh:t",
    "API_CPU": "1", "API_MEMORY": "1Gi", "PIPELINE_CPU": "2", "PIPELINE_MEMORY": "4Gi",
    "PIPELINE_TIMEOUT_SECONDS": "28800", "MESH_CPU": "4", "MESH_MEMORY": "8Gi",
    "MESH_TIMEOUT_SECONDS": "14400", "CORS_ORIGINS": "https://isolated-deploy-api.run.app",
}


def _write_env(tmp_path, overrides=None):
    d = {**INDEP, **(overrides or {})}
    txt = "\n".join(f"{k}={v}" for k, v in d.items()) + "\n"
    p = tmp_path / "generated.env"; p.write_text(txt)
    return p


def _run(script, tmp_path, env_file, extra_env=None, args=()):
    b = _bindir(tmp_path)
    state = tmp_path / "state"; state.mkdir(exist_ok=True)
    # RENDER_DIR keeps rendered manifests in THIS test's tmp_path instead of the checkout's
    # deploy/gcp/.rendered/, which the scripts would otherwise leave behind.
    env = {"PATH": f"{b}:{os.environ.get('PATH','')}", "HOME": str(tmp_path),
           "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1",
           "RENDER_DIR": str(tmp_path / "rendered"),
           "DEPLOY_OUTPUT_DIR": str(tmp_path / "deploy-output"),
           **(extra_env or {})}
    p = subprocess.run(["bash", str(SCRIPTS / script), *args], env=env,
                       capture_output=True, text=True, timeout=120)
    log = (state / "calls.log").read_text() if (state / "calls.log").exists() else ""
    return p, log


# bootstrap: fresh generates the mesh tier; existing needs it supplied






# create-service-accounts: fresh creates the mesh SA; existing validates
def test_fresh_creates_the_mesh_service_account(tmp_path):
    _, log = _run("create-service-accounts.sh", tmp_path, _write_env(tmp_path))
    assert "iam service-accounts create isolated-deploy-mesh" in log, log


def test_existing_validates_but_does_not_create_the_mesh_sa(tmp_path):
    env_file = _write_env(tmp_path, {"MESH_SA_DISPOSITION": "reused"})
    _, log = _run("create-service-accounts.sh", tmp_path, env_file,
                  extra_env={"FAKE_EXISTING_SAS": "isolated-deploy-mesh"})
    assert "service-accounts create isolated-deploy-mesh" not in log


# create-mesh-tier: fresh creates the bucket and the job. The mesh IMAGE is no longer built
# here - it is promoted from the validated release record, so this script must consume a digest
# and must NOT submit a build. (It used to Cloud Build the mesh image at deploy time, which meant
# the image that ran off-box had never been through the native tiers.)
def test_fresh_creates_exchange_bucket_and_mesh_job_without_building_an_image(tmp_path):
    digest = "europe-west1-docker.pkg.dev/independent-org-mesh/isolated-deploy/mesh@sha256:" + "ab" * 32
    _, log = _run("create-mesh-tier.sh", tmp_path, _write_env(tmp_path, {"MESH_IMAGE": digest}))
    assert "storage buckets create gs://isolated-deploy-exchange-778899" in log   # exchange bucket
    assert "run jobs replace" in log                                              # mesh job created
    assert "builds submit" not in log, "the mesh tier built an image at deploy time"


def test_the_mesh_tier_refuses_a_tag_only_image(tmp_path):
    tag = "europe-west1-docker.pkg.dev/independent-org-mesh/isolated-deploy/mesh:latest"
    p, log = _run("create-mesh-tier.sh", tmp_path, _write_env(tmp_path, {"MESH_IMAGE": tag}))
    assert p.returncode != 0
    assert "is a TAG, not a digest" in (p.stdout + p.stderr)
    assert "run jobs replace" not in log


def test_the_mesh_tier_refuses_an_unpromoted_image(tmp_path):
    p, log = _run("create-mesh-tier.sh", tmp_path, _write_env(tmp_path, {"MESH_IMAGE": ""}))
    assert p.returncode != 0
    assert "promote-release.sh" in (p.stdout + p.stderr)


def test_existing_mode_validates_the_mesh_tier_without_creating(tmp_path):
    env_file = _write_env(tmp_path, {"MESH_JOB_DISPOSITION": "reused", "MESH_BUCKET_DISPOSITION": "reused"})
    # existing-mode validation needs the resources to appear present
    b = _bindir(tmp_path)
    (b / "gcloud").write_text(_FAKE_GCLOUD.replace(
        'if has "storage buckets describe"; then exit 1; fi', 'if has "storage buckets describe"; then exit 0; fi'
    ).replace('if has "run jobs describe"; then exit 1; fi', 'if has "run jobs describe"; then echo sa@x; exit 0; fi'))
    (b / "gcloud").chmod(0o755)
    state = tmp_path / "state2"; state.mkdir()
    env = {"PATH": f"{b}:{os.environ.get('PATH','')}", "HOME": str(tmp_path),
           "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1",
           "RENDER_DIR": str(tmp_path / "rendered")}
    p = subprocess.run(["bash", str(SCRIPTS / "create-mesh-tier.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    log = (state / "calls.log").read_text()
    assert p.returncode == 0, p.stderr
    assert "buckets create" not in log and "run jobs replace" not in log and "builds submit" not in log


# apply-iam: the API identity must be able to WRITE the data bucket, not only read it


# apply-iam: fresh grants the mesh SA ONLY the exchange bucket; NEVER a secret
def test_fresh_mesh_identity_gets_exchange_bucket_and_no_secrets(tmp_path):
    _, log = _run("apply-iam.sh", tmp_path, _write_env(tmp_path))
    mesh = "isolated-deploy-mesh@independent-org-mesh.iam.gserviceaccount.com"
    # gets objectAdmin on the exchange bucket
    assert any("buckets add-iam-policy-binding gs://isolated-deploy-exchange-778899" in ln and mesh in ln
               for ln in log.splitlines()), log
    # MUTATION KILL: the mesh identity must receive NO secret binding
    assert not any("secrets add-iam-policy-binding" in ln and mesh in ln for ln in log.splitlines()), \
        "mesh identity was granted a secret - it must be compute-only"


# mesh-job manifest: digest-pinned, maxRetries 0, task 1, mesh SA, no secrets
def test_mesh_job_manifest_is_hardened_and_independent():
    m = (REPO / "deploy" / "gcp" / "cloud-run" / "mesh-job.yaml").read_text()
    # ignore comment lines - the manifest DOCUMENTS the compute-only property in prose
    body = "\n".join(ln for ln in m.splitlines() if not ln.lstrip().startswith("#"))
    assert "maxRetries: 0" in body and "taskCount: 1" in body and "parallelism: 1" in body
    assert "${MESH_IMAGE}" in body and "${MESH_SA_EMAIL}" in body and "${CLOUDRUN_MESH_JOB}" in body
    assert "mesh_runner" in body
    # actual secret wiring must be absent (no secretKeyRef/valueFrom, no db/redis/model env)
    assert "secretKeyRef" not in body and "valueFrom" not in body
    assert "DATABASE_URL" not in body and "REDIS_URL" not in body


# config validation


def test_validate_config_accepts_a_coherent_independent_config(tmp_path):
    p, _ = _run("validate-config.sh", tmp_path, _write_env(tmp_path))
    assert p.returncode == 0, p.stderr


# release archive: deployment must NOT require .git
def test_image_tagging_works_without_git_history(tmp_path):
    # A copy of the repo WITHOUT .git, exercised through lib.sh's source_tag. The fallback reads
    # the ONE version authority - src/meshpipeline/__init__.py - because pyproject carries no static
    # version (it derives one from that same attribute). The literal here is deliberately NOT the
    # real product version: it proves the tag is READ from the file rather than restated in lib.sh.
    fake_repo = tmp_path / "archive"; (fake_repo / "deploy" / "gcp" / "scripts").mkdir(parents=True)
    (fake_repo / "src" / "meshpipeline").mkdir(parents=True)
    (fake_repo / "src" / "meshpipeline" / "__init__.py").write_text('__version__ = "7.7.7"\n')
    (fake_repo / "pyproject.toml").write_text('[project]\ndynamic = ["version"]\n')
    for f in ("lib.sh",):
        (fake_repo / "deploy" / "gcp" / "scripts" / f).write_text((SCRIPTS / f).read_text())
    probe = fake_repo / "probe.sh"
    probe.write_text('set -euo pipefail\nsource "$(dirname "$0")/deploy/gcp/scripts/lib.sh"\n'
                     'REPO_ROOT="$(dirname "$0")"\nsource_tag\n')
    p = subprocess.run(["bash", str(probe)], capture_output=True, text=True,
                       cwd=str(fake_repo), env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "release-7.7.7", p.stdout
    # and an explicit RELEASE_TAG wins
    p2 = subprocess.run(["bash", str(probe)], capture_output=True, text=True, cwd=str(fake_repo),
                        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "RELEASE_TAG": "v9"})
    assert p2.stdout.strip() == "v9"


# deployment-state manifest: what was provisioned, its ownership, and never a credential
def test_deployment_state_manifest_records_mesh_ownership_only(tmp_path):
    env_file = _write_env(tmp_path)
    p, _ = _run("write-deployment-state.sh", tmp_path, env_file)
    state = tmp_path / "deploy-output" / "deployment.json"
    assert state.exists(), p.stderr
    import json
    doc = json.loads(state.read_text())
    assert doc["deployment_id"] == "isolated-deploy"
    assert doc["resources"]["mesh_job"]["disposition"] == "created"
    # A deployment that declares no database and no worker fleet provisions neither, so neither is
    # recorded: the manifest states what exists, and an empty entry would read as one that does.
    assert set(doc["resources"]) == {"artifact_registry", "mesh_job", "exchange_bucket",
                                     "mesh_service_account"}
    # ALL THREE images, unconditionally - the app and console digests are deployed state now, the
    # same as the mesh digest: the app runs the pre-deploy migration and the queue-depth publisher,
    # and the console is the browser front door, so a record naming only the mesh image could not
    # say which build did either.
    assert set(doc["images"]) == {"mesh", "app", "console"}
    blob = json.dumps(doc)
    for leak in ("postgres://", "postgresql://", "rediss://", "sk-", "BEGIN ", "secret_names"):
        assert leak not in blob, f"deployment manifest leaked {leak}"


def test_deployment_state_manifest_records_a_declared_database_by_address_not_by_url(tmp_path):
    # The other half: a deployment that DOES declare the two optional tiers records them - and the
    # database still appears as an address plus a secret NAME, never as a DSN carrying a password.
    env_file = _write_env(tmp_path, {
        "MIGRATE_DB_HOST": "10.66.0.3", "MIGRATE_DB_NAME": "meshpipeline",
        "POSTGRES_PASSWORD_SECRET": "postgres-password",
        "CLOUDRUN_MIGRATE_JOB": "isolated-deploy-migrate",
        "WORKER_MIG": "isolated-workers", "WORKER_MIG_ZONE": "europe-west1-b",
        "CLOUDRUN_QUEUE_DEPTH_JOB": "isolated-deploy-queue-depth",
        # QUOTED, the way bootstrap-env.sh writes it: a cron expression is the one value in this
        # file that would otherwise be glob-expanded by the shell that sources it.
        "QUEUE_DEPTH_SCHEDULE": '"* * * * *"', "QUEUE_NAME": "simulation_jobs",
    })
    p, _ = _run("write-deployment-state.sh", tmp_path, env_file)
    import json
    state = tmp_path / "deploy-output" / "deployment.json"
    assert state.exists(), p.stderr
    doc = json.loads(state.read_text())
    assert doc["resources"]["migration_job"]["database"] == "10.66.0.3:5432/meshpipeline"
    assert doc["resources"]["migration_job"]["password_secret"] == "postgres-password"
    assert doc["resources"]["queue_depth_job"]["scales"] == "isolated-workers"
    blob = json.dumps(doc)
    for leak in ("postgres://", "postgresql://", "rediss://", "password="):
        assert leak not in blob, f"deployment manifest leaked {leak}"


# purge is ownership-driven: it must never delete a 'reused' resource


# the operator always gets the application URL


# architecture guard: production settings hardcode no Cloud Run job identity. An empty default
# forces the deployment to supply its own; a baked-in name would silently dispatch work to whatever
# job that name resolves to in the operator's project.
#
# The claim is about the DECLARED default, so the environment is removed before it is read. Reading
# the live module instead made the guard fail on a correctly configured machine: `make dev-up`
# requires CLOUDRUN_JOB, and a developer who had set it - or run `make mesh-setup`, which writes it -
# failed `make check` for having configured the product properly.
def test_no_cloud_run_job_default_in_production_settings(monkeypatch):
    import importlib

    import meshpipeline.settings.providers as p
    monkeypatch.delenv("CLOUDRUN_JOB", raising=False)
    try:
        assert importlib.reload(p).CLOUDRUN_JOB == ""
    finally:
        monkeypatch.undo()
        importlib.reload(p)


def test_a_configured_cloud_run_job_is_read_from_the_environment(monkeypatch):
    # The other half of the same contract, and the reason the guard above must isolate: a supplied
    # value is honoured, so the absence of a default cannot be confused with ignoring configuration.
    import importlib

    import meshpipeline.settings.providers as p
    monkeypatch.setenv("CLOUDRUN_JOB", "operator-supplied-mesh")
    try:
        assert importlib.reload(p).CLOUDRUN_JOB == "operator-supplied-mesh"
    finally:
        monkeypatch.undo()
        importlib.reload(p)
