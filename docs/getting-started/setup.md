# Setup

Getting a working local system, and what each piece is for. Every setting mentioned here is
described in full in [configuration.md](../reference/configuration.md); this document says what you must do,
not what every value means.

**Just want the commands?** [Install](#install) is the complete sequence, in order, with nothing
implied. The two steps that trip up almost every first setup are called out there: ticking every
checkbox on the Google consent page, and copying the credential into `secrets/gcp/` afterwards.
Everything before that section is what you need in place first, and why.

## Prerequisites

| Requirement | Why |
|---|---|
| **Linux on x86-64 (`amd64`)** | the platform every build input is published for, and the one CI builds and tests on |
| **Docker** + the **Compose v2** plugin | every service runs in a container; nothing is installed on the host |
| **Python ≥ 3.11** | the host toolchain and test suites. 3.11 and 3.12 are the proven versions; setup accepts 3.13/3.14 with a warning and refuses anything older |
| **git**, **make** | the entry point to every workflow in this repository |
| **Graphics/OpenMP system libraries** | the pinned `vtk`/`pyvista`/`cadquery` wheels open them at import. On Debian/Ubuntu: `libglu1-mesa libgl1 libxrender1 libxcursor1 libxft2 libxinerama1 libgomp1` |
| **~20 GB free disk** | the mesh toolchain image is large; the application images are not |
| **Both model-provider keys** | `DEEPSEEK_API_KEY` (intake) and `DEEPINFRA_API_KEY` (builder and reviewer): both are required settings, not alternatives |
| **Google Cloud CLI** *(conditional)* | only to obtain your own credential, and to run `make mesh-setup`, which provisions cloud resources. `make setup` never needs it, and on Ubuntu offers to install it from Google's signed apt repository if it is absent: declining costs you nothing here. [Install](https://cloud.google.com/sdk/docs/install) · [ADC setup](https://cloud.google.com/docs/authentication/provide-credentials-adc) |
| **A Google Cloud project with billing enabled** | meshing runs on a Cloud Run job you own. Without billing, enabling the APIs fails and nothing can be provisioned |

`make setup` verifies **five** of these and names anything missing rather than failing later: the
interpreter, `git`, Docker, the Compose v2 plugin, and the graphics/OpenMP libraries (by importing
the render stack). It offers to install `gcloud` on Ubuntu from Google's signed apt repository, and
installs nothing else for you: a missing prerequisite is reported by name and you obtain it however
your platform provides it. On a distribution whose default `python3` is older than 3.11, Ubuntu
22.04 ships 3.10, install a supported interpreter (`python3.12`) before running `make setup`.

**Four it cannot check**, because setup reads no setting, needs no credential and contacts nobody:
the CPU architecture, free disk space, your two provider keys, and whether a billed Google Cloud
project exists. Those surface later, at `make dev-up` or `make mesh-setup`. Confirm the cloud ones
yourself before provisioning:

```bash
gcloud projects describe <PROJECT_ID> --format="value(projectId,lifecycleState)"
gcloud billing projects describe <PROJECT_ID> --format="value(billingEnabled)"
```

`gcloud config set project` does **not** validate that the project exists; it accepts any string.

The architecture is not a preference. The mesh toolchain is only published for Linux x86-64: the
OpenFOAM packages come from a Debian/Ubuntu APT repository, and the micromamba release the image
verifies is the `linux-64` asset. On `arm64`, including Apple Silicon, `make mesh-image` cannot
build, so the native test tiers are unavailable. The application itself is ordinary Python and
containers, and the mesh always runs off-box.

### Meshing compute

Meshing needs OpenFOAM, cfMesh, Gmsh and VMTK. You do not install them. Every mesh the
application runs is dispatched to the Cloud Run mesh job, which exchanges workspaces through a GCS
bucket. That needs `GCP_PROJECT_ID`, `CLOUDRUN_JOB` and `GCP_MESH_BUCKET`; `make dev-up` and
`make dev-doctor` refuse to start without them, so the stack never looks healthy while unable to
dispatch a mesh.

The mesh image (`make mesh-image`) is only for the native test tiers, which run the engines
in-process to validate that image. No application path meshes on your machine.

See [operating-modes.md](../architecture/operating-modes.md#mesh-execution) for what that means in practice.

## Install

The whole sequence, in order. Steps 4 and 5 are where first setups fail, and both are explained
directly below.

```bash
# 1. Toolchain, virtualenv, local directories and images. Needs no credential and no network login.
make setup

# 2. Sign the CLI in as yourself. Provisioning grants access to THIS account.
gcloud auth login

# 3. Select the project for the CLI.
gcloud config set project <PROJECT_ID>

# 4. Write Application Default Credentials.
#    TICK EVERY CHECKBOX on the consent page or this fails - see "Step 4" below.
gcloud auth application-default login

# 5. Copy the credential into the repository. Step 4 does NOT do this for you.
install -D -m 600 \
  "$HOME/.config/gcloud/application_default_credentials.json" \
  secrets/gcp/application_default_credentials.json

# 6. Which path are you on? Look - do not assume from an empty .env.
gcloud run jobs list --project=<PROJECT_ID>

# 7a. A MESH JOB WAS LISTED (Path A): let the executor tell you its own identifiers.
make mesh-adopt       # fills GCP_PROJECT_ID, GCP_REGION, CLOUDRUN_JOB and GCP_MESH_BUCKET
$EDITOR .env          # the two provider keys - the only values nothing can discover
make mesh-doctor      # must pass first
make dev-up

# 7b. NOTHING WAS LISTED (Path B): create the executor, then start.
$EDITOR .env          # provider keys, plus the GCP names you want created - mesh-setup
                      # refuses to start without GCP_PROJECT_ID
make mesh-setup       # creates the job, bucket, service account and IAM; ends by running mesh-doctor
make dev-up
```

Step 7 is a fork, not two steps: the listing in step 6 decides it. Both are described under
[Connecting to the mesh executor](#connecting-to-the-mesh-executor). Running B against an executor
that already exists rebuilds and republishes the OpenFOAM image for nothing.

`make mesh-adopt` exists because those four values name resources that already exist - they are not
values you invent, and the session you authenticated in step 4 can already read every one of them.
It fills only settings that are empty, so it never reverts something you typed, and it creates
nothing in the cloud. What it cannot resolve it refuses to guess: if two buckets are equally
consistent with the evidence it names them and stops, rather than picking one.

### Step 4: consent to every scope, or the login fails

`gcloud auth application-default login` opens a page headed **"Select what Google Auth Library can
access"**, listing checkboxes. **Those checkboxes are the consent.** If you press **Continue**
without ticking them, the browser reports success and the terminal then fails:

```
ERROR: There was a problem with web authentication. Try running again with --no-browser.
ERROR: (gcloud.auth.application-default.login) https://www.googleapis.com/auth/cloud-platform
scope is required but not consented. Please run the login command again and consent in the
login page.
```

Tick **Select all**, then Continue. Rerunning with `--no-browser`, which the first error line
suggests, does not help: an unticked box is the fault, not the browser.

A successful run ends with `Credentials saved to file:
[/home/<you>/.config/gcloud/application_default_credentials.json]`. That path is the **source** for
step 5, never a path the stack reads. A line about a quota project being added to ADC is normal.

### Step 5: the copy is a real step, not a note

Nothing copies the credential for you. `gcloud` writes it under `$HOME`; the stack only ever reads
`secrets/gcp/application_default_credentials.json` inside this repository. Until you run the
`install` command in step 5, `make dev-up` refuses to start and `make mesh-doctor` reports the
credential as missing. `install -D` creates the parent directory, `-m 600` sets owner-only
permissions, and **copying rather than moving** keeps every other gcloud tool on your machine
working. The details, and why it must be your own credential, are under
[The Google credential](#the-google-credential).

### About `make setup`

It creates `.env` (only when absent), creates the `.venv`, installs the host toolchain, prepares
local directories, validates Docker and builds the application images. It is safe to re-run: if
`.env` already exists it leaves every value untouched, so a re-run can never revert something you
set. It reads no setting and needs no credential, so it finishes on a fresh clone whose `.env` is
still entirely blank. `make dev-up` is what checks that the values are there.

### About `.env`

The repository-root `.env` is the one file you edit. `.env.example` is its tracked template: every
supported key, its default and a one-line comment, rendered from
`src/meshpipeline/settings/inventory.py`. It carries no secret, a key you must supply is blank.
Regenerate it after a settings change with
`.venv/bin/python -m meshpipeline.settings.inventory > .env.example`.

A variable exported in your shell wins over `.env`, and `.env` wins over the value the code falls
back to. The same file configures host commands (`make check`, `make test`) and the container
stack, which forwards it, the stack only overrides the addresses containers use to reach each
other.

## Credentials

Two model-provider keys and one Google credential. Add the keys to `.env`:

| Key | Used by |
|---|---|
| `DEEPSEEK_API_KEY` | the intake conversation and the search summarizer |
| `DEEPINFRA_API_KEY` | the builder and the reviewer |

Both are required: a job cannot run without either.

### The Google credential

Three commands are often confused, and none of them is the copy:

| Command | What it does | What it does **not** do |
|---|---|---|
| `gcloud auth login` | signs the **CLI** in as you | write anything client libraries read |
| `gcloud config set project <PROJECT_ID>` | selects the project **locally** | grant you access to it |
| `gcloud auth application-default login` | writes **Application Default Credentials** under `$HOME` | put them anywhere this repository looks |

So there is a fourth step, step 5 of the install sequence. The stack reads the file named by
`GOOGLE_ADC_FILE`, default `./secrets/gcp/application_default_credentials.json`. **Copy** your ADC
there; moving it breaks every other gcloud tool on your machine:

```bash
install -D -m 600 \
  "$HOME/.config/gcloud/application_default_credentials.json" \
  secrets/gcp/application_default_credentials.json
```

`install -D` creates the parent directory and `-m 600` sets owner-only permissions. Confirm it
landed, without printing the contents:

```bash
ls -l secrets/gcp/application_default_credentials.json   # expect -rw------- and a non-zero size
```

**If the stack ever started before that file existed**, Docker created an empty *directory* with the
credential's name, and the copy will fail or land inside it. Clear it first:

```bash
ls -ld secrets/gcp/application_default_credentials.json   # a directory here is the fault
sudo rmdir secrets/gcp/application_default_credentials.json
```

`make dev-up` refuses to start unless a regular, non-empty, readable credential is there. Only the
**worker** receives it, mounted read-only; api, beat and worker-utility get none, because no code
path in them reaches Google. The images carry the Google Python libraries, never `gcloud` itself.

### Rules

- `.env` and `secrets/` are both gitignored, and `secrets/` is excluded from every Docker build
  context. Never commit either.
- `GOOGLE_ADC_FILE` holds a **path**. Never paste credential JSON into `.env`.
- Keep the file at mode `0600`. **Never accept someone else's ADC**: it can carry a refresh token,
  so it is a credential in its own right. Authenticate as yourself and ask an administrator to grant
  your identity access to the job and bucket.
- Deleting your local credential changes nothing in the cloud project.

## Connecting to the mesh executor

Every mesh is dispatched to a Cloud Run job. Which path you are on is decided by **one question,
and it is not about who owns the project or whether the project exists**:

> Does a Cloud Run mesh job already exist in this project?

Answer it by looking, not by remembering. An empty `.env` proves nothing: `.env` is local, and
`make dev-uninstall` or any local reset deletes it while every cloud resource keeps running and,
for the bucket, keeps billing.

```bash
gcloud run jobs list --project=<PROJECT_ID>
```

| The listing shows | You are on | Because |
|---|---|---|
| a mesh job | **Path A** | the executor exists; configure `.env` to point at it and create nothing |
| nothing | **Path B** | there is no executor; `make mesh-setup` creates one |

This is why the paths are not "someone else's project" versus "your project". A project you own,
provisioned months ago and then re-cloned onto a new machine, is **Path A**: you are joining an
executor that already exists, and the fact that you are the one who created it changes nothing
about what you now run. Running Path B against an existing executor rebuilds and republishes the
OpenFOAM mesh image for resources that are already there.

### Path A: an executor already exists

You create nothing. You need four identifiers: `GCP_PROJECT_ID`, `GCP_REGION`, `CLOUDRUN_JOB` and
`GCP_MESH_BUCKET`.

**Start here:**

```bash
make mesh-adopt
```

It reads all four off the executor your session can already see and writes them into `.env`,
filling only what is empty. That is the whole step for most people, and `make mesh-doctor` is the
next command. It resolves the exchange bucket the way the rest of this section describes - by the
grant, not the name - and if the evidence points at more than one bucket it names them and stops
instead of choosing. Everything below is what to do when it stops, or when you would rather
establish the values yourself.

**Where they come from,** in order of authority:

1. The deployment record `mesh-setup` wrote, `deploy/output/deployment.json`. It is the only
   artefact that records which resources this tooling *created* rather than *reused*.
2. Failing that, the project's administrator.

**If you are the administrator and the record is gone** - a fresh clone, a wiped machine, a local
reset; `deploy/output/` is gitignored and never committed, so it does not survive one - you can
list what exists:

```bash
gcloud run jobs list --project=<PROJECT_ID>            # names the job and its region
gcloud storage buckets list --project=<PROJECT_ID>     # names every bucket
```

The job and region are unambiguous. **The bucket usually is not.** A listing tells you what is
there, not which one this deployment used, and projects routinely hold several. **Never guess the
exchange bucket from its name** - a name that looks like an exchange bucket may be something else
entirely, and pointing the stack at the wrong one produces jobs that fail after dispatch.

Two things narrow it down when the record is lost, neither of them proof:

```bash
# provisioning grants your own account storage.objectUser on the exchange bucket
gcloud storage buckets get-iam-policy "gs://<BUCKET>" --format=json | grep -A3 objectUser

# the exchange bucket is in the same region as the job, and dated with it
gcloud storage buckets describe "gs://<BUCKET>" --format="value(location,creation_time)"
```

Note that re-running `make -C deploy/gcp state` does **not** recover the answer: it writes the
record from whatever `.env` currently says, so it re-serializes your guess rather than discovering
the truth. If neither candidate can be distinguished, the safe resolution is to provision a new
exchange bucket rather than adopt one you cannot identify.

Then place your own ADC (above), put the four values and both provider keys in `.env`, and confirm:

```bash
make mesh-doctor
```

`make dev-up` runs this same diagnosis itself before starting anything, so a passing `mesh-doctor`
is a preview of that gate rather than an extra hurdle.

### Path B: no executor exists yet: provision one

**The job listing came back empty.** One command builds and validates the mesh image,
enables the APIs, publishes the image to a registry it creates, and provisions the exchange bucket,
service account, IAM and Cloud Run job under the names you declared in `.env`:

```bash
make mesh-setup
```

It shows you what it is about to create and asks you to type the project id before anything is
made. It grants **your own account** the two roles the adapter uses -
`run.jobsExecutorWithOverrides` on the job and `storage.objectUser` on the bucket, deriving the
principal from your active gcloud account rather than asking you for it. It finishes by running the
same diagnosis `make mesh-doctor` runs, and fails if that does not pass.

**Expect it to take a while.** It builds the mesh toolchain image, which downloads OpenFOAM, and
runs the release gate over it before publishing. It is idempotent, so an interrupted run resumes by
rerunning it, and rerunning it later creates nothing new.

Individual stages, if you need them, live in `deploy/gcp/Makefile` (`enable-apis`,
`artifact-registry`, `service-account`, `mesh-tier`, `iam`, `state`) and are documented in
[deployment/overview.md](../deployment/overview.md), which also lists the least-privilege IAM applied and the
roles the deployer needs.

### Checking the executor, `make mesh-doctor`

```bash
make mesh-doctor
```

**What it validates:** the four GCP settings, the credential file, the `gcloud` CLI and session,
the selected project, the deploy permissions, and whether the mesh job and exchange bucket exist.
It runs every check before deciding, so one run lists everything you need to fix. It is read-only
and never prints a setting's value or any credential content.

**Its exit code is the verdict**, so you can use it in a script:

| exit | meaning |
|---|---|
| 0 | every prerequisite it checks is satisfied |
| 1 | something on this machine is missing: a CLI, a setting, the credential file |
| 2 | this machine is fine, but the session or a cloud resource is not usable |

**What it does not do:** run a mesh, and create or modify anything. It cannot tell you the job will
succeed on your geometry, only that it exists and you may invoke it.

## First run

```bash
make dev-up
```

`make dev-up` proves the stack can mesh before it starts one. It runs four gates in order, and
starts no application container until all four pass:

1. **Configuration.** Checks `.env` against the settings catalogue and reports **every** missing or
   invalid value at once, rather than one per run.
2. **`mesh-doctor`.** The same diagnosis the standalone target runs, so a configured-but-unreachable
   executor is caught here instead of by a mesh that fails twenty minutes in.
3. **`dev-images`.** Rebuilds the application images only if the stamp they carry is not this
   working tree. Unchanged source means no rebuild.
4. **Cloud Run preflight, inside an application container.** Refreshes the mounted credential,
   describes the configured job and checks exchange-bucket access - so what it proves is what the
   stack will actually do, not what the host can do.

Gate 4 has to create this project's volumes to run, so a **failure at that gate tears them down
again** (`docker compose down -v`): a run that decided not to start leaves the machine as it found
it, and the next attempt is still a first attempt. You do not run `make dev-doctor` separately; it
remains available for diagnosing a configured stack.

Once up:

```bash
curl localhost:8000/readyz
```

reports ready when PostgreSQL, Redis and the object store are all reachable. Then open
<http://localhost:8000/ui>.

Upload a geometry file, describe the job in the conversation, approve the requirements Hexera
reads back, and watch the run.

## The local services

The stack is api, worker, worker-utility, beat, postgres, redis, minio and searxng, plus
`minio-init`, a one-shot container that creates the buckets and exits. What each one
owns, how the Compose network is scoped, and the database, object-store and Redis layouts are in
[architecture/overview.md](../architecture/overview.md#the-local-services).

`make dev-up` prints three addresses. The first two need no credential; the third does:

| address | what it is | sign in with |
|---|---|---|
| <http://localhost:8000/ui> | the run page | none |
| <http://localhost:8000> | the API (`/health`, `/readyz`) | none |
| <http://localhost:9001> | the **MinIO console** | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` from `.env` (both default to `minioadmin`) |

MinIO is this machine's S3: uploaded geometry, mesh artifacts and the reviewer's renders live in
the `MINIO_BUCKET` bucket, and the browser gets signed URLs rather than the bytes. The console is
the quickest way to see what a run actually produced. It is **local only** and unrelated to
`GCP_MESH_BUCKET`, the GCS bucket the Cloud Run job exchanges workspaces through.

**The API is the one service published to every interface.** Postgres, Redis, MinIO and the worker
bind to `127.0.0.1`, but `api` publishes `8000:8000`, so the run page is reachable from your
network at `http://<this-machine>:8000/ui`, not just locally. With `PUBLIC_TRACE_MODE=raw` - the
default - that page shows provider reasoning, real tool names, arguments, results and the
reviewer's inspection images to anyone who opens it. On a trusted network that is the point; on a
shared one, set `PUBLIC_TRACE_MODE=safe`, or bind the API to `127.0.0.1:8000:8000` in
`docker-compose.yml`.

MinIO ships with its own well-known default credentials, which is safe only because port 9001 is
localhost-bound. Change `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` before publishing that port
anywhere.

## Verifying the installation

```bash
make check-fast   # lint + fast unit suite
make check        # the full commit gate
```

Both run in the `.venv` created by `make setup`. What each gate proves:
[development/gates.md](../development/gates.md).

## Stopping and resetting

Routine:

```bash
make dev-down          # stop the stack; volumes, database and object store are kept
make logs              # tail all service logs
make restart           # restart the application services in place
make migrate           # apply migrations against the running stack
```

Rebuilds:

```bash
make dev-build         # build the application images; starts nothing
make dev-images        # build only if the images no longer match the committed source
make rebuild           # rebuild the app images from the working tree and restart them,
                       # keeping every volume. This is the one that also re-stamps the
                       # RUNNING containers, so it is what `make test-integration` needs
                       # after you change source.
make restart           # down, rebuild with no cache, up again
```

**Destructive.** All four are local only; none touches your Google Cloud project:

```bash
make dev-reset         # deletes this project's volumes: database, object store, job records
make clean             # this project's containers, volumes and locally built images
make clean-workspaces  # deletes every job workspace inside the running worker
make dev-uninstall     # dev-reset plus this project's images, .env, .venv and build residue
```

`dev-reset` is the routine one: it empties the databases and leaves the machine set up.

`dev-uninstall` additionally deletes this project's images, `.env`, `.venv`, `build/`, `dist/`,
`*.egg-info`, `.pytest_cache` and the contents of `output/`. It **keeps** your checkout, `secrets/`
(so your credential survives), and `data/` and `workspaces/`. It is therefore not an exact inverse
of `make setup`, and because it deletes `.env` it also destroys the only local copy of your four
GCP identifiers - record them somewhere first, or you will be recovering them from the cloud as
described under [Path A](#path-a-an-executor-already-exists).

Removing what you provisioned in Google Cloud is a separate command:

```bash
make mesh-destroy                       # dry run: prints the plan, deletes nothing
make mesh-destroy DESTROY_ARGS=--apply  # carries it out, after you type the project id
```

It deletes only what the deployment record says `mesh-setup` created. Anything that already existed
is recorded as reused and left alone, and the project itself is never deleted.

**Without `deploy/output/deployment.json` it does nothing at all** - it reports that nothing is
known to have been created here and exits 0, by design: it will not guess ownership from a name.
Since that record is gitignored and does not survive a fresh clone or a local reset, a cloud
executor can outlive every local trace of itself. Removing one in that state means deleting the
job, bucket, service account and registry by hand in the console.

After any command that removes images, the next build is a cold one: it re-pulls the pinned base
images and recompiles the render stack.
