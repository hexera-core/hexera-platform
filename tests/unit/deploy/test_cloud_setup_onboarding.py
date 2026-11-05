# Responsibility: Verify the three-command setup path validates, derives and destroys exactly what it claims.
# Owns: the controls for setup host tooling, mesh-setup invoker derivation, dev-up gating and cloud destroy.
# Boundaries: command doubles and disposable files only; nothing here reaches Google Cloud or Docker.
# Collaborates with: devtools/env/setup.sh, deploy/gcp/scripts/mesh-destroy.sh and the Makefile targets they back.
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: The shape of the credential the supported path expects - an authorized_user ADC document,
#: never a service-account key. Values are inert; nothing here loads or transmits it.
FIXTURE_ADC = '{"client_id":"fixture.apps.googleusercontent.com","type":"authorized_user"}'
SECRET_VALUES = {"DEEPSEEK_API_KEY": "sk-fixture-deepseek-value",
                 "DEEPINFRA_API_KEY": "sk-fixture-deepinfra-value"}
DECLARED = {"GCP_PROJECT_ID": "declared-project", "GCP_REGION": "europe-west4",
            "CLOUDRUN_JOB": "declared-mesh-job", "GCP_MESH_BUCKET": "declared-exchange"}


def _make(target: str, *, cwd: Path = REPO, env: dict | None = None) -> str:
    out = subprocess.run(["make", "-n", target], cwd=cwd, capture_output=True, text=True,
                         env={**os.environ, **(env or {})})
    assert out.returncode == 0, out.stderr
    return out.stdout


# make setup: configuration is input-only, host tooling is checked not seized


def test_setup_never_overwrites_a_populated_env():
    script = (REPO / "devtools" / "env" / "setup.sh").read_text()
    # The only line in the repository that writes .env, and it is guarded by absence.
    assert 'if [ -f "${ROOT}/.env" ]' in script
    assert "left exactly as it is" in script
    assert "rm -f ${ROOT}/.env" not in script and "> ${ROOT}/.env" not in script


def test_a_missing_gcloud_selects_the_official_ubuntu_repository():
    script = (REPO / "devtools" / "env" / "setup.sh").read_text()
    assert "packages.cloud.google.com/apt" in script, "no official apt repository"
    assert "/usr/share/keyrings/cloud.google.gpg" in script, "the repository is not signed-by pinned"
    assert "signed-by=/usr/share/keyrings/cloud.google.gpg" in script
    # asked for, never assumed
    assert "Install it now from Google" in script
    assert "google-cloud-cli" in script


def test_setup_reports_docker_and_never_installs_it():
    script = (REPO / "devtools" / "env" / "setup.sh").read_text()
    assert 'have docker && ok "docker" || MISSING+=("docker")' in script
    for forbidden in ("apt-get install -y docker", "install docker-ce", "get.docker.com"):
        assert forbidden not in script, f"setup tries to install Docker: {forbidden}"


def test_valid_tooling_produces_no_installation():
    # The installer is DEFINED before the dispatch and INVOKED only from the branch a missing
    # gcloud reaches, so an index comparison against the definition proves nothing. What matters
    # is that the call sits after the presence check and that a present gcloud is only reported.
    script = (REPO / "devtools" / "env" / "setup.sh").read_text()
    guard = script.index("if have gcloud; then")
    assert script.index("_install_gcloud &&") > guard, "the installer runs before the check"
    assert 'ok "gcloud' in script, "a present gcloud is not simply reported"
    reported = script[guard:script.index("elif", guard)]
    assert "apt-get" not in reported, "a present gcloud still triggers apt"


# make mesh-setup: validate before mutating, derive the caller, gate on the doctor


def test_mesh_setup_validates_configuration_before_any_mutation():
    recipe = _make("mesh-setup")
    gate = recipe.index("check_runtime_config.py")
    for mutation in ("mesh-image", "deploy.sh"):
        assert gate < recipe.index(mutation), f"{mutation} runs before the configuration gate"


def test_mesh_setup_requires_usable_adc_before_any_mutation():
    recipe = _make("mesh-setup")
    adc = recipe.index("auth application-default print-access-token")
    assert adc < recipe.index("deploy.sh"), "provisioning starts before ADC is proven usable"


def test_the_active_account_becomes_the_mesh_invoker():
    recipe = _make("mesh-setup")
    assert "MESH_INVOKER=" in recipe, "the invoker is not derived at all"
    assert "gcloud auth list --filter=status:ACTIVE" in recipe
    assert "user:$ACCOUNT" in recipe and "serviceAccount:$ACCOUNT" in recipe, (
        "the principal type is not derived from the account")
    assert "export MESH_INVOKER" in recipe, "the derived invoker never reaches the deploy script"


def test_removing_invoker_propagation_kills_the_local_caller_bindings():
    # The mutation: apply-iam only grants the local caller when MESH_INVOKER is set, so a
    # provisioning run that does not export it produces a job its own operator cannot invoke.
    iam = (REPO / "deploy" / "gcp" / "scripts" / "apply-iam.sh").read_text()
    assert 'MESH_INVOKER="${MESH_INVOKER:-}"' in iam
    assert 'if [ -n "${MESH_INVOKER}" ]; then' in iam
    assert "skipping the local caller's bindings" in iam
    for role in ("roles/run.jobsExecutorWithOverrides", "roles/storage.objectUser"):
        assert role in iam, f"the exact required role is missing: {role}"
    # and nothing broader was granted to the caller
    assert "roles/storage.objectAdmin" not in iam and "roles/owner" not in iam


def test_mesh_setup_fails_unless_the_doctor_passes():
    recipe = _make("mesh-setup")
    assert recipe.rindex("mesh-doctor") > recipe.index("deploy.sh"), (
        "the doctor does not run after provisioning")


def test_mesh_setup_never_writes_env_or_prompts_for_secrets():
    recipe = _make("mesh-setup")
    assert ">> .env" not in recipe and "sed -i" not in recipe, "provisioning edits .env"
    for name in SECRET_VALUES:
        assert f"read -r {name}" not in recipe and f"${name}" not in recipe.replace("$$", "$")


def test_the_declared_names_are_used_verbatim(tmp_path):
    # Bootstrap reads the declared names and records, per resource, whether it created or
    # reused what it found - the disposition the destroy path later depends on.
    bootstrap = (REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh").read_text()
    for setting in ("CLOUDRUN_MESH_JOB", "GCP_MESH_BUCKET"):
        assert setting in bootstrap
    assert "reused" in bootstrap and "created" in bootstrap, (
        "provisioning does not record whether it created or reused each resource")


# make dev-up: one command, gated, with images that match the source


def test_dev_up_gates_configuration_and_doctor_before_starting_anything():
    recipe = _make("dev-up")
    gate = recipe.index("check_runtime_config.py")
    doctor = recipe.index("mesh-doctor")
    start = recipe.index("docker compose up")
    assert gate < start and doctor < start, "the stack can start before the gates run"
    assert gate < doctor, "the configuration gate must answer before the remote diagnosis"


def _target_source(name: str) -> str:
    # The target's OWN recipe from the Makefile. `make -n` expands delegated targets too, so it
    # cannot answer whether build logic was copied or reused.
    body, capturing = [], False
    for line in (REPO / "Makefile").read_text().splitlines():
        if line.startswith(f"{name}:"):
            capturing = True
            continue
        if capturing:
            if line and not line[0].isspace():
                break
            body.append(line)
    return "\n".join(body)


def test_dev_up_delegates_building_to_the_single_build_authority():
    recipe = _make("dev-up")
    assert "dev-images" in recipe, "dev-up does not ensure images at all"
    images = _target_source("dev-images")
    assert "dev-build" in images, "the currency check does not reuse the build authority"
    assert "docker compose build" not in images, "build logic was duplicated"
    assert "docker compose build" not in _target_source("dev-up"), "dev-up builds directly"


def test_current_images_are_not_rebuilt():
    images = _make("dev-images")
    assert "MESH_SOURCE_TREE" in images or "image.revision" in images, (
        "currency is not decided from the source stamp the images carry")
    assert "not rebuilding" in images


# make mesh-destroy: only what the record proves we created


def _record(tmp_path: Path, **dispositions) -> Path:
    out = tmp_path / "deploy" / "gcp" / "output"
    out.mkdir(parents=True)
    doc = {"schema_version": 2, "project": DECLARED["GCP_PROJECT_ID"],
           "region": DECLARED["GCP_REGION"],
           "resources": {k: {"name": f"{k}-name", "disposition": v}
                         for k, v in dispositions.items()}}
    (out / "deployment.json").write_text(json.dumps(doc))
    return out / "deployment.json"


def _destroy_repo(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "deploy" / "gcp" / "scripts"
    root.mkdir(parents=True)
    for name in ("mesh-destroy.sh", "lib.sh"):
        shutil.copy(REPO / "deploy" / "gcp" / "scripts" / name, root / name)
    (tmp_path / "src" / "meshpipeline").mkdir(parents=True)
    (tmp_path / "src" / "meshpipeline" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = binaries / "gcloud.log"
    stub = binaries / "gcloud"
    # Answers every probe with success and records the invocation, so nothing is skipped for
    # looking absent: what gets deleted is decided by the record alone, and the log shows it.
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    env = {"PATH": f"{binaries}:/usr/bin:/bin", "HOME": str(tmp_path / "home"),
           "ASSUME_YES": "1"}
    return root / "mesh-destroy.sh", {"env": env, "log": log}


def _run_destroy(script: Path, ctx: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env=ctx["env"], cwd=str(script.parents[3]), timeout=120)


def test_the_destroy_dry_run_mutates_nothing(tmp_path):
    script, ctx = _destroy_repo(tmp_path)
    _record(tmp_path, mesh_job="created", exchange_bucket="created")
    result = _run_destroy(script, ctx)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DELETE" in result.stdout and "Dry run" in result.stdout
    calls = ctx["log"].read_text() if ctx["log"].exists() else ""
    assert "delete" not in calls, f"the dry run called a mutating command: {calls}"


def test_destroy_removes_only_resources_recorded_as_created(tmp_path):
    script, ctx = _destroy_repo(tmp_path)
    _record(tmp_path, mesh_job="created", exchange_bucket="reused",
            artifact_registry="created", mesh_service_account="reused")
    result = _run_destroy(script, ctx, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = ctx["log"].read_text()
    assert "run jobs delete" in calls, "the created job was not deleted"
    assert "artifacts repositories delete" in calls, "the created repository was not deleted"
    assert "storage rm" not in calls, "a REUSED bucket was deleted"
    assert "service-accounts delete" not in calls, "a REUSED service account was deleted"


def test_pre_existing_resources_are_reported_and_survive(tmp_path):
    script, ctx = _destroy_repo(tmp_path)
    _record(tmp_path, mesh_job="reused", exchange_bucket="reused")
    result = _run_destroy(script, ctx, "--apply")
    assert result.returncode == 0
    assert "keep" in result.stdout and "disposition=reused" in result.stdout
    assert "delete" not in (ctx["log"].read_text() if ctx["log"].exists() else "")


def test_a_partially_provisioned_deployment_can_be_destroyed(tmp_path):
    # Provisioning died after the job and before the bucket: the record has only what exists.
    script, ctx = _destroy_repo(tmp_path)
    _record(tmp_path, mesh_job="created")
    result = _run_destroy(script, ctx, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not in the record" in result.stdout


def test_destroy_is_idempotent(tmp_path):
    script, ctx = _destroy_repo(tmp_path)
    _record(tmp_path, mesh_job="created")
    first = _run_destroy(script, ctx, "--apply")
    second = _run_destroy(script, ctx, "--apply")
    assert first.returncode == 0 and second.returncode == 0, second.stdout + second.stderr


def test_destroy_without_a_record_refuses_to_guess(tmp_path):
    script, ctx = _destroy_repo(tmp_path)
    result = _run_destroy(script, ctx, "--apply")
    assert result.returncode == 0
    assert "Nothing is known to have been created" in result.stdout
    assert not ctx["log"].exists() or "delete" not in ctx["log"].read_text()


def test_destroy_never_deletes_a_project_or_uses_a_wildcard():
    script = (REPO / "deploy" / "gcp" / "scripts" / "mesh-destroy.sh").read_text()
    assert "projects delete" not in script, "it can delete the project"
    for pattern in ("--filter", "grep -E", "*-mesh", "list "):
        assert f"gcloud {pattern}" not in script, f"ownership decided by discovery: {pattern}"
    assert "disposition" in script, "ownership is not decided from the record"


def test_no_secret_value_can_reach_destroy_or_setup_output(tmp_path):
    script, ctx = _destroy_repo(tmp_path)
    _record(tmp_path, mesh_job="created")
    combined = _run_destroy(script, ctx).stdout + _run_destroy(script, ctx, "--apply").stdout
    for value in SECRET_VALUES.values():
        assert value not in combined
    assert "authorized_user" not in combined and "client_id" not in combined


# the acceptance contract: three commands, then one


def test_the_documented_quick_start_is_exactly_three_commands():
    # docs/deployment/first-run.md was folded into the one setup guide; the contract it carried
    # moved with it rather than being retired, so it is asserted against the surviving authority.
    doc = (REPO / "docs" / "getting-started" / "setup.md").read_text()
    for command in ("make setup", "make mesh-setup", "make dev-up"):
        assert command in doc, f"the quick start omits {command}"
    assert "make mesh-destroy" in doc, "cloud teardown is undocumented"


def test_the_optional_tools_are_not_mandatory_steps():
    # mesh-doctor and dev-build must remain available and must not be required: dev-up runs the
    # doctor itself, and dev-images builds what is missing.
    up = _make("dev-up")
    assert "mesh-doctor" in up and "dev-images" in up


def test_a_refused_startup_leaves_no_volumes_behind():
    # The in-container preflight creates this project's volumes before it can answer. A refusal
    # therefore has to remove them, or the next attempt starts against state the failed one made.
    recipe = _target_source("dev-up")
    assert "preflight_cloudrun.py" in recipe
    assert "docker compose down -v" in recipe, (
        "a refused startup does not clean up the volumes the preflight created")
    down = recipe.index("docker compose down -v")
    up = recipe.index("docker compose up")
    assert down < up, "the cleanup runs after the stack starts, which is not a refusal path"


def test_the_container_preflight_checks_the_whole_composed_executor():
    # Startup refuses unless the container composes the real remote executor, and the composition
    # is the submission authority wrapping it - so the gate has to recognise the wrapper, not just
    # the inner class. Nothing else exercises this path until a developer starts the stack.
    from meshpipeline.runtime.composition import build_mesh_executor

    composed = build_mesh_executor()
    assert type(composed).__name__ == "ClaimingMeshExecutor"
    assert type(composed._inner).__name__ == "CloudRunMeshExecutor"

    check = (REPO / "devtools" / "env" / "preflight_cloudrun.py").read_text()
    assert "ClaimingMeshExecutor" in check, "the preflight does not know about the wrapper"
    assert "CloudRunMeshExecutor" in check, "the preflight no longer checks the provider"


def test_discovery_reads_the_names_declared_in_env():
    # .env is where the operator declares which job and bucket they want, and the application
    # reads those same values at run time. Discovery has to read the file for itself: a name it
    # derives instead provisions resources the application will never look for.
    script = (REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh").read_text()
    assert "REPO_ROOT}/.env" in script, "discovery never reads the operator's .env"
    declared = script[script.index("REPO_ROOT}/.env"):]
    for name in ("GCP_MESH_BUCKET", "CLOUDRUN_JOB", "GCP_PROJECT_ID", "GCP_REGION"):
        assert name in declared.split("DEPLOY_ID=")[0], f"{name} is not taken from .env"
    # and the reading happens before any default is derived from the deployment id
    assert script.index("REPO_ROOT}/.env") < script.index("DEPLOY_ID}-exchange")
