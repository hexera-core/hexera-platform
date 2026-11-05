# Responsibility: Verify mesh-doctor reports every missing prerequisite and makes its exit code the verdict.
# Boundaries: the diagnosis script only; provisioning and the enforcing dev-up gate are elsewhere.
from __future__ import annotations

import pathlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCTOR = "deploy/gcp/scripts/deploy-doctor.sh"

#: What a fully configured machine looks like to the local half of the doctor. Values are shaped
#: like the real ones and are not real: the point is that none of them is ever printed.
COMPLETE_ENV = {
    "GCP_PROJECT_ID": "example-project",
    "GCP_REGION": "us-central1",
    "CLOUDRUN_JOB": "example-mesh-job",
    "GCP_MESH_BUCKET": "example-mesh-exchange",
}
CREDENTIAL_BODY = '{"client_id":"not-a-real-credential","type":"authorized_user"}'


#: A gcloud that answers the read-only queries the doctor makes. It creates nothing; every
#: subcommand the doctor uses is a describe, a list or a config read.
_GCLOUD_STUB = """#!/bin/sh
case "$*" in
  *"auth list"*)               echo "operator@example.com" ;;
  *"config get-value project"*) echo "example-project" ;;
  *"auth print-access-token"*) exit 1 ;;
  *"projects describe"*)       echo "123456789" ;;
esac
exit 0
"""


def _repo(tmp_path: Path, *, settings: dict | None = None, credential: bool = True,
          gcloud: bool = False, generated: bool = False) -> tuple[Path, dict]:
    # A throwaway checkout containing only what the script resolves relative to itself, so the
    # test never reads the developer's own .env or credentials.
    root = tmp_path / "repo"
    (root / "deploy" / "gcp" / "scripts").mkdir(parents=True)
    for name in ("deploy-doctor.sh", "lib.sh"):
        shutil.copy(REPO / "deploy" / "gcp" / "scripts" / name,
                    root / "deploy" / "gcp" / "scripts" / name)
    (root / "src" / "meshpipeline").mkdir(parents=True)
    (root / "src" / "meshpipeline" / "__init__.py").write_text('__version__ = "1.0.0"\n')

    if settings is not None:
        (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
    if credential:
        adc = root / "secrets" / "gcp"
        adc.mkdir(parents=True)
        (adc / "application_default_credentials.json").write_text(CREDENTIAL_BODY)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    if generated:
        (root / "deploy" / "gcp" / "generated.env").write_text(
            "".join(f"{k}={v}\n" for k, v in COMPLETE_ENV.items())
            + "CLOUDRUN_MESH_JOB=example-mesh-job\nMESH_SERVICE_ACCOUNT=example-mesh-sa\n"
            + "ARTIFACT_REGISTRY_REPOSITORY=example-repo\n")
    if gcloud:
        stub = binaries / "gcloud"
        stub.write_text(_GCLOUD_STUB)
        stub.chmod(0o755)
    # PATH mirrors the system tools MINUS gcloud, so a CLI installed on the developer's machine
    # cannot satisfy a case that is meant to have none. Naming /usr/bin directly would make these
    # cases depend on whether the host has the CLI, which is not what they are testing.
    for d in ("/usr/bin", "/bin", "/usr/local/bin"):
        base = pathlib.Path(d)
        if not base.is_dir():
            continue
        for real in base.iterdir():
            if real.name.startswith("gcloud") or (binaries / real.name).exists():
                continue
            try:
                (binaries / real.name).symlink_to(real)
            except OSError:                       # pragma: no cover - unreadable entry
                pass
    env = {"PATH": str(binaries), "HOME": str(tmp_path / "home")}
    return root, env


def _run(root: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(root / DOCTOR)], capture_output=True, text=True,
                          env=env, cwd=str(root), timeout=120)


def test_a_fully_satisfied_machine_exits_zero(tmp_path):
    # Every check the doctor claims to make is satisfied - only then may it report success.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=True,
                      generated=True)
    result = _run(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checked prerequisites are satisfied" in result.stdout


def test_an_unauthenticated_session_is_a_remote_failure_not_a_local_one(tmp_path):
    # The distinction matters: nothing on this machine needs fixing, the session does.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=False,
                      generated=True)
    result = _run(root, env)
    assert result.returncode == 1, result.stdout      # the CLI itself is a local prerequisite
    assert "gcloud CLI not installed" in result.stdout


def test_a_missing_gcloud_cli_is_a_local_failure(tmp_path):
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=False)
    result = _run(root, env)
    assert result.returncode == 1, result.stdout
    assert "gcloud CLI not installed" in result.stdout


@pytest.mark.parametrize("absent", sorted(COMPLETE_ENV))
def test_each_required_setting_is_named_and_fails(tmp_path, absent):
    root, env = _repo(tmp_path, settings={k: v for k, v in COMPLETE_ENV.items() if k != absent},
                      credential=True, gcloud=True, generated=True)
    result = _run(root, env)
    assert result.returncode != 0, result.stdout
    assert f"{absent} is not set in .env" in result.stdout


def test_a_missing_credential_file_fails(tmp_path):
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=False, gcloud=True,
                      generated=True)
    result = _run(root, env)
    assert result.returncode == 1, result.stdout
    assert "no application default credentials file" in result.stdout
    assert "gcloud auth application-default login" in result.stdout


def test_every_missing_item_is_reported_in_one_run(tmp_path):
    # A person fixing these should need one run, not one run per fault.
    root, env = _repo(tmp_path, settings={}, credential=False, gcloud=False)
    result = _run(root, env)
    assert result.returncode == 1
    for setting in COMPLETE_ENV:
        assert f"{setting} is not set in .env" in result.stdout, setting
    assert "gcloud CLI not installed" in result.stdout
    assert "no application default credentials file" in result.stdout
    assert "5 missing local prerequisite" in result.stdout or "missing local prerequisite" in result.stdout


def test_no_setting_value_or_credential_content_leaves_the_local_section(tmp_path):
    # The local section reports STATE, never the value: someone can paste it into an issue. The
    # cloud section deliberately echoes the active project and resource names, which are
    # identifiers a person needs to see, not credentials.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=True,
                      generated=True)
    output = _run(root, env).stdout
    local = output.split("Hosted deployment")[0]
    for value in COMPLETE_ENV.values():
        assert value not in local, f"the doctor printed the value of a setting: {value}"
    assert "not-a-real-credential" not in output and "authorized_user" not in output


def test_the_next_command_is_one_that_can_actually_run_here(tmp_path):
    # Naming the missing setting told someone what was wrong and not what to do about it: these
    # four are identifiers of resources that already exist, not values anyone invents. The next
    # command is now the one that reads them off the project - but that command needs the CLI, so
    # a host without it must be sent to get the CLI rather than to a command that would die.
    root, env = _repo(tmp_path, settings={}, credential=False, gcloud=False)
    result = _run(root, env)
    next_line = [ln for ln in result.stdout.splitlines() if "Next command:" in ln]
    assert next_line, result.stdout
    assert "Google Cloud CLI" in next_line[0], next_line
    assert next_line[0].strip().endswith("make mesh-adopt"), next_line


def test_a_host_with_the_cli_is_sent_to_read_the_settings_off_the_project(tmp_path):
    root, env = _repo(tmp_path, settings={}, credential=True, gcloud=True)
    result = _run(root, env)
    next_line = [ln for ln in result.stdout.splitlines() if "Next command:" in ln]
    assert next_line, result.stdout
    assert "make mesh-adopt" in next_line[0], next_line
    # and it never tells someone to go and find a value by hand
    assert "set GCP_PROJECT_ID" not in result.stdout


def test_a_satisfied_machine_is_sent_to_start_the_stack_not_to_provision(tmp_path):
    # The suggestion is only ever set by a failing check, so a machine with nothing unmet used to
    # fall through to the provisioning default. That told someone whose job and bucket both exist
    # to reconcile a deployment they may not own; what they actually need next is to start.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=True,
                      generated=True)
    result = _run(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    next_line = [ln for ln in result.stdout.splitlines() if "Next command:" in ln]
    assert next_line, result.stdout
    assert "make dev-up" in next_line[0], next_line
    assert "mesh-deploy" not in next_line[0], next_line


def test_the_doctor_creates_and_modifies_nothing(tmp_path):
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=True,
                      generated=True)
    before = {p: p.stat().st_mtime_ns for p in sorted(root.rglob("*")) if p.is_file()}
    _run(root, env)
    after = {p: p.stat().st_mtime_ns for p in sorted(root.rglob("*")) if p.is_file()}
    assert before == after, "the read-only doctor wrote to the checkout"
    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == sorted(
        p.name for p in before), "the read-only doctor created a file"


def test_existing_resources_do_not_require_a_provisioning_record(tmp_path):
    # An operator pointing at a job and bucket that already exist never runs provisioning, so
    # deploy/gcp/generated.env will never be written for them. Treating its absence as a fault
    # blocked their startup once dev-up began gating on this diagnosis.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=True,
                      generated=False)
    result = _run(root, env)
    assert result.returncode == 0, result.stdout
    assert "provisioning has not run here" in result.stdout
    assert "All checked prerequisites are satisfied" in result.stdout


def test_the_declared_job_and_bucket_are_checked_from_env(tmp_path):
    # The names the application will actually dispatch to are the ones in .env, so those are the
    # ones verified - not whatever a discovery file happens to remember.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=True,
                      generated=False)
    output = _run(root, env).stdout
    assert COMPLETE_ENV["CLOUDRUN_JOB"] in output
    assert COMPLETE_ENV["GCP_MESH_BUCKET"] in output


def test_the_doctor_can_never_block_on_a_gcloud_prompt():
    # Every probe sends gcloud's output to /dev/null, so a prompt - "API not enabled, enable and
    # retry?" - would be invisible and the doctor would wait on a terminal forever. Disabling
    # prompts also stops a read-only diagnosis from enabling an API as a side effect.
    script = (REPO / DOCTOR).read_text()
    assert "export CLOUDSDK_CORE_DISABLE_PROMPTS=1" in script
    lines = script.splitlines()
    export_at = next(i for i, ln in enumerate(lines) if "CLOUDSDK_CORE_DISABLE_PROMPTS" in ln)
    first_call = next(i for i, ln in enumerate(lines)
                      if "gcloud" in ln and not ln.lstrip().startswith("#"))
    assert export_at < first_call, "prompts are disabled after the first gcloud call"


def test_a_prompting_gcloud_does_not_hang_the_doctor(tmp_path):
    # A gcloud that would block forever on stdin if prompts were enabled.
    root, env = _repo(tmp_path, settings=COMPLETE_ENV, credential=True, gcloud=False,
                      generated=False)
    stub = Path(env["PATH"].split(":")[0]) / "gcloud"
    stub.write_text('#!/bin/sh\n'
                    '[ "$CLOUDSDK_CORE_DISABLE_PROMPTS" = "1" ] || cat >/dev/null\n'
                    'case "$*" in *"auth list"*) echo "op@example.com" ;; esac\n'
                    'exit 0\n')
    stub.chmod(0o755)
    result = subprocess.run(["bash", str(root / DOCTOR)], capture_output=True, text=True,
                            env=env, cwd=str(root), stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode in (0, 1, 2), result.stdout
