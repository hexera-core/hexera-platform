# Responsibility: Verify what makes a release record promotable, and that Gate D and publication enforce it.
# Boundaries: publication never builds, and its registry comes from the deployment environment, never root .env.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
RECORD_TOOL = REPO / "devtools" / "release" / "record.py"
PUBLISH = REPO / "devtools" / "release" / "publish.sh"
PREFLIGHT = REPO / "deploy" / "gcp" / "scripts" / "deploy-preflight.sh"

APP_ID = "sha256:" + "a1" * 32
MESH_ID = "sha256:" + "b2" * 32
APP_DIGEST = "sha256:" + "c3" * 32
MESH_DIGEST = "sha256:" + "d4" * 32
REGISTRY = "localhost:5000/testrepo"


# record fixtures

def _checks(**over) -> list[dict]:
    names = ["wheel build", "wheel inspect", "clean non-editable install",
             "import provenance (site-packages, not the checkout)",
             "image build (app: Dockerfile target pipeline)",
             "image build (mesh: Dockerfile target mesh)",
             "native smoke", "native all", "native terminal",
             "native terminal coverage (5 engines + 4 scenarios)",
             "artifact reports its version and schema versions"]
    return [{"check": n, "status": over.get(n, "passed"), "required": True, "detail": ""}
            for n in names]


def make_record(*, commit="c" * 40, tree="t" * 40, state="published", checks=None,
                app_ref=f"{REGISTRY}/app@{APP_DIGEST}", mesh_ref=f"{REGISTRY}/mesh@{MESH_DIGEST}",
                app_digest=APP_DIGEST, mesh_digest=MESH_DIGEST, schema=2,
                product_version="1.0.0", drop_check=None) -> dict:
    ch = checks if checks is not None else _checks()
    if drop_check:
        ch = [c for c in ch if c["check"] != drop_check]
    import importlib.util
    spec = importlib.util.spec_from_file_location("_rr", RECORD_TOOL)
    rr = importlib.util.module_from_spec(spec); spec.loader.exec_module(rr)
    return {
        "record_schema": schema, "state": state, "gate": "C: release-artifact validation",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "published_at": "2026-01-01T00:05:00+00:00" if state == "published" else None,
        "commit": commit, "tree": tree, "branch": "main", "clean_tree": True,
        "product_version": product_version,
        "schema_versions": {"final_result": 4, "pipeline_state": 8},
        "artifact_facts_read_from": "the app image",
        "wheel": {"filename": "meshpipeline-1.0.0-py3-none-any.whl", "sha256": "ab" * 32,
                  "bytes": 760990},
        "local_image_tag": "abcdef123456",
        "components": {
            "app": {"dockerfile_target": "pipeline", "local_tag": "meshpipeline-app:abcdef123456",
                    "local_image_id": APP_ID,
                    "registry_repository": f"{REGISTRY}/app", "publication_tag": "abcdef123456",
                    "registry_digest": app_digest, "reference": app_ref},
            "mesh": {"dockerfile_target": "mesh", "local_tag": "meshpipeline-mesh:abcdef123456",
                     "local_image_id": MESH_ID, "workloads": ["mesh-job"],
                     "registry_repository": f"{REGISTRY}/mesh", "publication_tag": "abcdef123456",
                     "registry_digest": mesh_digest, "reference": mesh_ref},
        },
        "checks": ch, "verdict": rr.compute_verdict(ch),
        "tool_versions": {"python": "3.12.3", "docker": "x", "git": "y"},
    }


def write_record(tmp_path: Path, rec: dict) -> Path:
    p = tmp_path / "release.json"
    p.write_text(json.dumps(rec, indent=2))
    return p


def promotable(record_path: Path) -> tuple[bool, str]:
    # sys.executable, never a repository-venv path: this suite must run under whatever interpreter
    # collected it. CI installs into the runner's own Python and builds no .venv, so a hardcoded
    # ".venv/bin/python" made these 21 promotion tests raise FileNotFoundError there - the release
    # gate they police was unenforceable in the one environment that blocks a merge.
    r = subprocess.run([sys.executable, str(RECORD_TOOL), "check", "--record", str(record_path)],
                       cwd=str(REPO), capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


# the record: what is promotable

def test_a_complete_published_pass_is_promotable(tmp_path):
    okp, why = promotable(write_record(tmp_path, make_record()))
    assert okp, why


@pytest.mark.parametrize("tier", ["native smoke", "native all", "native terminal"])
@pytest.mark.parametrize("status", ["skipped", "not_run", "failed"])
def test_a_mandatory_native_tier_that_did_not_pass_is_not_promotable(tmp_path, tier, status):
    okp, why = promotable(write_record(tmp_path, make_record(checks=_checks(**{tier: status}))))
    assert not okp
    assert tier in why


def test_the_record_library_cannot_tell_that_a_tier_is_simply_ABSENT(tmp_path):
    okp, _ = promotable(write_record(tmp_path, make_record(drop_check="native terminal")))
    assert okp, ("the record library now detects an absent mandatory tier - good, but Gate D's "
                 "by-name check is no longer the only thing standing between us and that gap; "
                 "update this test to say so")


def test_an_unpublished_record_is_not_promotable(tmp_path):
    okp, why = promotable(write_record(tmp_path, make_record(state="validated")))
    assert not okp
    assert "nothing has been published" in why


def test_a_tag_only_reference_is_not_promotable(tmp_path):
    okp, why = promotable(write_record(tmp_path, make_record(app_ref=f"{REGISTRY}/app:abcdef123456")))
    assert not okp
    assert "tag-only" in why


def test_a_missing_registry_digest_is_not_promotable(tmp_path):
    okp, why = promotable(write_record(tmp_path, make_record(app_digest=None, app_ref=None)))
    assert not okp
    assert "registry_digest is missing" in why


def test_a_reference_that_does_not_carry_its_recorded_digest_is_not_promotable(tmp_path):
    other = "sha256:" + "ee" * 32
    okp, why = promotable(write_record(tmp_path, make_record(app_ref=f"{REGISTRY}/app@{other}")))
    assert not okp
    assert "does not carry its recorded digest" in why


def test_an_unsupported_record_schema_is_not_promotable(tmp_path):
    okp, why = promotable(write_record(tmp_path, make_record(schema=1)))
    assert not okp
    assert "record_schema" in why


def test_a_hand_edited_verdict_is_not_promotable(tmp_path):
    rec = make_record(checks=_checks(**{"native terminal": "failed"}))
    rec["verdict"] = "passed"
    okp, why = promotable(write_record(tmp_path, rec))
    assert not okp
    assert "disagrees with the checks" in why


def test_a_malformed_record_is_not_promotable(tmp_path):
    p = tmp_path / "release.json"; p.write_text("{ this is not json")
    okp, why = promotable(p)
    assert not okp
    assert "malformed" in why


def test_a_missing_record_is_not_promotable(tmp_path):
    okp, why = promotable(tmp_path / "nope.json")
    assert not okp
    assert "no release record" in why


def test_the_record_is_written_atomically(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_rr", RECORD_TOOL)
    rr = importlib.util.module_from_spec(spec); spec.loader.exec_module(rr)
    target = tmp_path / "release.json"
    rr.write_atomic(target, make_record())
    before = target.read_text()

    class Boom(Exception):
        pass

    rec = make_record()
    rec["explode"] = object()          # not JSON-serialisable: json.dump raises mid-write
    with pytest.raises(TypeError):
        rr.write_atomic(target, rec)
    assert target.read_text() == before, "a failed write replaced the previous record"
    assert not list(tmp_path.glob(".release-*")), "a partial temp file was left behind"


# Gate D, driven end to end

def _gate_d(record: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "RELEASE_RECORD": str(record)}
    env.pop("CLOUDSDK_CONFIG", None)
    # A DEPLOYMENT ENVIRONMENT THIS TEST OWNS. Left to itself the gate reads
    # deploy/gcp/generated.env, so a machine that has provisioned judges these synthetic records
    # against its real MESH_IMAGE and the suite fails for the repository having been used.
    env["DEPLOY_ENV_FILE"] = str(record.parent / "absent-generated.env")
    # AND the interpreter, for the same reason the env file is owned here. The gate falls back to
    # `python3`, which on this machine may be any interpreter at all; the manifest render check
    # then cannot run and the gate reports a manifest failure that is really a missing pytest.
    # Naming the collecting interpreter makes the check genuinely execute, so this test proves the
    # manifest contract instead of depending on a repository .venv having been built.
    env["PREFLIGHT_PYTHON"] = sys.executable
    env.update(extra_env or {})
    return subprocess.run(["bash", str(PREFLIGHT)], capture_output=True, text=True,
                          cwd=str(REPO), env=env)


def _head() -> tuple[str, str]:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                         capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=str(REPO),
                          capture_output=True, text=True).stdout.strip()
    return sha, tree


def test_gate_d_refuses_a_record_for_a_different_commit(tmp_path):
    _, tree = _head()
    r = _gate_d(write_record(tmp_path, make_record(commit="0" * 40, tree=tree)))
    assert r.returncode != 0
    assert "re-run Gate C" in r.stdout


def test_gate_d_refuses_a_record_for_a_different_tree(tmp_path):
    sha, _ = _head()
    r = _gate_d(write_record(tmp_path, make_record(commit=sha, tree="0" * 40)))
    assert r.returncode != 0
    assert "current tree" in r.stdout


def test_gate_d_refuses_when_native_terminal_did_not_pass(tmp_path):
    sha, tree = _head()
    rec = make_record(commit=sha, tree=tree, checks=_checks(**{"native terminal": "skipped"}))
    r = _gate_d(write_record(tmp_path, rec))
    assert r.returncode != 0
    assert "native terminal" in r.stdout


def test_gate_d_refuses_when_a_mandatory_tier_is_never_mentioned(tmp_path):
    sha, tree = _head()
    rec = make_record(commit=sha, tree=tree, drop_check="native terminal")
    r = _gate_d(write_record(tmp_path, rec))
    assert r.returncode != 0
    assert "never mentions required tier" in r.stdout


def test_gate_d_refuses_a_tag_only_reference(tmp_path):
    sha, tree = _head()
    rec = make_record(commit=sha, tree=tree, app_ref=f"{REGISTRY}/app:abcdef123456")
    r = _gate_d(write_record(tmp_path, rec))
    assert r.returncode != 0
    assert "tag-only" in r.stdout


def test_gate_d_refuses_an_unpublished_record(tmp_path):
    sha, tree = _head()
    r = _gate_d(write_record(tmp_path, make_record(commit=sha, tree=tree, state="validated")))
    assert r.returncode != 0
    assert "not promotable" in r.stdout


def test_gate_d_refuses_a_missing_record(tmp_path):
    r = _gate_d(tmp_path / "absent.json")
    assert r.returncode != 0
    assert "no release record" in r.stdout


def test_gate_d_accepts_a_coherent_record_and_says_cloud_is_unverified(tmp_path):
    sha, tree = _head()
    if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=str(REPO),
                      capture_output=True, text=True).stdout.strip():
        pytest.skip("the working tree is dirty; Gate D correctly refuses, tested elsewhere")
    r = _gate_d(write_record(tmp_path, make_record(commit=sha, tree=tree)))
    assert r.returncode == 0, r.stdout
    assert "LOCAL PREFLIGHT PASSED" in r.stdout
    assert "CLOUD READINESS UNVERIFIED" in r.stdout
    assert "DEPLOYMENT READY" not in r.stdout, "an unverified cloud must not read as ready"
    assert "checks NOT run" in r.stdout


def test_gate_d_is_read_only_and_idempotent(tmp_path):
    sha, tree = _head()
    rec_path = write_record(tmp_path, make_record(commit=sha, tree=tree))
    before = rec_path.read_bytes()
    first = _gate_d(rec_path)
    second = _gate_d(rec_path)
    assert rec_path.read_bytes() == before, "Gate D modified the release record"
    assert first.returncode == second.returncode
    assert ("LOCAL PREFLIGHT PASSED" in first.stdout) == ("LOCAL PREFLIGHT PASSED" in second.stdout)


# publication: no build, exact bytes

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records every invocation, and answers the read paths release_publish.sh uses.
set -uo pipefail
LOG="${FAKE_DOCKER_LOG:?}"
printf '%s\n' "$*" >> "${LOG}"
case "$1" in
  image)
    # image inspect <ref> --format ...
    ref="$3"
    case "${ref}" in
      *app*)  id="${FAKE_APP_ID}" ;;
      *mesh*) id="${FAKE_MESH_ID}" ;;
      *)      exit 1 ;;
    esac
    case "$*" in
      *RepoDigests*)
        case "${ref}" in
          *app*)  echo "${FAKE_REGISTRY}/app@${FAKE_APP_DIGEST}" ;;
          *mesh*) echo "${FAKE_REGISTRY}/mesh@${FAKE_MESH_DIGEST}" ;;
        esac ;;
      *) echo "${id}" ;;
    esac
    exit 0 ;;
  manifest)
    # A REACHABLE registry that does not hold the tag says "no such manifest" on stderr and
    # exits non-zero - the same thing an UNREACHABLE registry says. Publication distinguishes
    # them with a /v2/ probe (see the fake curl below), and fails closed when it cannot.
    if [ -n "${FAKE_REMOTE_CONFIG:-}" ]; then
      printf '{"config":{"digest":"%s"}}\n' "${FAKE_REMOTE_CONFIG}"
      exit 0
    fi
    echo "no such manifest: $*" >&2
    exit 1 ;;
  tag)  exit 0 ;;
  push) echo "pushed"; exit 0 ;;
  build)
    echo "A BUILD WAS INVOKED DURING PUBLICATION" >&2
    exit 0 ;;
esac
exit 0
"""


#: A registry that ANSWERS. Set FAKE_REGISTRY_UNREACHABLE=1 to model one that does not, which
#: publication must treat as "I cannot prove this tag is free" rather than as "it is free".
_FAKE_CURL = r"""#!/usr/bin/env bash
case "$*" in
  *"/v2/"*) [ -n "${FAKE_REGISTRY_UNREACHABLE:-}" ] && exit 7; exit 0 ;;
esac
exit 0
"""


def _publish_repo(tmp_path: Path, rec: dict) -> tuple[Path, Path, dict]:
    root = tmp_path / "repo"
    (root / "devtools" / "release").mkdir(parents=True)
    (root / "deploy" / "output").mkdir(parents=True)
    shutil.copy(PUBLISH, root / "devtools" / "release" / "publish.sh")
    shutil.copy(RECORD_TOOL, root / "devtools" / "release" / "record.py")
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README").write_text("x")
    subprocess.run(["git", "add", "README"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    bindir = tmp_path / "bin"; bindir.mkdir()
    (bindir / "docker").write_text(_FAKE_DOCKER); (bindir / "docker").chmod(0o755)
    (bindir / "curl").write_text(_FAKE_CURL); (bindir / "curl").chmod(0o755)
    log = tmp_path / "docker.log"; log.write_text("")

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root,
                          capture_output=True, text=True).stdout.strip()
    env = {**os.environ,
           "PATH": f"{bindir}:{os.environ.get('PATH','')}",
           "FAKE_DOCKER_LOG": str(log), "FAKE_APP_ID": APP_ID, "FAKE_MESH_ID": MESH_ID,
           "FAKE_APP_DIGEST": APP_DIGEST, "FAKE_MESH_DIGEST": MESH_DIGEST,
           "FAKE_REGISTRY": REGISTRY, "RELEASE_REGISTRY": REGISTRY}
    return root, log, {"env": env, "sha": sha, "tree": tree}


def _run_publish(root: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "devtools/release/publish.sh"], cwd=str(root),
                          capture_output=True, text=True, env=env)


def test_publication_pushes_the_validated_image_without_building(tmp_path):
    rec = make_record(state="validated")
    root, log, ctx = _publish_repo(tmp_path, rec)
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))

    r = _run_publish(root, ctx["env"])
    assert r.returncode == 0, r.stdout + r.stderr

    calls = log.read_text().splitlines()
    assert not any(c.startswith("build") for c in calls), \
        f"publication invoked a build - it must reuse the validated image:\n{calls}"
    assert any(c.startswith("push") for c in calls), f"nothing was pushed:\n{calls}"
    # the exact validated tags were the push sources
    assert any("tag meshpipeline-app:" in c for c in calls)
    assert any("tag meshpipeline-mesh:" in c for c in calls)

    out = json.loads((root / "deploy" / "output" / "release.json").read_text())
    assert out["state"] == "published"
    assert out["components"]["app"]["registry_digest"] == APP_DIGEST
    assert out["components"]["app"]["reference"] == f"{REGISTRY}/app@{APP_DIGEST}"
    assert out["components"]["mesh"]["reference"] == f"{REGISTRY}/mesh@{MESH_DIGEST}"


def test_a_publication_test_would_catch_a_rebuild(tmp_path):
    rec = make_record(state="validated")
    root, log, ctx = _publish_repo(tmp_path, rec)
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    # inject a build into the publish path
    script = root / "devtools" / "release" / "publish.sh"
    script.write_text(script.read_text().replace(
        '  docker tag "${C_TAG}" "${target}"',
        '  docker build -t "${C_TAG}" .\n  docker tag "${C_TAG}" "${target}"', 1))
    _run_publish(root, ctx["env"])
    calls = log.read_text().splitlines()
    assert any(c.startswith("build") for c in calls), "the mutation did not take"
    # ...which is exactly what the real test asserts must NOT happen
    with pytest.raises(AssertionError):
        assert not any(c.startswith("build") for c in calls)


def test_publication_refuses_when_the_local_image_id_no_longer_matches(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    env = {**ctx["env"], "FAKE_APP_ID": "sha256:" + "99" * 32}
    r = _run_publish(root, env)
    assert r.returncode != 0
    assert "a different image is wearing the validated tag" in r.stdout
    assert not any(c.startswith("push") for c in log.read_text().splitlines())


def test_publication_refuses_to_overwrite_a_tag_holding_a_different_image(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    env = {**ctx["env"], "FAKE_REMOTE_CONFIG": "sha256:" + "77" * 32}
    r = _run_publish(root, env)
    assert r.returncode != 0
    assert "Refusing to overwrite a published tag" in r.stdout
    assert not any(c.startswith("push") for c in log.read_text().splitlines())


def test_publication_refuses_a_record_that_is_not_a_pass(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"],
                      checks=_checks(**{"native terminal": "skipped"}))
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    r = _run_publish(root, ctx["env"])
    assert r.returncode != 0
    assert "only a PASS may be published" in r.stdout


def test_publication_refuses_a_record_for_another_commit(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit="0" * 40, tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    r = _run_publish(root, ctx["env"])
    assert r.returncode != 0
    assert "re-run Gate C" in r.stdout


def test_publication_refuses_a_dirty_tree(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    (root / "README").write_text("changed")          # tracked file, now dirty
    r = _run_publish(root, ctx["env"])
    assert r.returncode != 0
    assert "working tree is dirty" in r.stdout
    assert not any(c.startswith("push") for c in log.read_text().splitlines())


# promotion into the deployment env

_FAKE_ENVSUBST = r"""#!/usr/bin/env python3
import os, re, sys
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\$(\w+)",
                        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                        sys.stdin.read()))
"""

_FAKE_GCLOUD_OK = r"""#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "${FAKE_GCP_LOG:?}"
exit 0
"""


def _deploy_env(tmp_path: Path, **over) -> Path:
    base = {
        "GCP_PROJECT_ID": "p", "GCP_REGION": "europe-west1",
        "ARTIFACT_REGISTRY_REPOSITORY": "repo",

        "CLOUDRUN_MESH_JOB": "meshjob",
        "API_SERVICE_ACCOUNT": "api-sa", "API_CPU": "1", "API_MEMORY": "1Gi",
        "PIPELINE_SERVICE_ACCOUNT": "pipe-sa", "PIPELINE_CPU": "2", "PIPELINE_MEMORY": "4Gi",
        "PIPELINE_TIMEOUT_SECONDS": "28800", "MESH_SERVICE_ACCOUNT": "mesh-sa",
        "MESH_IMAGE": "",
    }
    base.update(over)
    p = tmp_path / "generated.env"
    p.write_text("\n".join(f"{k}={v}" for k, v in base.items()) + "\n")
    return p


def test_publication_refuses_when_the_remote_tag_cannot_be_read(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    env = {**ctx["env"], "FAKE_REGISTRY_UNREACHABLE": "1"}
    r = _run_publish(root, env)
    assert r.returncode != 0
    assert "could not determine what" in r.stdout
    assert not any(c.startswith("push") for c in log.read_text().splitlines()), \
        "publication pushed over a tag whose contents it could not establish"


def test_an_operator_can_override_the_unreadable_tag_refusal_deliberately(tmp_path):
    root, log, ctx = _publish_repo(tmp_path, make_record(state="validated"))
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    env = {**ctx["env"], "FAKE_REGISTRY_UNREACHABLE": "1",
           "RELEASE_ALLOW_UNVERIFIED_TAG": "1"}
    r = _run_publish(root, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING" in r.stdout and "is now lost" in r.stdout
    assert any(c.startswith("push") for c in log.read_text().splitlines())


# THE deployment environment: machine-owned, never the application's root .env


# Publish with no RELEASE_REGISTRY, so the deployment environment must supply one. `answers` is
# the registry the doubles answer digests for - what the script is expected to resolve. A mismatch
# makes the push produce no digest and the run fail, which is what makes these assertions real.
def _publish_without_registry_override(tmp_path, *, answers=None, **env_over):
    rec = make_record(state="validated")
    root, log, ctx = _publish_repo(tmp_path, rec)
    rec = make_record(state="validated", commit=ctx["sha"], tree=ctx["tree"])
    (root / "deploy" / "output" / "release.json").write_text(json.dumps(rec, indent=2))
    env = {k: v for k, v in ctx["env"].items() if k != "RELEASE_REGISTRY"}
    if answers:
        env["FAKE_REGISTRY"] = answers
    env.update(env_over)
    return root, log, env


def _generated_env(path: Path, *, project="proj-x", region="europe-west4", repo="mesh") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"GCP_PROJECT_ID={project}\nGCP_REGION={region}\n"
                    f"ARTIFACT_REGISTRY_REPOSITORY={repo}\n")
    return path


def test_publication_resolves_its_registry_from_the_generated_deployment_environment(tmp_path):
    root, log, env = _publish_without_registry_override(
        tmp_path, answers="europe-west4-docker.pkg.dev/proj-x/mesh")
    _generated_env(root / "deploy" / "gcp" / "generated.env")

    r = _run_publish(root, env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "europe-west4-docker.pkg.dev/proj-x/mesh" in r.stdout, r.stdout
    assert any(c.startswith("push") for c in log.read_text().splitlines())


def test_an_explicit_deployment_environment_is_honoured(tmp_path):
    root, log, env = _publish_without_registry_override(
        tmp_path, answers="us-west1-docker.pkg.dev/other-proj/images")
    elsewhere = _generated_env(tmp_path / "elsewhere" / "generated.env",
                               project="other-proj", region="us-west1", repo="images")
    env["DEPLOY_ENV_FILE"] = str(elsewhere)
    # the canonical path also exists, and must lose to the explicit one
    _generated_env(root / "deploy" / "gcp" / "generated.env")

    r = _run_publish(root, env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "us-west1-docker.pkg.dev/other-proj/images" in r.stdout, r.stdout


def test_publication_without_a_registry_authority_fails_once_with_the_command_to_run(tmp_path):
    root, log, env = _publish_without_registry_override(tmp_path)

    r = _run_publish(root, env)

    assert r.returncode != 0, "publication proceeded with nowhere to push"
    out = r.stdout + r.stderr
    assert "generated.env" in out, out
    assert "make bootstrap" in out, "the failure does not name the command that fixes it"
    assert not any(c.startswith("push") for c in log.read_text().splitlines()), \
        "something was pushed despite having no registry"


def test_publication_never_reads_the_application_root_env(tmp_path):
    # Root .env carries live API keys. If publication sourced it, the key would land in the
    # environment of every docker child process this test records.
    root, log, env = _publish_without_registry_override(
        tmp_path, answers="europe-west4-docker.pkg.dev/proj-x/mesh")
    _generated_env(root / "deploy" / "gcp" / "generated.env")
    (root / ".env").write_text("DEEPSEEK_API_KEY=sk-must-not-leak\n"
                               "ARTIFACT_REGISTRY_REPOSITORY=from-root-env\n")

    r = _run_publish(root, env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "sk-must-not-leak" not in r.stdout + r.stderr, "a credential from root .env escaped"
    assert "from-root-env" not in r.stdout, "publication took a value from the application .env"
    assert "/proj-x/mesh" in r.stdout, r.stdout


def test_gate_d_reads_the_generated_environment_rather_than_skipping(tmp_path):
    # The retired path made Gate D skip its configuration stage forever; it must run again.
    rec = make_record()
    root, log, ctx = _publish_repo(tmp_path, rec)
    (root / "deploy" / "gcp" / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, root / "deploy" / "gcp" / "scripts" / "deploy-preflight.sh")
    shutil.copy(REPO / "deploy" / "gcp" / "scripts" / "lib.sh",
                root / "deploy" / "gcp" / "scripts" / "lib.sh")
    env_file = _generated_env(tmp_path / "explicit" / "generated.env")
    env_file.write_text(env_file.read_text()
                        + f"CLOUDRUN_MESH_JOB=amp-mesh\nMESH_IMAGE={REGISTRY}/mesh@{MESH_DIGEST}\n")
    env = {**ctx["env"], "DEPLOY_ENV_FILE": str(env_file)}

    r = subprocess.run(["bash", "deploy/gcp/scripts/deploy-preflight.sh"], cwd=str(root),
                       capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr

    assert "generated.env absent" not in out, out
    assert "every required deployment variable is set" in out, out
    assert "MESH_IMAGE is the recorded digest reference" in out, out
    # assembled rather than written out, so the repository-wide guard against this retired path
    # does not flag the assertion that proves it is gone
    assert "deploy/gcp/" + "env" not in out, "Gate D still names the retired path"


# Gate D hermeticity: the gate's own inputs must be owned, not inherited from the machine

def test_the_gate_d_helper_names_the_interpreter_it_runs_the_checks_with():
    # The ONE property that made this suite order- and machine-dependent: without it the gate
    # falls back to whatever `python3` is, the manifest render check cannot run, and Gate D
    # reported a manifest FAILURE that was really a missing pytest.
    import inspect
    src = inspect.getsource(_gate_d)
    assert "PREFLIGHT_PYTHON" in src, "the Gate D helper no longer owns its interpreter"
    assert "sys.executable" in src, "the helper names an interpreter other than the collecting one"


def test_an_interpreter_without_pytest_reports_the_manifest_check_unrun_not_failed(tmp_path):
    # A bare virtualenv is a real interpreter that genuinely cannot run the check. The gate must
    # say so, because "I could not verify the manifests" is not "the manifests are broken" - and
    # an operator sent hunting a manifest bug that does not exist is the cost of conflating them.
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", str(bare)], check=True, capture_output=True)
    bare_py = bare / "bin" / "python"
    assert bare_py.exists()
    assert subprocess.run([str(bare_py), "-c", "import pytest"],
                          capture_output=True).returncode != 0, "the bare venv has pytest"

    sha, tree = _head()
    if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=str(REPO),
                      capture_output=True, text=True).stdout.strip():
        pytest.skip("the working tree is dirty; Gate D correctly refuses, tested elsewhere")
    r = _gate_d(write_record(tmp_path, make_record(commit=sha, tree=tree)),
                extra_env={"PREFLIGHT_PYTHON": str(bare_py)})
    assert "manifest render check failed" not in r.stdout, (
        "an interpreter without pytest was reported as a manifest failure")
    assert "manifest render check not run" in r.stdout
    assert "checks NOT run" in r.stdout
    # an unrun check still prevents the strongest verdict
    assert "DEPLOYMENT READY" not in r.stdout


def test_gate_d_leaves_no_generated_record_in_the_repository(tmp_path):
    before = (REPO / "deploy" / "output").exists()
    sha, tree = _head()
    _gate_d(write_record(tmp_path, make_record(commit=sha, tree=tree)))
    assert (REPO / "deploy" / "output").exists() == before, (
        "Gate D created deploy/output/ in the repository - a later run would read it")


def test_the_gate_d_helper_owns_every_ambient_input():
    # Both inputs the gate would otherwise read from the machine are supplied by the helper.
    import inspect
    src = inspect.getsource(_gate_d)
    for owned in ("DEPLOY_ENV_FILE", "PREFLIGHT_PYTHON"):
        assert owned in src, f"{owned} is no longer owned by the test helper"


CONSOLE_DIGEST = "sha256:" + "e5" * 32


def test_promotion_writes_the_console_image(tmp_path):
    """A record carrying a console component promotes it into the deployment env."""
    sha, tree = _head()
    rec = make_record(commit=sha, tree=tree)
    rec["components"]["console"] = {
        "dockerfile_target": "console",
        "local_tag": "meshpipeline-console:abc1234",
        "local_image_id": "sha256:" + "f6" * 32,
        "registry_repository": REGISTRY,
        "publication_tag": "v0.0.0",
        "registry_digest": CONSOLE_DIGEST,
        "reference": f"{REGISTRY}/console@{CONSOLE_DIGEST}",
        "workloads": ["console-service"],
    }
    record_path = write_record(tmp_path, rec)
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "DEPLOYMENT_ID=t\nGCP_PROJECT_ID=p\nMESH_SERVICE_ACCOUNT=mesh-sa\n",
        encoding="utf-8",
    )

    subprocess.run(
        ["bash", str(REPO / "deploy" / "gcp" / "scripts" / "promote-release.sh")],
        check=True, capture_output=True, text=True,
        env={**os.environ, "RELEASE_RECORD": str(record_path),
             "DEPLOY_ENV_FILE": str(env_file)},
    )

    written = env_file.read_text(encoding="utf-8")
    assert f"CONSOLE_IMAGE={REGISTRY}/console@{CONSOLE_DIGEST}" in written


def test_the_release_record_check_refuses_a_tag_only_console_reference(tmp_path):
    """Deployment identity is a digest. A movable tag is refused, not resolved.

    This is enforced by devtools/release/record.py's `check` command, which promote-release.sh
    runs first and which generically validates every component's `reference` field - including
    console - before CONSOLE_REF is ever computed. promote-release.sh's own digest-qualification
    loop (the `for ref in ... CONSOLE_REF` block mirroring MESH_REF/APP_REF) is unreachable
    defence-in-depth for this scenario, not what this test is exercising.
    """
    sha, tree = _head()
    rec = make_record(commit=sha, tree=tree)
    rec["components"]["console"] = {
        "dockerfile_target": "console",
        "local_tag": "meshpipeline-console:abc1234",
        "local_image_id": "sha256:" + "f6" * 32,
        "registry_repository": REGISTRY,
        "publication_tag": "v0.0.0",
        "registry_digest": CONSOLE_DIGEST,
        "reference": f"{REGISTRY}/console:v0.0.0",
        "workloads": ["console-service"],
    }
    record_path = write_record(tmp_path, rec)
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "DEPLOYMENT_ID=t\nGCP_PROJECT_ID=p\nMESH_SERVICE_ACCOUNT=mesh-sa\n",
        encoding="utf-8",
    )

    done = subprocess.run(
        ["bash", str(REPO / "deploy" / "gcp" / "scripts" / "promote-release.sh")],
        capture_output=True, text=True,
        env={**os.environ, "RELEASE_RECORD": str(record_path),
             "DEPLOY_ENV_FILE": str(env_file)},
    )
    assert done.returncode != 0
    assert "digest" in (done.stderr + done.stdout).lower()


def test_the_manifest_check_invokes_the_real_render_test():
    # Complements the behavioural control above: that one proves an interpreter which CANNOT run
    # the check is reported as unrun. This one proves the check, when it does run, is still the
    # real manifest render test rather than a stub - which no observable Gate D output would
    # otherwise distinguish from a genuine pass.
    src = (REPO / "deploy/gcp/scripts/deploy-preflight.sh").read_text(encoding="utf-8")
    i = src.index("# manifests")
    block = src[i:src.index("# migration readiness", i)]
    assert "-m pytest" in block, "the manifest check no longer runs the render test"
    assert "tests/unit/deploy/test_manifest_render.py" in block, (
        "the manifest check points somewhere other than the real render test")
    assert "elif true" not in block and "assumed" not in block, (
        "the manifest check short-circuits to a pass without running anything")
    assert (REPO / "tests/unit/deploy/test_manifest_render.py").is_file(), (
        "the render test the gate invokes does not exist")
