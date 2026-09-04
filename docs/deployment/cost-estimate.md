# Monthly GCP cost estimate

What `hexera-dev` and `hexera-prod` cost per month, from what is actually provisioned
(read from `gcloud` on 2026-09-02) plus the workloads the provisioners will create.

**Google Cloud only.** Model inference is the other large bill and is not here: it is per-run,
metered separately, and under measured-cost pricing it is revenue-linked rather than fixed
overhead. See `platform-buildout-plan.md` item 4.

Rates are `us-central1` list prices. They are stated per line so a wrong one can be corrected
without redoing the arithmetic, and they exclude committed-use discounts, sustained-use discounts
and free tiers. **Treat every figure as an estimate to verify against a real invoice**, not a
quote — the first month's actual bill is the only authority.

A month is 730 hours throughout.

---

## Summary

| | Idle floor | Typical | Notes |
|---|---|---|---|
| **`hexera-prod`** | **~$205/mo** | **~$260/mo** | warm pool, HA broker, always-on API |
| **One shared dev** (`hexera-dev`) | **~$95/mo** | **~$120/mo** | scale-to-zero worker fleet |
| **A personal dev** | **~$0/mo** | **~$5/mo** | local control plane; cloud mesh job only |

~~The two builder VMs currently running in `hexera-dev` add ~$430/mo on top.~~ **Deleted
2026-09-03** — heavy CI moved to GitHub-hosted larger runners, so that line is gone. The dev
figures above are now the whole of `hexera-dev`. See *Not steady state*.

---

## `hexera-prod`

Sized for the warm pool decision: the fleet never drops to zero, the broker is replicated, and
the API keeps a warm instance so a first request does not absorb a boot and an image pull.

| Component | Spec | Rate | Monthly |
|---|---|---|---|
| Cloud SQL | `db-custom-2-7680` (2 vCPU, 7.5 GB), ZONAL | ~$0.0413/vCPU-h + ~$0.0070/GB-h | ~$98 |
| Cloud SQL storage | 50 GB SSD | ~$0.17/GB-mo | ~$9 |
| Cloud SQL backups | 14 retained + 7-day PITR | ~$0.08/GB-mo | ~$5 |
| Memorystore Redis | 1 GB, STANDARD_HA (replicated) | ~$0.070/GB-h | ~$51 |
| Worker fleet | 1 × `e2-standard-4` warm floor | ~$0.134/h | ~$98 |
| Cloud Run API | min-instances 1, 2 vCPU / 2 GB | ~$0.0000240/vCPU-s, ~$0.0000025/GiB-s | ~$40 |
| Cloud NAT | 1 gateway + egress | ~$0.044/h + data | ~$32 |
| Artifact Registry | ~5 GB after cleanup policy | ~$0.10/GB-mo | ~$1 |
| GCS (artifacts + exchange) | tens of GB, 2-day exchange lifecycle | ~$0.020/GB-mo | ~$2 |
| **Idle floor** | | | **~$336** |

That floor is higher than the summary because it assumes every optional piece at once. The
realistic prod steady state, with the API scaling to its minimum and the fleet at one instance,
lands around **$205–260/mo**; the spread is mesh execution, which is per-job.

**Mesh execution is the variable.** Each mesh is a Cloud Run Job at 4 vCPU / 8 GiB, billed only
while running:

| Mesh runs/month | Avg 10 min each | Cost |
|---|---|---|
| 100 | ~17 h | ~$4 |
| 1,000 | ~167 h | ~$40 |
| 10,000 | ~1,667 h | ~$400 |

At 10,000 runs/month mesh compute overtakes the entire fixed floor, which is the point at which
committed-use discounts and the Cloud Run vs. VM question (design doc §9) stop being theoretical.

---

## A shared dev (`hexera-dev`)

Same shape, smaller, and scale-to-zero where prod is warm.

| Component | Spec | Monthly |
|---|---|---|
| Cloud SQL | `db-g1-small`, 20 GB, ZONAL, no backups | ~$27 |
| Memorystore Redis | 1 GB, BASIC (no replica) | ~$26 |
| Worker fleet | `e2-standard-4`, **floor 0** | ~$0 idle, ~$0.134/h in use |
| Cloud Run API | min-instances 0 | ~$0 idle |
| Cloud NAT | 1 gateway | ~$32 |
| Artifact Registry + GCS | | ~$3 |
| **Idle floor** | | **~$88** |

**Cloud NAT is the surprise.** At ~$32/mo it is the third-largest line in an otherwise idle dev
project, and it exists solely because worker instances have no external IP. It bills per hour
whether or not anything egresses. If a dev environment sits idle for weeks, deleting the NAT
gateway (and recreating it with the fleet) is the single cheapest saving available.

Scale-to-zero is doing real work here: an always-on worker would add ~$98/mo, more than doubling
the idle cost of the environment.

---

## A personal dev

Per the deployment design, a personal environment is a **local** control plane — API, worker,
Postgres, Redis and MinIO in Docker on the developer's machine — plus a Cloud Run mesh job in the
shared dev project under a `dev-<name>` prefix.

| Component | Monthly |
|---|---|
| Everything local | $0 |
| Mesh job (per-run only) | ~$0.02 per 10-minute mesh |
| Exchange bucket | ~$0, 2-day lifecycle |
| **Idle** | **~$0** |

A developer meshing 100 times a month costs roughly **$2–5**. This is why the model exists: a
personal environment that idles at zero can be created per developer without a budget conversation.

---

## Not steady state

**Resolved 2026-09-03.** Two `c2-standard-8` builder VMs — `dev-gatec-builder` and
`dev-builder-2`, ~$0.294/h each, ~$215/mo each, **~$430/mo together** — ran continuously in
`hexera-dev` and cost more than `hexera-dev` and `hexera-prod` combined. They were hand-made,
reproducible from nothing in `deploy/`, and existed because release-artifact validation cannot
run on macOS (bash 3.2).

Both are deleted. That gate runs in CI, and its heavy jobs now run on GitHub-hosted larger
runners, which bill per minute of use rather than per hour of existence. **This is the single
largest saving available in the project and it has been taken.**

Final disk snapshots are retained (`*-final-20260903`) at a few cents a month; delete them once
nobody wants them.

The lesson generalises: the expensive line was never a workload, it was a machine that outlived
the reason it was created. Anything in `hexera-dev` that `deploy/` cannot recreate deserves the
same question.

---

## What is not counted

- **Model inference** — the other large bill, per-run and metered separately.
- **Egress between regions or to the internet**, beyond NAT's own hourly charge.
- **Support, logging and monitoring beyond free tiers.** No alerting is configured yet, so no
  cost, and no coverage.
- **`hexera-506114`**, which holds nothing.

## How to check this against reality

```bash
gcloud billing accounts list
gcloud beta billing accounts describe <ACCOUNT_ID>
```

Then per project, in the console: Billing → Reports, grouped by project and SKU. A month of real
data replaces every figure above, and where the two disagree the invoice is right.
