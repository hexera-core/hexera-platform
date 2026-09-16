# Personal environments inside hexera-dev, created by the deploy that needs them

Date: 2026-09-16
Status: approved, not yet implemented

## The problem this solves

A deploy into a personal environment that does not exist fails in the target picker:

```
Error: no personal environment named 'areen'. The repository variable HEXERA_PERSONAL_ENVS
does not carry a project number for it, which means the environment was never created or was
destroyed. Create it with: make new-env SLUG=areen
```

The message is accurate and the remedy works, but it puts a laptop, an owner's Google
credentials, a billing account and three minutes between a developer and their first deploy.
The environment should be created by the deploy that asks for it.

Making the *existing* design self-service was rejected: creating a project needs
`resourcemanager.projectCreator`, `billing.user` and `orgpolicy.policyAdmin`, so auto-provisioning
would put an identity that can mint billable projects behind a free-text box on
`workflow_dispatch`. Instead the shape of a personal environment changes.

## The decision

**A personal environment is a set of resources inside `hexera-dev`, named by
`DEPLOYMENT_ID=dev-<slug>`, not a project of its own.**

This reverses the one-project-per-developer decision recorded in the header of
`deploy/gcp/scripts/new-env.sh` and shipped in `5b84cc2`. That header's three arguments are
answered rather than ignored:

- **Teardown.** Was: deleting a project is one complete call. Now: teardown enumerates by label,
  and §7 states plainly what that cannot reach. This is a real loss and is accepted.
- **Blast radius.** Was: shared Cloud SQL, Memorystore, private-services range and quota. Now:
  accepted deliberately for the data tier (§5), and answered by per-slug naming for everything
  else. A developer still gets their own database, their own broker keyspace, their own services,
  buckets and identities.
- **The tooling already assumes it.** Was: `bootstrap-env.sh` defaults `DEPLOYMENT_ID` to the
  project id. It only *defaults* — `bootstrap-env.sh:89` reads `DEPLOYMENT_ID` when set, and every
  `create-*.sh` derives its resource names from it (`create-api-service.sh:41`,
  `create-object-storage.sh:44`, `create-queue-depth-publisher.sh:37-40`,
  `create-data-tier.sh:56,79`). The prefix mechanism this design needs is already the one in use.

What it buys: no project creation, no billing link, no per-environment workload identity pool, no
org-policy override, no secret seeding, and **no `HEXERA_PERSONAL_ENVS` register** — the variable
whose absence produced the error. A personal environment is created by running a deploy, and
nothing else.

## 1. Naming and the slug rule

`hexera-dev` is project id `hexera-dev`, project number `224734058693`.

Every resource takes the `dev-<slug>-` prefix that `DEPLOYMENT_ID` already produces:

| Resource | Name |
| --- | --- |
| Cloud Run services | `dev-<slug>-api`, `dev-<slug>-console` |
| Cloud Run jobs | `dev-<slug>-mesh`, `dev-<slug>-migrate`, `dev-<slug>-queue-depth` |
| Runtime service accounts | `dev-<slug>-{api,console,mesh,migrate,queue-depth,workers}` |
| Buckets | `dev-<slug>-{exchange,artifacts,transfer}-224734058693` |
| Worker fleet | MIG `dev-<slug>-workers` in `us-central1-a` |
| Database | `meshpipeline_<slug>` on `hexera-dev-pg` |
| Object-store secret | `minio-secret-key-<slug>` |
| Celery key prefix | `dev-<slug>:` |

**Slug rule:** lowercase letters, digits and hyphens; must start with a letter; must not end with
one; **at most 14 characters**; and not one of `dev`, `prod`, `production`, `shared`, `main`,
`staging`.

The 14 is arithmetic, not taste. A service account id may be 30 characters. The longest suffix any
script appends is `-queue-depth` (12), on top of `dev-` (4), leaving 14. `create-queue-depth-publisher.sh:38`
is the binding constraint. A slug of 15 characters would fail there, eleven stages into a deploy,
with a Google error naming the field and not the cause.

## 2. The target picker

In `.github/workflows/deploy.yml`, the personal-environment branch of the `target` job (currently
lines ~325-515) loses:

- the `PERSONAL_ENVS` environment binding,
- the `_entry` / `_num` / `_access` parse,
- the "no personal environment named" guard,
- the "project number ... is not a number" guard,
- the "registered without an object-store access id" guard.

It gains literals identical to shared dev's for the project and its number, and a `dev-<slug>-`
prefix on everything else:

```
project=hexera-dev
region=us-central1
deployment_id=dev-<slug>
wif_provider=projects/224734058693/locations/global/workloadIdentityPools/github-actions/providers/github
deployer_sa=github-deployer@hexera-dev.iam.gserviceaccount.com
registry=us-central1-docker.pkg.dev/hexera-dev/mesh
mesh_job=dev-<slug>-mesh
mesh_bucket=dev-<slug>-exchange-224734058693
api_service=dev-<slug>-api
console_service=dev-<slug>-console
cloudsql_instance=hexera-dev-pg
redis_instance=hexera-dev-redis
migrate_db_name=meshpipeline_<slug>
migrate_db_user=meshpipeline
postgres_password_secret=postgres-password
worker_mig=dev-<slug>-workers
worker_mig_zone=us-central1-a
worker_env_uri=gs://dev-<slug>-transfer-224734058693/worker.env
minio_bucket=dev-<slug>-artifacts-224734058693
minio_secret_key_secret=minio-secret-key-<slug>
celery_key_prefix=dev-<slug>:
```

`minio_access_key` is **no longer emitted** by the picker — see §4.

**A personal run always selects `storage`.** The picker appends `storage` to `DEPLOY_COMPONENTS`
for a personal run whatever the checkboxes said. This is load-bearing, not tidiness:

- `create-api-service.sh` emits its object-store block only when `MINIO_ENDPOINT` is set, and the
  access id is no longer pinned — so the value has to come from somewhere in-run. The `storage`
  stage writes `MINIO_ACCESS_KEY` into the deployment env (`create-object-storage.sh:324`) and runs
  before the API stage, so selecting it is what makes the API's store configured.
- Without it, an `images`-only first run would deploy an API whose adapter falls back to
  `localhost:9000` and returns 503 on every geometry upload — which `deploy.yml` records as having
  actually shipped on shared dev before its values were pinned.
- It is also what creates `dev-<slug>-api` (`create-object-storage.sh:62`), the identity the API
  service runs as.

The stage is idempotent and cheap on reruns — a bucket describe and an HMAC list.

Unchanged from today's personal branch: `admin_service`, `billing_export_table`, `console_domain`,
`admin_domain` and every `outreach_*` output stay empty, `app_env=dev`, `api_cors_origins=*`,
`minio_endpoint=storage.googleapis.com`, `minio_region=us-central1`, `minio_secure=true`.

The picker still holds no `id-token` permission and still authenticates to nothing. The difference
is that the identity it names now always exists, because it is shared dev's.

**`worker_env_uri` changes hands.** `new-env.sh` used to publish `.env.example` to the transfer
bucket. With `new-env.sh` retired, `create-worker-fleet.sh` must publish it if the object is
absent, or the `workers` component fails on a first deploy with `WORKER_ENV_URI` naming an object
that does not exist. This is the one piece of work `new-env.sh` did that nothing else does yet.

## 3. Two grants, scoped to the host project

`create-workload-identity.sh`'s `DEPLOYER_ROLES` (line 323) gains two roles **only when the project
is a personal-environment host**:

- `roles/iam.serviceAccountAdmin` — create the six runtime identities per slug.
  **No new code is needed for this.** Every stage already creates the identity it needs and
  already prints the remediation when it cannot: `create-object-storage.sh:62`,
  `create-api-service.sh:106`, `create-console-service.sh:125`, `run-migrations.sh:84`,
  `create-queue-depth-publisher.sh:71`, `create-worker-fleet.sh:205`, plus the mesh identity in
  `create-service-accounts.sh:25`. They fail today only for lack of this role — which is why
  `new-env.sh` grew a block creating all six by hand. That block is deleted, not relocated.
- `roles/storage.hmacKeyAdmin` — mint the slug's object-store key. The grant
  `create-object-storage.sh:207-210` already prints in its remediation text.

The roster is applied to every project the script runs against, so the widening is gated on an
explicit opt-in variable — `PERSONAL_ENV_HOST=1` — set for `hexera-dev` and nowhere else.
Defaulting it off means `hexera-prod`'s roster is byte-identical to today's, and turning it on is a
visible line in a diff rather than a silent consequence of running the script.

The comment at `create-workload-identity.sh:320-322` currently reads:

> WHAT IS STILL DELIBERATELY ABSENT: no roles/owner, no resourcemanager.projectIamAdmin and no
> iam.serviceAccountAdmin - so a compromised run still cannot grant itself anything further, mint
> a new identity, or widen the provider that admitted it.

It must be amended rather than contradicted. The honest replacement states that on a
personal-environment host the deploy identity **can** mint identities and HMAC keys, that this is
what lets a developer's first deploy create their own environment, and that `roles/owner` and
`resourcemanager.projectIamAdmin` remain absent everywhere — so a compromised run still cannot
grant itself anything further or widen the provider that admitted it. On `hexera-prod` nothing
changes.

`create-workload-identity.sh:389`'s drift check reports roles held beyond the roster; adding these
to `DEPLOYER_ROLES` under the flag keeps that check meaningful instead of noisy.

## 4. The access id, discovered instead of registered

`create-object-storage.sh` decides reuse-or-mint by testing the recorded `MINIO_ACCESS_KEY`
against live ACTIVE keys (lines 181-232). Against an empty string it concludes no usable key
exists and mints another — which is precisely why the register had to carry the access id, and why
`new-env.sh` grew the `_prev_entry` seeding at lines 320-330.

Per-slug service accounts remove the ambiguity. `dev-<slug>-api` is created by this tooling, is
used by nothing else, and holds at most one key. So:

> When no access id is recorded, exactly one ACTIVE key exists for this deployment's own object-store
> service account, and the HMAC secret holds a version — adopt that key. Do not mint.

Minting stays the behaviour when there are zero ACTIVE keys. The existing guards are unchanged and
still do their jobs: `HMAC_LIST_READ_OK=0` (a listing this identity cannot read) still refuses to
conclude anything, and the five-key ceiling still stops the run. The five-key walk the old comments
warn about cannot recur, because it was caused by `new-env.sh` handing in a blank temporary env on
every run, and there is no such env any more.

Shared dev and prod are unaffected: both pin `minio_access_key` as a literal, so `RECORDED_ACCESS_ID`
is never empty there and the new branch is never taken.

This rule is what makes the always-selected `storage` stage (§2) safe to run on every personal
deploy. Run 1 finds no ACTIVE key and mints one. Run 2 arrives with nothing recorded — because the
picker pins no access id — finds exactly one ACTIVE key for `dev-<slug>-api`, and adopts it.
Without the rule, every run after the first would mint another key and walk the account to Google's
five-key ceiling.

## 5. The data tier: shared instances, own database

`create-data-tier.sh` already reuses an existing instance untouched (lines 201-217, "AN EXISTING
INSTANCE IS REUSED - VALIDATED AND LEFT UNTOUCHED") and already takes the instance names and the
database name from variables (`CLOUDSQL_INSTANCE:56`, `REDIS_INSTANCE:79`, `DB_NAME` from
`MIGRATE_DB_NAME:72`). A personal run therefore needs no change to that script: it points at
`hexera-dev-pg` and `hexera-dev-redis` and creates `meshpipeline_<slug>` beside shared dev's
`meshpipeline`.

The database **user** stays shared (`meshpipeline`, `postgres-password`). A per-slug user would
need per-slug grants and a per-slug password secret to isolate a sandbox from a sandbox, which is
not what the isolation is for — the point is that a migration run by one developer cannot move
another's schema, and a separate database achieves that.

**Stated cost:** one Cloud SQL instance and one Memorystore instance are now a shared failure
domain across shared dev and every personal environment. Exhausting connections, filling the disk
or restarting the instance affects everyone. This was accepted for the saving: a first deploy in
minutes instead of ~30, and no per-developer instance bill.

## 6. Broker isolation

One Memorystore instance means one Redis keyspace, and Celery's queue names are hardcoded literals
(`celery_app.py:22-27`) — so without a change, two personal environments would consume each other's
tasks.

`celery_app.py` gains, from a new settings-inventory variable:

```python
broker_transport_options={"global_keyprefix": <prefix>}
result_backend_transport_options={"global_keyprefix": <prefix>}
```

Celery is 5.4.0 (`requirements/runtime.txt:45`), so `global_keyprefix` is available.

The new variable is declared in `src/meshpipeline/settings/inventory.py` — the authority that
`.env.example` is generated from — in the existing `Group("Redis (broker + pub/sub)")`, **defaulting
to empty**. An empty prefix is exactly today's behaviour, so shared dev, prod and the local compose
stack are unchanged and need no migration. The deploy sets it only for a personal run.

`create-queue-depth-publisher.sh` passes the prefixed name as `QUEUE_NAME` (it is already
env-overridable at line 41 and already exported into the job at line 107), because
`queue_depth_publisher.py:126` does a bare `client.llen(queue)` and would otherwise read shared
dev's depth and scale the wrong fleet.

An alternative — a per-slug Redis database index via the `/N` path in `REDIS_URL` — was rejected:
Redis offers 16 databases, capping personal environments at ~15, and assigning an index per slug
requires a slug-to-index map, which is the register returning under a different name.

## 7. Teardown

`destroy-env.sh` is rewritten. It currently deletes a project (176 lines, and its header explains
at length why that is the complete and honest approach). It becomes a label-driven delete inside
`hexera-dev`.

Every resource this tooling creates already carries `deployment-id=<DEPLOYMENT_ID>`
(`create-api-service.sh:348`, `create-console-service.sh:240`, `create-object-storage.sh:117,276`,
`create-mesh-tier.sh:53`). The new script deletes, for `deployment-id=dev-<slug>`: the Cloud Run
services and jobs, the MIG and its template and autoscaler, the Cloud Scheduler job, the three
buckets, the six service accounts and the slug's HMAC key, the `minio-secret-key-<slug>` secret,
and the `meshpipeline_<slug>` database.

It refuses a slug that is empty or reserved, and it refuses to run against any project other than
the configured personal-environment host — the check that used to ask "is this project personal?"
becomes "is this prefix personal, and is this the host project?".

**What it cannot reach, printed rather than implied.** A resource created by hand that carries no
`deployment-id` label survives. The old header was right that this is the weakness of deleting by
prefix; the answer is not to claim completeness but to end the run with what was deleted and what
was found-but-skipped, so the gap is visible at the moment it matters. The shared Cloud SQL and
Memorystore instances are never touched.

Reachable as `make destroy-env SLUG=<slug>`, as today.

## 8. Retired

- `deploy/gcp/scripts/new-env.sh` — deleted. Its one still-needed act, publishing `worker.env`,
  moves to `create-worker-fleet.sh` (§2).
- `new-env` targets in `Makefile:229-230` and `deploy/gcp/Makefile:68-69` — deleted. The lifecycle
  comment at `Makefile:225-228` becomes two commands: deploy, and destroy.
- `HEXERA_PERSONAL_ENVS` — deleted from the repository. It currently holds
  `pranav=720680343126:GOOG1EX...`, pointing at a project deleted on 2026-09-16; left in place it
  would let a `slug=pranav` dispatch past the picker and fail at the WIF exchange instead.
- `seed-secrets.sh` and `make seed-secrets` — kept. They are not part of this path any more (a
  personal environment inherits `hexera-dev`'s provider-key secrets) but they remain the tool for
  filling an empty secret container in a real environment.
- `docs/deployment/personal-environments.md` — rewritten, including the register section at line 228.

## 9. Testing

`tests/unit/deploy/test_personal_environments.py` (672 lines, ~45 tests) is the existing contract
and most of it survives. Specifically:

**Delete** — they assert the register that no longer exists, or creation this design removes:
`test_an_unregistered_slug_cannot_deploy`, `test_a_non_numeric_project_number_is_refused`,
`test_an_environment_registered_without_an_access_id_is_refused`,
`test_creating_an_environment_is_not_something_ci_can_do`,
`test_the_project_display_name_uses_only_characters_google_accepts`,
`test_creation_establishes_what_the_first_deploy_cannot`,
`test_new_env_hands_bootstrap_a_path_not_an_empty_file`,
`test_rerunning_creation_reuses_the_object_store_key`,
`test_the_runtime_identities_are_created_by_an_owner_not_by_the_deploy`,
`test_domain_restricted_sharing_is_relaxed_for_the_project_and_said_out_loud`.

**Rewrite, do not delete** — `test_a_personal_environment_widens_no_deploy_identity` (line 316)
asserts exactly what this design changes. It becomes a *scoping* test: the two new roles appear
only under `PERSONAL_ENV_HOST`, and `hexera-prod`'s roster is unchanged.

**Keep, retargeted to the new names** — `test_a_personal_run_derives_every_target_from_the_slug`,
`test_a_personal_run_emits_every_target_the_shared_one_does`,
`test_a_personal_run_states_every_target_an_unattended_deploy_requires`,
`test_a_personal_run_states_a_complete_object_store`, the reserved-slug and malformed-slug tests,
`test_the_existing_targets_are_untouched`, `test_a_tag_cannot_be_redirected_by_a_slug`, the admin
and outreach refusals, and the destroy tests (retargeted from project evidence to prefix-and-host
evidence).

**New:**

- a slug of 15 characters is refused, and the refusal names the 30-character service-account limit
- every emitted resource name carries the `dev-<slug>-` prefix, so no personal target can collide
  with a shared-dev resource — asserted against the shared-dev branch's own output, not a literal list
- `cloudsql_instance` and `redis_instance` on a personal run equal shared dev's, and
  `migrate_db_name` does not
- `celery_key_prefix` is non-empty for a personal run and empty for shared dev and prod
- the new inventory variable defaults to empty, and `.env.example` regenerates cleanly
- `create-object-storage.sh` adopts a single ACTIVE key when none is recorded, and still mints when
  there are none, and still refuses when the listing could not be read
- the queue-depth publisher is given the prefixed queue name
- a personal run selects `storage` even when the box is unticked, and a shared-dev run does not
- a personal run never emits `minio_access_key`, and shared dev and prod still pin theirs

## 10. Risks

1. **One deploy identity now provisions many developers' environments.** A compromised workflow run
   in `hexera-dev` can already delete its database and broker (the roster header says so); it can
   now also mint identities and HMAC keys there, and it can name any `dev-<slug>-` resource. What
   contains it is unchanged: the provider's repository condition, `prod`'s required reviewers, and
   Cloud SQL deletion protection.
2. **Shared data tier.** §5. Accepted.
3. **Teardown is not complete.** §7. Made visible rather than solved.
4. **A typo'd slug still creates resources** — cheaper than before (no project, no 30-day id lock)
   but not free: a MIG instance and a set of Cloud Run services. `make destroy-env SLUG=<typo>`
   removes them.
5. **`global_keyprefix` runs in prod too**, with an empty prefix. The default makes it a no-op, and
   the test asserting shared dev and prod emit an empty prefix is what keeps it one.

## Out of scope

Custom hostnames, the admin console, outreach and billing export remain unavailable in a personal
environment, exactly as today. Migrating any existing personal project is not needed:
`hexera-dev-pranav` and `hexera-dev-areen` were both deleted on 2026-09-16, so there are none.
