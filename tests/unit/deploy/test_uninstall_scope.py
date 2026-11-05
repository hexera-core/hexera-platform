# Responsibility: Verify the complete uninstall removes what it promises and nothing outside this checkout.
# Boundaries: the target's declared scope; it is not executed here - running it would delete the developer's stack.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _recipe(target: str) -> str:
    out = subprocess.run(["make", "-n", target], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_project_scope_matches_what_compose_actually_names():
    # Compose lowercases the directory name. If this diverges, every destructive target below
    # silently matches nothing and leaves behind exactly what it claims to remove.
    scope = subprocess.run(["make", "-s", "-f", "-", "p"], cwd=REPO, text=True,
                           capture_output=True,
                           input=f"include {REPO}/Makefile\np:\n\t@echo $(COMPOSE_PROJECT)\n").stdout.strip()
    assert scope == REPO.name.lower(), scope
    running = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=REPO,
                             capture_output=True, text=True)
    if running.returncode == 0 and '"name"' in running.stdout:
        assert f'"name": "{scope}"' in running.stdout or f'"name":"{scope}"' in running.stdout


def test_the_complete_uninstall_removes_everything_it_names():
    recipe = _recipe("dev-uninstall")
    assert "docker compose down -v" in recipe, "it does not remove services and data volumes"
    assert re.search(r"docker image rm .*reference=\S+-\*", recipe), "it does not remove the images"
    for residue in (".venv", "build", "dist", "egg-info", ".env"):
        assert residue in recipe, f"the complete uninstall leaves {residue}"
    assert "output" in recipe, "the complete uninstall leaves root output/"


def test_the_uninstall_cannot_reach_outside_this_checkout():
    recipe = _recipe("dev-uninstall")
    # No global prune, and no unqualified wildcard: this must not be able to touch another
    # project's containers, images or volumes - including a second checkout of this product.
    assert "system prune" not in recipe and "image prune" not in recipe
    assert "volume prune" not in recipe and "builder prune" not in recipe
    for line in recipe.splitlines():
        if "docker image rm" in line:
            assert "reference=" in line, f"unscoped image removal: {line}"
        if "rm -rf" in line or "rm -f" in line:
            assert ".." not in line and " /" not in line.replace("rm -rf ", "").replace("rm -f ", ""), line


def test_the_routine_reset_is_still_the_non_destructive_one():
    # dev-reset must NOT remove images, .env or the virtualenv - it is the one you run between
    # experiments, and someone reaching for it should not lose their machine setup.
    recipe = _recipe("dev-reset")
    assert "docker compose down -v" in recipe
    for kept in ("image rm", ".venv", ".env"):
        assert kept not in recipe, f"dev-reset removes {kept}; that belongs to dev-uninstall"


def test_the_image_build_does_not_require_cloud_configuration():
    # Building is independent of Google Cloud. Keeping it behind the runtime gate meant a peer
    # could not discover a broken build until they had a project, a job and a bucket.
    recipe = _recipe("dev-build")
    assert "docker compose build" in recipe
    assert "check_runtime_config" not in recipe, "the build was put behind the runtime gate"
    assert "docker compose up" not in recipe, "the build target starts services"


def test_starting_the_stack_still_gates_on_configuration_first():
    recipe = _recipe("dev-up")
    gate = recipe.find("check_runtime_config")
    up = recipe.find("docker compose up")
    assert gate != -1, "dev-up no longer checks the runtime configuration"
    assert gate < up, "the configuration gate no longer runs before the stack starts"


def test_no_deploy_script_can_wait_on_a_gcloud_prompt():
    # gcloud offers to enable an API and retry when one is off. These scripts route command output
    # away from the terminal, so the question would be invisible and the run would wait on it
    # forever. Enabling an API is provisioning's own explicit step, not a side effect of a query.
    lib = (REPO / "deploy" / "gcp" / "scripts" / "lib.sh").read_text()
    assert "export CLOUDSDK_CORE_DISABLE_PROMPTS=1" in lib, (
        "the shared deploy library does not disable gcloud prompts")
    sourced = [p for p in (REPO / "deploy" / "gcp" / "scripts").glob("*.sh")
               if "lib.sh" in p.read_text() and p.name != "lib.sh"]
    assert len(sourced) >= 5, f"only {len(sourced)} scripts source the library"


def test_the_apis_are_enabled_before_anything_queries_them():
    # Discovery asks Cloud Run and Cloud Storage what exists. On a project where those APIs were
    # never enabled the query itself fails, and a blank project is exactly what this path is for.
    mk = (REPO / "deploy" / "gcp" / "Makefile").read_text()
    line = next(ln for ln in mk.splitlines() if ln.startswith("publication-target:"))
    deps = line.split(":", 1)[1].split("##")[0].split()
    assert deps.index("enable-apis") < deps.index("bootstrap"), (
        f"discovery runs before the APIs are enabled: {deps}")


def test_enabling_apis_does_not_require_discovery_output():
    # Discovery cannot run until the APIs are on, so enabling must not need discovery's output -
    # otherwise the two require each other and a blank project can satisfy neither.
    script = (REPO / "deploy" / "gcp" / "scripts" / "enable-apis.sh").read_text()
    assert "generated.env" in script and "REPO_ROOT}/.env" in script, (
        "enabling has no source for the project id other than discovery")
    assert script.index("if [ -f") < script.index("load_env"), (
        "load_env runs unconditionally, so a blank project still fails")


def test_creating_the_registry_also_makes_it_pushable():
    # A repository nobody can push to is not provisioned, so the credential helper is configured
    # here rather than left to an instruction the operator may not act on.
    script = (REPO / "deploy" / "gcp" / "scripts" / "create-artifact-registry.sh").read_text()
    assert "gcloud auth configure-docker" in script
    assert "--quiet" in script, "configuring docker would prompt inside provisioning"
    assert script.index("repositories create") < script.index("configure-docker"), (
        "docker is configured before the repository exists")


def test_an_unreadable_registry_tag_names_the_likely_cause():
    # The failure a person actually hits is an unconfigured Docker, not a mysterious tag.
    script = (REPO / "devtools" / "release" / "publish.sh").read_text()
    assert "credential helper" in script
    assert "gcloud auth configure-docker" in script


def test_registry_reachability_accepts_an_authenticated_registry():
    # A real registry answers 401 to an unauthenticated /v2/, so a success requirement would read
    # every healthy hosted registry as unreachable.
    script = (REPO / "devtools" / "release" / "publish.sh").read_text()
    assert "curl -sf" not in script, "reachability still requires a 2xx from /v2/"
    assert "http_code" in script, "reachability does not inspect the status code"
    assert '"${code}" != "000"' in script, "any HTTP status must count as an answer"


def test_preflight_does_not_block_on_resources_this_run_will_create():
    # Absence is only a fault for a resource being REUSED. For one this deployment creates it is
    # the premise, and the steps after preflight exist to make it so.
    script = (REPO / "deploy" / "gcp" / "scripts" / "preflight.sh").read_text()
    for var in ("MESH_JOB_DISPOSITION", "MESH_BUCKET_DISPOSITION"):
        assert var in script, f"preflight ignores {var} when judging absence"
    assert script.count('= "created" ]') >= 2
    assert "will be created" in script


def test_the_bucket_is_created_with_flags_create_accepts():
    # Versioning is set by update, not at creation - gcloud rejects it as a create-time flag.
    script = (REPO / "deploy" / "gcp" / "scripts" / "create-mesh-tier.sh").read_text()
    create = script[script.index("buckets create"):script.index("buckets update")]
    assert "--no-versioning" not in create, "creation passes a flag it does not accept"
    for hardening in ("--uniform-bucket-level-access", "--public-access-prevention"):
        assert hardening in create, f"the bucket is created without {hardening}"
    # and the setting is still asserted, just where it belongs
    assert "--no-versioning" in script[script.index("buckets update"):]


def test_the_lifecycle_document_carries_only_keys_gcloud_accepts():
    # gcloud rejects a lifecycle file containing any key it does not recognise, so a comment
    # field in the document fails the update outright and the bucket keeps no expiry at all.
    import json
    doc = json.loads((REPO / "deploy" / "gcp" / "storage"
                      / "exchange-bucket-lifecycle.json").read_text())
    assert set(doc) == {"rule"}, f"unrecognised top-level keys: {sorted(set(doc) - {'rule'})}"
    for entry in doc["rule"]:
        assert set(entry) == {"action", "condition"}, entry


def test_the_mesh_job_manifest_has_no_duplicate_keys():
    # gcloud parses this with a loader that warns today and will error later; a duplicate key also
    # means the value somebody reads in the file is not the one that reaches Cloud Run.
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def no_duplicates(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            assert key not in seen, f"duplicate key {key!r} in the mesh job manifest"
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_duplicates)
    raw = (REPO / "deploy" / "gcp" / "cloud-run" / "mesh-job.yaml").read_text()
    yaml.load(raw.replace("${", "PLACEHOLDER{"), Loader=Strict)


def test_every_variable_the_mesh_tier_prints_is_one_it_has():
    # A summary that names a variable the script never sets aborts under `set -u` after the job
    # has already been created - the worst moment to stop.
    script = (REPO / "deploy" / "gcp" / "scripts" / "create-mesh-tier.sh").read_text()
    import re
    used = set(re.findall(r"\$\{([A-Z_]+)\}", script))
    lib = (REPO / "deploy" / "gcp" / "scripts" / "lib.sh").read_text()
    generated_keys = {"GCP_PROJECT_ID", "GCP_REGION", "GCP_MESH_BUCKET", "CLOUDRUN_MESH_JOB",
                      "MESH_SERVICE_ACCOUNT", "MESH_SA_EMAIL", "MESH_IMAGE", "DEPLOY_DIR",
                      "DEPLOYMENT_ID", "ARTIFACT_REGISTRY_REPOSITORY", "REPO_ROOT"}
    unknown = {v for v in used
               if v not in generated_keys and f"{v}=" not in script and f"{v}=" not in lib}
    assert not unknown, f"names no source sets: {sorted(unknown)}"


def test_each_mesh_resource_follows_its_own_disposition():
    # Discovery decides per resource: a project that already owns a mesh job may still need its
    # exchange bucket created, so neither disposition may speak for the other.
    script = (REPO / "deploy" / "gcp" / "scripts" / "create-mesh-tier.sh").read_text()
    body = script[script.index("provisioning the mesh tier"):]
    assert '"${MESH_BUCKET_DISPOSITION}" = "reused"' in body, "the bucket has no disposition of its own"
    assert '"${MESH_JOB_DISPOSITION}" = "reused"' in body, "the job has no disposition of its own"
    # a reused resource is validated and never reconfigured
    reused_bucket = body[body.index('"${MESH_BUCKET_DISPOSITION}" = "reused"'):]
    reused_bucket = reused_bucket[:reused_bucket.index("else")]
    assert "buckets update" not in reused_bucket, "a reused bucket is reconfigured"
    assert "buckets create" not in reused_bucket, "a reused bucket is recreated"
