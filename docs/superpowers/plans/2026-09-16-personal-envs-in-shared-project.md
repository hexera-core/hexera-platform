# Personal environments inside hexera-dev Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deploy into a personal environment that does not exist creates it, by making a personal environment a set of `dev-<slug>-` resources inside `hexera-dev` rather than a project of its own.

**Architecture:** The target picker stops resolving a slug through the `HEXERA_PERSONAL_ENVS` register and instead pins `hexera-dev` literals plus a `dev-<slug>-` prefix on every resource name. The prefix mechanism already exists — every `create-*.sh` derives its names from `DEPLOYMENT_ID`. Two project-scoped IAM grants, gated on an opt-in flag, let the existing stages create the identities and the HMAC key they already try to create.

**Tech Stack:** GitHub Actions (`deploy.yml`), Bash (`deploy/gcp/scripts/*.sh`), Python 3 (Celery 5.4.0, pytest), Google Cloud (Cloud Run, Cloud SQL, Memorystore, IAM, GCS).

**Spec:** `docs/superpowers/specs/2026-09-16-personal-envs-in-shared-project-design.md`

## Global Constraints

- `hexera-dev` is project id `hexera-dev`, project number `224734058693`. Both are literals; never derived, never read from a variable.
- Slug rule: lowercase letters, digits, hyphens; starts with a letter; no trailing hyphen; **max 14 characters**; not `dev`, `prod`, `production`, `shared`, `main`, `staging`.
- Every personal resource name is `dev-<slug>-<thing>`. No personal name may equal a shared-dev name.
- Shared dev and `hexera-prod` outputs must be **byte-identical** to today's after every task.
- The new Celery key-prefix setting defaults to **empty**, so dev, prod and local compose are unchanged.
- Tests run the picker as a real program via `_run_picker` in `tests/unit/deploy/test_personal_environments.py`. Assert behaviour, not source text.
- Run the suite with `pytest tests/unit/deploy/test_personal_environments.py -q`.

## File Structure

| File | Responsibility after this plan |
| --- | --- |
| `.github/workflows/deploy.yml` | Picker resolves a slug to `hexera-dev` + `dev-<slug>` with no register lookup |
| `deploy/gcp/scripts/create-object-storage.sh` | Adopts the single ACTIVE HMAC key when none is recorded |
| `deploy/gcp/scripts/create-workload-identity.sh` | Roster gains two roles under `PERSONAL_ENV_HOST` |
| `deploy/gcp/scripts/create-worker-fleet.sh` | Publishes `worker.env` when the object is absent |
| `deploy/gcp/scripts/create-queue-depth-publisher.sh` | Reads the prefixed queue key |
| `deploy/gcp/scripts/destroy-env.sh` | Label-driven teardown inside the host project |
| `deploy/gcp/scripts/new-env.sh` | **Deleted** |
| `src/meshpipeline/settings/inventory.py` | Declares the Celery key-prefix setting |
| `src/meshpipeline/adapters/pipeline_execution/celery_app.py` | Applies the prefix to broker and result backend |
| `tests/unit/deploy/test_personal_environments.py` | The contract, retargeted |
| `docs/deployment/personal-environments.md` | Rewritten |
| `Makefile`, `deploy/gcp/Makefile` | `new-env` targets removed |

---

### Task 1: Celery key prefix

**Files:**
- Modify: `src/meshpipeline/settings/inventory.py` (the `Group("Redis (broker + pub/sub)")` block, ~line 316)
- Modify: `src/meshpipeline/settings/providers.py` (~line 139, beside `REDIS_URL`)
- Modify: `src/meshpipeline/adapters/pipeline_execution/celery_app.py:16`
- Modify: `.env.example` (regenerated, not hand-edited)
- Test: `tests/unit/deploy/test_personal_environments.py`

**Interfaces:**
- Produces: env var `CELERY_KEY_PREFIX` (default `""`); `provcfg.CELERY_KEY_PREFIX: str`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_celery_key_prefix_defaults_to_empty():
    """An empty prefix is exactly today's behaviour, which is what lets shared dev, prod and the
    local compose stack take this change with no migration."""
    import meshpipeline.settings.providers as provcfg
    assert provcfg.CELERY_KEY_PREFIX == ""


def test_the_celery_app_isolates_broker_and_results_by_prefix(monkeypatch):
    """Two personal environments share one Memorystore instance, and Celery's queue names are
    hardcoded literals - so without a prefix they would consume each other's tasks."""
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
    for opts in (celery_app.conf.broker_transport_options,
                 celery_app.conf.result_backend_transport_options):
        assert "global_keyprefix" in opts, (
            "the broker and the result backend must BOTH carry global_keyprefix; prefixing only "
            "the broker leaves results colliding in the shared keyspace")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k celery`
Expected: FAIL — `AttributeError: module ... has no attribute 'CELERY_KEY_PREFIX'`

- [ ] **Step 3: Declare the setting**

In `inventory.py`, inside `Group("Redis (broker + pub/sub)", vars=[...])`, beside `REDIS_URL`:

```python
        EnvVar("CELERY_KEY_PREFIX", "",
               help="prefix for every Celery broker and result key; set per personal environment "
                    "so environments sharing one Redis do not consume each other's tasks"),
```

In `providers.py`, beside `REDIS_URL`:

```python
CELERY_KEY_PREFIX: str = optional_env("CELERY_KEY_PREFIX", "")
```

- [ ] **Step 4: Apply it**

In `celery_app.py`, inside `celery_app.conf.update(...)`:

```python
    # ONE REDIS, MANY ENVIRONMENTS. The queue names below are literals, so two deployments sharing
    # a Memorystore instance would consume each other's tasks. The prefix is empty by default,
    # which is byte-for-byte today's behaviour; a personal environment sets it to `dev-<slug>:`.
    # BOTH options are set: prefixing only the broker leaves results colliding.
    broker_transport_options={"global_keyprefix": provcfg.CELERY_KEY_PREFIX},
    result_backend_transport_options={"global_keyprefix": provcfg.CELERY_KEY_PREFIX},
```

- [ ] **Step 5: Regenerate `.env.example`**

Run the generator the repo already uses (check `make help` for the target; `.env.example` is generated from `inventory.py` and must not be hand-edited). Verify `CELERY_KEY_PREFIX=` appears under the Redis group with an empty value.

- [ ] **Step 6: Run tests**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k celery`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/settings/inventory.py src/meshpipeline/settings/providers.py \
        src/meshpipeline/adapters/pipeline_execution/celery_app.py .env.example \
        tests/unit/deploy/test_personal_environments.py
git commit -m "Celery keys carry a per-environment prefix, empty everywhere it is not set"
```

---

### Task 2: Adopt the single ACTIVE HMAC key

**Files:**
- Modify: `deploy/gcp/scripts/create-object-storage.sh:221-256`
- Test: `tests/unit/deploy/test_personal_environments.py`

**Interfaces:**
- Consumes: nothing. Produces: the rule that lets the picker stop pinning `minio_access_key`.

- [ ] **Step 1: Write the failing test**

```python
def test_an_unrecorded_key_is_adopted_rather_than_reminted():
    """A personal run pins no access id, so without this every run after the first mints another
    key and walks the account to Google's five-key ceiling - which is what the register existed
    to prevent. A per-slug service account holds at most one key, so adoption is unambiguous."""
    body = (SCRIPTS / "create-object-storage.sh").read_text(encoding="utf-8")
    assert "ADOPT" in body, "the adopt-when-unrecorded branch is missing"
    assert re.search(r'ACTIVE_COUNT.*-eq\s+1', body), (
        "adoption must require EXACTLY ONE active key; adopting one of several would pick a key "
        "whose secret this deployment may not hold")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k adopted`
Expected: FAIL

- [ ] **Step 3: Implement**

Read `create-object-storage.sh:221-256` first. After `RECORDED_IS_ACTIVE` is computed and before the mint branch, add:

```bash
# ADOPT AN UNRECORDED KEY rather than mint beside it.
#
# A personal environment pins no MINIO_ACCESS_KEY - the value does not exist until its first
# deploy - so every later run arrives with nothing recorded. The old behaviour concluded "no
# usable key exists" and minted another, every run, until the five-key ceiling stopped the
# deploy dead; carrying the id in a repository variable is what used to avoid that.
#
# EXACTLY ONE is the whole safety argument. This service account is created by this tooling,
# is used by nothing else, and holds at most one key - so a single ACTIVE key is necessarily
# the one whose secret sits in this deployment's secret. Two would not be, and adopting the
# wrong one produces a store that authenticates as nobody, so that case falls through to the
# existing guards rather than guessing.
if [ -z "${RECORDED_ACCESS_ID}" ] && [ "${ACTIVE_COUNT}" -eq 1 ] && [ "${SECRET_HAS_VERSION}" = "1" ]; then
  RECORDED_ACCESS_ID="$(printf '%s\n' "${ACTIVE_IDS}" | head -1)"
  RECORDED_IS_ACTIVE=1
  log "hmac key        ${RECORDED_ACCESS_ID}  (ADOPTED - the one ACTIVE key for ${OBJECT_STORE_SA_EMAIL})"
fi
```

Confirm the existing `HMAC_LIST_READ_OK=0` refusal still precedes this — a listing this identity could not read must never reach adoption.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k adopted && bash -n deploy/gcp/scripts/create-object-storage.sh`
Expected: PASS, no syntax errors

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/create-object-storage.sh tests/unit/deploy/test_personal_environments.py
git commit -m "Adopt the one active object-store key instead of minting beside it"
```

---

### Task 3: Widen the deployer, on the host project only

**Files:**
- Modify: `deploy/gcp/scripts/create-workload-identity.sh:320-323` (comment) and `DEPLOYER_ROLES`
- Test: `tests/unit/deploy/test_personal_environments.py` — rewrite `test_a_personal_environment_widens_no_deploy_identity` (line 316)

- [ ] **Step 1: Rewrite the existing test**

That test asserts the opposite of this design. Replace it with a scoping test:

```python
def test_the_deployer_is_widened_only_on_a_personal_environment_host():
    """hexera-prod's roster must be byte-identical to today's. The two roles a personal
    environment needs are real authority - minting identities and object-store keys - so they are
    gated on an explicit opt-in rather than granted wherever this script happens to run."""
    body = (SCRIPTS / "create-workload-identity.sh").read_text(encoding="utf-8")
    assert "PERSONAL_ENV_HOST" in body, "the widening is ungated"
    for role in ("roles/iam.serviceAccountAdmin", "roles/storage.hmacKeyAdmin"):
        assert role in body, f"{role} is missing; the host cannot create a personal environment"
    assert "roles/owner" not in body.split("DEPLOYER_ROLES=(")[1].split(")")[0], (
        "roles/owner must remain absent everywhere")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k widened`
Expected: FAIL

- [ ] **Step 3: Implement**

Append to `DEPLOYER_ROLES` construction, after the array literal:

```bash
# A PERSONAL-ENVIRONMENT HOST holds two roles no other project's deployer does, because on this
# one project the deploy creates the environment it deploys to rather than finding it.
#
# WHY THEY ARE NEEDED. Every stage already creates the identity it needs - the API service, the
# console, the migration job, the queue-depth publisher, the worker fleet and the object store
# all call `iam service-accounts create` and all print a remediation when they cannot. They fail
# only for lack of the role, which is why creating an environment used to be an owner's act on a
# laptop. storage.hmacKeyAdmin is the same story for the object-store key.
#
# WHY IT IS OPT-IN. This roster is applied to every project this script runs against, so an
# ungated addition would widen hexera-prod's deployer as a side effect of a change aimed at
# development. Off by default means prod's roster is unchanged and turning it on is a visible
# line in a diff.
#
# WHAT IT COSTS, PLAINLY: a compromised workflow run against THIS project can mint service
# accounts and object-store keys in it. roles/owner and resourcemanager.projectIamAdmin remain
# absent here as everywhere, so it still cannot grant itself anything further or widen the
# provider that admitted it.
if [ "${PERSONAL_ENV_HOST:-0}" = "1" ]; then
  DEPLOYER_ROLES+=(
    roles/iam.serviceAccountAdmin        # the runtime identities each stage creates for its slug
    roles/storage.hmacKeyAdmin           # the slug's object-store key
  )
  log "personal-environment host - the roster carries iam.serviceAccountAdmin and storage.hmacKeyAdmin"
fi
```

- [ ] **Step 4: Amend the comment at lines 320-322**

It currently claims `iam.serviceAccountAdmin` is absent. Replace with text that states: absent by default and on `hexera-prod`; present on a personal-environment host, where it is what lets a developer's first deploy create their own environment; `roles/owner` and `resourcemanager.projectIamAdmin` absent everywhere.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k widened && bash -n deploy/gcp/scripts/create-workload-identity.sh`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/create-workload-identity.sh tests/unit/deploy/test_personal_environments.py
git commit -m "A personal-environment host's deployer may create the identities each stage needs"
```

---

### Task 4: The worker settings object

**Files:**
- Modify: `deploy/gcp/scripts/create-worker-fleet.sh` (near the `WORKER_ENV_URI` requirement)
- Test: `tests/unit/deploy/test_personal_environments.py`

**Interfaces:**
- Consumes: `WORKER_ENV_URI` from the picker (`gs://dev-<slug>-transfer-224734058693/worker.env`).

- [ ] **Step 1: Write the failing test**

```python
def test_the_worker_settings_object_is_published_when_absent():
    """new-env.sh used to publish .env.example to the transfer bucket. With it deleted, a first
    personal deploy would hand create-worker-fleet.sh a WORKER_ENV_URI naming an object nothing
    had written, and the `workers` component would fail on an environment that looks complete."""
    body = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
    assert ".env.example" in body, "nothing publishes the worker settings object any more"
    assert "storage cp" in body or "storage buckets create" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k worker_settings`
Expected: FAIL

- [ ] **Step 3: Implement**

Read the `WORKER_ENV_URI` handling first. Before the fleet is created, add: if the bucket named by `WORKER_ENV_URI` does not exist, create it (`--uniform-bucket-level-access --public-access-prevention`, matching `new-env.sh`); if the object does not exist, copy `${REPO_ROOT}/.env.example` to it. An existing object is left untouched — it may carry settings an operator changed. Carry over `new-env.sh`'s reasoning: the file is generated from `inventory.py`, carries policy and limits and no credential, and both a bucket object and instance metadata are readable by anyone who can describe the instance.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k worker_settings && bash -n deploy/gcp/scripts/create-worker-fleet.sh`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/create-worker-fleet.sh tests/unit/deploy/test_personal_environments.py
git commit -m "The fleet publishes its settings object when nothing else has"
```

---

### Task 5: The picker

The core change. Everything else exists to let this be correct.

**Files:**
- Modify: `.github/workflows/deploy.yml` — the personal branch of the `target` job (~lines 325-515)
- Test: `tests/unit/deploy/test_personal_environments.py`

**Interfaces:**
- Consumes: Task 2's adoption rule (no pinned access id), Task 4's published `worker.env`, Task 1's `CELERY_KEY_PREFIX`.
- Produces: outputs listed in spec §2.

- [ ] **Step 1: Delete the register tests**

Remove `test_an_unregistered_slug_cannot_deploy`, `test_a_non_numeric_project_number_is_refused`, `test_an_environment_registered_without_an_access_id_is_refused`. They assert a register this task removes. Remove `PERSONAL_ENVS` from `_run_picker`'s default env.

- [ ] **Step 2: Write the failing tests**

```python
def test_a_personal_run_resolves_to_the_shared_project(tmp_path):
    """No register, no project number to look up: the identity the picker names is shared dev's,
    which always exists. This is what makes an unknown slug deployable instead of an error."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="areen", DISPATCH_IMAGES="true")
    assert rc == 0, log
    assert out["project"] == "hexera-dev"
    assert out["deployment_id"] == "dev-areen"
    assert out["deployer_sa"] == "github-deployer@hexera-dev.iam.gserviceaccount.com"
    assert out["wif_provider"].startswith("projects/224734058693/")
    assert out["registry"] == "us-central1-docker.pkg.dev/hexera-dev/mesh"


def test_a_personal_run_shares_the_instances_but_not_the_database(tmp_path):
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="areen", DISPATCH_DATA="true")
    assert rc == 0, log
    assert out["cloudsql_instance"] == "hexera-dev-pg"
    assert out["redis_instance"] == "hexera-dev-redis"
    assert out["migrate_db_name"] == "meshpipeline_areen", (
        "a shared database would let one developer's migration move another's schema, which is "
        "the isolation a personal environment exists to provide")


def test_no_personal_name_can_collide_with_a_shared_one(tmp_path):
    """Asserted against shared dev's own output rather than a literal list, so a target added to
    one branch and not the other is caught here instead of in the project."""
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="areen", DISPATCH_IMAGES="true")
    _, shared, _ = _run_picker(tmp_path, DISPATCH_SLUG="", DISPATCH_IMAGES="true")
    named = ("mesh_job", "api_service", "console_service", "worker_mig",
             "mesh_bucket", "minio_bucket", "deployment_id")
    for key in named:
        assert personal[key] != shared[key], f"{key} is the same resource in both environments"
        assert "areen" in personal[key], f"{key}={personal[key]!r} carries no slug"


def test_a_personal_run_always_reconciles_its_object_store(tmp_path):
    """The access id is no longer pinned, so the storage stage is what supplies it in-run. An
    images-only run without it deploys an API whose adapter dials localhost:9000 and 503s every
    upload - which is what shipped on shared dev before its values were pinned."""
    rc, out, log = _run_picker(tmp_path, DISPATCH_SLUG="areen", DISPATCH_IMAGES="true")
    assert rc == 0, log
    assert "storage" in out["components"].split(","), out["components"]
    assert "minio_access_key" not in out, "a personal run must not pin an access id"
    assert out["minio_bucket"] == "dev-areen-artifacts-224734058693"
    assert out["minio_secret_key_secret"] == "minio-secret-key-areen", (
        "one secret container per slug; sharing it would have each environment overwrite the "
        "previous one's HMAC secret")


def test_a_slug_that_cannot_name_a_service_account_is_refused(tmp_path):
    """`dev-` + slug + `-queue-depth` must fit Google's 30-character service account id. A longer
    slug fails eleven stages into a deploy with an error naming the field and not the cause."""
    rc, _, log = _run_picker(tmp_path, DISPATCH_SLUG="a" * 15, DISPATCH_IMAGES="true")
    assert rc != 0
    assert "30" in log and "14" in log


def test_the_celery_prefix_isolates_a_personal_run_only(tmp_path):
    _, personal, _ = _run_picker(tmp_path, DISPATCH_SLUG="areen", DISPATCH_IMAGES="true")
    _, shared, _ = _run_picker(tmp_path, DISPATCH_SLUG="", DISPATCH_IMAGES="true")
    assert personal["celery_key_prefix"] == "dev-areen:"
    assert shared.get("celery_key_prefix", "") == "", (
        "shared dev must keep an empty prefix - its queues are the ones that already exist")
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q`
Expected: the new tests FAIL

- [ ] **Step 4: Rewrite the picker's personal branch**

Delete the `PERSONAL_ENVS:` env binding, the `_entry`/`_num`/`_access` parse and all three register guards. Add the 14-character check to the existing slug validation, naming the 30-character limit and the `-queue-depth` suffix. Set `_num=224734058693` and `_project=hexera-dev` as literals. Force `storage` into `_selected`. Emit every output from spec §2, keeping the empty `admin_*`, `outreach_*`, `*_domain` and `billing_export_table` outputs and their existing reasoning.

Rewrite the branch's prose comments: they currently explain the register at length. They must now explain why no register is needed, that the project and identity are shared dev's, that isolation is by name, and what that costs.

- [ ] **Step 5: Run the whole suite**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q`
Expected: PASS, including `test_the_existing_targets_are_untouched` and `test_a_tag_cannot_be_redirected_by_a_slug` unchanged.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/deploy.yml tests/unit/deploy/test_personal_environments.py
git commit -m "A slug resolves to dev-<slug> inside hexera-dev, with nothing to register"
```

---

### Task 6: The queue-depth publisher reads the prefixed key

**Files:**
- Modify: `deploy/gcp/scripts/create-queue-depth-publisher.sh:41`
- Test: `tests/unit/deploy/test_personal_environments.py`

**Interfaces:**
- Consumes: `CELERY_KEY_PREFIX` from the deployment env (Task 1, set by the picker in Task 5).

- [ ] **Step 1: Write the failing test**

```python
def test_the_depth_publisher_reads_the_prefixed_queue():
    """queue_depth_publisher.py does a bare llen(queue). Under a key prefix the unprefixed name is
    a key nobody writes, so the publisher would report 0 forever and the autoscaler would never
    add an instance - or, worse, read shared dev's depth and scale the wrong fleet."""
    body = (SCRIPTS / "create-queue-depth-publisher.sh").read_text(encoding="utf-8")
    assert "CELERY_KEY_PREFIX" in body, "the publisher is pointed at an unprefixed key"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k prefixed_queue`
Expected: FAIL

- [ ] **Step 3: Implement**

Change line 41 to prepend the prefix, with a comment explaining that Celery stores a queue as a Redis list under `<global_keyprefix><queue name>`, so the publisher must read the same key the workers write:

```bash
QUEUE_NAME="${QUEUE_NAME:-${CELERY_KEY_PREFIX:-}simulation_jobs}"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k prefixed_queue && bash -n deploy/gcp/scripts/create-queue-depth-publisher.sh`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/create-queue-depth-publisher.sh tests/unit/deploy/test_personal_environments.py
git commit -m "The depth publisher reads the key the workers actually write"
```

---

### Task 7: Label-driven teardown

**Files:**
- Rewrite: `deploy/gcp/scripts/destroy-env.sh` (currently 176 lines, deletes a project)
- Test: `tests/unit/deploy/test_personal_environments.py` — retarget `test_destroy_refuses_the_shared_projects`, `test_destroy_requires_positive_evidence_that_the_project_is_personal`, `test_the_two_reserved_slug_lists_agree`

- [ ] **Step 1: Write the failing tests**

```python
def test_destroy_refuses_a_reserved_slug():
    body = DESTROY_ENV.read_text(encoding="utf-8")
    for reserved in ("dev", "prod", "production", "shared", "main", "staging"):
        assert reserved in body
    assert "deployment-id" in body, "teardown must select by label, not by a guessed prefix"


def test_destroy_never_touches_the_shared_data_tier():
    """The instances are shared with shared dev and every other personal environment. Deleting
    one to tear down a sandbox would take everyone with it."""
    body = DESTROY_ENV.read_text(encoding="utf-8")
    assert "sql instances delete" not in body
    assert "redis instances delete" not in body


def test_destroy_reports_what_it_could_not_reach():
    """Deleting by label cannot cover a resource somebody made by hand that carries no label. The
    honest answer is to print the gap at the moment it matters, not to claim completeness."""
    body = DESTROY_ENV.read_text(encoding="utf-8")
    assert "skipped" in body.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k destroy`
Expected: FAIL

- [ ] **Step 3: Rewrite the script**

New header stating the responsibility honestly: deletes the resources labelled `deployment-id=dev-<slug>` in the host project, and cannot reach what carries no label. Refuse an empty or reserved slug. Refuse any project other than the configured host. Confirm before the first mutation (reuse `confirm` from `lib.sh`; `ASSUME_YES=1` honoured). Delete, in dependency order: Cloud Scheduler job, MIG + autoscaler + instance template, Cloud Run services and jobs, the three buckets, `meshpipeline_<slug>`, the slug's HMAC key, `minio-secret-key-<slug>`, then the six service accounts. End with a summary of what was deleted and what was found-but-skipped. Never touch `hexera-dev-pg` or `hexera-dev-redis`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k destroy && bash -n deploy/gcp/scripts/destroy-env.sh`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add deploy/gcp/scripts/destroy-env.sh tests/unit/deploy/test_personal_environments.py
git commit -m "Teardown deletes what it labelled and says what it could not reach"
```

---

### Task 8: Retire creation

**Files:**
- Delete: `deploy/gcp/scripts/new-env.sh`
- Modify: `Makefile:225-230`, `deploy/gcp/Makefile:18,68-69`
- Rewrite: `docs/deployment/personal-environments.md` (register section at line 228)
- Modify: `tests/unit/deploy/test_personal_environments.py` — delete the creation tests named in spec §9

- [ ] **Step 1: Delete the creation tests**

`test_creating_an_environment_is_not_something_ci_can_do`, `test_the_project_display_name_uses_only_characters_google_accepts`, `test_creation_establishes_what_the_first_deploy_cannot`, `test_new_env_hands_bootstrap_a_path_not_an_empty_file`, `test_rerunning_creation_reuses_the_object_store_key`, `test_the_runtime_identities_are_created_by_an_owner_not_by_the_deploy`, `test_domain_restricted_sharing_is_relaxed_for_the_project_and_said_out_loud`. Remove the `NEW_ENV` constant.

- [ ] **Step 2: Write the failing test**

```python
def test_creating_an_environment_needs_no_second_command():
    """The whole point: a deploy into an environment that does not exist creates it."""
    assert not (SCRIPTS / "new-env.sh").exists()
    root = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "new-env" not in root, "the retired target is still advertised"
    assert "destroy-env" in root, "teardown is still a command"
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/unit/deploy/test_personal_environments.py -q -k second_command`
Expected: FAIL

- [ ] **Step 4: Delete and update**

```bash
git rm deploy/gcp/scripts/new-env.sh
```

Remove the `new-env` targets from both Makefiles and drop `new-env` from `deploy/gcp/Makefile:18`'s `.PHONY`. Rewrite `Makefile:225-228`'s lifecycle comment as two commands — deploy, and destroy. Keep `seed-secrets`.

- [ ] **Step 5: Rewrite the documentation**

`docs/deployment/personal-environments.md`: a personal environment is `dev-<slug>` inside `hexera-dev`; it is created by deploying to it; the first deploy is still two runs (the console needs the API's URL); shared instances with an own database; the 14-character slug rule and why; teardown and what it cannot reach; and the two roles the host's deployer holds and what they cost. Delete the register section at line 228.

- [ ] **Step 6: Run the whole suite**

Run: `pytest tests/unit/deploy/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Retire new-env: the deploy creates the environment it deploys to"
```

---

### Task 9: Whole-suite verification

- [ ] **Step 1: Full unit suite**

Run: `pytest tests/unit -q`
Expected: PASS. Investigate any failure outside `tests/unit/deploy/` — the Celery change in Task 1 is the one with reach beyond the deploy tests.

- [ ] **Step 2: Shell syntax**

Run: `bash -n` over every modified script, and `shellcheck` if the repo runs it in CI.

- [ ] **Step 3: Workflow parses**

Run: `python -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('.github/workflows/deploy.yml').read_text())"`

- [ ] **Step 4: Confirm shared dev and prod are unmoved**

```bash
git diff origin/main -- .github/workflows/deploy.yml
```

Read the shared-dev and prod branches in the diff and confirm no output changed. This is the one regression that would be invisible in tests but visible in production.

- [ ] **Step 5: Commit any fixes**

## Remaining manual step

The `HEXERA_PERSONAL_ENVS` repository variable still holds `pranav=720680343126:GOOG1EX...`, pointing at a project deleted on 2026-09-16. Nothing reads it after this plan. Delete it at
`https://github.com/hexera-core/hexera-platform/settings/variables/actions`, or:

```bash
gh variable delete HEXERA_PERSONAL_ENVS --repo hexera-core/hexera-platform
```
