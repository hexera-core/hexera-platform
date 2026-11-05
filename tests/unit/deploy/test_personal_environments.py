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
DESTROY_ENV = SCRIPTS / "destroy-env.sh"
VALIDATE = REPO / "devtools" / "release" / "validate.sh"


def _doc() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def _picker_script() -> str:
    """The target picker's shell body - the program every assertion below is about."""
    return _doc()["jobs"]["target"]["steps"][0]["run"]


# The checkbox roster, so a run can be composed without restating it in every test. These are
# TIERS, not stages - the picker translates them into deploy.sh's component names.
_TICKED = {f"DISPATCH_{c.upper()}": "false" for c in
           ("app", "data", "fleet", "console", "admin", "outreach", "edge")}


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
    """The slug names an environment INSIDE hexera-dev. It decides the deployment id, and the
    deployment id decides every resource name - but it cannot decide the PROJECT, which is pinned,
    so a free-text box can never point a deploy at a project a slug does not already name."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    assert rc == 0, log
    assert out["project"] == "hexera-dev"
    assert out["deployment_id"] == "dev-pranav"
    assert out["deployer_sa"] == "github-deployer@hexera-dev.iam.gserviceaccount.com"
    assert out["registry"] == "us-central1-docker.pkg.dev/hexera-dev/mesh"
    assert out["wif_provider"].startswith("projects/224734058693/"), (
        "the identity pool is shared dev's - it already exists, which is what lets a deploy into "
        "an environment nobody has created resolve at all")
    assert out["mesh_job"] == "dev-pranav-mesh"
    assert out["api_service"] == "dev-pranav-api"
    assert out["console_service"] == "dev-pranav-console"
    assert out["worker_mig"] == "dev-pranav-workers"


def test_a_personal_run_shares_the_instances_but_not_the_database(tmp_path):
    """The expensive, slow tier is shared deliberately - it buys a first deploy in minutes rather
    than half an hour. The database is not, because the isolation that matters is that one
    developer's migration cannot move another's schema."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_DATA="true")
    assert rc == 0, log
    assert out["cloudsql_instance"] == "hexera-dev-pg"
    assert out["redis_instance"] == "hexera-dev-redis"
    assert out["migrate_db_name"] == "meshpipeline_pranav"
    assert out["migrate_db_user"] == "meshpipeline"


def test_no_personal_name_can_collide_with_a_shared_one(tmp_path):
    """Asserted against shared dev's OWN output rather than a hand-written list, so a target added
    to one branch and not the other is caught here rather than by two deployments discovering they
    share a Cloud Run service."""
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    _, shared, _ = _run_picker(tmp_path, DISPATCH_SLUG="", DISPATCH_APP="true")
    for key in ("deployment_id", "mesh_job", "mesh_bucket", "api_service", "console_service",
                "worker_mig", "worker_env_uri", "minio_bucket", "minio_secret_key_secret",
                "migrate_db_name"):
        assert personal[key] != shared[key], (
            f"{key} is the SAME value in a personal environment and in shared dev "
            f"({personal[key]!r}) - deploying to a slug would reconcile shared dev's resource")
        assert "pranav" in personal[key], f"{key}={personal[key]!r} carries no slug"


def test_a_personal_run_always_reconciles_its_object_store(tmp_path):
    """The access id is no longer pinned, so the storage stage is what supplies it in-run. An
    images-only run without it deploys an API whose adapter dials localhost:9000 and returns 503
    on every upload - which is what shipped on shared dev before its values were pinned."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    assert rc == 0, log
    assert "storage" in out["components"].split(","), (
        f"components={out['components']!r} omits storage, so MINIO_ACCESS_KEY reaches the API "
        f"stage unset")
    assert "minio_access_key" not in out, (
        "a personal run must pin no access id - the key is minted by the first deploy and adopted "
        "by every one after, so a pinned value here could only be a stale one")
    assert out["minio_secret_key_secret"] == "minio-secret-key-pranav", (
        "one secret container per slug; sharing it would have each environment overwrite the "
        "previous one's HMAC secret")


def test_ticking_storage_does_not_duplicate_it(tmp_path):
    """deploy.sh validates every name in DEPLOY_COMPONENTS, and a doubled entry is a wasted stage
    at best. The forced selection has to be idempotent against the checkbox."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav",
                            DISPATCH_APP="true")
    assert out["components"].split(",").count("storage") == 1, out["components"]


def test_a_slug_that_cannot_name_a_service_account_is_refused(tmp_path):
    """`dev-` + slug + `-queue-depth` must fit Google's 30-character service account id. Caught
    here rather than eleven stages into a deploy, where it arrives as a Google error naming the
    field and not the cause - after Cloud SQL and the object store are already reconciled."""
    rc, _, log = _run_picker(tmp_path, DISPATCH_SLUG="a" * 15, DISPATCH_APP="true")
    assert rc != 0, "a 15-character slug was accepted"
    assert "14" in log and "30" in log, log
    rc_ok, _, log_ok = _run_picker(tmp_path, DISPATCH_SLUG="a" * 14, DISPATCH_APP="true")
    assert rc_ok == 0, f"14 characters is the documented maximum and was refused: {log_ok}"


def test_the_celery_prefix_isolates_a_personal_run_and_only_that(tmp_path):
    """One Memorystore instance serves shared dev and every personal environment, and Celery's
    queue names are literals - so the prefix is what stops two environments consuming each other's
    tasks. Shared dev must keep an EMPTY prefix: its queues are the ones that already exist."""
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    _, shared, _ = _run_picker(tmp_path, DISPATCH_SLUG="", DISPATCH_APP="true")
    assert personal["redis_key_prefix"] == "dev-pranav:"
    assert shared.get("redis_key_prefix", "") == "", (
        "a non-empty prefix on shared dev would move its queues to new Redis keys on the next "
        "roll and strand whatever was already enqueued under the old ones")



def test_a_personal_run_emits_every_target_the_shared_one_does(tmp_path):
    """The failure this prevents is silent. Every output is consumed as an environment variable by
    the provision job, and an output the picker never emitted arrives as the EMPTY STRING rather
    than as an error - so a forgotten key is a deploy that runs with a target unset and discovers
    it several stages in, or does not discover it at all."""
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    _, shared, _ = _run_picker(tmp_path, DISPATCH_SLUG="", DISPATCH_APP="true")
    # minio_access_key is the ONE deliberate omission, and it is deliberate in one direction only:
    # shared dev's key was minted by an owner years ago and is pinned, while a personal
    # environment's is minted by its own first deploy and adopted thereafter, so there is nothing
    # to state here. Every OTHER shared-dev target must have a personal counterpart.
    missing = set(shared) - set(personal) - {"minio_access_key"}
    assert not missing, (
        f"the personal branch emits no value for {sorted(missing)}, which the shared-dev branch "
        f"does. Each one reaches deploy.sh as an empty variable rather than as a failure.")


@pytest.mark.parametrize("slug", ["dev", "prod", "production", "shared", "main", "staging"])
def test_a_personal_run_refuses_to_name_a_shared_environment(tmp_path, slug):
    """hexera-dev-prod is not hexera-prod, and that is exactly the danger: it provisions cleanly
    under a name that reads like production."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG=slug, DISPATCH_APP="true")
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
    rc, out, _ = _run_picker(tmp_path, DISPATCH_SLUG=slug, DISPATCH_APP="true")
    assert rc != 0, f"slug {slug!r} was accepted and resolved to {out.get('project')!r}"




# ---------------------------------------------------------------------------------------------
# 3. what a personal environment deliberately does NOT get


def test_a_personal_run_never_provisions_the_admin_console(tmp_path):
    """Not because it cannot be built - an IAP OAuth brand creates fine, verified against a project
    made the same day - but because nothing needs it in a project with one owner. What the admin
    console offers is reading the worker fleet's metrics and changing its scaling, which is the
    Cloud Console's own job for someone who already has full access to the project. A second
    IAP-gated front end onto the same controls is machinery to keep working for no gain, so the
    name is empty and the stage skips itself even when the box is ticked."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav",
                            DISPATCH_APP="true", DISPATCH_ADMIN="true")
    assert out["admin_service"] == ""
    assert out["admin_domain"] == ""


def test_a_personal_run_never_enables_outreach(tmp_path):
    """Outreach can email real people. A sandbox created in thirty seconds is the last place it
    should be reachable, and no checkbox may change that."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav",
                            DISPATCH_APP="true", DISPATCH_OUTREACH="true")
    assert out["outreach_enabled"] == ""
    assert out["outreach_worker_job"] == ""


def test_a_personal_run_states_a_complete_object_store(tmp_path):
    """The bug this prevents shipped once already on shared dev. create-api-service.sh emits its
    object-store block only when MINIO_ENDPOINT is set, and a selection that reconciles `images`
    without `storage` supplies none of these - so the API rolls with no store, the adapter falls
    back to localhost:9000, and every upload returns 503 "Storage is unavailable"."""
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    for key in ("minio_endpoint", "minio_region", "minio_secure",
                "minio_bucket", "minio_secret_key_secret"):
        assert out.get(key), f"a personal environment states no {key}"
    assert out["minio_secure"] == "true", (
        "Google's S3-interoperability endpoint refuses plain HTTP")
    # THE ACCESS ID IS THE ONE FIELD NOT PINNED, and its absence is covered by
    # test_a_personal_run_always_reconciles_its_object_store rather than left implicit: the key is
    # minted by the first deploy and adopted by every one after, so it does not exist at the
    # moment this picker runs and a value here could only ever be a stale one.



# ---------------------------------------------------------------------------------------------
# 4. the shared environments are unchanged by any of this


@pytest.mark.parametrize("ref,event,expected_project", [
    ("refs/heads/main", "push", "hexera-dev"),
    ("refs/heads/feature", "workflow_dispatch", "hexera-dev"),
    ("refs/tags/v9.9.9", "push", "hexera-prod"),
])
def test_the_existing_targets_are_untouched(tmp_path, ref, event, expected_project):
    rc, out, log = _run_picker(tmp_path, GITHUB_REF=ref, GITHUB_EVENT_NAME=event,
                               DISPATCH_SLUG="", DISPATCH_APP="true")
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




def test_destroy_refuses_a_reserved_slug():
    """`dev` typed by somebody who believes they are naming shared dev is the mistake worth
    refusing outright - it would otherwise build the prefix dev-dev, which is not shared dev but
    is one keystroke away from somebody thinking it is."""
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert "dev|prod|production|shared|main|staging" in text, (
        "the reserved slugs are not refused by name, before any check that depends on an API call "
        "- a check that can fail open is not a check")


def test_destroy_never_deletes_the_shared_data_tier():
    """The Cloud SQL and Memorystore instances are shared with shared dev and with every other
    personal environment. Deleting one to tear down a sandbox takes everybody with it. Only the
    slug's own DATABASE goes."""
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert "sql instances delete" not in text, "destroy deletes the shared Cloud SQL INSTANCE"
    assert "redis instances delete" not in text, "destroy deletes the shared Memorystore instance"
    assert "sql databases delete" in text, "the slug's own database is never removed"


def test_destroy_never_deletes_a_project():
    """It runs inside a project it shares with shared dev. Deleting that project is the one
    mutation this script must never make, however it is invoked."""
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert "projects delete" not in text


def test_destroy_reports_what_it_could_not_reach():
    """Deleting by label cannot cover a resource somebody created by hand that carries no label.
    The honest answer is to print the gap at the moment somebody is about to stop paying attention
    to the environment, not to claim completeness the script does not have."""
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert "NOT DELETED" in text, "a failed or skipped deletion is never reported"
    assert "deployment-id" in text, "teardown does not select on the label the deploy stamps"
    assert "WITHOUT a deployment-id label" in text, (
        "the run never admits that an unlabelled resource survives it")


def test_destroy_deactivates_a_key_before_deleting_its_account():
    """An HMAC key must be deactivated before it can be deleted, and a service account cannot be
    deleted while it still owns one - so deleting the identity first strands a key nothing can
    name, and it keeps billing."""
    text = DESTROY_ENV.read_text(encoding="utf-8")
    assert text.index("hmac update") < text.index("service-accounts delete")
    assert "--deactivate" in text


def test_the_two_reserved_slug_lists_agree():
    """The workflow refuses reserved slugs at deploy and destroy-env.sh refuses them at teardown.
    Two lists that drift mean a name that can be created but not destroyed, or worse - a teardown
    that accepts a name the deploy treats as shared."""
    reserved = "dev|prod|production|shared|main|staging"
    assert reserved in WF.read_text(encoding="utf-8")
    assert reserved in DESTROY_ENV.read_text(encoding="utf-8")





def test_the_deployer_is_widened_only_on_a_personal_environment_host():
    """The host project's deploy identity creates the environment it deploys to, which is the
    whole point - so it holds two roles no other project's deployer does. Everywhere else,
    including hexera-prod, the roster must be exactly what it was.

    This is the test that changed when personal environments stopped being projects. It used to
    assert that no deploy identity was ever widened; now it asserts that the widening is gated,
    because an ungated one would reach hexera-prod as a side effect of a development change."""
    wif = (SCRIPTS / "create-workload-identity.sh").read_text(encoding="utf-8")
    assert "PERSONAL_ENV_HOST" in wif, (
        "the two extra roles are granted unconditionally, so running this script against "
        "hexera-prod would widen production's deploy identity")
    for role in ("roles/iam.serviceAccountAdmin", "roles/storage.hmacKeyAdmin"):
        assert role in wif, (
            f"{role} is absent, so no stage can create the identity or the object-store key its "
            f"slug needs and a first deploy cannot build an environment")
    # The gate must come BEFORE the roles, or they are in the base roster and the flag is decoration.
    assert wif.index("PERSONAL_ENV_HOST") < wif.index("roles/iam.serviceAccountAdmin")


def test_the_base_roster_still_withholds_the_roles_that_would_end_containment():
    """Whatever a host may do, no deployer anywhere may grant itself more or widen the provider
    that admitted it. These two absences are what keep a compromised run contained."""
    wif = (SCRIPTS / "create-workload-identity.sh").read_text(encoding="utf-8")
    roster = wif.split("DEPLOYER_ROLES=(", 1)[1].split("\n)", 1)[0]
    for forbidden in ("roles/owner", "roles/resourcemanager.projectIamAdmin"):
        assert forbidden not in roster, f"{forbidden} is in the roster"


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
    _, out, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_APP="true")
    for var in required:
        key = env_to_output.get(var)
        assert key, f"deploy.sh now requires {var}, which this test does not know how to map"
        assert out.get(key), (
            f"a personal run states no {key}, so deploy.sh refuses to start: it requires {var}")



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


def test_the_database_password_is_left_to_the_data_tier():
    """create-data-tier.sh owns that credential end to end, because the user and the password are
    one credential - "an account without the password its runtimes hold is no more usable than a
    password with no account". Its first-run path generates, stores and creates the user in that
    order, needing only versions.add, which the deploy identity has.

    Seeding a value here manufactured the one state that path cannot recover from: a stored
    version with no database user. That reads as "the runtimes already hold this password, so
    create the user WITH it" - which needs versions.access, deliberately withheld from the deploy
    identity. The deploy created Cloud SQL, created the database, then died one step short of the
    user, needing an owner on every new environment.
    """
    text = (SCRIPTS / "seed-secrets.sh").read_text(encoding="utf-8")
    generated = text[text.index("for entry in"):text.index("# ------", text.index("for entry in"))]
    assert "PG_SECRET" not in generated, (
        "seed-secrets.sh seeds a database password, which strands create-data-tier.sh in a branch "
        "needing versions.access that the deploy identity does not hold")
    assert 'ensure_container "${PG_SECRET}"' in text, (
        "the container must still exist so the data tier can add a version to it")



def test_the_data_tier_exchanges_custom_routes_so_memorystore_is_reachable():
    """Without this, Memorystore is unreachable from Cloud Run and the symptom is misleading:
    Cloud SQL on the SAME reserved range, over the SAME Cloud Run VPC egress, works - the schema
    migration connects and completes - while every Redis connection times out. Range, peering,
    egress setting and firewall all look correct, because they are.

    Never hit before because hexera-dev's Memorystore predates this script: it was made by hand in
    DIRECT_PEERING mode with its own `redis-peer-...` peering and never used the servicenetworking
    path. This script has always created Memorystore with PRIVATE_SERVICE_ACCESS, so the first
    environment provisioned end to end BY the script is the first to exercise the combination.

    Verified live: enabling the exchange turned a timing-out job into `queue_depth=0 published`.
    """
    text = (SCRIPTS / "create-data-tier.sh").read_text(encoding="utf-8")
    assert "--export-custom-routes" in text and "--import-custom-routes" in text, (
        "the servicenetworking peering does not exchange custom routes, so Memorystore is "
        "unreachable from Cloud Run while Cloud SQL on the same range works")
    # Applied unconditionally: an environment built before this existed needs it too, and the
    # update is idempotent.
    peering_block = text[text.index("PEERING_DISPOSITION=created"):text.index("# 2) the Cloud SQL")]
    assert "peerings update" in peering_block


def test_the_queue_depth_publisher_tolerates_a_cold_vpc_attach():
    """Measured, not guessed. A probe job with this publisher's own image, service account and
    egress settings took 11.2 seconds to complete the TCP handshake to Memorystore over private
    service access, then got an immediate +PONG. The publisher allowed 5 seconds, so it failed
    roughly four runs in five and succeeded only when the interface happened to attach quickly.

    Invisible in the environment it has always run in: shared dev's Memorystore predates the
    provisioning script, was built in DIRECT_PEERING mode, and its publisher runs every two
    minutes and is never cold.
    """
    src = (REPO / "deploy" / "gcp" / "worker" / "queue_depth_publisher.py").read_text("utf-8")
    assert "REDIS_CONNECT_TIMEOUT_SECONDS = 30" in src, (
        "the connect timeout must exceed the measured 11.2s cold VPC attach")
    assert "REDIS_CONNECT_ATTEMPTS" in src, (
        "one attempt leaves the run dependent on a single variable-cost attach")
    # The READ timeout stays low on purpose: once connected, a slow LLEN means something is wrong.
    assert "REDIS_READ_TIMEOUT_SECONDS = 5" in src


def test_the_autoscaler_attachment_is_deferred_when_there_is_no_fleet():
    """Stage 13 owns the autoscaler's metric wiring; stage 17 creates the managed instance group
    and deliberately never reconciles the policy, because its sizing belongs to the admin console.
    That split is right and it assumes the group already exists - true of every environment whose
    fleet predates this tooling, false of every environment built from nothing:

        ERROR: The resource '.../instanceGroupManagers/dev-workers' was not found

    The publisher, its schedule and its identity are the valuable half of the stage and are all
    established before this point. Only the attachment defers.
    """
    text = (SCRIPTS / "create-queue-depth-publisher.sh").read_text(encoding="utf-8")
    guard = text.index("instance-groups managed describe")
    apply_at = text.index("instance-groups managed set-autoscaling")
    assert guard < apply_at, (
        "create-queue-depth-publisher.sh attaches the autoscaling policy without first checking "
        "that the group exists, so a first deploy fails at stage 13")



# ---------------------------------------------------------------------------------------------
# 9. one Redis, many environments


def test_the_redis_key_prefix_defaults_to_empty():
    """An empty prefix is exactly today's behaviour, which is what lets shared dev, prod and the
    local compose stack take this change with no migration of any kind."""
    import meshpipeline.settings.providers as provcfg
    assert provcfg.REDIS_KEY_PREFIX == "", (
        "a non-empty default would move every existing deployment's queues to new Redis keys on "
        "the next roll, stranding whatever was already enqueued under the old ones")


def test_the_celery_app_isolates_broker_and_results_by_prefix():
    """Personal environments share one Memorystore instance and Celery's queue names are
    hardcoded literals (task_routes below), so without a prefix two of them consume each other's
    tasks - a developer's job running against somebody else's worker fleet."""
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
    for which, opts in (("broker", celery_app.conf.broker_transport_options),
                        ("result backend", celery_app.conf.result_backend_transport_options)):
        assert "global_keyprefix" in (opts or {}), (
            f"the {which} carries no global_keyprefix; prefixing only one of the two leaves the "
            f"other colliding in the shared keyspace")


def test_an_unrecorded_object_store_key_is_adopted_rather_than_reminted():
    """A personal environment pins no access id - the value does not exist until its first deploy
    - so every later run arrives with nothing recorded. Without adoption that reads as "no usable
    key exists" and mints another, every run, until Google's five-key ceiling stops the deploy
    dead. Carrying the id in a repository variable is what used to prevent this."""
    body = (SCRIPTS / "create-object-storage.sh").read_text(encoding="utf-8")
    assert "ADOPTED" in body, "nothing adopts an existing key, so a personal run re-mints forever"
    assert re.search(r'\$\{ACTIVE_COUNT\}"?\s*\]?\s*-eq\s+1|-eq\s+1\s*\]', body), (
        "adoption must require EXACTLY ONE active key: GCS will not say which key a stored secret "
        "belongs to, so adopting one of several is a coin flip whose losing side is a store that "
        "authenticates as nobody")


def test_adoption_cannot_follow_a_listing_that_could_not_be_read():
    """An unreadable key listing is INCONCLUSIVE, not proof of absence - the script says so at
    length. Adoption must sit after that refusal, or a missing storage.hmacKeys.list permission
    would present as a key that does not exist."""
    body = (SCRIPTS / "create-object-storage.sh").read_text(encoding="utf-8")
    assert body.index('HMAC_LIST_READ_OK}" = "0"') < body.index("ADOPTED"), (
        "the adopt branch precedes the unreadable-listing refusal")


def test_the_worker_settings_object_is_published_when_nothing_has():
    """new-env.sh used to publish .env.example into the transfer bucket at creation time. With
    creation gone, a first personal deploy would hand create-worker-fleet.sh a WORKER_ENV_URI
    naming an object nobody had written, and the `workers` component would fail on an environment
    that otherwise looks finished. The fleet publishes it if it is missing."""
    body = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
    assert ".env.example" in body, "nothing publishes the worker settings object any more"
    assert "storage cp" in body, "the object is never written"
    assert "buckets create" in body, (
        "a first deploy has no transfer bucket either - publishing into one that does not exist "
        "fails exactly where creating the fleet would have")


def test_the_depth_publisher_reads_the_key_the_workers_write():
    """Celery stores a queue as a Redis list under <global_keyprefix><queue name>, and
    queue_depth_publisher.py does a bare LLEN of whatever it is handed. Handed the unprefixed name
    in a prefixed environment it reads a key nobody writes: depth publishes as 0 forever and the
    autoscaler never adds an instance, however long the real queue gets. Nothing errors."""
    body = (SCRIPTS / "create-queue-depth-publisher.sh").read_text(encoding="utf-8")
    assert "REDIS_KEY_PREFIX" in body, "the publisher is pointed at an unprefixed key"
    assert "simulation_jobs" in body


def test_an_unprefixed_environment_still_names_the_bare_queue():
    """Shared dev and production set no prefix, and their queues are the ones that already exist -
    so the default has to collapse to exactly `simulation_jobs`."""
    body = (SCRIPTS / "create-queue-depth-publisher.sh").read_text(encoding="utf-8")
    line = next(ln for ln in body.splitlines() if ln.startswith("QUEUE_NAME="))
    out = subprocess.run(["bash", "-c", f"{line}\nprintf '%s' \"$QUEUE_NAME\""],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert out.stdout == "simulation_jobs", out.stdout
    out2 = subprocess.run(["bash", "-c", f"{line}\nprintf '%s' \"$QUEUE_NAME\""],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "REDIS_KEY_PREFIX": "dev-pranav:"})
    assert out2.stdout == "dev-pranav:simulation_jobs", out2.stdout


def test_creating_an_environment_needs_no_second_command():
    """The whole point of the change: a deploy into an environment that does not exist creates it.
    There is no script to run first, no owner credential to hold, and nothing to register - so
    there must be no `new-env` left advertising otherwise."""
    assert not (SCRIPTS / "new-env.sh").exists(), (
        "new-env.sh is back; an environment created out-of-band would now be created in the wrong "
        "shape - a project rather than a prefix")
    root = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "new-env" not in root, "the retired target is still advertised in the Makefile"
    assert "destroy-env" in root, "teardown is still a command and must stay reachable"


def test_nothing_reads_the_retired_register():
    """HEXERA_PERSONAL_ENVS resolved a slug to a project number. Nothing resolves a slug that way
    any more, and a leftover read would be a deploy target nobody reviews."""
    assert "HEXERA_PERSONAL_ENVS" not in WF.read_text(encoding="utf-8")
    assert "HEXERA_PERSONAL_ENVS" not in DESTROY_ENV.read_text(encoding="utf-8")


def test_the_queue_depth_publisher_can_write_its_metric(tmp_path):
    """Writing a custom metric needs roles/monitoring.metricWriter bound at the PROJECT level, and
    granting a project-level role means setIamPolicy on the project - the one authority the deploy
    identity is deliberately built without, because a run that can grant is a run that can grant
    itself anything.

    So a freshly created dev-<slug>-queue-depth account could never be given the role it needs,
    and stage 13 would fail its smoke run with HTTP 403 on every personal deploy. It reuses the
    account that already holds the grant. The metric's series is labelled with the deployment's
    own namespace, so two environments publishing through one account stay two series."""
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="pranav", DISPATCH_FLEET="true")
    assert personal["queue_depth_service_account"] == "dev-queue-depth", (
        "a personal environment must reuse the identity that already holds metricWriter; its own "
        "would be created without the role and nothing in the deploy could grant it")
    wf = WF.read_text(encoding="utf-8")
    assert "QUEUE_DEPTH_SERVICE_ACCOUNT: ${{ needs.target.outputs.queue_depth_service_account }}" in wf, (
        "the picker emits the identity but the provision job never passes it, so the script falls "
        "back to <deployment-id>-queue-depth and the 403 returns")


def test_the_celery_prefix_reaches_the_scripts_that_need_it():
    """A picker output that no job maps to an environment variable does nothing at all. Both
    consumers matter: the API and workers read the prefix to find their queues, and the depth
    publisher reads it to measure the same key."""
    wf = WF.read_text(encoding="utf-8")
    assert "REDIS_KEY_PREFIX: ${{ needs.target.outputs.redis_key_prefix }}" in wf, (
        "the prefix is emitted and never consumed, so every environment would share one keyspace "
        "while the workflow looks as though it had separated them")


def test_the_first_deploy_is_documented_as_two_runs():
    """A console is given the origin of the API it proxies to, that origin is the API service's
    Cloud Run URL, and Google assigns one only once the service exists - so on an environment
    where the API has never been deployed, validate-config.sh refuses at stage 2:

        CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not

    The gate is correct. What was wrong, and stayed wrong long enough to be worth a test, was the
    instructions telling people to tick `console` on the very first run."""
    doc = (REPO / "docs" / "deployment" / "personal-environments.md").read_text(encoding="utf-8")
    # The FIRST of the two documented runs: from the first `gh workflow run` to the second.
    runs = doc.split("gh workflow run")
    assert len(runs) >= 3, "the guide no longer shows two runs for a first deploy"
    first_cmd = runs[1]
    assert "-f console=true" not in first_cmd, (
        "the guide tells the operator to deploy the console on the first run, which stage 2 "
        "refuses - the API has no URL yet")
    assert "-f fleet=true" in first_cmd, (
        "the first run must create the fleet, or the second run has no instance group for the "
        "autoscaler to attach to")
    assert "-f app=true" in first_cmd and "-f data=true" in first_cmd, (
        "the first run must ship the code and build the data tier it runs against")


def test_the_celery_prefix_reaches_the_running_containers():
    """Wiring the prefix into the deploy does nothing on its own - the API and the workers read it
    at runtime. An API rolled WITHOUT the prefix while the workers carry one enqueues to keys no
    worker reads, so jobs sit in a queue nobody is watching rather than failing."""
    api = (SCRIPTS / "create-api-service.sh").read_text(encoding="utf-8")
    assert "REDIS_KEY_PREFIX=${REDIS_KEY_PREFIX:-}" in api, (
        "the API container never receives the prefix, so it would use the shared keyspace")
    fleet = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
    assert "redis-key-prefix=" in fleet, "the fleet's metadata never carries the prefix"
    startup = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text(encoding="utf-8")
    assert "redis-key-prefix" in startup and "REDIS_KEY_PREFIX=" in startup, (
        "startup.sh does not turn the metadata key into the worker's environment, so the fleet "
        "boots with an empty prefix however the template was written")


def test_the_prefixed_queue_name_is_composed_where_the_file_cannot_overwrite_it():
    """lib.sh's load_env sources the generated env with `set -a`, so the FILE wins over the
    environment. bootstrap-env.sh writes QUEUE_NAME, which means a default computed in
    create-queue-depth-publisher.sh is overwritten by the file's value and never takes effect -
    the publisher would go on reading the unprefixed key, publish 0 forever, and the autoscaler
    would never add an instance. So the prefix has to be applied where the file is WRITTEN."""
    boot = (SCRIPTS / "bootstrap-env.sh").read_text(encoding="utf-8")
    assert "QUEUE_NAME=${QUEUE_NAME:-${REDIS_KEY_PREFIX:-}simulation_jobs}" in boot, (
        "the generated env writes an unprefixed QUEUE_NAME, which load_env then makes win over "
        "anything the publisher computes")
    assert "REDIS_KEY_PREFIX=${REDIS_KEY_PREFIX:-}" in boot, (
        "the prefix itself is not carried in the generated env, so scripts that load_env lose it")


def test_the_generated_queue_name_collapses_to_the_bare_queue_without_a_prefix():
    """Shared dev and production set no prefix and their queues are the ones that already exist,
    so the composed value has to be exactly `simulation_jobs` for them."""
    expr = 'echo "${QUEUE_NAME:-${REDIS_KEY_PREFIX:-}simulation_jobs}"'
    bare = subprocess.run(["bash", "-c", expr], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"})
    assert bare.stdout.strip() == "simulation_jobs", bare.stdout
    pref = subprocess.run(["bash", "-c", expr], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "REDIS_KEY_PREFIX": "dev-pranav:"})
    assert pref.stdout.strip() == "dev-pranav:simulation_jobs", pref.stdout


# ---------------------------------------------------------------------------------------------
# 10. the checkboxes are tiers; DEPLOY_COMPONENTS is stages

_KNOWN = "images data storage migrate queue workers console admin outreach edge".split()


def _components(tmp_path, **boxes) -> list[str]:
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="", **{
        f"DISPATCH_{b.upper()}": "true" for b in boxes})
    assert rc == 0, log
    return out["components"].split(",") if out["components"] else []


def test_the_app_tier_ships_the_code_and_the_schema_it_expects(tmp_path):
    """One box for the things that move together when you ship. Shipping a new image against last
    week's schema was previously two boxes and a piece of knowledge about which."""
    assert _components(tmp_path, app=True) == ["images", "storage", "migrate"]


def test_the_data_tier_carries_the_schema_onto_the_database_it_creates(tmp_path):
    """A database created without its schema is an empty one, and nothing says so until something
    queries it."""
    assert _components(tmp_path, data=True) == ["data", "migrate"]


def test_the_fleet_tier_carries_the_signal_that_sizes_it(tmp_path):
    """A worker group without the queue-depth publisher never scales - it sits at its floor
    forever, which on a dev environment's floor of zero means nothing runs at all.

    The order matters and is stage order, not preference: stage 13 establishes the publisher and
    stage 18 creates the group."""
    assert _components(tmp_path, fleet=True) == ["queue", "workers"]


def test_the_schema_is_requested_once_when_both_tiers_want_it(tmp_path):
    """migrate belongs to `app` and to `data`. deploy.sh matches component NAMES so a repeat would
    not break it, but a log reading `migrate,migrate` invites the reader to ask whether the schema
    moved twice - and the schema is the one stage where that question matters."""
    parts = _components(tmp_path, app=True, data=True)
    assert parts.count("migrate") == 1, parts
    assert parts == ["images", "data", "storage", "migrate"]


def test_every_tier_expands_only_to_stages_deploy_sh_knows(tmp_path):
    """DEPLOY_COMPONENTS is validated by deploy.sh against its `_known` list, and a name that is
    not on it stops the run. This is the test that catches a tier renamed on one side only."""
    known = set(_KNOWN)
    for box in ("app", "data", "fleet", "console", "admin", "outreach", "edge"):
        parts = _components(tmp_path, **{box: True})
        assert parts, f"the `{box}` box selected no stage at all"
        unknown = [c for c in parts if c not in known]
        assert not unknown, f"`{box}` expands to {unknown}, which deploy.sh has no stage for"


def test_the_component_list_is_emitted_in_stage_order(tmp_path):
    """Composed in the order deploy.sh's own `_known` line lists them, so the deploy log reads the
    same way however the boxes were ticked."""
    parts = _components(tmp_path, app=True, data=True, fleet=True, console=True,
                        admin=True, outreach=True, edge=True)
    assert parts == _KNOWN, parts


def test_ticking_every_box_deploys_every_stage(tmp_path):
    """The roster has no 'all' shortcut on purpose, so ticking everything must be the way to get
    everything - if a stage existed that no box reached, it could only ever run on a release tag."""
    assert set(_components(tmp_path, app=True, data=True, fleet=True, console=True,
                           admin=True, outreach=True, edge=True)) == set(_KNOWN)


# ---------------------------------------------------------------------------------------------
# 11. binding a secret to an identity that was created seconds ago


def test_secret_bindings_ride_out_the_identity_propagation_window():
    """A service account is not visible to IAM for a few seconds after it is created, so the first
    binding attempted against it fails with "Service account ... does not exist" - a propagation
    delay reported as a missing resource.

    This is not hypothetical and it is not cosmetic. On the first personal deploy that reached the
    console stage, the identity was created at :56, the binding for console-auth-secret failed at
    :58, and the two bindings that followed at :59 and :01 both succeeded. The failure was a
    warning, so the rollout proceeded and then died several minutes later on a Cloud Run IAM check
    naming the secret rather than the race.

    It stopped being rare when environments began being created BY the deploy: every runtime
    identity is now seconds old on a first deploy, where before an owner had made them in advance.
    """
    lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
    assert "grant_secret_accessor()" in lib, "there is no retrying helper for this binding"

    # Every stage that creates a runtime identity and then binds a secret to it must use it - a
    # single raw binding left behind is a single stage that still loses the race.
    for script in ("create-console-service.sh", "create-api-service.sh", "create-admin-service.sh",
                   "create-worker-fleet.sh", "run-migrations.sh", "create-object-storage.sh"):
        body = (SCRIPTS / script).read_text(encoding="utf-8")
        assert "grant_secret_accessor" in body, f"{script} binds secrets without the retry"
        raw = [ln for ln in body.splitlines()
               if "secrets add-iam-policy-binding" in ln and not ln.strip().startswith("#")
               and "gcloud secrets add-iam-policy-binding" not in ln]
        assert not raw, f"{script} still has an unretried binding: {raw}"


def test_the_retry_actually_retries_and_eventually_gives_up():
    """A helper that returns success on the first failure would be worse than none: the caller's
    warning would still fire and the rollout would still die, but the log would claim a retry."""
    lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
    body = lib[lib.index("grant_secret_accessor()"):]
    body = body[:body.index("\n}")]
    assert "for attempt in" in body, "the helper makes a single attempt"
    assert "sleep" in body, "the helper retries without waiting, so it retries inside the window"
    assert "return 1" in body, "the helper never reports failure, so callers cannot warn"


def test_the_runtime_identities_are_created_early_not_moments_before_use():
    """IAM does not make a new service account usable the instant it is created. A stage that
    creates its identity and then deploys a workload running AS it, seconds later, fails:

        Permission 'iam.serviceaccounts.actAs' denied on service account
        dev-pranav-migrate@hexera-dev.iam.gserviceaccount.com (or it may not exist)

    The deployer holds iam.serviceAccountUser project-wide, so the permission IS granted - the
    account is simply not visible to the actAs check yet, which Google's own message hints at with
    "or it may not exist".

    Creating the whole roster at stage 6 puts minutes between creation and first use rather than
    seconds. new-env.sh used to do this as an owner's act; what made it work was never that an
    owner did it, it was that it happened early.
    """
    body = (SCRIPTS / "create-service-accounts.sh").read_text(encoding="utf-8")
    for purpose in ("-api", "-console", "-migrate", "-queue-depth", "-workers"):
        assert purpose in body, f"the roster does not establish the {purpose} identity"
    # It must run unconditionally - a roster gated on a component would leave exactly the tiers a
    # partial first deploy selects without their identities.
    deploy = (SCRIPTS / "deploy.sh").read_text(encoding="utf-8")
    i = deploy.index("create-service-accounts.sh")
    preceding = deploy[:i].rsplit("stage ", 1)[1]
    assert "_selected" not in preceding and "want " not in preceding, (
        "create-service-accounts.sh sits behind a component gate, so a deploy that does not select "
        "that component creates no identities early and races again")


def test_creating_an_identity_early_cannot_fail_the_whole_deploy():
    """This stage is an optimisation of TIMING, not the authority on these accounts - every stage
    still creates the identity it needs. Dying here would turn 'could not create an identity
    early' into 'could not deploy at all', which is the worse trade."""
    body = (SCRIPTS / "create-service-accounts.sh").read_text(encoding="utf-8")
    roster = body[body.index("Runtime identities the later stages"):]
    assert "warn " in roster, "a failure to pre-create is fatal"
    assert "die " not in roster, "a failure to pre-create kills the deploy"


# ---------------------------------------------------------------------------------------------
# 12. the keyspace, not just the Celery queues


def test_every_redis_key_goes_through_the_keyspace_prefix():
    """Celery's global_keyprefix isolates the broker and the result backend and NOTHING ELSE. The
    dead letter queue, the inference feed, job event logs, websocket tickets, rate limits, delivery
    guards and capacity leases are addressed by adapters that talk to Redis directly, and their key
    names are literals - `simulation:dlq` is the same string in every deployment.

    Personal environments share one Memorystore instance whose URL carries no authentication, so
    without this every environment reads, writes and trims those keys out from under every other.
    """
    import meshpipeline.settings.providers as provcfg
    from meshpipeline.events import channels
    from meshpipeline.redis_keys import k

    before = provcfg.REDIS_KEY_PREFIX
    try:
        provcfg.REDIS_KEY_PREFIX = ""
        assert channels.channel_for("J") == "jobs:J:events", (
            "an empty prefix must be byte-for-byte today's behaviour, or every existing deployment "
            "moves to new keys on the next roll and strands what is already enqueued")
        assert k("simulation:dlq") == "simulation:dlq"

        provcfg.REDIS_KEY_PREFIX = "dev-slug:"
        for name, value in (("channel", channels.channel_for("J")),
                            ("event log", channels.log_key_for("J")),
                            ("sequence", channels.seq_key_for("J")),
                            ("op set", channels.opkey_set_for("J")),
                            ("fence", channels.fence_key_for("J")),
                            ("dead letter", k("simulation:dlq")),
                            ("inference feed", k("inference:calls"))):
            assert value.startswith("dev-slug:"), f"the {name} key is not in this deployment's keyspace: {value}"
    finally:
        provcfg.REDIS_KEY_PREFIX = before


def test_no_adapter_addresses_a_literal_redis_key_directly():
    """The guarantee above is only worth as much as its coverage: one adapter still building a key
    by hand is one class of state still shared across every environment."""
    import re
    adapters = (REPO / "src" / "meshpipeline" / "adapters")
    offenders = []
    for path in list(adapters.rglob("*.py")) + [REPO / "src" / "meshpipeline" / "events" / "channels.py"]:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "redis.call" in line or line.strip().startswith("#"):
                continue
            # a Redis command handed a literal or f-string key that was not wrapped in k(...)
            if re.search(r'\.(lpush|ltrim|lrange|rpush|getdel|incr|sadd|zadd|expire|publish)\(\s*f?"', line):
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "these address Redis keys without the keyspace prefix:\n  " + "\n  ".join(offenders)
