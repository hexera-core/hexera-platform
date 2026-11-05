# Responsibility: Verify the gate that refuses a secret VALUE in a deployment spec or deployment environment.
# Boundaries: it reads and renders fixtures; it makes no cloud call and deploys nothing.
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CHECKER = REPO / "devtools" / "quality" / "check_deploy_secrets.py"
PREFLIGHT = REPO / "deploy" / "gcp" / "scripts" / "deploy-preflight.sh"
CI = REPO / ".github" / "workflows" / "ci.yml"

sys.path.insert(0, str(REPO / "devtools" / "quality"))
import check_deploy_secrets as gate  # noqa: E402

# The four credentials docs/deployment/gcp-live-inventory.md found as literal values in the public
# hexera-dev-api service spec. They are the reason this gate exists, so the gate must know them.
LIVE_EXPOSURE = ("DEEPINFRA_API_KEY", "DEEPSEEK_API_KEY", "POSTGRES_PASSWORD", "MINIO_SECRET_KEY")

#: A credential-shaped literal that says what it is, so a leak into any output is
#: self-explaining rather than a hex string a reader has to trace back to this file.
PLAINTEXT_KEY = "sk-this-value-must-never-be-printed"


def service(env: list[dict]) -> dict:
    # The shape `gcloud run services describe --format=json` emits, trimmed to what the gate reads.
    return {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Service",
        "metadata": {"name": "hexera-dev-api"},
        "spec": {"template": {"spec": {"containers": [
            {"image": "europe-west2-docker.pkg.dev/hexera-dev/mesh/api@sha256:" + "a" * 64,
             "env": env},
        ]}}},
    }


def write(tmp_path: Path, name: str, doc: dict | str) -> Path:
    path = tmp_path / name
    if isinstance(doc, str):
        path.write_text(doc)
    elif name.endswith(".json"):
        path.write_text(json.dumps(doc))
    else:
        path.write_text(yaml.safe_dump(doc))
    return path


# which settings are secret-bearing

def test_the_secret_roster_is_the_declared_catalogue_flag_not_a_name_pattern():
    from meshpipeline.settings import inventory as cat
    assert gate.secret_settings() == frozenset(v.name for v in cat.all_vars() if v.secret)


def test_every_credential_the_live_inventory_found_exposed_is_secret_bearing():
    roster = gate.secret_settings()
    assert set(LIVE_EXPOSURE) <= roster, f"the gate would not have caught: {set(LIVE_EXPOSURE) - roster}"


def test_a_setting_that_is_not_declared_secret_is_not_policed():
    # POSTGRES_USER is configuration, not a credential. Policing it would make the gate noisy and
    # teach operators to route non-secrets through Secret Manager.
    assert "POSTGRES_USER" not in gate.secret_settings()


# Cloud Run service specs

def test_a_spec_with_no_secret_env_passes(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "POSTGRES_USER", "value": "meshpipeline"},
        {"name": "CORS_ORIGINS", "value": "https://app.hexera.dev"},
    ]))
    assert gate.scan_path(path) == []


def test_a_literal_secret_value_fails_and_names_the_key(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "POSTGRES_USER", "value": "meshpipeline"},
        {"name": "DEEPINFRA_API_KEY", "value": PLAINTEXT_KEY},
    ]))
    findings = gate.scan_path(path)
    assert [f.name for f in findings] == ["DEEPINFRA_API_KEY"]
    # The locator has to point AT the entry, or an operator with a 400-line spec is left grepping.
    assert findings[0].locator == "spec.template.spec.containers[0].env[1]"


def test_a_finding_records_the_length_of_the_literal_and_not_the_literal(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "DEEPSEEK_API_KEY", "value": PLAINTEXT_KEY},
    ]))
    finding = gate.scan_path(path)[0]
    assert finding.length == len(PLAINTEXT_KEY)
    assert PLAINTEXT_KEY not in repr(finding)


def test_a_secret_key_ref_passes(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "DEEPINFRA_API_KEY",
         "valueFrom": {"secretKeyRef": {"name": "deepinfra-api-key", "key": "latest"}}},
        {"name": "POSTGRES_PASSWORD",
         "valueFrom": {"secretKeyRef": {"name": "postgres-password", "key": "3"}}},
    ]))
    assert gate.scan_path(path) == []


def test_a_secret_key_ref_carrying_a_value_as_well_still_fails(tmp_path):
    # The reference does not erase the literal: the value is still readable in the spec.
    path = write(tmp_path, "service.json", service([
        {"name": "MINIO_SECRET_KEY", "value": PLAINTEXT_KEY,
         "valueFrom": {"secretKeyRef": {"name": "minio-secret-key", "key": "latest"}}},
    ]))
    assert [f.name for f in gate.scan_path(path)] == ["MINIO_SECRET_KEY"]


def test_an_empty_or_absent_value_is_not_a_violation(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "DEEPSEEK_API_KEY", "value": ""},
        {"name": "MESH_API_KEY", "value": "   "},
        {"name": "TAVILY_API_KEY"},
    ]))
    assert gate.scan_path(path) == []


def test_a_secret_manager_resource_name_is_a_reference_not_a_value(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "DEEPSEEK_API_KEY", "value": "projects/hexera-dev/secrets/deepseek-api-key"},
        {"name": "MINIO_SECRET_KEY",
         "value": "projects/224734058693/secrets/minio-secret-key/versions/latest"},
    ]))
    assert gate.scan_path(path) == []


def test_a_yaml_spec_is_read_the_same_way_as_json(tmp_path):
    path = write(tmp_path, "service.yaml", service([
        {"name": "POSTGRES_PASSWORD", "value": "hunter2hunter2"},
    ]))
    assert [f.name for f in gate.scan_path(path)] == ["POSTGRES_PASSWORD"]


def test_a_cloud_run_job_spec_is_reached_through_its_deeper_template(tmp_path):
    # A Job nests template/spec twice. The walk must not depend on a fixed path, or every new
    # resource kind silently becomes unchecked.
    job = {"apiVersion": "run.googleapis.com/v1", "kind": "Job",
           "spec": {"template": {"spec": {"template": {"spec": {"containers": [
               {"image": "x", "env": [{"name": "MESH_API_KEY", "value": "live-key-value"}]}]}}}}}}
    path = write(tmp_path, "job.yaml", job)
    assert [f.name for f in gate.scan_path(path)] == ["MESH_API_KEY"]


def test_an_unrendered_template_placeholder_is_a_violation(tmp_path):
    # `env: - name: X value: ${X}` renders to a plaintext value in the deployed spec. Passing it
    # here would bless exactly the manifest that produced the live exposure.
    path = write(tmp_path, "service.yaml", service([
        {"name": "DEEPSEEK_API_KEY", "value": "${DEEPSEEK_API_KEY}"},
    ]))
    assert [f.name for f in gate.scan_path(path)] == ["DEEPSEEK_API_KEY"]


def test_a_malformed_document_is_a_refusal_not_a_pass(tmp_path):
    path = tmp_path / "service.json"
    path.write_text("{not json at all")
    with pytest.raises(gate.Unavailable):
        gate.scan_path(path)


# deployment environment files

def test_an_env_file_of_secret_names_only_passes(tmp_path):
    path = write(tmp_path, "generated.env", (
        "# NO SECRET VALUES: only Secret Manager CONTAINER names appear below.\n"
        "GCP_PROJECT_ID=hexera-dev\n"
        "DEEPSEEK_API_KEY_SECRET=deepseek-api-key\n"
        "POSTGRES_PASSWORD_SECRET=postgres-password\n"
        "DEEPSEEK_API_KEY=\n"
    ))
    assert gate.scan_path(path) == []


def test_an_env_file_carrying_a_credential_value_fails_at_its_line(tmp_path):
    path = write(tmp_path, "generated.env", (
        "GCP_PROJECT_ID=hexera-dev\n"
        f"DEEPSEEK_API_KEY={PLAINTEXT_KEY}\n"
        "export MINIO_SECRET_KEY='minioadmin'\n"
    ))
    findings = gate.scan_path(path)
    assert [f.name for f in findings] == ["DEEPSEEK_API_KEY", "MINIO_SECRET_KEY"]
    assert [f.locator for f in findings] == ["line 2", "line 3"]


def test_an_env_file_may_name_a_secret_manager_reference(tmp_path):
    path = write(tmp_path, "deploy.env", (
        "DEEPINFRA_API_KEY=projects/hexera-dev/secrets/deepinfra-api-key/versions/latest\n"
        "POSTGRES_PASSWORD=postgres-password:latest\n"
    ))
    assert gate.scan_path(path) == []


# the command line

def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKER), *args],
                          cwd=REPO, capture_output=True, text=True)


def test_every_tracked_deploy_spec_in_the_repository_is_clean_right_now():
    # The tracked specs ONLY. A developer's deploy/gcp/generated.env is machine state, and judging
    # this repository by it makes the suite fail for the repository having been used.
    specs = [p for p in gate.default_subjects() if p.suffix in gate.SPEC_SUFFIXES]
    assert specs, "the scan found no tracked deployment spec at all - its verdict would be vacuous"
    findings, _ = gate.enforce(specs)
    assert findings == [], f"a tracked deployment spec carries a credential: {findings}"


def test_the_cli_reports_a_clean_subject_and_says_what_it_examined(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "POSTGRES_USER", "value": "meshpipeline"},
    ]))
    r = _run(str(path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK:" in r.stdout
    # A gate that cannot say what it looked at cannot be told apart from one that looked at nothing.
    assert "1 subject(s), 1 environment entry examined" in r.stdout, r.stdout


def test_the_cli_exits_one_and_reports_the_key_without_the_value(tmp_path):
    path = write(tmp_path, "service.json", service([
        {"name": "DEEPINFRA_API_KEY", "value": PLAINTEXT_KEY},
    ]))
    r = _run(str(path))
    assert r.returncode == 1
    assert "DEEPINFRA_API_KEY" in r.stdout
    # A gate that echoes the credential into a CI log has re-published the thing it is refusing.
    assert PLAINTEXT_KEY not in r.stdout + r.stderr


def test_an_interpreter_without_the_catalogue_is_unavailable_not_a_violation(tmp_path, monkeypatch):
    # Exit 2, not 1. Without the catalogue the gate does not know which settings are secret-bearing,
    # so it has learned nothing about this file - and an operator told their deployment carries a
    # credential it does not carry will stop believing the gate that told them.
    path = write(tmp_path, "service.json", service([
        {"name": "DEEPSEEK_API_KEY", "value": PLAINTEXT_KEY},
    ]))
    monkeypatch.setitem(sys.modules, "meshpipeline.settings", None)
    assert gate.main(["check_deploy_secrets.py", str(path)]) == 2


def test_a_run_with_no_subject_at_all_is_a_refusal(tmp_path):
    # Scanning nothing and reporting OK is the one outcome worse than failing.
    with pytest.raises(gate.Unavailable):
        gate.enforce([])


# one rule, enforced in both places that promote a deployment

def test_ci_runs_the_gate_as_a_blocking_step():
    text = CI.read_text(encoding="utf-8")
    assert "python devtools/quality/check_deploy_secrets.py" in text, (
        "the rule is only real if something enforces it; CI does not run the gate")
    # "Blocking" is now a STRUCTURAL fact rather than a word in a step name: CI runs one concern
    # per lane and ci-gate aggregates them, so a lane blocks exactly when the gate waits on it.
    # Asserting the job is in ci-gate's needs is the property; a name could say BLOCKING and be
    # wired to nothing.
    import yaml
    wf = yaml.safe_load(text)
    owning = [name for name, job in wf["jobs"].items()
              if "check_deploy_secrets.py" in yaml.dump(job)]
    assert owning, "no CI job runs the gate"
    gated = set(wf["jobs"]["ci-gate"]["needs"])
    assert set(owning) & gated, (
        f"the job(s) running the gate {owning} are not in ci-gate's needs {sorted(gated)}, "
        "so a failure there would not block the merge")


def test_gate_d_asks_this_checker_rather_than_grepping_for_itself():
    assert "devtools/quality/check_deploy_secrets.py" in PREFLIGHT.read_text(encoding="utf-8")


def test_gate_d_keeps_no_second_roster_of_credential_names():
    # The list this replaced named four settings and missed both POSTGRES_PASSWORD and
    # MINIO_SECRET_KEY - two of the four a live audit then found published in a Cloud Run spec.
    # A second roster is how a deployment gate and CI come to refuse different things.
    code = [ln for ln in PREFLIGHT.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]
    named = sorted({n for n in gate.secret_settings() for ln in code if n in ln})
    assert named == [], f"Gate D names credentials itself instead of asking the catalogue: {named}"


def test_gate_d_fails_on_a_deployment_environment_carrying_a_credential(tmp_path):
    env_file = write(tmp_path, "generated.env", (
        "GCP_PROJECT_ID=hexera-dev\n"
        f"MINIO_SECRET_KEY={PLAINTEXT_KEY}\n"
    ))
    r = subprocess.run(["bash", str(PREFLIGHT)], cwd=str(REPO), capture_output=True, text=True,
                       env={**os.environ,
                            "DEPLOY_ENV_FILE": str(env_file),
                            "PREFLIGHT_PYTHON": sys.executable,
                            "RELEASE_RECORD": str(tmp_path / "no-such-record.json")})
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "carries a credential VALUE" in out, out
    assert "MINIO_SECRET_KEY" in out, out
    assert PLAINTEXT_KEY not in out, "Gate D echoed the credential it is refusing"


def test_gate_d_reports_an_interpreter_without_the_catalogue_as_unrun_not_as_a_credential(tmp_path):
    # A bare virtualenv is a real interpreter that genuinely cannot run the check. Gate D says so,
    # the same way it does for the manifest render check, rather than accusing a clean deployment
    # environment of carrying a credential.
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", str(bare)], check=True, capture_output=True)
    env_file = write(tmp_path, "generated.env", "GCP_PROJECT_ID=hexera-dev\n")
    r = subprocess.run(["bash", str(PREFLIGHT)], cwd=str(REPO), capture_output=True, text=True,
                       env={**os.environ,
                            "DEPLOY_ENV_FILE": str(env_file),
                            "PREFLIGHT_PYTHON": str(bare / "bin" / "python"),
                            "RELEASE_RECORD": str(tmp_path / "no-such-record.json")})
    out = r.stdout + r.stderr
    assert "carries a credential VALUE" not in out, out
    assert "credential-value check not run" in out, out
    # An unrun check still withholds the strongest verdict.
    assert "DEPLOYMENT READY" not in out
