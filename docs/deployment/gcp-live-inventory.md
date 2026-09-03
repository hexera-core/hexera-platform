# GCP live inventory

A point-in-time record of what actually exists in Google Cloud, read directly from `gcloud`
on **2026-08-30 ~11:30 UTC**. This is an observation, not a specification: where it disagrees
with `deploy/gcp/generated.env` or `deploy/output/deployment.json`, the cloud is right and the
files have drifted.

Authenticated as `pranav@hexera.ai`. No secret values are reproduced here — only the names of
the places secrets live, plus one finding about where they currently live in the clear.

## Organisation and projects

Organisation `hexera.ai` (`896858594150`, customer `C04l53wnx`), billing account
`Hexera Billing` (`01EFBB-8FF368-9E335F`).

| Project | Number | State | What is in it |
| --- | --- | --- | --- |
| `hexera-dev` | 224734058693 | ACTIVE, billing on | **Everything below.** Created 2026-08-29 23:44 UTC. |
| `hexera-prod` | 688073002171 | ACTIVE | Empty. Only the default API set; Compute/Run/SQL not enabled. |
| `hexera-506114` | 737785361719 | ACTIVE | Empty, same as above. |
| `gen-lang-client-0538862942` | 214432523008 | ACTIVE | Empty (Gemini default project). |
| `psyched-choir-507100-d4` | 829638646302 | ACTIVE | Empty. |

Only `hexera-dev` carries workloads. There is no production deployment yet.

Everything runs in **`us-central1`**, zone **`us-central1-a`** where zonal.

## The running system

```
                       internet
                          │
                  ┌───────┴────────┐
                  │  Cloud Run     │  hexera-dev-api   (public, allUsers invoker)
                  │  service       │  min 1 / max 5, concurrency 160
                  └───┬────────┬───┘
       Direct VPC     │        │  RunJobs
       egress         │        │
   ┌──────────────────┴──┐     │
   │ default VPC         │     ▼
   │ 10.128.0.0/20       │   Cloud Run job  dev-mesh  (4 vCPU / 8Gi / 4h, maxRetries 0)
   │                     │     │  SA dev-mesh@hexera-dev
   │  ┌───────────────┐  │     │
   │  │ MIG           │  │     ▼
   │  │ hexera-dev-   │  │   gs://dev-exchange-224734058693   (workspace exchange, 2-day TTL)
   │  │ workers  1..5 │  │
   │  └───┬───────────┘  │
   │      │              │
   │  Cloud SQL PG 16 (private 10.66.0.3 + public 35.225.236.203)
   │  Memorystore Redis 7.0 (10.108.144.235:6379)
   └─────────────────────┘
```

### Cloud Run — API service `hexera-dev-api`

- URLs: `https://hexera-dev-api-s7toqcfd3a-uc.a.run.app` and
  `https://hexera-dev-api-224734058693.us-central1.run.app`. `GET /health` returns
  `{"status":"ok","version":"0.1"}` in ~180 ms.
- Created 2026-08-30 10:04 UTC, generation 1, single revision `hexera-dev-api-00001-tmr` (Ready).
- Image: `us-central1-docker.pkg.dev/hexera-dev/mesh/app@sha256:5a1c68f40387…`, tagged
  `1445b8b71902` — the digest Gate C validated and `release-publish` recorded.
  **Corrected 2026-08-31:** this section previously named the untagged `9175191354ce…`. That was
  wrong. Re-read from `gcloud run services describe` twice since, the service has always run the
  tagged, release-recorded digest. The untagged digest exists in the registry but is not deployed.
- Entrypoint `/srv/entrypoint.sh uvicorn meshpipeline.runtime.api_server:app --host 0.0.0.0 --port 8000`.
- Scaling: `minScale 1`, `maxScale 5` on the revision (the service annotation still says
  `maxScale: 20`), container concurrency 160, startup CPU boost on.
- Networking: Direct VPC egress into `default/default`, `private-ranges-only`, gen2 execution env,
  ingress `all`.
- IAM: `roles/run.invoker` granted to **`allUsers`** — the API is open to the internet.
- ~150 environment variables set inline (agent model/provider/timeout knobs, DB and Redis
  connection settings, MinIO-compatible storage settings, workspace archive limits, viewer
  tuning). Notable values: `ENV=dev`, `DEPLOYMENT_ID=dev`, `CLOUDRUN_JOB=dev-mesh`,
  `GCP_MESH_BUCKET=dev-exchange-224734058693`, `CORS_ORIGINS=*`,
  `ALLOW_PUBLIC_RAW_TRACE=true`, `DATA_COLLECTION_ENABLED=true`.

### Cloud Run — mesh job `dev-mesh`

- Created 2026-08-30 08:49 UTC, generation 2 (last updated 10:18 UTC), Ready.
- Command `python -m meshpipeline.runtime.mesh_runner`.
- Image `…/mesh/mesh@sha256:adec28bf0d86…` (tag `1445b8b71902`).
- `parallelism 1`, `taskCount 1`, `maxRetries 0`, `timeoutSeconds 14400`, 4 vCPU / 8 GiB.
- Runs as `dev-mesh@hexera-dev.iam.gserviceaccount.com`.
- Labels `app=hexera, component=mesh, deployment-id=dev, managed-by=deploy, version=0-0-1`.
- **Zero executions so far.**

### Compute Engine

| Name | Type | Zone | IPs | Notes |
| --- | --- | --- | --- | --- |
| `dev-gatec-builder` | c2-standard-8 | us-central1-a | 10.128.0.2 / 34.41.226.43 | Ubuntu 22.04, tag `hexera-ui`, startup-script metadata. External IP is the reserved static `hexera-dev-console`. Created 2026-08-29 23:56 PDT. |
| `dev-builder-2` | c2-standard-8 | us-central1-a | 10.128.0.3 / 34.170.203.15 | Ubuntu 22.04, no tags, startup-script metadata. Created 2026-08-30 02:47 PDT. |
| `hexera-dev-worker-hxjh` | e2-standard-4 | us-central1-a | 10.128.0.4, no external | MIG member. Metadata carries `worker-image`, `env-uri`, `database-url`, `redis-url`, `startup-script`. |

All three run on the **default compute service account**
(`224734058693-compute@developer.gserviceaccount.com`) with the `cloud-platform` scope, Secure
Boot off, vTPM and integrity monitoring on. Disks: 200 GB pd-balanced for each builder, 100 GB
for the worker.

The two builders are hand-made dev boxes, not part of any managed group or template — nothing
in `deploy/` recreates them.

### Worker fleet (MIG + autoscaler)

- MIG `hexera-dev-workers`, zone `us-central1-a`, base name `hexera-dev-worker`, size 1,
  template `hexera-dev-worker-tpl-1445b8b` (e2-standard-4, 100 GB pd-balanced, Ubuntu 22.04
  LTS family, `automaticRestart`, `onHostMaintenance MIGRATE`, standard provisioning).
- Update policy `REPLACE` / `SUBSTITUTE`, maxSurge 1, maxUnavailable 1, repair on failure. Stable.
- Autoscaler `hexera-dev-workers-9k6e`: min 1, max 5, cooldown 180 s, scaling on the custom
  gauge **`custom.googleapis.com/hexera/queue_depth`** with `utilizationTarget 1.0`.
- The metric descriptor exists and the worker (`instance_id 3984636889477824823`) is publishing
  it against `gce_instance` every ~30 s (current value 0). The autoscaler reported
  `CUSTOM_METRIC_INVALID` shortly after creation and is now **ACTIVE** — the error was the gap
  before the first data point, and it cleared itself.

### Data stores

**Cloud SQL — `hexera-dev-pg`**
- PostgreSQL 16.14, `db-g1-small`, ENTERPRISE edition, ZONAL (no HA), 20 GB PD_SSD with
  auto-resize on. Created 2026-08-30 09:16 UTC. Connection name
  `hexera-dev:us-central1:hexera-dev-pg`.
- Addresses: private `10.66.0.3` (peered `default` network), public `35.225.236.203`,
  outgoing `34.122.232.172`.
- Databases `postgres`, `meshpipeline`. Users `postgres`, `meshpipeline` (both built-in).
- **Backups disabled.** `deletionProtectionEnabled: false`. `requireSsl: false`,
  `sslMode: ALLOW_UNENCRYPTED_AND_ENCRYPTED`, `connectorEnforcement: NOT_REQUIRED`,
  public IP enabled with **no authorized networks configured**.

**Memorystore Redis — `hexera-dev-redis`**
- Redis 7.0, BASIC tier (no replica), 1 GB, `10.108.144.235:6379`, reserved range
  `10.108.144.232/29` on the `default` network via peering `redis-peer-134275458568`.
  Created 2026-08-30 09:13 UTC.

**Cloud Storage** (all US-CENTRAL1, uniform bucket-level access, 7-day soft delete)

| Bucket | Purpose | Config | Contents |
| --- | --- | --- | --- |
| `dev-exchange-224734058693` | workspace exchange between pipeline and mesh job | public access prevention **enforced**, lifecycle: delete at age 2 days, labels `app=hexera/deployment-id=dev/managed-by=deploy/version=0-0-1`; `objectUser` to `dev-mesh` and the default compute SA | empty |
| `dev-transfer-224734058693` | ad-hoc source transfer | PAP inherited, no lifecycle, no versioning; `objectViewer` to default compute SA | `env.txt`, `hexera-clone.tgz`, `hx2.tgz`, `hx3.tgz`, `hx4.tgz` |
| `hexera-dev-artifacts-224734058693` | build artifacts | PAP inherited, no lifecycle; `objectAdmin` + `objectViewer` to default compute SA | `sources/` |

Neither `dev-transfer-…` nor `hexera-dev-artifacts-…` is described by anything in `deploy/`.

### Artifact Registry

Repository `mesh` (DOCKER, standard, us-central1, Google-managed key, **2.4 GB**), created
2026-08-30 01:18 UTC.

| Package | Tag | Digest |
| --- | --- | --- |
| `mesh/app` | `1445b8b71902` | `sha256:5a1c68f40387…` |
| `mesh/app` | `a368459179ea` | `sha256:7a7f50a9c074…` |
| `mesh/app` | — | `0fa0b0fd9f27…`, `31ad55a11e51…`, `3fbb21e92e83…`, `9175191354ce…` (**deployed**) |
| `mesh/mesh` | `1445b8b71902` | `sha256:adec28bf0d86…` (**deployed to the job**) |
| `mesh/mesh` | `a368459179ea` | `sha256:e6f8b3f024f6…` |
| `mesh/mesh` | — | `1d85ff17a003…`, `851f8078ed1d…`, `a7efcaf033da…` |

Six untagged digests accumulated in one day. A cleanup policy now keeps tagged images and deletes
untagged ones after seven days.

### Networking

- Single VPC `default` (auto mode, regional routing). Subnet `default` in us-central1 is
  `10.128.0.0/20`.
- Reserved addresses: `hexera-dev-console` `34.41.226.43` (EXTERNAL, in use by
  `dev-gatec-builder`), `nat-auto-ip-…` `34.61.46.14`, `google-managed-services-default`
  `10.66.0.0/16` (VPC peering range for Cloud SQL), `serverless-ipv4-…` `10.128.0.16/28`
  (Cloud Run Direct VPC egress).
- Cloud NAT `hexera-dev-nat` on router `hexera-dev-router`, auto IP allocation, all subnets /
  all ranges — this is how the private worker reaches the internet.
- Peerings: `redis-peer-134275458568` and `servicenetworking-googleapis-com`, both ACTIVE.
- Firewall (all on `default`, all ingress, none disabled):

| Rule | Source | Allow | Targets |
| --- | --- | --- | --- |
| `allow-hexera-web` | 0.0.0.0/0 | tcp:80, tcp:443 | tag `hexera-ui` |
| `default-allow-ssh` | 0.0.0.0/0 | tcp:22 | all |
| `default-allow-rdp` | 0.0.0.0/0 | tcp:3389 | all |
| `default-allow-icmp` | 0.0.0.0/0 | icmp | all |
| `default-allow-internal` | 10.128.0.0/9 | tcp/udp 0-65535, icmp | all |

### Identity

Service accounts in the project:
- `dev-mesh@hexera-dev.iam.gserviceaccount.com` — "Hexera mesh runner (compute-only)". Holds
  **no project-level roles at all**; its only grant is `roles/storage.objectUser` on the
  exchange bucket. Empty IAM policy of its own (no impersonators).
- `224734058693-compute@developer.gserviceaccount.com` — default compute SA. Project-level it
  holds only `roles/monitoring.metricWriter` (the usual `roles/editor` has been removed).
  Bucket-level it has `objectUser` on exchange, `objectViewer` on transfer, `objectAdmin` on
  artifacts. Attached to all three VMs and the worker template with `cloud-platform` scope.

Project IAM otherwise: `user:pranav@hexera.ai` as `roles/owner`,
`224734058693@cloudservices.gserviceaccount.com` as `roles/editor`, plus the standard Google
service agents (artifactregistry, compute, container, containerregistry, file, pubsub, redis,
run, servicenetworking).

### Observability

- Only the default log sinks `_Required` and `_Default`. No log-based metrics, no exported sinks.
- One custom metric descriptor, `custom.googleapis.com/hexera/queue_depth` (GAUGE, DOUBLE),
  written by `deploy/gcp/worker/queue_depth_exporter.py` on the worker.
- **No alert policies. No uptime checks.** Cloud Trace and Monitoring APIs are enabled but
  nothing is configured on top of them.

### Enabled APIs (44 in `hexera-dev`)

Artifact Registry, Autoscaling, BigQuery (+ 7 related), Cloud APIs, Cloud DNS, Cloud Filestore,
Cloud Resource Manager, Cloud Run Admin, Cloud SQL (+ Admin), Cloud Storage (3 endpoints),
Cloud Trace, Compute Engine, Container File System, Container Registry, Dataform, Dataplex,
Datastore, Deployment Manager, GKE + Backup for GKE, IAM (+ Credentials), Logging, Monitoring,
Network Connectivity, Org Policy, OS Login, Pub/Sub, Memorystore for Redis, Secret Manager,
Service Management / Networking / Usage, Telemetry.

Several of these are enabled but unused: **no Pub/Sub topics or subscriptions, no GKE clusters,
no Filestore instances, no Secret Manager secrets, no DNS zones.**

## Drift against the repo's recorded state

1. **`deploy/output/deployment.json` names a stale mesh image.** It records
   `mesh@sha256:e6f8b3f024f6…` (tag `a368459179ea`); the live `dev-mesh` job runs
   `sha256:adec28bf0d86…` (tag `1445b8b71902`). `deploy/gcp/generated.env` has the same stale
   `MESH_IMAGE`.
2. **The API service is not in either state file.** `deployment.json` lists only the artifact
   registry, the mesh job, the exchange bucket and the mesh SA. `hexera-dev-api`, the MIG, the
   autoscaler, Cloud SQL, Redis, the router/NAT and two of the three buckets were created
   outside that record.
3. ~~The deployed API image is untagged.~~ **Withdrawn 2026-08-31** — it is
   `sha256:5a1c68f40387…`, tagged `1445b8b71902` and recorded in `release.json`. The original
   reading was a mistake, and it mattered: the Artifact Registry cleanup policy deletes untagged
   digests, so an untagged running image would have expired underneath the service. It is tagged,
   so the policy keeps it.
4. **`release.json` describes commit `a368459179ea` as published**, but the images actually
   running come from the later `1445b8b71902` build.

## Things worth fixing

Ordered by how much they would hurt.

- **Provider API keys sit in plaintext Cloud Run environment variables.** `DEEPINFRA_API_KEY`
  and `DEEPSEEK_API_KEY` are literal values in the service spec, readable by anyone with
  `run.services.get`, and they appear in `gcloud` output and deployment logs. Secret Manager is
  enabled and holds zero secrets. These keys should be rotated and moved to secret references.
- **`POSTGRES_PASSWORD` is likewise an inline env var** on the same service.
- **Cloud SQL has backups disabled and deletion protection off**, on the instance holding
  `meshpipeline`. One `gcloud sql instances delete` away from total loss, with no PITR.
- **Cloud SQL accepts unencrypted public-IP connections** (`sslMode:
  ALLOW_UNENCRYPTED_AND_ENCRYPTED`, `requireSsl: false`) with public IP on and no authorized
  networks. Either drop the public IP or require SSL and restrict the network list.
- **The API is `allUsers`-invokable with `CORS_ORIGINS=*` and `ALLOW_PUBLIC_RAW_TRACE=true`.**
  Intentional for a dev sandbox, but it is a public endpoint that spends money on LLM calls.
- **`default-allow-ssh` and `default-allow-rdp` are open to 0.0.0.0/0** on every instance in
  the project. Nothing here needs RDP.
- **All VMs run as the default compute SA with `cloud-platform` scope.** The scope is wide even
  though the project-level roles have been trimmed; a dedicated worker SA would match what
  `dev-mesh` already does.
- **No alerting of any kind.** No uptime check on the public API, no alert on job failures,
  Cloud SQL disk, or the queue-depth metric the autoscaler depends on.
- **Two untracked builder VMs** (`dev-gatec-builder`, `dev-builder-2`, c2-standard-8 each) are
  running continuously and are not reproducible from `deploy/`. They are the largest steady
  cost in the project.
- ~~No Artifact Registry cleanup policy.~~ **Resolved 2026-08-31**: tagged images are kept,
  untagged are deleted after seven days.
- **`dev-transfer-…` holds source tarballs** (`hexera-clone.tgz`, `hx2-4.tgz`) and `env.txt`
  with no lifecycle rule and public access prevention merely "inherited".

## How this was collected

`gcloud projects list`, `services list`, `run services/jobs/revisions describe`,
`compute instances/instance-groups/instance-templates/disks/addresses/networks/subnets/
firewall-rules/routers/nats/peerings list`, `sql instances/databases/users`,
`redis instances list`, `storage buckets describe|get-iam-policy`,
`artifacts repositories/docker images list`, `iam service-accounts list`,
`projects get-iam-policy`, `secrets list`, `logging sinks/metrics list`, `billing`, plus the
Monitoring v3 REST API for `metricDescriptors`, `timeSeries`, `alertPolicies` and
`uptimeCheckConfigs` (the `gcloud alpha monitoring` component is not installed locally).

Re-run any of these to refresh a section; the numbers above are only true for
2026-08-30 ~11:30 UTC.
