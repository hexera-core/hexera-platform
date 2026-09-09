# Environments and delivery

What exists in Google Cloud, what it is called, and what has to happen for a commit to reach it.

Read from `gcloud`, the GitHub API and the workflow files on **2026-09-04**. Where this disagrees
with a comment in the repository, this is the observation and the comment is the claim — two
claims in `deploy.yml` were found to be false on the day this was written, and both are noted
below rather than quietly corrected.

---

## 1. The two environments

| | `hexera-dev` | `hexera-prod` |
| --- | --- | --- |
| Project number | 224734058693 | 688073002171 |
| Region / zone | us-central1 / us-central1-a | us-central1 / us-central1-a |
| Reached by | merge to `main`, or a manual run on any branch | a `v*` tag, after human approval |
| Purpose | shared sandbox | the product |

Both are real and both serve traffic. **`hexera-prod` is not empty** — it was stood up by hand
between 2026-09-01 and 2026-09-03, and `docs/deployment/gcp-live-inventory.md` (dated 08-30)
describing it as empty is stale.

### What runs in each

| Role | `hexera-dev` | `hexera-prod` |
| --- | --- | --- |
| API (Cloud Run service) | `hexera-dev-api` | `prod-api` |
| Console (Cloud Run service) | `dev-console` | *(deliberately unset — see below)* |
| Mesh executor (Cloud Run job) | `dev-mesh` | `prod-mesh` |
| Schema migration (job) | `dev-migrate` | `prod-migrate` |
| Queue-depth publisher (job) | `dev-queue-depth` | `prod-queue-depth` |
| Worker fleet (MIG) | `hexera-dev-workers` | `prod-workers` |
| PostgreSQL (Cloud SQL) | `hexera-dev-pg` | `hexera-prod-pg` |
| Redis (Memorystore) | `hexera-dev-redis` | `hexera-prod-redis` |
| Workspace exchange (bucket) | `dev-exchange-224734058693` | `prod-exchange-688073002171` |
| Build artifacts (bucket) | `hexera-dev-artifacts-224734058693` | `prod-artifacts-688073002171` |
| Image registry | Artifact Registry `mesh` | Artifact Registry `mesh` |

Dev additionally has `dev-transfer-224734058693`, holding source tarballs and an `env.txt`, which
nothing in `deploy/` describes or creates.

### Posture difference — this is the important table

| | dev | prod |
| --- | --- | --- |
| Cloud SQL tier | `db-g1-small` | `db-custom-2-7680` |
| Cloud SQL availability | ZONAL | ZONAL (**not HA**) |
| Cloud SQL backups | **disabled** | enabled |
| Cloud SQL deletion protection | **off** | on |
| Cloud SQL public IP | **yes**, `35.225.236.203`, `requireSsl: false`, no authorized networks | no — private `10.62.0.3` only |
| Redis tier | BASIC (no replica) | STANDARD_HA |
| API invoker | `allUsers` | **`allUsers`** |

Two things worth stopping on:

- **The dev database accepts unencrypted connections from anywhere on the public internet, has no
  backups, and has deletion protection off.** It is a sandbox, but it is a sandbox reachable by
  the whole internet. The pending dev rebuild (§6) replaces it with an instance `deploy/` creates,
  which is private-only with backups and protection on — the same shape prod already has.
- **The prod API is open to the internet** (`roles/run.invoker` → `allUsers`), and every request
  can spend money at DeepInfra and DeepSeek. That may be intended for a public product; it should
  be an explicit decision rather than an inherited default from the dev service it was copied from.

`hexera-prod`'s Cloud SQL is ZONAL, i.e. **no high availability**, despite being the production
database. `docs/deployment/cost-estimate.md` assumes the same. Worth confirming that is deliberate.

### Networking

Identical shape in both: the single auto-mode `default` VPC, a Cloud NAT
(`hexera-dev-nat` / `hexera-prod-nat`) on a router (`hexera-dev-router` / `hexera-prod-router`) so
private workers can egress, and a `servicenetworking` peering carrying the private-services range
that Cloud SQL and Memorystore are addressed out of. Dev additionally carries a
`redis-peer-…` peering from the hand-made Memorystore instance.

Cloud Run reaches the VPC by Direct VPC egress, `private-ranges-only`.

### Console (Cloud Run service)

The Next.js console — the browser front door — is a third promotable workload beside the mesh
job and the API, provisioned by `deploy/gcp/scripts/create-console-service.sh` and named
`<deployment-id>-console` (`dev-console` / `prod-console`) following the naming rule in §2.

Unlike the API, **it is publicly invokable by design**: `CONSOLE_ALLOW_UNAUTHENTICATED` defaults
to `1` where `API_ALLOW_UNAUTHENTICATED` defaults to `0`, because the console's own Auth.js
session is the gate — putting Cloud Run IAM in front of it would mean nobody could reach the
sign-in page to authenticate at all.

It scales `0..3` instances on dev (`CONSOLE_MIN_INSTANCES` / `CONSOLE_MAX_INSTANCES`), smaller
than the API's `0..5`, because it renders pages and proxies rather than running model calls.

Credentials reach it as four Secret Manager references, never as literal values — two it owns and
two it shares with the API:

| Runtime var | Container name | Shared with the API? |
| --- | --- | --- |
| `AUTH_SECRET` | `console-auth-secret` | no — the console's own Auth.js session key |
| `CONSOLE_AUTH_USERS` | `console-auth-users` | no — scrypt password hashes for console sign-in |
| `MESH_API_KEY` | `mesh-api-key` | yes |
| `USER_TOKEN_SECRET` | `user-token-secret` | yes |

Selected by the `console` component in `DEPLOY_COMPONENTS` (§5), and rolled out *after* the API
stage — every console page load reaches the API, so a console that rolls out first would serve
errors until the API catches up.

**Two known limits, stated rather than fixed here:**

- `NEXT_PUBLIC_HEXERA_API_BASE_URL` **does** take effect at deploy time today, but only by
  accident, and the obvious "fix" would break it. Read on 2026-09-08: the `console` Dockerfile
  target passes no `NEXT_PUBLIC_*` build argument, so there is nothing for Next to inline; the
  built SSR chunk still reads `process.env.NEXT_PUBLIC_HEXERA_API_BASE_URL` at request time, no
  literal origin appears in the server or client bundles, and the only consumer
  (`apps/console/src/app/(console)/page.tsx`) is a server component that injects the value into the
  page as it renders. So the Cloud Run setting wins.

  The trap: `NEXT_PUBLIC_*` is the prefix Next inlines at build time whenever a value IS present
  then. Supplying this one as a build argument — which is what a reader would reasonably do to make
  it "properly" per-environment — is exactly what would freeze it, silently, at whichever origin
  the image was built against. The realtime WebSocket dials this origin directly, so the symptom
  would be a console that renders fine and never streams. The durable fix is to stop using the
  `NEXT_PUBLIC_` prefix for a value no client code reads.
- The console runs on the generated `run.app` URL with no `AUTH_URL` set, because
  `apps/console/src/auth.ts` sets `trustHost: true` — Auth.js derives its callback URL from the
  request host instead of requiring one to be configured per environment.

**Pinned on dev, deliberately not on prod:** `deploy.yml` pins `console_service=dev-console` for
dev, the same way it pins `CLOUDRUN_API_SERVICE` (`api_service=dev-api` / `api_service=prod-api`,
§1) — so a merge to main, which selects `console` in its default component set (§5), actually
reconciles a named service instead of `create-console-service.sh` stating its own skip.

Prod's `console_service=` is left empty, and that is a decision, not a gap. `components=all` on a
release tag means a tag reconciles every tier it is *told about* — pinning a name here is what
would tell it to reconcile the console, provisioning a new, billed Cloud Run service in production
on the very next tag, unasked. That is the same class of mistake PR #12's naming slip nearly made
with `hexera-prod-pg` (§2): a name typed into this file quietly becoming a second, unwanted, paid
resource standing next to the real one — there it would have been a second, empty Cloud SQL
instance beside the production database; here it would be a console nobody requested, serving from
a project nobody pointed it at. Prod gets no console until a human deliberately pins a name here,
in a reviewed diff, the same gate that governs everything else in this file.

---

## 2. Naming

**The rule: `<deployment-id>-<role>`.** `dev-mesh`, `prod-api`, `dev-queue-depth`. The project is
already called `hexera-dev`, so a `hexera-dev-` prefix inside it says the same word twice.

Every default in `deploy/gcp/scripts/` already follows it — `${CLOUDRUN_MESH_JOB:-${DEPLOYMENT_ID}-mesh}`,
`${CLOUDSQL_INSTANCE:-${DEPLOYMENT_ID}-pg}`, `${API_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-api}` and so
on. The exceptions are all resources a human created before the scripts did.

### Where the rule does not hold, and why

| Resource | Why it still carries the old prefix |
| --- | --- |
| `hexera-dev-api`, `hexera-dev-workers` | hand-made. **Being replaced** by the pending dev rebuild (§6). |
| **`hexera-dev-pg`, `hexera-dev-redis`** | **stateful, live, and holding dev's `meshpipeline` database.** Same exception as prod — see below. |
| `hexera-dev-artifacts-…` | hand-made; not yet reconciled. |
| **`hexera-prod-pg`, `hexera-prod-redis`** | **stateful, live, and holding the production database.** See below. |
| `hexera-*-nat`, `hexera-*-router` | created outside `deploy/`; nothing reconciles them. |

**Both data tiers are a deliberate exception, not an oversight.** Cloud SQL and Memorystore
cannot be renamed in place, and `deploy.sh` reconciles *by exact name* — so pinning `prod-pg` in
the workflow would not rename anything. It would build a **second, empty, paid** database beside
the live one on the next release tag. That very mistake was committed earlier in this work and
caught by reading the live project; the config now names `hexera-prod-pg` and `hexera-prod-redis`
explicitly, with the reason stated at the pin.

**Dev carried the identical slip for longer, and it was not merely latent.** The workflow pinned
`dev-pg` and `dev-redis`, which do not exist. Discovery therefore reported *"no hosted database
declared"* and *"has no private address yet"* on every dev deploy, and `MIGRATE_DB_HOST` and
`REDIS_URL` reached the API service empty — while a single `components=all` would have built the
second paid instance the prod fix was written to prevent. Dev now pins `hexera-dev-pg` and
`hexera-dev-redis` for the same reason and with the same wording.

Renaming either later is a data migration, not a config edit. It is an open decision (§7).

### Identities

Convention `<deployment-id>-<role>`, one per workload, no shared identity:

| | dev | prod |
| --- | --- | --- |
| Mesh runner | `dev-mesh@` | `prod-mesh@` |
| API | `dev-api@` | `prod-api@` |
| Console | `dev-console@` | *(none — prod has no console)* |
| Migration | `dev-migrate@` | `prod-migrate@` |
| Queue depth | `dev-queue-depth@` | `prod-queue-depth@` |
| CI deployer | `github-deployer@` | `github-deployer@` |

Two oddities in dev:

- **`mesh-runner@hexera-dev`** holds `run.developer`, `storage.objectAdmin` and
  `iam.serviceAccountUser` at project level. It is not `dev-mesh@`, nothing in `deploy/` creates
  it, and it is the broadest non-deployer identity in either project. It looks like a superseded
  hand-made identity and should probably be deleted — but confirm nothing uses it first.
- The hand-made `hexera-dev-api` runs as the **default compute service account** with
  `cloud-platform` scope. Its replacement `dev-api` runs as `dev-api@`, created below; once
  `dev-api` is verified serving, `hexera-dev-api` is superseded and should be deleted.

### Runtime identities are created by an owner, once, before their first deploy

`github-deployer@` holds eleven narrow roles and **`iam.serviceAccountAdmin` is deliberately not
one of them** — a deploy identity that can mint identities can escalate past its own ceiling. So
the first deploy of any workload under a *new* name stops at its runtime identity with the exact
command to run, and nothing is mutated before that point:

```
ERROR: could not create dev-api@hexera-dev.iam.gserviceaccount.com.
Creating identities needs iam.serviceAccountAdmin, which a DEPLOY identity is
deliberately not given. Create it once, as an owner:
```

This is the same class of owner-run prerequisite as `create-secrets.sh`, which is not a `deploy.sh`
stage either. Both were hit for real while standing the console up on dev. Create every identity a
new deployment names before its first run, not one failed run at a time:

```bash
gcloud iam service-accounts create dev-api     --project hexera-dev --display-name 'Hexera API service'
gcloud iam service-accounts create dev-console --project hexera-dev --display-name 'Hexera console service'
```

Everything the API and console stages do *after* that point is within the deployer's roles: they
make only resource-level bindings (on secrets and on the Cloud Run services), never project-level
ones, and the custom `hexeraDeploySecrets` role carries `secrets.setIamPolicy` — but not
`versions.access`, so the deployer can grant a runtime identity access to a secret it cannot
itself read.

---

## 3. How a commit reaches an environment

```
                    ┌──────────────────────────────────────────────┐
  merge to main ───▶│ Deploy: components = images,migrate,console  │──▶ hexera-dev
                    │ gate: hexera/ci-gate MUST pass (blocking)    │
                    └──────────────────────────────────────────────┘

                    ┌──────────────────────────────────────────────┐
  manual run     ───▶│ Deploy: components = your choice            │──▶ hexera-dev
  (any branch)      │ gate: CI reported, NOT required (advisory)   │
                    └──────────────────────────────────────────────┘

                    ┌──────────────────────────────────────────────┐
  push tag v*   ───▶│ Deploy: components = all (forced)            │──▶ hexera-prod
                    │ gate: ci-gate blocking + HUMAN APPROVAL      │
                    └──────────────────────────────────────────────┘
```

### What actually enforces prod

Not the workflow — GitHub Environments. Verified live:

| Environment | Allowed refs | Approval |
| --- | --- | --- |
| `dev` | any branch (`*`) | none |
| `prod` | **tags matching `v*` only** | **required reviewer: `pranavkannepalli`** |

So prod cannot be reached from a branch by anyone, however the workflow is invoked, and a tag
alone never ships — the run parks until a human approves.

### What does *not* protect main

**`main` has no required status checks and no required reviews.** Its ruleset carries `creation`,
`update`, `deletion` and `non_fast_forward` only, and organisation admins bypass all four. A red
commit can reach `main`; nothing on the repository side stops it.

This is why the `await-ci` job in `deploy.yml` matters more than it looks: it is not duplicating a
protection that exists, it is currently **the only thing** between a failing suite and a deployed
artifact. A `required_status_checks` rule naming `hexera/ci-gate` would be the obvious fix and is
an open decision (§7).

---

## 4. The gates

Internal vocabulary, defined in `docs/development/gates.md`. Useful to know because the scripts
still refer to it, though no longer in any user-visible workflow name.

| Gate | Command | Question |
| --- | --- | --- |
| A | `make check-fast` | seconds, no services — is this worth running the suite on? |
| B | `make check` | the full blocking suite — may this commit produce a release? |
| C | `make release-validate` → `make release-publish` | build the images **once**, validate *those artifacts*, push exactly what was validated, record the digests |
| D | `make mesh-preflight` | read-only — may the validated image be promoted into this project? |

The property that matters: **there is one image producer and one consumer of what it produced.**
Deployment never builds. It promotes digests recorded in `deploy/output/release.json`.

### CI (`ci.yml`) — 13 jobs

`preflight` → everything else → `hexera/ci-gate`.

Lanes: workflow audit (actionlint + zizmor), ruff, mypy ratchet, repository contracts, unit suite,
architecture/provider contracts, dependency audit, CodeQL, image targets (api/pipeline/mesh),
**integration tier** (real Postgres + Redis + MinIO, ~14 min, 878 tests), UI in real headless
Chrome.

`ci-gate` runs `if: !cancelled()` so it reports for every change, and verifies each selected lane.
A docs-only change legitimately skips the Python and image lanes; `failure` is never legitimate.

Triggers: `pull_request`, push to `main`, weekly schedule, and a bare `workflow_dispatch`.
The dispatch **used to take a `target_ref`** that validated a different commit from the one the
run belongs to — while check runs attach to `github.sha`. That let a green `hexera/ci-gate` land
on `main`'s head having tested something else entirely, which is exactly the claim `await-ci`
trusts. The input has been removed.

---

## 5. Deploy components

A full reconcile of every tier is the right default and the wrong routine: rebuilding the fleet,
re-reading Cloud SQL and re-minting the object-store credential to ship one image costs minutes
and money for resources the change never touched.

`DEPLOY_COMPONENTS` selects what a run reconciles. Default `all` — an unset variable never means
*less*.

| Component | Stage |
| --- | --- |
| `images` | mesh job + API service — the two workloads carrying an application digest |
| `data` | Cloud SQL + Memorystore |
| `storage` | artifacts bucket + S3-interoperability credential |
| `migrate` | schema, applied once before anything serves the new image |
| `queue` | queue-depth publisher + autoscaling policy |
| `workers` | managed instance group + rolling update |
| `console` | Cloud Run console service — the promoted console digest, in front of the API |

Always on, never selectable: discovery, config validation, preflight, plan confirmation, API
enablement, Artifact Registry, runtime identities, release promotion, IAM. Each is read-only or
cheap and idempotent, and skipping them is how a run deploys against configuration it never checked.

Defaults: merge to main → `images,migrate,console`. Release tag → `all`, forced. Manual → your
choice. Locally: `make mesh-deploy COMPONENTS=images,migrate,console`.

Every skipped stage says so, and the summary distinguishes *reconciled* / *not declared* /
**not selected** — a summary reading "Schema at head" after `migrate` was excluded would be the
most misleading line the script prints.

**This saves reconcile time, not build time.** `release-validate` still rebuilds the OpenFOAM mesh
image on every run (~20 min) because GitHub runners keep no layer cache between runs. That is the
dominant cost and is not addressed here — see §7.

### The sixteen stages, in order

1. Discover environment, generate config
2. Validate configuration schema (typed, read-only)
3. Preflight (auth, project, permissions, region, existing resources — read-only)
4. **Confirm the plan** — last read-only step before any mutation
5. Enable required Google APIs
6. Artifact Registry + mesh runtime identity
7. Data tier — Cloud SQL + Memorystore *(`data`)*
8. Object store *(`storage`)*
9. Promote the validated release artifact — **no build**
10. Mesh tier *(`images`)*
11. IAM
12. Schema migrations *(`migrate`)*
13. Queue-depth publisher *(`queue`)*
14. API service *(`images`)*
15. Console service *(`console`)*
16. Worker fleet + rolling update *(`workers`)*

Ordering is load-bearing: schema before the API serves it; the console after the API it talks to;
the fleet last, so a worker never starts before the schema, queue signal and object store exist.

---

## 6. Pending: the dev rebuild

`deploy.sh` reconciles **by exact name**, so renaming dev's targets from `hexera-dev-*` to `dev-*`
renames nothing — every lookup misses and the provisioner builds the new stack *beside* the
serving one. That is the intended cutover, because the old stack is hand-made and drifted, but it
means dev briefly runs two of everything.

| Legacy | Replacement |
| --- | --- |
| `hexera-dev-api` | `dev-api` |
| `hexera-dev-pg` | `dev-pg` |
| `hexera-dev-redis` | `dev-redis` |
| `hexera-dev-workers` | `dev-workers` |

**Both halves must run.** Until the second does, the old stack keeps billing and `hexera-dev-api`
keeps serving publicly, on an image no release record names, against a database the new deploy is
not migrating.

```
make mesh-decommission-legacy                              # report only
make mesh-decommission-legacy DECOMMISSION_ARGS=--delete   # after reading it
```

It refuses to retire anything whose replacement it cannot see running, so a partial deploy cannot
be followed by a decommission leaving the environment with neither. Cloud SQL needs the separate
`--delete-database` flag; take an export first.

**This does not apply to prod.** Prod's stateful tier keeps its names (§2).

---

## 7. Open decisions

Ordered by how much they would hurt.

1. **`main` has no required status checks.** The deploy gate is currently the only thing stopping
   a red commit from shipping. Adding a `required_status_checks` rule naming `hexera/ci-gate` is
   one API call.
2. **The prod API is `allUsers`-invokable** and every request can spend model-inference money.
   Intended, or inherited from the dev service it was copied from?
3. **Prod Cloud SQL is ZONAL** — no HA on the production database.
4. **The dev database is publicly reachable, unencrypted, unbacked-up, unprotected.** The rebuild
   fixes this; until it lands, it is live.
5. **`mesh-runner@hexera-dev`** — broad, undescribed, probably superseded. Confirm and delete.
6. **`hexera-prod-pg` / `hexera-prod-redis` naming.** Live with the exception, or plan a migration?
7. **No image layer cache.** Every deploy rebuilds OpenFOAM from scratch, ~20 min. A registry-backed
   buildx cache would cut most deploys to a few minutes.
8. **`apt-get` has no retry** in the `Dockerfile`. An Ubuntu mirror mid-sync failed one CI run on
   2026-09-04. It will recur.
9. **Every merge to main deploys**, including a docs-only change. `ci.yml`'s `preflight` already
   computes a changed-scope signal that `deploy.yml` does not consult.
10. **GitHub-hosted larger runners are unavailable to the org** — verified with an `admin:org`
    token; list, machine-sizes and create all return
    `404 GitHub hosted runners are not supported for this organization`. Enabling them is an
    enterprise billing change. `vars.HEXERA_RUNNER_HEAVY` is wired with a fallback and unset.
11. **`dev-transfer-…` and `hexera-dev-artifacts-…`** are described by nothing in `deploy/`.
12. **A deploy that fails after `release-publish` cannot be retried on the same commit.** The
    publication tag is the commit sha12, and `publish.sh` refuses — correctly — to move a tag that
    already points at different bytes. Retrying rebuilds the image, and the rebuild is not
    byte-identical, so the second run dies at publish with
    `already exists and points at <other digest>`. Hit for real: a run failed at stage 14 on a
    missing runtime identity, and every retry of that commit was then blocked by its own
    successful publish. The guard is right; the gap is that nothing reuses the already-published,
    already-validated image on a retry. Workarounds today are a new commit or
    `RELEASE_PUBLISH_TAG`, neither of which the workflow sets. Related to 7 — reproducible image
    builds would close this and the cache complaint together.

---

## 8. The deploy identity

One `github-deployer@` per project, reached by Workload Identity Federation — **no service-account
key exists anywhere.** GitHub mints a short-lived OIDC token, Google exchanges it, and the
impersonation binding names one repository rather than the whole pool.

Roles, verified live in both projects:

```
roles/run.admin                        the jobs and the API service
roles/artifactregistry.writer          push the validated images
roles/iam.serviceAccountUser           actAs the runtime identities
roles/compute.instanceAdmin(.v1)       the worker MIG and its autoscaler
roles/compute.networkAdmin             the private-services range
roles/servicenetworking.networksAdmin  the peering carrying it
roles/serviceusage.serviceUsageAdmin   enable the APIs it then calls
roles/cloudsql.admin                   the Postgres instance, database and user
roles/redis.admin                      the Memorystore broker
roles/storage.admin                    exchange and artifact buckets
projects/<id>/roles/hexeraDeploySecrets   custom — see below
```

This list **grew from four on 2026-09-03**, deliberately. The original four expressed "a CI
identity should not create infrastructure"; the consequence was a provisioner that could not
provision, failing late and opaquely — the first release died at the data tier in dev and at
`storage.buckets.create` in prod.

**What that costs, stated plainly: a compromised workflow run can delete the project's database
and broker.** What contains it is no longer the role list — it is the `prod` environment's
required reviewers, the provider's repository attribute condition, and Cloud SQL deletion
protection. `roles/owner`, `resourcemanager.projectIamAdmin` and `iam.serviceAccountAdmin` remain
absent, so a compromised run still cannot widen itself.

`hexeraDeploySecrets` is a custom role replacing `roles/secretmanager.admin`, which carried
`versions.access` over *every* secret — enough to read the provider API keys in one call. It can
create secret containers, add versions and set their IAM; it **cannot** read a payload, delete a
secret, or destroy a version. It keeps `secrets.setIamPolicy`, because the deploy grants runtime
identities their bindings — and anything that can rewrite a secret's IAM policy can grant itself
access. **This is defence in depth, not a boundary.** It converts a silent one-call bulk read into
a logged, attributable policy change, and removes destruction outright.
