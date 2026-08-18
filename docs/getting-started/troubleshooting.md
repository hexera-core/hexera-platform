# Troubleshooting the local stack with a Cloud Run mesh

Every symptom here is one you can hit on a fresh clone. Each entry says what you would see, what it
means, and the one thing to do about it.

## The mesh submission contract: first

Most confusing symptoms are this contract working as designed, so it is worth reading before the
table.

Meshing runs on the configured Cloud Run job; everything else runs on your machine. Before a run is
sent, the worker claims an **operation identity** derived from the job id and the run's execution
generation. Only the worker that wins that claim uploads the input and triggers Cloud Run, and the
claim is what makes the submission **at most once**:

- The trigger returns a long-running **Operation**, whose name is stored as the claim's evidence and
  marks it `accepted`.
- If the same generation runs the node again, a worker died, a task was redelivered: it finds the
  accepted claim and reads the result from the **same** object keys instead of starting a second
  mesh. It does not trigger Cloud Run twice.
- If the outcome is not known, the request may have reached Cloud Run and the answer did not come
  back: the claim is `indeterminate` and the run **fails safely**. It is never retried
  automatically, because a mesh is expensive and an automatic retry is exactly how you pay for the
  same one twice.
- A genuinely **new execution generation** is a new identity, and may submit once of its own.

The exchange objects live in `GCP_MESH_BUCKET` under
`jobs/<operation key>/{input.tar.gz,output.tar.gz,result.json}`. The keys are derived from the
operation identity, which is why a replacement worker can find its predecessor's objects.

## Symptoms

| You see | It means | Do this |
|---|---|---|
| `[CLOUD_RUN_FAILED] <engine>: the submission may have been accepted and its acknowledgement was lost` | The request left the machine and no answer arrived. The claim is `indeterminate`. A mesh may be running and billing right now. | Look at the Cloud Run job's executions in the Google Cloud console. If one is running, let it finish. Start a **new run** when you want to try again: the same generation will not resubmit, by design. |
| `[CLOUD_RUN_FAILED] <engine>: an earlier invocation holds this operation and its outcome is not durably known` | A previous worker claimed this operation and died before recording what happened. | Same as above. This is the safe answer, not a bug: the system will not guess that nothing was submitted. |
| `[CLOUD_RUN_FAILED] <engine>: no execution ownership is bound` | A run reached the mesh executor outside a claimed execution. Nothing was submitted. | This is a defect, not a configuration problem. Capture the worker log and open an issue. |
| `ConfigurationError: CLOUDRUN_JOB` (or `GCP_PROJECT_ID`, `GCP_MESH_BUCKET`) | The cloud mesh executor is not configured. Nothing was submitted and no local mesh was started. | You do not have to find these by hand: `make mesh-adopt` reads them off the executor your gcloud session can see and writes them into `.env`, filling only what is empty. Then `make dev-down && make dev-up`. `make mesh-doctor` checks all of them read-only. |
| `make mesh-adopt` says it cannot identify the exchange bucket | Two or more buckets in the job's region are equally consistent with the evidence, and it will not guess: pointing the stack at the wrong bucket produces jobs that fail after dispatch. | Take the name from `deploy/output/deployment.json` or the project administrator and set `GCP_MESH_BUCKET` yourself. [setup.md, Path A](setup.md#path-a-an-executor-already-exists) explains how to tell the candidates apart. |
| `cloud-platform scope is required but not consented`, after `gcloud auth application-default login` | You pressed **Continue** on the "Select what Google Auth Library can access" page without ticking the checkboxes. Those boxes are the consent. | Run the command again and tick **Select all** before Continue. `--no-browser`, which the error above it suggests, does not help: the unticked box is the fault. |
| HTTP 401/403 from Cloud Run, or `could not obtain an access token` | Application Default Credentials are missing or cannot reach the project. | Run `gcloud auth application-default login`, then **copy** the file it names: `install -D -m 600 "$HOME/.config/gcloud/application_default_credentials.json" secrets/gcp/application_default_credentials.json`. The login alone is never enough, and moving it breaks every other gcloud tool. `make setup` never signs you in. Confirm with `make mesh-doctor`. |
| HTTP 404 from Cloud Run | `CLOUDRUN_JOB` or `GCP_REGION` does not name a job that exists in `GCP_PROJECT_ID`. | Check the three values agree with the console. `make mesh-deploy` provisions the job if it was never created. |
| The job finishes but no result appears | The mesh job ran and wrote nothing to the result key, or wrote it somewhere else. | Check the Cloud Run execution's logs, then look for `jobs/<operation key>/result.json` in `GCP_MESH_BUCKET`. A missing result object is **not** evidence that nothing ran. |
| Start-up exits with `... is stamped with a revision this build ships, but N table(s) it defines are absent` | The database was created from an earlier form of the single baseline revision, so no migration step remains to add the missing objects. | `make dev-reset` (or `docker compose down -v`) and start again. Do not create the tables by hand. |
| `make mesh-doctor` exits 1 | A prerequisite on this machine is missing: it lists each one. | Fix everything it names, then run it again. It reports all faults per run, so one pass is enough. |
| `make mesh-doctor` exits 2 | This machine is configured, but the gcloud session or a cloud resource is not usable. | Re-authenticate, or create the missing job or bucket with `make mesh-deploy`. |
| Start-up exits with `already contains Hexera objects ... but has no alembic_version table` | The database has our objects but nothing describes which revision they match. | Point at an empty database, or adopt this one deliberately after verifying it. Do not stamp a revision you have not checked. |

## Checks worth running first

```bash
curl localhost:8000/readyz   # PostgreSQL, Redis and the object store
make mesh-doctor             # read-only: every prerequisite, in one run; exit 0 only when all pass
docker compose ps            # is every service actually up?
docker compose logs worker   # where a submission failure is reported
```

## Starting over

```bash
make dev-reset      # empty the local databases, keep the machine set up
make dev-uninstall  # remove this project's images, .env and .venv as well
make mesh-destroy   # dry run of the cloud half; add DESTROY_ARGS=--apply to carry it out
```

All are safe to run twice. The local ones never reach outside this checkout's Compose project, and
`mesh-destroy` deletes only what the deployment record says `mesh-setup` created, a job or bucket
you supplied yourself is recorded as reused and survives.

## What never appears in a failure message

Failures name the engine and the reason. They deliberately do not carry credentials, authorization
headers, the provider's response body, bucket names, object keys or internal paths, so a log or a
screenshot is safe to share. If you ever see one of those in user-facing output, treat it as a
security defect and report it.
