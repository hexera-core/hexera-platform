# Setting up dev-raymond

A walkthrough for giving Raymond his own API, console, mesh job and worker fleet inside
`hexera-dev`. Nothing needs creating first. The deploy that names the slug is what creates the
environment.

Substitute any slug for `raymond` and this works unchanged.
[personal-environments.md](personal-environments.md) is the design and the reasoning. This is the
order to do it in, and the two things that doc leaves out.

## What Raymond needs before he starts

| | why |
| --- | --- |
| a `@hexera.ai` Google account | the org binding is what gets him `hexera-dev`; a gmail account will show no IAM |
| write access to the repo and `gh` logged in | the deploy is a `workflow_dispatch`, so it runs on his branch |
| `gcloud` logged in as that account | two of the five steps run on his machine, not in CI |
| a branch pushed to the remote | `--ref` has to resolve on the remote, a local-only branch fails |

Slug rules: lowercase, starts with a letter, does not end with a hyphen, **at most 14
characters**. `raymond` is 7. `dev`, `prod`, `production`, `shared`, `main` and `staging` are
refused. Shared dev is reached by leaving the slug empty.

## The order

```bash
# 0.  on Raymond's machine. Fills the secret containers, creating any that are missing.
#     Run this FIRST so the API never rolls with empty credentials.
make seed-secrets SLUG=raymond

# 1.  the slow one, about half an hour. Reconciles the data tier and builds the fleet.
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=raymond -f app=true -f data=true -f fleet=true

# 2.  the second half of the first deploy. Fast.
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=raymond -f console=true -f fleet=true

# 3.  every deploy from then on. One run.
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=raymond -f app=true -f console=true
```

### Why step 0 exists, and why it is not in CI

The deploy identity deliberately cannot read a secret's payload. `create-workload-identity.sh`
builds a custom Secret Manager role that omits `versions.access`, on the grounds that a deploy
which can read the credentials it deploys is a deploy that can leak them. So the first value in
each container has to be written by a person.

Two kinds go in:

- **Generated.** `MESH_API_KEY`, `USER_TOKEN_SECRET`, `AUTH_SECRET`. Just randomness, minted
  locally, so one environment cannot sign a token another environment accepts.
- **Copied.** `DEEPINFRA_API_KEY`, `DEEPSEEK_API_KEY`. A third party issued these and they cannot
  be generated. They are read out of `hexera-dev` under Raymond's own credentials and written
  straight through, never logged and never written to a file. **Without them the environment
  cannot run a single job.**

It is idempotent, and that is the safety property. A container that already holds an enabled
version is left exactly as it is, so rerunning after a partial failure fills only what is still
empty. It can never rotate a credential a running service is using.

### Why the first deploy is two runs

Both halves fail for a reason, not a flake.

`console` on a first run dies at stage 2 with `CLOUDRUN_CONSOLE_SERVICE is set but
HEXERA_API_BASE_URL is not`. The console is handed the origin of the API it proxies to, and Google
does not assign a Cloud Run URL until the service exists.

`fleet` appears on both runs on purpose. Stage 13 attaches an autoscaling policy to the managed
instance group and stage 18 is what creates that group, so on the first run only that attachment
defers. The publisher, its schedule and its identity all land on run 1.

`storage` is selected on every personal run whether or not it is ticked. It mints the object-store
key on the first deploy and hands the access id to the API on every one after. Without it the API
rolls with no store and returns 503 on every upload.

## What he ends up with

The slug becomes deployment id `dev-raymond`, and every resource is named off it.

| | |
| --- | --- |
| API | `dev-raymond-api`, on its own `run.app` URL |
| Console | `dev-raymond-console` |
| Jobs | `dev-raymond-mesh`, `-migrate`, `-queue-depth`, `-maintenance-sweep` |
| Workers | `dev-raymond-workers`, autoscaled |
| Buckets | `dev-raymond-exchange-...`, `-artifacts-...`, `-transfer-...` |
| Database | `meshpipeline_raymond` on the shared instance |
| Celery keys | prefixed `dev-raymond:` |

Finding the URLs:

```bash
gcloud run services list --project hexera-dev \
  --filter "metadata.labels.deployment-id=dev-raymond" \
  --format "table(metadata.name,status.url)"
```

## The one grant he may have to ask for

`create-worker-fleet.sh` tries to grant the mesh job's identity `roles/artifactregistry.reader` on
the image's repository. The deploy identity does not hold
`artifactregistry.repositories.setIamPolicy`, so it may not be able to. When it cannot, it prints
the exact command and carries on.

That matters because of how it then fails: the VM boots, cannot pull the image, exits, and **the
instance group still reports RUNNING**. The job times out with nothing useful in the log. If a big
mesh hangs on a brand new environment, check this first.

Somebody with rights on the registry runs the printed command once. It is per repository, not per
environment, so once a repository has it no later slug needs it again.

## What a personal environment deliberately does not get

- No admin console. Ticking `admin` on a personal run reconciles nothing and says so.
- No custom hostname or certificate. The generated `run.app` URLs work unmodified.
- No outreach sender, with no switch that can change it. It can email real people, and a sandbox
  made by typing a name into a text box is the last place it should be reachable.
- No billing export. It accumulates in a table `hexera-prod` owns.

## What is shared, stated plainly

The project, its VPC and deploy identity, the Artifact Registry, the provider keys, **one Cloud
SQL instance** (`hexera-dev-pg`) and **one Memorystore instance** (`hexera-dev-redis`).

Raymond gets his own database and his own Celery key prefix on those, not his own instances. So
exhausting connections, filling the disk or restarting one affects everybody. That was the trade
for a first deploy measured in minutes instead of half an hour, and no per-developer instance
bill.

It does not mean shared state. His migrations move his schema only, and his jobs cannot be picked
up by anyone else's workers.

## Tearing it down

```bash
make destroy-env SLUG=raymond
```

It removes the Cloud Run services and jobs, the scheduler job, the instance group and its
templates, his three buckets, his database, his HMAC key and secret, and his service accounts. It
never touches `hexera-dev-pg` or `hexera-dev-redis`, because deleting one to tear down a sandbox
would take everybody with it.

**It does not claim to be complete.** It can only delete what carries the `deployment-id` label
the deploy stamps. Anything made by hand is swept for, reported, and left where it is. Read that
list before assuming the environment is gone. None of this has an undelete window, unlike
deleting a project.

## Costs

A personal environment sitting idle is close to free: Cloud Run scales to zero, the fleet
autoscales to zero, and the two expensive instances are shared and already paid for. What costs
money is a worker fleet left running and disks left behind after a by-hand experiment. Both carry
the `deployment-id` label, so both turn up in the teardown sweep.
