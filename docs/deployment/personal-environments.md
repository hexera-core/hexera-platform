# Personal development environments

Your own API, console, mesh job and worker fleet, inside `hexera-dev`, deployable from any branch
as often as you like. Nothing to create first: the deploy that names your environment is what
creates it.

## 1. The two commands

```bash
# THE FIRST DEPLOY IS TWO RUNS - two things need something the run before them creates. See §4.
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f app=true -f data=true -f fleet=true

gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f console=true -f fleet=true

# every deploy after that: one run, and a fast one
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f app=true -f console=true

# when you are done with it
make destroy-env SLUG=pranav
```

There is no `make new-env`. There used to be, and removing it is the point of the current design:
your environment is created by the first deploy that names it.

## 2. What you get

Your slug becomes a **deployment id** — `dev-pranav` — and every resource the deploy creates is
named from it:

| | |
| --- | --- |
| API | `dev-pranav-api` (Cloud Run, its own `run.app` URL) |
| Console | `dev-pranav-console` |
| Jobs | `dev-pranav-mesh`, `dev-pranav-migrate`, `dev-pranav-queue-depth` |
| Workers | `dev-pranav-workers` (managed instance group, autoscaled) |
| Buckets | `dev-pranav-exchange-…`, `dev-pranav-artifacts-…`, `dev-pranav-transfer-…` |
| Database | `meshpipeline_pranav` |
| Identities | `dev-pranav-api`, `-console`, `-mesh`, `-migrate`, `-workers` |
| Celery keys | prefixed `dev-pranav:` |

**Your slug must be at most 14 characters**, lowercase, starting with a letter. That is not
style: a service account id may be 30 characters, `dev-` takes 4 and `-queue-depth` takes 12. The
picker refuses a longer slug immediately rather than letting you find out eleven stages into a
deploy.

`dev`, `prod`, `production`, `shared`, `main` and `staging` are refused. Shared dev is reached by
leaving the slug **empty**; production only by a `v*` tag.

## 3. What you share, and what that costs

**Shared with shared dev and with every other personal environment:**

- the `hexera-dev` project, its VPC and its deploy identity
- the Cloud SQL instance `hexera-dev-pg` — you get your own *database* on it, not your own instance
- the Memorystore instance `hexera-dev-redis` — you get your own Celery *key prefix* on it
- the Artifact Registry, so your first build starts from a warm cache instead of re-downloading
  the whole native floor
- the provider API keys in Secret Manager

**Stated plainly:** one Cloud SQL instance and one Memorystore instance are a shared failure
domain. Exhausting connections, filling the disk or restarting an instance affects everybody. That
was accepted in exchange for a first deploy measured in minutes rather than half an hour, and no
per-developer instance bill.

What it does **not** mean is shared state. Your migrations move your schema only, and your Celery
queues are keyed under `dev-pranav:` so your jobs cannot be picked up by somebody else's workers.

## 4. Why the first deploy is two runs

**The console needs the API's URL.** A console is given the origin of the API it proxies to, that
origin is the API service's Cloud Run URL, and Google does not assign one until the service
exists. On an environment where the API has never been deployed there is nothing to tell it, so
stage 2 refuses the run rather than rolling out a console pointing at nothing:

```
CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not
```

**The queue signal needs a fleet.** Stage 13 attaches an autoscaling policy to the worker fleet's
managed instance group, and stage 17 is what creates that group.

So: `app,data,fleet` first — this is the slow one, roughly half an hour, because it reconciles
the data tier and builds the fleet. Then `console,fleet`. After that every run is one command.

`fleet` appears on both runs on purpose. Stage 13 establishes the queue-depth publisher and stage
18 creates the instance group, so on the first run there is no group for the autoscaler to attach
to yet and only that attachment defers — the publisher, its schedule and its identity are all in
place. The second run finds the group and attaches it.

`storage` is selected for you on every personal run whether or not you tick it. It is what mints
your object-store key on the first deploy and hands the access id to the API stage on every one
after; without it the API would roll with no store and return 503 on every upload.

## 5. What a personal environment deliberately does not get

- **No admin console.** What it offers — reading fleet metrics, changing scaling — is the Cloud
  Console's job for a developer who can already see their own resources. Ticking `admin` on a
  personal run reconciles nothing and says so.
- **No custom hostname or certificate.** The console and API serve on their generated `run.app`
  URLs, which work unmodified. A per-developer certificate would mean a DNS record and a 15–60
  minute wait before the environment could be used at all.
- **No outreach sender**, unconditionally and with no checkbox that can change it. It can email
  real people, and a sandbox created by typing a name into a text box is the last place it should
  be reachable.
- **No billing export.** It accumulates in one table `hexera-prod` owns.

## 6. How it is put together, and why

### Isolation is by name, not by project

A personal environment used to be its own Google Cloud project, `hexera-dev-<slug>`. That gave
real blast-radius isolation and a teardown that was one complete call — but it also meant you
could not deploy until somebody had run a creation script on a laptop, under owner credentials,
with a billing account, and recorded the resulting project number in a repository variable. A
workload identity pool is addressed by project *number*, Google assigns that at creation, and the
job that picks a deploy target holds no credential with which to look one up. So deploying to a
slug nobody had created produced an error telling you to go and run a command somewhere else.

Inside `hexera-dev` that problem does not exist. The project, its number, its identity pool and
its deploy identity are shared dev's; they are literals in `deploy.yml` and they already exist. An
unknown slug is therefore not an error — it is an environment the deploy is about to create.

The trade is the shared data tier (§3) and a teardown that is no longer complete (§8).

### The two extra roles the host holds

Every provisioning stage already creates the identity it needs — the API service, the console, the
migration job, the queue-depth publisher, the worker fleet and the object store all call
`iam service-accounts create`. They could never succeed, which is precisely why creation used to
be an owner's act. `hexera-dev`'s deploy identity now holds `roles/iam.serviceAccountAdmin` and
`roles/storage.hmacKeyAdmin`, gated behind `PERSONAL_ENV_HOST=1` so that running the same script
against `hexera-prod` does not widen production.

**What that costs:** a compromised workflow run against `hexera-dev` can mint service accounts and
object-store keys there. It still cannot grant itself any further role or widen the provider that
admitted it — `roles/owner` and `resourcemanager.projectIamAdmin` are absent here as everywhere.

### The one identity you do not get your own of

The queue-depth publisher runs as shared dev's `dev-queue-depth`. Writing a custom metric needs
`roles/monitoring.metricWriter` bound at the *project* level, granting a project-level role means
`setIamPolicy` on the project, and that is the authority the deploy identity is deliberately built
without. A freshly created `dev-pranav-queue-depth` could never be given the one role it needs, so
stage 13 would fail its smoke run with HTTP 403 on every personal deploy. The metric's series is
labelled with your deployment id, so sharing the account still produces separate series.

### The object store

Your `dev-pranav-api` account gets its own HMAC key, minted by your first deploy. Nothing pins the
access id, because it does not exist until that deploy has run. Every later deploy *adopts* it:
the account is created by this tooling and used by nothing else, so its single ACTIVE key is
necessarily the one whose secret sits in your `minio-secret-key-pranav` secret. That is what stops
each run minting another key and walking the account to Google's five-key ceiling.

## 7. Destroying one

```bash
make destroy-env SLUG=pranav
```

It deletes the Cloud Run services and jobs, the scheduler job, the instance group and its
templates, your three buckets, your database, your HMAC key and secret, and your service accounts.

**It never touches `hexera-dev-pg` or `hexera-dev-redis`** — they are shared, and deleting one to
tear down a sandbox would take everybody with it. Only your database goes.

**It does not claim to be complete.** When a personal environment was a project, destroying it was
one call that removed everything including whatever somebody had made by hand. Inside a shared
project the script can only delete what it can find, and what it can find is what carries the
`deployment-id` label the deploy stamps. It sweeps for anything else carrying your label and
**reports** it rather than deleting resources it cannot name, and it ends by saying that an
unlabelled resource survives. Read that list before assuming the environment is gone.

Unlike deleting a project, none of this has a 30-day undelete window. Your database and its
contents go for good.

## 8. When something goes wrong

**`slug 'x' is not a valid environment name`** — lowercase letters, digits and hyphens; start with
a letter; do not end with one.

**`slug '...' is ... characters; the most that fits is 14`** — see §2.

**`slug 'dev' is reserved`** — leave the slug empty to deploy shared dev.

**The console run fails at stage 2 with `HEXERA_API_BASE_URL is not`** — you ticked `console` on a
first deploy. Run the API first; see §4.

**The queue run fails with no managed instance group** — same shape: `workers` has to have run.

**A deploy fails partway.** Every stage is idempotent and safe to rerun; the run continues from
the state it finds rather than starting over.
