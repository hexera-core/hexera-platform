# Test and release gates

Five gates, each answering exactly one question. They are ordered, and the order is the
point: **B gates a commit, C builds one artifact set from that commit and validates it,
D promotes that same artifact, and E checks the running revision. Nothing after C
rebuilds the application.**

```
source commit
    │
    ├─ A  make check-fast        am I safe to keep coding?
    │
    ├─ B  make check             may this COMMIT produce a release artifact?
    │
    ├─ C  make release-validate  build the deployable images ONCE; validate THOSE,
    │                            native-terminal INCLUDED and mandatory
    │                            → deploy/output/release.json   state=validated
    │
    ├─    make release-publish   push those exact images without rebuilding;
    │                            record local image ID ↔ registry digest
    │                            → deploy/output/release.json   state=published
    │
    ├─ D  make mesh-preflight  may the published artifact be promoted here?
    │                            (read-only; runs NO source tests)
    │
    ├─    make mesh-deploy            promotes registry/component@sha256:… - never builds
    │
    └─ E  make mesh-doctor      is the mesh executor configured and reachable?
                                 (a real submission is deploy/gcp's opt-in `make smoke`)
```

---

## Gate A: fast development

| | |
|---|---|
| **Command** | `make check-fast` |
| **Question** | Am I safe to keep coding? |
| **Inputs** | the working tree |
| **Checks** | ruff over `src tests alembic devtools`; the graph-wiring test; the import sweep |
| **Excludes** | the full unit suite, mypy, containers, services, native tooling |
| **Output** | pass/fail only |
| **Network** | none |
| **Mutates** | nothing |
| **A failure means** | the tree does not lint, a module does not import, or the graph cannot be composed: you are not safe to keep going |

Deliberately not the whole suite. The whole suite is Gate B and takes ~25s; A exists for
the edit loop, and every test it omits is run by B before anything can be released.

## Gate B: pre-merge / source validation

| | |
|---|---|
| **Command** | `make check PY=.venv/bin/python` |
| **Question** | May this commit produce a release artifact? |
| **Inputs** | the working tree, the pinned dev environment |
| **Checks** | ruff (`src tests alembic devtools`); the mypy ratchet (fails on any NEW error); the dependency source-of-truth check; the complete hermetic unit suite (`tests/unit`: API/contract/security/schema/migration-consistency); the UI in a **real headless Chrome** (`tests/ui`): the live page boots, with no uncaught exception, no console error and no missing asset |
| **Excludes** | anything needing a container, a service, licensed CAD, or the native toolchain |
| **Requires** | Chrome/Chromium on `PATH`, or `CHROME_BIN`. Its absence **fails** the gate with installation instructions: it is never skipped, because a release that could not run its browser validation has not been validated. On Ubuntu the `chromium` package is a snap wrapper that will not run where snapd is absent (containers, CI); install Google Chrome's `.deb` or point `CHROME_BIN` at a real binary |
| **Output** | pass/fail; CI reports it per step |
| **Network** | none |
| **Mutates** | nothing |
| **A failure means** | the commit is not eligible to become a release artifact |

`tests/unit/migrations` runs here and reads the alembic chain **statically**; nothing in
Gate B touches a database.

### The mypy ratchet

`make typecheck` (also part of `make check`) runs `devtools/quality/mypy_ratchet.py`. Its contract:

- The language target is pinned by the project (`python_version` under `[tool.mypy]`), **not** by
  whichever interpreter launches mypy, so the supported host Python and the validation image's
  Python analyse the same target and agree on the same baseline.
- A **new** error, one not in `devtools/quality/mypy_baseline.txt`, fails the gate.
- A baseline error that **no longer occurs** also fails: a cured error must leave the baseline, or
  it silently re-permits itself. Re-key it with `--update`, which refuses to run in an environment
  missing the dependencies that change what mypy sees.
- **Exit 2 means no verdict was reached**: mypy could not run at all, typically because the
  checkout was read-only and it could not write its cache. The baseline is left untouched and
  nothing is concluded; this is deliberately distinct from the exit code for a real ratchet event.

The dependency-backed integration tier (`tests/integration`, real Postgres/Redis/MinIO)
runs in CI's `images` job against the built image. It is not in `make check` because it
needs Docker; treat a CI red there as a Gate B failure.

## Gate C: release-artifact validation

| | |
|---|---|
| **Command** | `make release-validate` |
| **Question** | Does the distributable artifact work, independently of the source checkout? |
| **Inputs** | a **clean** checkout (refused otherwise: the record could not identify what was built) |
| **Checks** | wheel build; wheel inspection; clean **non-editable** install into a throwaway venv; import provenance asserted from `site-packages` and refused if it resolves into `src/`; the two **deployable** image builds; both entrypoints on the app image; OpenFOAM + vmtk on the mesh image; in-image package from `dist-packages`; **the UI served by the app image to a real browser** (the container publishes a port and mounts nothing, so the page under test is the one baked into the image); **native smoke, native all AND native terminal**, all inside the image with zero bind mounts |
| **Excludes** | source-level unit tests (Gate B already ran them against this commit) |
| **Output** | `deploy/output/release.json`, `state=validated`. Written atomically. Gitignored, like `deployment.json`. |
| **Network** | whatever `docker build` needs; the service images are pulled if absent |
| **Mutates** | `deploy/output/` and the containers/networks/venvs it creates and removes |
| **A failure means** | the artifact is broken even though the source passed: do not promote |

**Components.** Two images are deployable, each a Dockerfile target: `app` (target
`pipeline`) serves **both** the API service and the pipeline job, and `mesh` serves the mesh job.
Gate C validates `app` with both entrypoints. It used to build a separate `api`-target image and
validate that; nothing ever deployed it.

**Native-terminal is mandatory.** Gate C provisions its own Postgres, Redis and MinIO on a
per-run network, health-checks them with bounded timeouts, runs the five-engine matrix, copies
the evidence out, and removes every container it created from an `EXIT` trap, matched by a
per-run label, so a developer's stack is never touched. Coverage is verified by **node ID**
(five engines plus restart/replay, gate containment, delivery failure and stale-worker CAS), not
by a hardcoded count, so a legitimate new test is accepted rather than failing the gate. Evidence
is checked for freshness: distinct job ids, `final_result` schema 4, and a native marker proving
the engine binary ran.

**The verdict is computed, not asserted.** Every check is required or not:

| verdict | meaning |
|---|---|
| `passed` | every required check passed |
| `incomplete` | a required check was skipped or never ran, nothing failed |
| `failed` | a required check failed |

`RELEASE_ALLOW_INCOMPLETE=1` skips the native tiers for debugging and produces `incomplete`,
which can never be promoted. There is no flag that yields a PASS without the tiers.

**One scratch directory, removed when the run ends.** Gate C works in a single `mktemp -d`
workspace and deletes that exact directory on the way out, on success, on failure, and on
interrupt or terminate alike. The verdict lives in `deploy/output/release.json`, which is written
before the workspace goes and is the thing meant to outlive it. Set `KEEP_WORK=1` on an invocation
to keep the build and tier logs instead; the retained path is printed. A failing run does **not**
keep the directory on your behalf.

**Zero bind mounts.** Every native tier creates a **stopped** container, `docker cp`s the
committed `tests/` and `pyproject.toml` into it, starts it, copies evidence back out, then
records `Mounts` and `HostConfig.Binds` from `docker inspect`. A bind-mounted `src/` would
shadow the installed package and the tier would be testing the checkout rather than the
artifact, so the run **fails** if either field is non-empty.

## Publication, `make release-publish`

| | |
|---|---|
| **Question** | can the exact validated bytes be identified in a registry? |
| **Inputs** | a `state=validated`, `verdict=passed` record, and the local images it names |
| **Checks** | the record is a PASS for HEAD; the tree is clean; each component's local image is still present **and its ID still matches the record**; the registry does not already hold this tag pointing at a different image |
| **Does** | `docker tag` + `docker push` of those image objects. **It never builds.** |
| **Output** | the same record, `state=published`, each component carrying `registry_digest` and a `registry/component@sha256:…` reference |
| **A failure means** | the bytes on this host are not the bytes that were validated: re-run Gate C |

## Gate D: deployment readiness

| | |
|---|---|
| **Command** | `make mesh-preflight` |
| **Question** | May the artifact Gate C validated be promoted into this environment? |
| **Inputs** | `deploy/output/release.json`, the deployment environment (`$DEPLOY_ENV_FILE`, else `deploy/gcp/generated.env`), a gcloud session (optional) |
| **Checks** | the record exists, parses, is schema 2, and is promotable by the same authority Gate C and publish use; **native smoke, native all and native terminal are each named and passed** (a record that simply omits a mandatory tier is refused); every required check passed; record commit == HEAD and record tree == current tree; tree and index clean; every component carries a **digest-qualified** reference; the local image, if still present, still matches the recorded ID; `MESH_IMAGE` in the deployment environment is the recorded digest, not a tag; no credential values in that file; manifests render and parse, and any previously rendered manifest is digest-qualified; the alembic chain has one head, one baseline, every `down_revision` resolves; **each recorded digest resolves in the registry to the same digest**; GCP IAM preflight; a rollback target revision exists |
| **Excludes** | every source-level test: they gated the commit that produced the artifact; re-running them here proves nothing about the artifact and hides the checks that are specific to promotion |
| **Output** | one of three distinct outcomes: `DEPLOYMENT READY`, `LOCAL PREFLIGHT PASSED / CLOUD READINESS UNVERIFIED`, or `GATE D FAILED`: plus **every skipped check listed by name**. The three are never collapsed: a host with no gcloud cannot produce a "ready" verdict. |
| **Network** | read-only Google Cloud calls for the last two sections; loudly skipped if gcloud is absent or unauthenticated |
| **Mutates** | nothing |
| **A failure means** | do not run `make mesh-deploy` |

## Gate E: post-deployment verification

Nothing public is deployed: the application runs on the operator's machine and the only remote
component is a **private** Cloud Run job. So this gate has two halves, one free and read-only, one
that costs a real mesh and is therefore never automatic.

| | |
|---|---|
| **Command** | `make mesh-doctor` |
| **Question** | Is the mesh executor configured, reachable and usable from this machine? |
| **Inputs** | `.env`, the credential file, the gcloud session, the provisioned resources |
| **Checks** | the required settings, the credential, the CLI and its session, the selected project, deploy permissions, and the existence of the mesh job and exchange bucket |
| **Excludes** | running a mesh, and creating or modifying anything |
| **Output** | every fault named in one run, the exact next command, and a verdict in the exit code (0 / 1 local / 2 remote) |
| **Mutates** | nothing |
| **A failure means** | the stack cannot dispatch; `make dev-up` will refuse for the same reason |

| | |
|---|---|
| **Command** | `cd deploy/gcp && make smoke JOB_ID=<uuid>` |
| **Question** | Does a real submission reach the job and come back? |
| **Inputs** | a job the local application has already prepared |
| **Does** | submits **one real execution** and waits for it. The only script here that mutates anything remote, which is why it refuses to run without a job id stated explicitly |
| **A failure means** | the resources exist but the path through them does not work: read the execution's logs in the console |

A native mesh matrix is **not** a routine post-deployment smoke. If a deployment policy
wants native proof in the target environment, run it as an explicit, controlled canary
job, not on every promotion.

---

## Artifact promotion

There is exactly one image producer, Gate C, and exactly one consumer of what it produced.
`make release-publish` pushes the validated images and records their immutable digests;
`make mesh-deploy` runs `promote-release.sh`, which copies those digests into the deployment env.
Deployment promotes. It does not rebuild, and it never resolves a mutable tag.

Two ways that could silently break, and how Gate D closes both:

* **a dirty tree**, `source_tag()` appends `-dirty`, so deploy would build and promote
  an image Gate C never saw. Gate D fails on a dirty tree.
* **a moved HEAD**: the release record would describe a different commit than the one
  deploy is about to tag. Gate D compares the record's commit to HEAD and fails on a
  mismatch.

The record carries the wheel SHA-256 and the local image IDs, so what was validated can
always be named. `deploy/output/deployment.json`, written after a deploy, records the
resolved registry **digest** of what actually shipped; the two together link
commit → validated artifact → deployed digest.

## What runs where

| Check | A | B | C | publish | D | E |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| ruff | ● | ● | | | | |
| mypy ratchet | | ● | | | | |
| dependency source-of-truth | | ● | | | | |
| hermetic unit suite | partial | ● | | | | |
| browser smoke (`tests/ui`) | | ● | | | | |
| wheel build + inspect + clean install | | | ● | | | |
| import provenance from site-packages | | | ● | | | |
| image builds + entrypoints | | | ● | | | |
| native smoke / all (zero bind mounts) | | | ● | | | |
| **native terminal matrix** | | | **●** | | | |
| push validated bytes, record digest | | | | ● | | |
| artifact identity vs release record | | | | | ● | |
| config, secrets, manifests, migrations | | | | | ● | |
| registry digest resolves / IAM / rollback | | | | | ● | |
| anonymous public smoke | | | | | | ● |
