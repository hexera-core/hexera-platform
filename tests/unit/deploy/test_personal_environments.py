# Responsibility: Verify a personal environment is reachable, isolated, derived, and refuses the shared ones.
# Boundaries: it reads the workflow and the scripts, and RUNS the target picker as a program; it deploys nothing.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[3]
WF = REPO / ".github" / "workflows" / "deploy.yml"
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
NEW_ENV = SCRIPTS / "new-env.sh"
DESTROY_ENV = SCRIPTS / "destroy-env.sh"
VALIDATE = REPO / "devtools" / "release" / "validate.sh"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _picker_script() -> str:
    """The target picker's shell body - the program every assertion below is about."""
    return _doc()["jobs"]["target"]["steps"][0]["run"]


# The checkbox roster, so a run can be composed without restating it in every test.
_TICKED = {f"DISPATCH_{c.upper()}": "false" for c in
           ("images", "data", "storage", "migrate", "queue",
            "workers", "console", "admin", "outreach", "edge")}


def _run_picker(tmp_path: Path, **env: str) -> tuple[int, dict[str, str], str]:
    """Run the picker as GitHub would and return (exit code, its outputs, stderr+stdout).

    The picker is the one place a typed slug turns into a project, a registry and an identity to
    impersonate, so it is tested by RUNNING it rather than by matching strings in it. A regex over
    the YAML cannot tell you that `slug=../hexera-prod` is refused; executing it can.
    """
    script = tmp_path / "picker.sh"
    script.write_text(_picker_script(), encoding="utf-8")
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    full = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_REF": "refs/heads/some-branch",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "PERSONAL_ENVS": "pranav=123456789012:GOOG1EPRANAV,areen=999888777666:GOOG1EAREEN",
        **_TICKED,
        **env,
    }
    p = subprocess.run(["bash", str(script)], env=full, capture_output=True, text=True)
    parsed: dict[str, str] = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            parsed[k] = v
    return p.returncode, parsed, p.stdout + p.stderr


# ---------------------------------------------------------------------------------------------
# 1. the input exists and defaults to the shared environment


def test_a_manual_run_can_name_a_personal_environment():
    inputs = _doc()[True]["workflow_dispatch"]["inputs"]
    assert "slug" in inputs, (
        f"workflow_dispatch offers no `slug`, so a personal environment cannot be deployed to at "
        f"all. Offered: {list(inputs)}")
    assert inputs["slug"]["type"] == "string"
    assert inputs["slug"]["default"] == "", (
        "the slug must default to empty - an empty slug is what selects SHARED dev, so any other "
        "default would silently redirect every manual run at somebody's personal project")


def test_the_dispatch_input_budget_is_not_exceeded():
    """GitHub caps workflow_dispatch at a small number of inputs and rejects the whole workflow
    past it - a failure that shows up as "workflow does not have workflow_dispatch trigger",
    naming neither the cap nor the input that breached it. This workflow sits close to it."""
    inputs = _doc()[True]["workflow_dispatch"]["inputs"]
    assert len(inputs) <= 12, (
        f"{len(inputs)} workflow_dispatch inputs. Adding more risks GitHub refusing to dispatch "
        f"the workflow at all; consolidate the checkboxes before adding another field.")


# ---------------------------------------------------------------------------------------------
# 2. what a personal run actually resolves to


def test_a_personal_run_derives_every_target_from_the_slug(tmp_path):
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_IMAGES="true")
    assert rc == 0, log
    assert out["project"] == "hexera-dev-pranav"
    assert out["deployer_sa"] == "github-deployer@hexera-dev-pranav.iam.gserviceaccount.com"
    assert out["registry"] == "us-central1-docker.pkg.dev/hexera-dev-pranav/mesh"
    assert out["wif_provider"].startswith("projects/123456789012/"), (
        "the workload identity provider must be addressed by the project NUMBER registered for "
        "this slug; anything else authenticates against the wrong project")
    # The project is the isolation boundary, so the id inside it is the same `dev` shared dev uses.
    assert out["deployment_id"] == "dev"
    assert out["cloudsql_instance"] == "dev-pg"
    assert out["redis_instance"] == "dev-redis"


def test_a_personal_run_emits_every_target_the_shared_one_does(tmp_path):
    """The failure this prevents is silent. Every output is consumed as an environment variable by
    the provision job, and an output the picker never emitted arrives as the EMPTY STRING rather
    than as an error - so a forgotten key is a deploy that runs with a target unset and discovers
    it several stages in, or does not discover it at all."""
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_IMAGES="true")
    _, shared, _ = _run_picker(tmp_path, DISPATCH_SLUG="", DISPATCH_IMAGES="true")
    missing = set(shared) - set(personal)
    assert not missing, (
        f"the personal branch emits no value for {sorted(missing)}, which the shared-dev branch "
        f"does. Each one reaches deploy.sh as an empty variable rather than as a failure.")


@pytest.mark.parametrize("slug", ["dev", "prod", "production", "shared", "main", "staging"])
def test_a_personal_run_refuses_to_name_a_shared_environment(tmp_path, slug):
    """hexera-dev-prod is not hexera-prod, and that is exactly the danger: it provisions cleanly
    under a name that reads like production."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG=slug, DISPATCH_IMAGES="true")
    assert rc != 0, f"slug '{slug}' was accepted and resolved to {out.get('project')!r}"
    assert "reserved" in log


@pytest.mark.parametrize("slug", [
    "Pranav",            # uppercase is not a legal project id
    "pranav_x",          # underscore is not either
    "9lives",            # must start with a letter
    "pranav-",           # must not end with a hyphen
    "pranav; rm -rf /",  # shell metacharacters
    "../hexera-prod",    # traversal into a shared project's name
    "$(whoami)",         # command substitution
])
def test_a_personal_run_refuses_a_malformed_slug(tmp_path, slug):
    """The slug is the only free-text deploy target on this workflow. It is validated before it is
    interpolated into a project id, a registry path or a service-account email."""
    rc, out, _ = _run_picker(tmp_path, DISPATCH_SLUG=slug, DISPATCH_IMAGES="true")
    assert rc != 0, f"slug {slug!r} was accepted and resolved to {out.get('project')!r}"


def test_an_unregistered_slug_cannot_deploy(tmp_path):
    """A slug resolves only if a project number was registered for it, so a typo names nothing
    rather than naming a project that happens to exist."""
    rc, _, log = _run_picker(tmp_path, DISPATCH_SLUG="nobody", DISPATCH_IMAGES="true")
    assert rc != 0
    assert "HEXERA_PERSONAL_ENVS" in log


def test_a_non_numeric_project_number_is_refused(tmp_path):
    """The register is the one repository variable this workflow trusts, and it is trusted for
    NUMBERS only - so a project id smuggled into it does not become a deploy target."""
    rc, _, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_IMAGES="true",
                           PERSONAL_ENVS="pranav=hexera-prod")
    assert rc != 0


# ---------------------------------------------------------------------------------------------
# 3. what a personal environment deliberately does NOT get


def test_a_personal_run_never_provisions_the_admin_console(tmp_path):
    """The admin console's only gate is IAP, and IAP on Cloud Run needs an OAuth brand created by
    hand per project. Deploying the service without one publishes an unauthenticated admin
    console, so the name is empty and the stage skips itself even when the box is ticked."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav",
                            DISPATCH_IMAGES="true", DISPATCH_ADMIN="true")
    assert out["admin_service"] == ""
    assert out["admin_domain"] == ""


def test_a_personal_run_never_enables_outreach(tmp_path):
    """Outreach can email real people. A sandbox created in thirty seconds is the last place it
    should be reachable, and no checkbox may change that."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav",
                            DISPATCH_IMAGES="true", DISPATCH_OUTREACH="true")
    assert out["outreach_enabled"] == ""
    assert out["outreach_worker_job"] == ""


def test_a_personal_run_states_a_complete_object_store(tmp_path):
    """The bug this prevents shipped once already on shared dev. create-api-service.sh emits its
    object-store block only when MINIO_ENDPOINT is set, and a selection that reconciles `images`
    without `storage` supplies none of these - so the API rolls with no store, the adapter falls
    back to localhost:9000, and every upload returns 503 "Storage is unavailable"."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_IMAGES="true")
    for key in ("minio_endpoint", "minio_region", "minio_secure",
                "minio_bucket", "minio_access_key", "minio_secret_key_secret"):
        assert out.get(key), f"a personal environment states no {key}"
    assert out["minio_secure"] == "true", (
        "Google's S3-interoperability endpoint refuses plain HTTP")
    assert out["minio_access_key"] == "GOOG1EPRANAV", (
        "the access id must come from the register - an empty one makes every deploy mint a NEW "
        "HMAC key until the account hits Google's five-key limit")


def test_an_environment_registered_without_an_access_id_is_refused(tmp_path):
    """An empty access id passes validate-config.sh (the store is not half-stated - the bucket is
    set), so nothing downstream would catch it. It would instead re-mint an HMAC key on every run
    and break on the fifth, for reasons nothing connects back to the register."""
    rc, _, log = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_IMAGES="true",
                             PERSONAL_ENVS="pranav=123456789012")
    assert rc != 0, "a register entry with no access id was accepted"
    assert "access id" in log


# ---------------------------------------------------------------------------------------------
# 4. the shared environments are unchanged by any of this


@pytest.mark.parametrize("ref,event,expected_project", [
    ("refs/heads/main", "push", "hexera-dev"),
    ("refs/heads/feature", "workflow_dispatch", "hexera-dev"),
    ("refs/tags/v9.9.9", "push", "hexera-prod"),
])
def test_the_existing_targets_are_untouched(tmp_path, ref, event, expected_project):
    rc, out, log = _run_picker(tmp_path, GITHUB_REF=ref, GITHUB_EVENT_NAME=event,
                               DISPATCH_SLUG="", DISPATCH_IMAGES="true")
    assert rc == 0, log
    assert out["project"] == expected_project


def test_a_tag_cannot_be_redirected_by_a_slug(tmp_path):
    """The slug is read on workflow_dispatch alone. A release tag reaches prod and nothing typed
    anywhere can make it reach something else."""
    rc, out, log = _run_picker(tmp_path, GITHUB_REF="refs/tags/v9.9.9", GITHUB_EVENT_NAME="push",
                               DISPATCH_SLUG="pranav")
    assert rc == 0, log
    assert out["project"] == "hexera-prod"
    assert out["environment"] == "prod"


# ---------------------------------------------------------------------------------------------
# 5. the lifecycle scripts


def test_destroy_refuses_the_shared_projects():
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert "hexera-dev|hexera-prod|hexera)" in text, (
        "destroy-env.sh must refuse the shared projects by NAME, before any check that depends on "
        "an API call - a check that can fail open is not a check")


def test_destroy_requires_positive_evidence_that_the_project_is_personal():
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert "labels.personal" in text, (
        "destroy-env.sh must require the personal=true label new-env.sh stamps; a name prefix "
        "alone only establishes that a project is not obviously something else")


def test_the_two_reserved_slug_lists_agree():
    """new-env.sh refuses reserved slugs at creation and the workflow refuses them at deploy. Two
    lists that drift mean a name that can be created but not deployed, or worse."""
    reserved = "dev|prod|production|shared|main|staging"
    assert reserved in NEW_ENV.read_text(encoding="utf-8")
    assert reserved in WF.read_text(encoding="utf-8")


def test_creating_an_environment_is_not_something_ci_can_do():
    """Creating a project, minting an identity and seeding secret VALUES are owner's acts. The
    federated deploy identity is deliberately built without any of them, and new-env.sh refuses
    to run as a service account rather than failing obscurely partway through."""
    text = NEW_ENV.read_text(encoding="utf-8")
    # Asserted on the REFUSAL, not on the service-account domain that appears in it. Testing for
    # the domain read as a URL-sanitisation check to CodeQL (py/incomplete-url-substring-
    # sanitization, high) - and it was the weaker assertion anyway: the domain is an incidental
    # token that appears elsewhere in this script, whereas this message only exists on the path
    # that turns a service-account caller away.
    assert "this is running as the service account" in text, (
        "new-env.sh must refuse to run as a service account - creating a project, minting an "
        "identity and seeding secret values are authorities the deploy identity is built without")
    assert "owner's acts" in text


def test_the_project_display_name_uses_only_characters_google_accepts():
    """A project's DISPLAY NAME has different rules from its id: letters, digits, single quotes,
    hyphens, spaces and exclamation points, 4-30 characters, and nothing else. Parentheses are
    rejected - which cost the first real run of this script, failing with `INVALID_ARGUMENT: field
    [display_name] has issue`, an error that names the field but never the character.
    """
    text = NEW_ENV.read_text(encoding="utf-8")
    name = re.search(r'--name "([^"]*)"', text)
    assert name, "new-env.sh passes no --name to `gcloud projects create`"
    literal = name.group(1).replace("${SLUG}", "")
    assert not set(literal) & set("()[]{}<>/\\:;,.?*&%$#@+=|~`\"_"), (
        f"the project display name {name.group(1)!r} carries punctuation Google rejects")
    # 11 for "Hexera dev " plus a slug of at most 19 is exactly the 30 the field allows.
    assert len(literal) + 19 <= 30, (
        f"display name prefix {literal!r} plus a maximum-length slug exceeds the 30-character limit")


def test_creation_establishes_what_the_first_deploy_cannot():
    """Two things must exist BEFORE the first deploy, and neither is created early enough by
    deploy.sh to help it.

    The Artifact Registry repository is created at stage 6, inside the `provision` job - a whole
    job AFTER `release` has already tried to `docker push` into it. And the object-store HMAC key
    needs an authority the deploy identity is deliberately not given. Both are harmless in an
    environment that has deployed before, which is why neither shows up until the first personal
    environment is created.
    """
    text = NEW_ENV.read_text(encoding="utf-8")
    assert "create-artifact-registry.sh" in text, (
        "new-env.sh does not create the image repository, so the FIRST deploy of every personal "
        "environment fails when release-publish pushes into a repository nothing created")
    assert "create-object-storage.sh" in text


def test_a_personal_environment_widens_no_deploy_identity():
    """A personal environment is a REHEARSAL of a real deploy, not a more permissive one. Its
    deploy identity holds exactly the roster hexera-dev and hexera-prod reconcile against, so
    anything a deploy cannot do here it could not do in prod either - which is what makes testing
    one here worth anything. The owner-only acts (minting the object-store key, seeding secret
    values) stay owner-only and happen at creation."""
    wif = (SCRIPTS / "create-workload-identity.sh").read_text(encoding="utf-8")
    assert "roles/storage.hmacKeyAdmin" not in wif, (
        "the deploy identity must not be granted HMAC-key authority; the key is minted once by an "
        "owner in new-env.sh and its access id is pinned thereafter")
    assert "create-object-storage.sh" in NEW_ENV.read_text(encoding="utf-8"), (
        "new-env.sh must establish the object store as the owner, or the first deploy has no "
        "access id to pin and mints a new key on every run")


# ---------------------------------------------------------------------------------------------
# 6. the build cache


def test_the_build_cache_is_opt_in():
    """validate.sh is also the fresh-install path: `make mesh-setup` runs it on a laptop against a
    blank project, where there is no registry to cache into and no credential to reach one."""
    text = VALIDATE.read_text(encoding="utf-8")
    assert 'RELEASE_BUILD_CACHE:-' in text, (
        "the cache must be opt-in; an unset RELEASE_BUILD_CACHE has to fall back to plain "
        "`docker build` or the fresh-install path breaks")
    assert "docker build --target" in text, "the uncached fallback path is gone"


def test_the_cache_export_cannot_fail_the_release():
    """A run that cannot WRITE the cache must still produce the artifact: losing the cache costs
    minutes, failing the release costs the deploy."""
    assert "ignore-error=true" in VALIDATE.read_text(encoding="utf-8")


def test_the_built_image_is_still_loaded_locally():
    """publish.sh pushes THE EXACT image objects validation built, by local tag. A buildx build
    that did not --load would leave nothing for it to push and nothing to inspect."""
    assert "--load" in VALIDATE.read_text(encoding="utf-8")


def test_the_deploy_passes_a_cache_to_the_release_job():
    body = yaml.dump(_doc()["jobs"]["release"])
    assert "RELEASE_BUILD_CACHE" in body, (
        "nothing passes a cache reference to the release job, so every run still rebuilds the "
        "native toolchain from scratch on a fresh runner")


# ---------------------------------------------------------------------------------------------
# 7. lessons from the first real run


def test_bootstrap_lets_an_explicit_project_outrank_the_ambient_one():
    """`gcloud config get-value project` answers "what did this machine last `gcloud config set`",
    which is a fact about a developer's shell rather than about what a run was asked to do.

    Reading only that refused the first real `make new-env`: it provisions a project which is
    deliberately NOT the one the operator's gcloud points at, and the guard rejected it naming a
    project the caller had never mentioned. CI has no ambient config at all.
    """
    text = (SCRIPTS / "bootstrap-env.sh").read_text(encoding="utf-8")
    assert '_REQ_PROJECT="${GCP_PROJECT_ID:-}"' in text, (
        "bootstrap-env.sh must capture an explicitly requested project BEFORE sourcing the env "
        "file overwrites it - the region check beside it already does")
    assert '_ambient_project="${_REQ_PROJECT:-' in text, (
        "an explicit GCP_PROJECT_ID must outrank the ambient gcloud config, the same precedence "
        "lib.sh's load_env applies")


def test_new_env_hands_bootstrap_a_path_not_an_empty_file():
    """bootstrap-env.sh branches on whether its target EXISTS. `mktemp` creates the file, so an
    empty-but-present one sends it down the "reuse what is already configured" path, where it
    sources nothing and discovers nothing."""
    text = NEW_ENV.read_text(encoding="utf-8")
    assert "mktemp -d" in text, (
        "new-env.sh must use a temporary DIRECTORY; `mktemp` creates a file, and a present-but-"
        "empty env file makes bootstrap-env.sh reuse a configuration that describes no project")
    assert 'OBJECT_STORE_ENV="${OBJECT_STORE_DIR}/generated.env"' in text


def test_rerunning_creation_reuses_the_object_store_key():
    """The first rerun of `make new-env` minted a SECOND HMAC key, walking the account toward
    Google's limit of five - past which the storage stage stops dead and an owner has to retire
    keys by hand.

    create-object-storage.sh decides "reuse or mint" from the MINIO_ACCESS_KEY recorded in the
    deployment env, and new-env.sh deliberately hands it a FRESH temporary env each run (so it
    never touches the developer's own generated.env). A fresh env records nothing, so every rerun
    looked like a first run. The register is the only memory this script has of a key it already
    minted, so it seeds the recorded id from there before the stage runs.
    """
    text = NEW_ENV.read_text(encoding="utf-8")
    assert "register_entry" in text and "MINIO_ACCESS_KEY=%s" in text, (
        "new-env.sh must seed the recorded access id from the register before running the object-"
        "store stage, or every rerun mints another HMAC key")
    # A shell function must be DEFINED before the line that calls it runs, not merely somewhere in
    # the file - getting this wrong failed with `register_entry: command not found` mid-run.
    assert text.index("register_entry()") < text.index("$(register_entry)"), (
        "register_entry is defined after the line that calls it")


def test_the_generated_env_heredoc_does_not_execute_its_own_prose():
    """`emit_env` writes generated.env from an UNQUOTED heredoc, because it interpolates real
    values. Its prose quotes shell snippets in backticks - which an unquoted heredoc runs as
    command substitution. The run printed `default: command not found` and
    `make: No rule to make target 'bootstrap'`, and the comments it wrote were the OUTPUT of
    those commands rather than the text."""
    text = (SCRIPTS / "bootstrap-env.sh").read_text(encoding="utf-8")
    body = text[text.index("emit_env() {"):text.index("\nENVFILE\n}")]
    unescaped = [ln for ln in body.splitlines()
                 if "`" in ln.replace("\\`", "")]
    assert not unescaped, (
        f"unescaped backticks inside emit_env's heredoc execute as commands: {unescaped[:3]}")


def test_a_personal_run_states_every_target_an_unattended_deploy_requires(tmp_path):
    """deploy.sh under DEPLOY_NONINTERACTIVE=1 refuses to mutate a project on any target it was
    not explicitly told - "discovery will work it out" is ambient configuration wearing a
    different hat. Leaving one empty is not caught by the workflow, by validate-config.sh, or by
    any other test here: the first real deploy into a personal environment died at stage 1 with
    `requires every deployment target to be explicit - set: GCP_MESH_BUCKET`.

    The list is READ FROM deploy.sh rather than restated, so a fifth required target added there
    fails this test instead of the next person's deploy.
    """
    driver = (SCRIPTS / "deploy.sh").read_text(encoding="utf-8")
    line = next(ln for ln in driver.splitlines()
                if ln.strip().startswith("for _v in") and "GCP_PROJECT_ID" in ln)
    required = line.split("for _v in", 1)[1].split(";")[0].split()
    assert len(required) >= 4, f"could not read the required-target list from deploy.sh: {line!r}"

    # deploy.yml maps each picker output into the provision job's environment under these names.
    env_to_output = {"GCP_PROJECT_ID": "project", "GCP_REGION": "region",
                     "CLOUDRUN_MESH_JOB": "mesh_job", "GCP_MESH_BUCKET": "mesh_bucket"}
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_IMAGES="true")
    for var in required:
        key = env_to_output.get(var)
        assert key, f"deploy.sh now requires {var}, which this test does not know how to map"
        assert out.get(key), (
            f"a personal run states no {key}, so deploy.sh refuses to start: it requires {var}")


def test_the_documented_first_deploy_does_not_select_the_console():
    """A console is given the origin of the API it proxies to, that origin is the API service's
    Cloud Run URL, and Google assigns one only once the service exists - so on an environment
    where the API has never been deployed, validate-config.sh refuses at stage 2:

        CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not

    That gate is correct and deliberate (see its own comment about stage 14 being twelve stages
    away). What was wrong was the instructions: both this script's closing message and the guide
    told people to tick `console` on the very first run, which cannot work. The first deploy is
    two runs.
    """
    text = NEW_ENV.read_text(encoding="utf-8")
    first = text[text.index("DEPLOY INTO IT"):]
    first_cmd = first[:first.index("# 2.")]
    assert "-f console=true" not in first_cmd, (
        "new-env.sh tells the operator to deploy the console on the first run, which stage 2 "
        "refuses - the API has no URL yet")
    assert "# 2." in first and "console=true" in first, (
        "new-env.sh must still show the second run that deploys the console")


def test_the_runtime_identities_are_created_by_an_owner_not_by_the_deploy():
    """create-workload-identity.sh withholds iam.serviceAccountAdmin so a compromised workflow run
    cannot mint an identity. Every stage that needs a runtime identity therefore expects to FIND
    one - in the shared environments they were created by hand long before this was scripted,
    which is why nothing noticed until a genuinely empty project ran preflight:

        FAIL permission MISSING: iam.serviceAccounts.create

    preflight.sh requires that permission only when the mesh identity's disposition is `created`,
    so creating the identities up front is what makes a personal environment pass under the SAME
    roster prod deploys with - as opposed to widening the deploy identity to match.
    """
    text = NEW_ENV.read_text(encoding="utf-8")
    assert "iam service-accounts create" in text, (
        "new-env.sh creates no runtime identities, so the first deploy fails preflight on "
        "iam.serviceAccounts.create - a permission the deploy identity must not be given")
    for role in ("-mesh", "-api", "-console", "-migrate", "-queue-depth", "-workers"):
        assert f'"${{DEPLOYMENT_ID}}{role}' in text, f"no runtime identity created for {role}"
    wif = (SCRIPTS / "create-workload-identity.sh").read_text(encoding="utf-8")
    assert "iam.serviceAccountAdmin" not in wif.split("DEPLOYER_ROLES=(")[1].split(")")[0], (
        "the deploy identity must not be granted authority to mint identities")


def test_the_deployer_can_schedule_the_queue_depth_publisher():
    """The `queue` stage creates a Cloud Scheduler job, and no other role in the roster carries
    cloudscheduler.jobs.create - so a release tag, which selects every component through `all`,
    could not complete. preflight.sh has been naming roles/cloudscheduler.admin in its remediation
    text all along, and its own header records this costing a v0.1.4 rerun at stage 13/19."""
    wif = (SCRIPTS / "create-workload-identity.sh").read_text(encoding="utf-8")
    roster = wif.split("DEPLOYER_ROLES=(")[1].split("\n)")[0]
    assert "roles/cloudscheduler.admin" in roster, (
        "the deploy identity cannot create the queue-depth schedule the `queue` component needs")


def test_discovery_is_pinned_to_the_project_it_is_discovering():
    """Discovery asks gcloud a dozen questions - does this job exist, does this bucket, has this
    instance an address - and a bare `gcloud` answers them about whichever project the MACHINE
    last selected. In CI the two always agree because the auth action sets CLOUDSDK_CORE_PROJECT
    from the deploy target; on a laptop they routinely do not, and new-env.sh runs on a laptop.

    The failure is silent and lies in the convincing direction: discovering hexera-dev-pranav from
    a shell pointed at hexera-dev reported `mesh job dev-mesh exists - reusing it`, because
    dev-mesh exists in hexera-dev.
    """
    text = (SCRIPTS / "bootstrap-env.sh").read_text(encoding="utf-8")
    assert 'export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"' in text, (
        "bootstrap-env.sh does not pin gcloud to the project it is discovering, so its probes "
        "answer about whatever project the machine last selected")


def test_the_mesh_identity_disposition_is_probed_not_inferred_from_the_job():
    """The mesh job is created at stage 10; its runtime identity may exist long before that, and
    in a project stood up by new-env.sh it always does - creating identities needs an owner.

    Inferring one disposition from the other made preflight demand iam.serviceAccounts.create on
    every run that had not yet made the job, and refuse the deploy for a permission it would never
    exercise: the account it was going to create was already there.
    """
    text = (SCRIPTS / "bootstrap-env.sh").read_text(encoding="utf-8")
    branch = text[text.index("MESH_JOB_DISPOSITION=created"):]
    branch = branch[:branch.index("info \"mesh job")]
    assert "iam service-accounts describe" in branch, (
        "the mesh identity's disposition is assumed from the job's absence rather than probed")
