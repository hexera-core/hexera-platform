# Personal development environments

One Google Cloud project per developer, created in one command and deleted in one command.

This is the answer to "I want to try this on real infrastructure without waiting for shared dev,
and without breaking it for everybody else."

---

## 1. The three commands

```bash
make new-env SLUG=pranav                  # once, ~5 minutes

# the first deploy is TWO runs - two things need something the run before them creates. See section 4.
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f images=true -f data=true -f migrate=true -f workers=true
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f console=true -f queue=true

# every deploy after that: one run, and a fast one
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f images=true -f migrate=true -f console=true

make destroy-env SLUG=pranav              # when you are done
```

The slug is your name. It becomes the project id, `hexera-dev-pranav`, and it is permanent — a
project id cannot be renamed, and cannot be reused for 30 days after deletion.

**Deploying is the ordinary deploy workflow.** There is no separate pipeline: the same Gate C
builds the same four images from your branch, the same `deploy.sh` runs the same eighteen stages.
The only thing the slug changes is which project they run against. That is the point — a personal
environment is a rehearsal of a real deploy, not a simplified one.

Leave `slug` empty and you get shared dev, exactly as before. Nothing about the existing habit
changed.

---

## 2. What you get

Every resource has the same name it has in shared dev, because the project is already named after
you — `dev-api`, `dev-mesh`, `dev-pg`, `dev-redis`, `dev-workers`. There is one naming convention
to learn, not one per developer.

| | Shared dev | Your environment |
| --- | --- | --- |
| Project | `hexera-dev` | `hexera-dev-<slug>` |
| Deployment id | `dev` | `dev` |
| Reached by | merge to `main`, or a manual run with no slug | a manual run naming your slug |
| Console | `dev.console.hexera.ai` | the generated `run.app` URL |
| Admin console | `dev-admin`, behind IAP | **not provisioned** — see §5 |
| Outreach | off | **off, and not selectable** — see §5 |
| Blast radius | everyone | you |

**No DNS, no certificate, no wait.** The console runs on its generated `run.app` URL. That works
unmodified because `apps/console/src/auth.ts` sets `trustHost: true`, so Auth.js derives its
callback URL from the request host rather than from a per-environment setting. A per-developer
managed certificate would otherwise mean a DNS record and a 15–60 minute wait before the
environment could be used at all.

---

## 3. What it costs

About **$88/month** while it exists, from `docs/deployment/cost-estimate.md`'s dev profile:
Cloud SQL `db-g1-small` (~$27), Memorystore 1 GB BASIC (~$26), Cloud NAT (~$32), and a few dollars
of registry and storage. The Cloud Run services scale to zero and the worker fleet has a floor of
zero, so an idle environment is close to that floor and nothing else.

**Cloud NAT is the one line that is worse than sharing** — it is per-project, so each environment
pays its own $32. If your work does not need the worker fleet, skip `workers` and `queue` and the
NAT gateway is the thing to delete.

The real cost control is `make destroy-env`. An environment nobody is using should not exist.

---

## 4. The first run, and every run after

**The first deploy is two runs, and the second one is the console.** A console has to be told the
origin of the API it proxies to; that origin is the API service's Cloud Run URL; and Google does not
assign one until the service exists. On an environment where the API has never been deployed there
is nothing to tell it, so `validate-config.sh` refuses at stage 2 rather than let a console roll out
pointing at nothing:

> `CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not`

**The queue signal is the second thing in run two, for the same shape of reason.** Its autoscaling
policy attaches to the worker fleet's managed instance group, and stage 13 runs *before* stage 17
creates that group — the split is deliberate (the policy's sizing belongs to the admin console, so
the fleet stage never reconciles it) and it quietly assumes the group already exists. The publisher
and its schedule are still established in run one; only the attachment waits.

Run one creates the API and the fleet; run two deploys the console, by which time discovery can
find the API's URL, and attaches the autoscaling policy, by which time there is a group to attach
it to. Neither is specific to personal environments — both are what any environment's *first*
deploy has to do. Shared dev simply passed that point long ago.

Run one also creates Cloud SQL and Memorystore and takes roughly half an hour, most of it Google
creating the database instance. Tick `data` for it.

**Every run after that should leave `data` unticked**, and can do everything in one go. A typical
iteration is `images,migrate,console` — build, move the schema if it moved, roll the services.
Cloud SQL and Memorystore are reused untouched.

Image builds are cached in Artifact Registry, and a brand-new environment reads shared dev's cache
on its first build (`RELEASE_BUILD_CACHE_FROM` in `deploy.yml`), so it starts warm rather than
re-downloading the OpenFOAM toolchain from scratch.

> **Do not tick `storage` after the first creation.** The object store is established once, by
> `new-env.sh`, running as you. See §6.

---

## 5. What a personal environment deliberately does not get

**The admin console.** Its only gate is IAP, and IAP on Cloud Run needs an OAuth brand that — since
Google shut down the IAP OAuth Admin APIs in March 2026 — has to be created by hand, per project,
in the Cloud Console. Deploying the service without that gate would put an unauthenticated admin
console on the internet. So `admin_service` is empty and the stage skips itself **even if you tick
the box**. `hexera-prod` is blocked on the same thing; see `docs/deployment/console-domains.md` §7.

**Outreach.** It can email real people. A sandbox created in thirty seconds by one developer is the
last place a cold-email sender should be reachable, so it is empty unconditionally and no checkbox
changes that.

**A custom hostname.** See §2.

**An initialised Identity Platform.** `new-env.sh` enables `identitytoolkit.googleapis.com`, which
is *not* the same as Identity Platform being initialised — and there is no working `gcloud
identity-platform` command to do it. Until you do it once, by hand, the console renders its sign-up
page and **fails on submit**. It is a one-time console action per project:

> https://console.cloud.google.com/customer-identity/providers?project=hexera-dev-&lt;slug&gt;

Enable the Email/Password provider there. `new-env.sh` prints this reminder as it runs. You only
need it if you intend to sign in to the console; the API and the mesh job do not care.

---

## 6. How it is put together, and why

### The project is the isolation boundary

The cheaper-looking design is to put `pranav-api` and `pranav-mesh` beside `dev-api` inside
`hexera-dev`. It was rejected for three reasons:

- **Teardown.** Deleting a project is one call that removes *everything*, including the resource
  somebody created by hand last week that no label names and no script knows about. Deleting a name
  prefix means enumerating resource types and hoping the list is complete — and an incomplete list
  means a bill that keeps arriving for an environment its owner believes they destroyed.
  `destroy-env.sh` is short because of this decision.
- **Blast radius.** Shared dev's Cloud SQL, Memorystore, private-services range and quota would be
  shared with every personal environment.
- **The tooling already assumes it.** `bootstrap-env.sh` defaults `DEPLOYMENT_ID` to the project
  id, `lib.sh` refuses to mix one project's identity with another's resource names, and
  `create-workload-identity.sh` was written to bring a blank project up to deployable — because
  `hexera-prod` once was one.

### What is an owner's act and what is not

`new-env.sh`, `seed-secrets.sh` and `destroy-env.sh` run **as you**, never as CI. They refuse to run
as a service account rather than failing obscurely partway through. Creating a project, minting an
identity, minting the object-store key and writing secret *values* are precisely the authorities the
federated deploy identity is built without — and a personal environment does not relax that. Its
deploy identity holds exactly the roster `hexera-dev` and `hexera-prod` reconcile against, which is
what makes testing a deploy here worth anything.

### Credentials

`seed-secrets.sh` fills each container once and **never overwrites an enabled version**, so
rerunning it is always safe: `make seed-secrets SLUG=<slug>`.

- **Generated** — `POSTGRES_PASSWORD`, `MESH_API_KEY`, `USER_TOKEN_SECRET`, `AUTH_SECRET`. These are
  randomness, minted per environment, so a personal environment cannot sign a token another one
  accepts.
- **Copied** — `DEEPINFRA_API_KEY`, `DEEPSEEK_API_KEY`. A provider key is issued by a third party
  and cannot be generated. They are read from `hexera-dev` under your own credentials. If they
  cannot be read, the container is left **empty and reported** — never faked, because a faked key
  produces an environment that provisions cleanly and fails hours later inside an agent run.

### The object store, and the one thing you must not do

Minting the HMAC key the API and workers authenticate to Cloud Storage with needs
`storage.hmacKeyAdmin`, which the deploy identity is deliberately not given. So in every
environment the key is minted **once, by an owner**, and its access id — the public half — is then
pinned as a deploy target. Shared dev and prod pin theirs as literals in `deploy.yml`.

A personal environment cannot be pinned in a commit: the value does not exist until the environment
does, and requiring a code review before anybody could create one is the whole friction this
removes. So `new-env.sh` mints it at creation and records it in the register (§7).

This is also why **the picker refuses to deploy an environment registered without an access id**.
An empty one passes `validate-config.sh` — the store is not half-stated, the bucket beside it is
set — and would then reach `create-object-storage.sh`, which reads an empty recorded id as "no
usable key exists" and mints another. Silently, every run, until the account hits Google's five-key
limit and the deploy stops dead.

---

## 7. The register

One repository variable, `HEXERA_PERSONAL_ENVS`, carries one entry per environment:

```
areen=999888777666:GOOG1EAREEN...
pranav=224734058693:GOOG1EPRANAV...
```

`new-env.sh` writes the entry, `destroy-env.sh` removes it, and the deploy workflow's target picker
reads it.

**Why a repository variable at all**, when `deploy.yml` argues at length that every deploy target
should be a reviewed literal. A workload identity pool is addressed by project *number*, which
Google assigns at creation and which cannot be derived from the project id — so the value does not
exist until the environment does. The alternatives were a commit per environment created, or giving
the target-selection job a credential so it could look up a project it has not yet been told to
trust.

**What it is not trusted for.** It carries a project number and an object-store access id, and
neither is a credential. The project id, the registry, the service account and the component list
are all *derived* from the validated slug. Editing this variable cannot point a deploy at a project
some slug does not already name, and it cannot reach prod, which is tag-only.

If `gh` is missing or unauthenticated when you create an environment, registration is skipped with
a warning and the exact line to paste — everything else is already provisioned.

---

## 8. Destroying one

```bash
make destroy-env SLUG=pranav
```

Three refusals stand in front of the deletion, in this order:

1. **A literal name list** — `hexera-dev`, `hexera-prod` and `hexera` are never deletable by this
   script. First, because every check after it reads the live project, and a check that depends on
   an API call can be defeated by that call failing. A name match cannot.
2. **The prefix rule** — the project must be named `hexera-dev-*`.
3. **The label** — the project must carry `personal=true`, which `new-env.sh` stamps on every
   project it creates. The first two only establish that a project is *not obviously something
   else*; this is the positive evidence that it is what the script is for.

Deletion is **recoverable for about 30 days** (`gcloud projects undelete`), during which the project
serves nothing and bills nothing. After that it is permanent, and the project id cannot be reused
until the window closes.

---

## 9. When something goes wrong

| Symptom | What it means |
| --- | --- |
| `no personal environment named '<slug>'` | The register has no entry. The environment was never created, or was destroyed. Run `make new-env SLUG=<slug>`. |
| `registered without an object-store access id` | The entry is `slug=number` with no `:accessId`. Re-record it by rerunning `make new-env SLUG=<slug>` — it is resumable and will not re-create what exists. |
| `slug '<x>' is reserved` | You typed a shared environment's name. Leave the slug **empty** for shared dev; prod is reached only by a `v*` tag. |
| A provider key is reported EMPTY | `seed-secrets.sh` could not read it from `hexera-dev`. Supply it: `DEEPINFRA_API_KEY=... make seed-secrets SLUG=<slug>`. |
| `CLOUDRUN_CONSOLE_SERVICE is set but HEXERA_API_BASE_URL is not` | The API has never been deployed here, so it has no URL for the console to proxy to. Deploy without `console` first — see section 4. |
| `instanceGroupManagers/dev-workers was not found` | The queue signal ran before the fleet existed. Harmless — the publisher is in place; re-run with `queue` once `workers` has completed. See section 4. |
| `could not list the HMAC keys` | You ticked `storage` on a later run. Don't — the object store is established once, at creation (§6). |
| The console renders sign-up but submitting fails | Identity Platform is enabled but not **initialised** in your project. One-time console action — see §5. |
| The deploy authenticates as the wrong project | The register's project number is wrong. Check it against `gcloud projects describe hexera-dev-<slug>`. |

`new-env.sh` is **resumable**: every step tests for what it is about to create and skips it if
present, so rerunning after any failure continues from where it stopped.
