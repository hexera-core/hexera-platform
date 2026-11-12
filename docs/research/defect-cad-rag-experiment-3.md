# Experiment 3: the shadow posture on a deployed personal environment

**Date:** 2026-11-05 · **Env:** `dev-rag-*` (hexera-dev, us-central1) · **Branch:**
`defect-cad-rag` (PR #422, draft) · **Follows:** experiments 1–2.

v1 of the track: `CAD_REPAIR_RAG=shadow` running where the product runs — the real API image,
Cloud SQL schema, worker fleet — checked against the spec's acceptance items. Shadow only:
geometry and planner decisions unchanged by construction, and tested.

## Deploys

| run | tiers | result |
|---|---|---|
| 42154443202 | app+data+fleet, `cad_repair=true`, `cad_repair_review=false`, `cad_repair_rag=shadow` | **failed at stage 20/21**: the branch's merge of origin/main had committed conflict markers in `create-worker-fleet.sh` (`syntax error near unexpected token '<<<'`). Everything before the fleet was created. |
| 42161421801 | console+fleet (launched before reading run 1's conclusion — a sequencing mistake) | cancelled |
| 42161630592 | app+data+fleet, same flags, on the fixed commit | success |
| 42163065023 | console+fleet, flags restated (they are declarative) | success |

Lessons that cost time: a `head`-truncated `git status` hid the third conflicted file, and
`git add -A` staged its markers — `bash -n` on every touched script is now part of the loop; and
never launch run 2 in the same command that reads run 1's conclusion.

## Acceptance items

### 1. The flag reached the deployment

`gcloud run services describe dev-rag-api` shows
`CAD_REPAIR_ENABLED=true … CAD_REPAIR_MAX_OPS=12 CAD_REPAIR_TIMEOUT_S=420 CAD_REPAIR_RAG=shadow`,
and the worker template carries `cad-repair-rag=shadow` metadata (startup.sh maps it into
`worker.env`).

### 2. Environment smoke

`devtools/validation/smoke_hosted_repair.sh rag`: readyz 200 with postgres/redis/object_store ok,
migrations applied. The operator-queue checks SKIP (ADMIN_API_KEY is deliberately not given to
personal environments — `deploy.yml` sets `ADMIN_API_KEY_SECRET` only for an empty slug), so
shadow evidence is read from the environment's own database instead. Console 404 until run
42163065023 landed it.

### 3. pgvector probe (spec phase-0 acceptance)

On `meshpipeline_rag` (the env's own database on shared `hexera-dev-pg`), via cloud-sql-proxy:

- `pg_available_extensions` lists `vector` 0.8.5; `CREATE EXTENSION IF NOT EXISTS vector`
  succeeded; `pg_extension` confirms 0.8.5 installed.
- Loaded the shipped snapshot's 1 765 × 65-dim vectors into a `lab_fix_memory` table
  (COPY 0.45 s, 1 176 kB total relation).
- Exact top-10 with code prefilter, 100 queries: **p50 56 ms, p90 70 ms, max 109 ms per query;
  1.34 s for a 24-defect file** — measured from this laptop through the proxy, so it is mostly
  round-trip time, an upper bound on in-VPC latency.
- The table was dropped after the probe.

Reading: even the upper bound is workable for batch-per-file retrieval, but the in-process
snapshot (0.4–3.6 ms per defect, no network) remains the right store for shadow at this corpus
size, exactly as the spec's store choice says. pgvector is proven available for the
durable-shared-access step.

### 4. Shadow logging on a real upload

Upload path: `POST /api/v1/upload/step-file` with `X-API-Key` (mesh-api-key) and the signed
user headers. Personal envs mint accounts through Firebase sign-in, which a curl probe does not
have, so a `users` row, an organisation, a membership and a 1000-credit grant were seeded
directly in the env's own database (the spend gate otherwise refuses dispatch as a 429
`quota_exceeded` — the first two upload sessions spent an hour looking like rate limiting).
Uploaded `bug24983_111FACE.stp` (OCCT; locally produced three shadow rows in experiment 2's
smoke), drove the intake conversation to dispatch.

**What it took to get a worker to run the job — four environment defects, in order found:**

1. **Transfer-bucket read is granted by nobody.** Every `dev-rag-workers` instance booted, failed
   `gcloud storage cp .../worker.env` with 403 and sat RUNNING while consuming nothing. On
   projects whose default compute account still holds legacy Editor the read works by accident.
   Hand-granted `roles/storage.objectViewer`, and the grant is now in `create-worker-fleet.sh`
   beside the mesh-bucket grant (commit on this branch).
2. **`worker.env` ships `.env.example`'s `POSTGRES_HOST=localhost`.** The fleet metadata
   deliberately carries no database endpoint (a DSN would put the password in metadata), and
   nothing patches the published env object for a new slug - so the first two dispatched jobs
   failed inside the worker with `Connect call failed 127.0.0.1:5432`, were dead-lettered to
   `dev-rag:simulation:dlq`, and their rows sat `pending` forever. Found via Redis (`docker exec`
   on the worker): `_celery-task-meta-*` carried the OSError. Fixed operationally by editing the
   env's `worker.env` object (POSTGRES_HOST=10.66.0.3, POSTGRES_DB=meshpipeline_rag); the
   documented flow needs this step written down or automated.
3. **A `pending` job is invisible to recovery.** The dead-lettered jobs never set `started_at`,
   so `reap_stalled_jobs` reaped 0; the queue-depth metric read 0; the fleet self-retired. A job
   can be in the DLQ with its DB row `pending` and nothing will ever touch it again.
4. **Shadow evidence needs the capture opt-in.** `rag_shadow` reaches durable rows through
   `TrainingLogger` → `capture_operations`, and `DATA_COLLECTION_ENABLED` defaults to false
   (GDPR opt-in) — so a personal env records no shadow evidence until the flag is set in its
   `worker.env`. Set for dev-rag; a shadow phase rollout must state this beside the RAG flag.
5. **The snapshot was not in the wheel.** With a healthy worker and database, job 3
   (`82e3c05d`) ran the whole pipeline to `succeeded`, the repair loop made 4 attempts, kept 0,
   rolled back exactly as the rag-off baseline would - and logged
   `fix memory unavailable ... site-packages/.../data/fixmemory-hand-v2.json.gz: No such file`:
   `pip install` ships only what `package-data` names. **The fail-open contract did its job in
   production** - shadow disabled itself with one warning and cost the run nothing - but on an
   input that should exist. Fixed in `pyproject.toml` (wheel member verified); redeploy and the
   repeat upload follow.

### 5. Geometry untouched

Three deployed runs of the same upload, all `succeeded`, all with the repair loop at
`stopped=stalled attempts=4 kept=0`, "Nothing could be repaired safely; meshing your original
file":

| job | shadow state |
|---|---|
| `82e3c05d` | flag on, snapshot missing — shadow inert (the de-facto off baseline) |
| `d28d3f0d`'s job | flag on, snapshot packaged, capture off |
| `a012191d` | flag on, snapshot packaged, capture on |

Identical loop outcome across all three and against experiment 2's local baseline for this file:
**shadow changes nothing the kernel or the caps see.**

## The deployed shadow rows (job `a012191d`)

From `capture_operations` (`name='repair_attempt'`), the fix memory's predictions beside the
session's verdicts, recorded by the real worker in the real pipeline:

| op | defect | P(kept) | verdict | predict_s |
|---|---|---:|---|---:|
| sew_faces | open_shell `edge:fceb8170…` | 0.052 | rolled_back | 0.0029 |
| fill_planar_hole | open_shell `edge:fceb8170…` | 0.069 | rolled_back | 0.0009 |
| fill_nonplanar_hole | open_shell `edge:fceb8170…` | 0.002 | rolled_back | 0.0007 |
| sew_faces | open_shell `edge:3a7bd90c…` | 0.057 | rolled_back | 0.0010 |

Every attempt the memory called near-hopeless (P ≤ 0.07) was indeed rolled back; prediction cost
0.7–2.9 ms per attempt against a 420 s repair budget; fingerprints match the local run of the
same file. This is the per-attempt evidence the experiment-2 skip policy would act on: at the
offline threshold all four attempts would have been skipped, saving the attempt time at zero
kept-repair cost on this file.

## Verdict against the acceptance items

| item | state |
|---|---|
| flag reaches API + fleet | pass |
| env smoke | pass (admin surface deliberately absent on personal envs) |
| pgvector probe + latency | pass (0.8.5 installed; exact top-k p50 56 ms from the laptop, upper bound) |
| shadow rows on a real upload | pass (four rows, above) |
| geometry untouched | pass (three-way identical loop outcome + off-is-off unit test) |

## What this cost, and what to fix before anyone repeats it

Five environment defects stood between "deployed successfully" and one shadow row; each is listed
above with its fix or workaround. The fleet-script IAM grant and the wheel packaging are fixed on
this branch; the worker.env database endpoint, the pending-DLQ blind spot and the capture opt-in
need their own issues. The env (`dev-rag-*`) stays up for review and is destroyed with
`make destroy-env SLUG=rag` when this branch merges or is abandoned; the probe user, organisation
and credits live only in `meshpipeline_rag`, which dies with it.
