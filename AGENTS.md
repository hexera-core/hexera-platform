# Agent rules

Rules for any coding agent working in this repository. Human contributors may read them as
guidance; for an agent they are the default and a departure needs a reason stated in the change.

## The default loop is a dev deploy, not a local stack

This repository has a documented per-developer cloud environment: your own API, console, mesh job
and worker fleet inside `hexera-dev`, named `dev-<slug>-*`, created by the first deploy that names
the slug. See [personal environments](docs/deployment/personal-environments.md) for what you get,
what you share and what it costs.

**Use one.** `make dev-up` (docker-compose plus a Cloud Run mesh job) still works and is still
documented in [development overview](docs/development/overview.md), but it is no longer where an
agent should spend its time. A personal environment runs the real API image, the real Cloud SQL
schema, the real worker fleet and the real mesh job; a compose stack runs a locally built image
against local Postgres, Redis and MinIO, so every failure it reproduces still has to be re-proved
somewhere real. Cloud spend is not the constraint here — an agent's wall-clock and a reviewer's
trust in the result are.

### What stays on the host

Fast, hermetic, no infrastructure. Run these locally, every time, before deploying anything:

```bash
make lint
make typecheck
make test-fast
make check-fast     # lint + import sweep + graph wiring + configuration certification
```

If one of these fails, fix it on the host. Deploying to find out that ruff is unhappy wastes
twenty minutes for an answer that took two seconds.

### What goes to your environment

Anything that needs Postgres, Redis, the object store, a real image, a worker, or a mesh: the
integration and container tiers, end-to-end pipeline runs, console and API behaviour, worker
autoscaling, migrations, and anything whose failure you cannot explain from unit tests. Prefer a
redeploy over an hour of local reconstruction.

## Deploying

```bash
# first deploy of a NEW slug is two runs - see personal-environments.md §4
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=<slug> -f app=true -f data=true -f fleet=true
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=<slug> -f console=true -f fleet=true

# every deploy after that
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=<slug> -f app=true -f console=true

gh run watch                                          # follow it
make destroy-env SLUG=<slug>                          # when the branch is done
```

Reaching what you deployed:

```bash
gcloud run services describe dev-<slug>-api --region us-central1 --format='value(status.url)'
gcloud logging read 'resource.labels.service_name="dev-<slug>-api"' --limit 50 --freshness 10m
```

Every stage is idempotent, so a failed run is rerun rather than unwound.

## What an agent may deploy, and what it may not

| Target | Reached by | Agent |
|---|---|---|
| a personal environment | `-f slug=<slug>` | **yes** — deploy, redeploy and destroy freely, without asking |
| shared dev | an EMPTY slug, or a merge to `main` | no — propose the command, let a human run it |
| production | a `v*` tag, after approval | no, and the workflow will not accept one from a branch anyway |

Deploy only to a slug the task owns. Never guess somebody else's slug, and never run
`make destroy-env` against one you did not create: it deletes a database with no undelete window.

`outreach` is never ticked on a personal run — the sender can email real people.

## Cost hygiene

Redeploying is cheap and encouraged; leaving resources standing is what costs. So:

- `data` and `fleet` are for the first deploy of an environment. Every run after that is
  `app`/`console` unless the schema or the fleet actually changed.
- `make destroy-env SLUG=<slug>` when the branch merges or is abandoned. Read what it reports as
  surviving — it deletes only what carries your `deployment-id` label.
- One environment per developer, not one per branch. Redeploy the same slug from a new branch.

## Verification

Claim a thing works only after running the command that proves it, and say which command that was.
"Deployed successfully" is a statement about the workflow, not about the change: name the request
you made, the log line you read, or the tier that passed. A green deploy with an unexercised code
path is an untested change.

## Change size

Prefer small, reviewable changes. Explain verification clearly.
