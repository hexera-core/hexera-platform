# Admin console: fleet management, cloud cost and customer billing — design

Date: 2026-09-10
Status: approved
Scope: one sub-project, **Admin-2 (revised)** — three pages (Fleet, Costs, Billing) and the
deploy-script ownership change that makes the fleet controls durable.

## 1. Why this exists, and what changed

`apps/admin-console` ships one real page. Fleet state is read from the Cloud console, spend from the
billing page, and every scaling change is a `gcloud` invocation or an edit to a generated env file
followed by a full deploy. There is no view of how many workers ran yesterday, and no way to raise
the warm-pool floor before a demo without redeploying the fleet tier.

This design supersedes [`2026-09-08-admin-console-design.md`](2026-09-08-admin-console-design.md)
§2 and §5. That document made **"writing to infrastructure from the console"** an explicit
non-goal: *"It reads fleet state; it does not scale, drain or restart anything."* That non-goal is
retired. The console now owns the elastic scaling knobs outright, and the deploy scripts stop
reconciling them.

Retiring it is the decision with consequences, so it is stated first and its costs are stated in
§7 and §8 rather than left implicit: the admin service account gains mutate authority on the
fleet, and an IAP compromise becomes an infrastructure compromise.

### Verified starting facts

Read from this repository and from the npm registry on 2026-09-10, not from documentation.

| Fact | Evidence |
|---|---|
| The fleet is a **zonal GCE managed instance group**, not Cloud Run | `create-worker-fleet.sh` — `compute instance-groups managed create`, `WORKER_MIG_ZONE` required |
| Its autoscaler is reconciled on **every deploy run** | `create-worker-fleet.sh` step 6 calls `set-autoscaling` unconditionally |
| The knobs are `WORKER_MIG_MIN_REPLICAS`, `_MAX_REPLICAS`, `_COOLDOWN_SECONDS`, `WORKER_JOBS_PER_INSTANCE` | ibid., defaults `0`, `5`, `180`, `1` |
| `scaleInControl` is **not set at all** on the live autoscaler | no `--scale-in-control` flag anywhere in `deploy/gcp/scripts/` |
| Scaling is driven by one custom gauge, whose resource labels are known exactly | ibid. — `custom.googleapis.com/hexera/queue_depth`, `generic_task` with `location`/`namespace`/`job`/`task_id` |
| Cloud Run `--min-instances` is likewise re-applied on every deploy, for all three services | `create-api-service.sh:325`, `create-console-service.sh:233`, `create-admin-service.sh:161` |
| The admin service holds **no project-level IAM role today** | `apply-iam.sh` grants it nothing; `create-admin-service.sh` grants only the IAP agent `run.invoker` |
| The admin service has **no VPC egress and no database connection** | `create-admin-service.sh` header: *"WHY NO VPC EGRESS, NO DATABASE CONNECTION"* |
| Google's APIs are reachable without a VPC connector | Cloud Run default egress routes public internet; the Google API endpoints are public |
| The admin console has three runtime dependencies | `apps/admin-console/package.json` — `@hexera/api-client`, `next`, `react`/`react-dom` |
| There is no `users`, `organizations` or `credit_ledger` table, and `PLANS` is empty | `persistence/models.py`; `settings/plans.py:32` |
| Drain-before-delete does not exist | `platform-buildout-plan.md` item 6 lists it as unbuilt; `create-worker-fleet.sh` step 5 names it as the reason `proactive` rolling is risky |
| The Google client libraries are pure JS and lighter than assumed | installed 2026-09-10: 79 packages, no `binding.gyp`, no `.node` artifact |

### Verified library surface

Installed and introspected on 2026-09-10, not recalled.

| Package | Version | Clients used |
|---|---|---|
| `@google-cloud/compute` | 7.3.0 | `InstanceGroupManagersClient`, `AutoscalersClient`, `InstanceTemplatesClient`, `ZoneOperationsClient` |
| `@google-cloud/monitoring` | 6.1.0 | `MetricServiceClient` |
| `@google-cloud/run` | 4.1.0 | `ServicesClient`, `RevisionsClient` |
| `@google-cloud/billing` | 6.1.0 | `CloudBillingClient` |
| `@google-cloud/billing-budgets` | 7.1.0 | `BudgetServiceClient` |

## 2. Goals and non-goals

**Goals**

- Answer "what is the fleet doing, and what did it do overnight" without opening the Cloud console.
- Change the warm-pool floor, the ceiling and the cold-start knobs from the console, durably —
  a change that survives the next deploy.
- Show what the platform costs, honestly, including when the data to show it does not exist.
- Establish the customer-billing page's shape now, so that landing accounts and metering is
  wiring rather than design.

**Non-goals**

- Per-admin permissions. Everyone IAP admits is an admin, and now everyone IAP admits can resize
  the fleet. Distinguishing them is later work, and §8 names it as a risk.
- Changing fleet **shape** from the console — machine type, image, boot disk, rolling policy.
  Those stay with the deploy scripts. See Decision 1.
- Chargeback-grade cost attribution. The Costs page answers "roughly what, roughly where".
- Drain-before-delete, graceful worker shutdown, per-queue scaling. Those are buildout items 5
  and 6, and this design deliberately does not pretend to them. See §8.
- The Activity and Customers sections. They stay `available: false` in `sections.ts`.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Split ownership.** Deploy owns fleet shape; the console owns the elastic knobs and is their source of truth | Two writers of one autoscaler policy is a race whose loser is silent — a floor raised in the console, reset by the next unrelated deploy, discovered as a cold start under load |
| 2 | Deploy scripts set the scaling knobs **only at resource creation**, never on a reconcile | The narrowest change that makes Decision 1 true. The env vars survive as creation defaults, so a fresh environment still comes up correctly configured |
| 3 | Time series come from **Cloud Monitoring only**; no database connection is added | Monitoring already retains what the graphs need. A read-only Postgres role and a VPC connector are real infrastructure, and nothing on these three pages needs them. Its cost is stated in §8 |
| 4 | Each console reads and writes **its own project only** | A dev console holding mutate authority on prod is blast radius bought for a side-by-side view that two browser tabs already give |
| 5 | Official `@google-cloud/*` client libraries, not hand-written REST | Chosen by the user over thin `fetch` wrappers. Pagination, retries, LRO polling and auth are the parts that are tedious and easy to get subtly wrong |
| 6 | Writes authenticate off the **verified IAP JWT assertion**, not the email header | The header is only trustworthy because no `allUsers` binding exists. Verifying the assertion does not depend on that invariant holding |
| 7 | The audit trail is **structured Cloud Logging**, not a table | It is the only durable store the admin service can reach without the database connection Decision 3 declines |
| 8 | The Billing page ships as **empty states, not sample data** | A dashboard showing a plausible credit balance that is invented is worse than one that says the meter does not exist yet |
| 9 | Costs renders honestly when the BigQuery billing export is absent | The export cannot be backfilled. The page must not imply history it does not have |
| 10 | `recharts` for the graphs | Hover, tooltip and shared-crosshair behaviour is the thing an ops graph is read for. Stated as a deliberate dependency rather than absorbed silently |

## 4. Fleet

### 4.1 Live state

`InstanceGroupManagersClient.get` supplies `targetSize`, `status.isStable` and `currentActions`
(creating / deleting / recreating / verifying / refreshing / none). `AutoscalersClient.get` supplies
the policy and — the reason it is on the page at all — `status` and `statusDetails`.

`CUSTOM_METRIC_INVALID` is the failure this panel exists to surface. It is what an autoscaler
reports when the gauge it scales on has no data, and its symptom is a fleet pinned silently at its
floor while the queue grows. `create-worker-fleet.sh` step 6 documents the project having already
been through it once. Nothing today would tell an operator it had happened again.

### 4.2 Graphs

`MetricServiceClient.listTimeSeries`, aligned server-side, over a selectable window (1h / 6h / 24h /
7d).

| Graph | Series |
|---|---|
| Workers up, against floor and ceiling | `compute.googleapis.com/instance_group/size`, filtered to the MIG ¹ |
| Queue depth, against floor and ceiling | `custom.googleapis.com/hexera/queue_depth` — resource labels taken from `create-worker-fleet.sh`, not guessed |
| API requests | `run.googleapis.com/request_count` grouped by `response_code_class`; `request_latencies` p50/p95 |
| Cold-start posture | `run.googleapis.com/container/instance_count` split `active`/`idle` — the idle count *is* the warm-instance reading |

¹ **Unverified.** The exact metric type and resource labels for MIG size must be confirmed against
the live project during implementation. If that series is not populated, the graph renders an
explicit "no series" state naming the metric it looked for. It does not draw a flat line, and it
does not silently fall back to something that means something else.

Overlaying the floor and the ceiling on the first two graphs is the point of them: it turns "we ran
five workers" into "we ran at the ceiling for forty minutes", which is the reading that justifies
raising it.

### 4.3 Worker profile and instances

`InstanceTemplatesClient.get` renders machine type, boot disk size and type, OS image, the pinned
app image digest, service account, scopes and network.

Metadata **keys are listed; values are never rendered.** The values are secret *names* and are not
themselves sensitive — but a page that prints instance metadata is one template change away from
printing a credential, and `create-worker-fleet.sh` spends four paragraphs establishing that
metadata is a public surface. The page keeps that discipline rather than relying on the template
never changing.

`InstanceGroupManagersClient.listManagedInstances` renders each instance: name, `instanceStatus`,
zone, `currentAction`, the template version it is on, and `lastAttempt.errors`. The version column
is what makes a half-finished roll legible — two template names in one table means the rotation is
still in flight.

### 4.4 Controls

Server Actions, one per knob group.

| Control | Call |
|---|---|
| Warm floor, ceiling, cooldown, jobs-per-instance, scale-in control | `AutoscalersClient.update` |
| Set target size now | `InstanceGroupManagersClient.resize` |
| Recreate instance | `InstanceGroupManagersClient.recreateInstances` |
| Delete instance | `InstanceGroupManagersClient.deleteInstances` |
| Cloud Run min / max instances, per service | `ServicesClient.updateService`, `updateMask` on `template.scaling` |

Two mechanics that are easy to get wrong and are therefore specified:

**The autoscaler update is a read-modify-write of the whole resource.** `autoscalingPolicy` is
replaced wholesale, not merged. Sending a policy containing only `minNumReplicas` deletes the
custom-metric utilization the fleet scales on, which does not fail — it quietly converts the fleet
to CPU-based autoscaling. Every write therefore GETs first, mutates the returned object, and PUTs
it back.

**Compute mutations are long-running zonal operations.** `resize`, `deleteInstances` and
`recreateInstances` return an operation, not a result. The action does **not** block on it. It
returns as soon as the operation is accepted, and the page reflects progress through
`currentActions` on the next read — which is also how the group itself reports the work. Blocking a
Server Action on a VM deletion would hold a request open for minutes and time out before the
operation finished.

## 5. Split ownership — the deploy change

This is the smallest change that makes Decision 1 true, and it is confined to four scripts.

**`create-worker-fleet.sh`.** Step 6 currently calls `set-autoscaling` on every run. It becomes
conditional on the autoscaler not already existing. When one does exist, the step is skipped and
states why, in the voice the other skips already use:

> `autoscaler ${WORKER_MIG} exists - the scaling knobs belong to the admin console and are not reconciled here`

The `WORKER_MIG_*` variables are not deleted. They are demoted to **creation defaults**, which is
what they already are for a fresh environment, and `validate-config.sh` keeps validating them as
such — including the existing min ≤ max check and the `WORKER_ROLLING_MAX_UNAVAILABLE` interlock,
which is a *shape* rule and stays with deploy.

**`create-api-service.sh`, `create-console-service.sh`, `create-admin-service.sh`.** Each passes
`--min-instances` and `--max-instances` to a `gcloud run deploy` that is create-or-update. Each
gains the same carve-out: pass the flags when the service does not yet exist, omit them when it
does. Omitting a flag from `gcloud run deploy` leaves the existing value untouched, which is
precisely the required behaviour.

The scripts remain idempotent and reconciling for everything they still own. What changes is the
set of things they own, and each states it.

## 6. Costs and Billing

**Costs — our GCP spend.** `CloudBillingClient` gives the billing account and the project's link to
it; `BudgetServiceClient` gives budgets, their amounts and their thresholds.

Neither gives per-service spend over time. That requires a **BigQuery billing export**, a
project-level setting that accumulates only from the day it is enabled and cannot be backfilled.
Whether it is enabled on `hexera-dev` or `hexera-prod` is **unverified** — see open question 1.

The page is built to be correct either way. With an export, it shows spend by service and by SKU
over the selected window. Without one, it shows the budget, the current-month consumption against
it, the forecast, and a plain statement that per-service history begins when the export is enabled,
naming the setting. It never renders an empty chart that reads as "we spent nothing".

**Billing — customer plans and credits.** Layout only. Plan tiers, credit balance, the ledger
table, usage by period, and an invoice list — each rendered in an explicit "arrives with accounts
and metering" state, the pattern `sections.ts` already establishes for unavailable data. `Billing`
joins `ADMIN_SECTIONS` as `available: true`, because the page renders and explains itself; the
individual panels carry the honesty.

The shapes are chosen against buildout item 4's decisions — an append-only ledger of grants, holds,
debits and refunds with the balance derived, not a counter — so that landing that schema is wiring
rather than redesign.

## 7. Access and safety

**Authentication.** Reads are gated by IAP alone, as today. Writes additionally verify the
`X-Goog-IAP-JWT-Assertion` header — signature, issuer, audience — and take the actor identity from
the verified claims. `X-Goog-Authenticated-User-Email` is trustworthy only for as long as no
`allUsers` binding exists; `create-admin-service.sh` works hard to keep that true, but an audit
trail should not depend on a second script's invariant.

**Audit.** Every write emits one structured Cloud Logging entry before returning: actor email,
resource, action, the value before, the value after, and the operation id where there is one. A
mutation whose audit entry cannot be written still proceeds — refusing infrastructure changes
because logging is degraded is the wrong failure — but the failure is itself logged.

**Guardrails.**

- `minNumReplicas > maxNumReplicas` refused server-side, not only in the form.
- The ceiling is capped at a constant. It is the cost ceiling, and a fat-fingered `50` should not
  be able to request fifty `e2-standard-4` VMs.
- `deleteInstances` requires the operator to type the instance name. `recreateInstances` does not —
  it is how a wedged worker is fixed, and it is the same disruption a rolling update already causes.

**IAM.** The admin service account holds nothing today. It gains:

| Role | For |
|---|---|
| `roles/monitoring.viewer` | every graph |
| `roles/billing.viewer` on the billing account | Costs |
| `roles/run.viewer` | revisions, current scaling |
| `roles/run.admin` | Cloud Run min-instances |
| custom `hexeraFleetOperator` | the MIG and its autoscaler |

The custom role is deliberate. `roles/compute.instanceAdmin.v1` would work and would also grant
disk and instance creation this console never performs. The custom role carries exactly:
`compute.autoscalers.get`, `compute.autoscalers.update`, `compute.instanceGroupManagers.get`,
`compute.instanceGroupManagers.update`, `compute.instanceTemplates.get`, `compute.instances.list`,
`compute.instances.get`, `compute.zoneOperations.get`, and `compute.zones.get`.

That list is a starting point and **must be validated by exercising each action against the custom
role before the role is called done.** GCP's permission requirements for managed-instance-group
mutations are not reliably derivable from the API reference — `deleteInstances` in particular may
require instance-level permissions the group's own identity does not supply. A role that is too
narrow fails at the first click; a role widened by reflex to `instanceAdmin.v1` gives up the point
of having a custom role at all. Widen it permission by permission, and record what each action
turned out to need.

## 8. Risks

**An IAP compromise is now an infrastructure compromise.** Before this change, the worst a stolen
admin session could do was read. Now it can set the ceiling to its cap, delete every worker, and
scale the fleet to zero. The ceiling cap and the typed-name confirmation bound the damage; nothing
bounds it to a *subset of admins*, because per-admin permissions are a non-goal. This is the
largest cost of the feature and it is accepted knowingly.

**The delete confirmation is blind.** Decision 3 declines the database connection, so the console
cannot read `native_submission_claims` and cannot tell an operator that the instance they are about
to delete is four hours into a mesh job. Combined with the absence of drain-before-delete, a delete
loses that work with no warning and no recovery. The confirmation dialog **must say so explicitly**
rather than implying a check it did not perform. Two ways to close it later, both out of scope: add
the read-only database connection, or build the drain contract from buildout item 6.

**A knob the deploy no longer reconciles can drift out of a fresh environment's reach.** Once the
console owns the floor, `generated.<env>.env` no longer describes the running fleet. An operator
reading that file will get the creation defaults and believe them. The Fleet page is the authority
after this change, and the env files should say so in a comment.

**Cost data may be thinner than the page implies it could be** — see §6 and open question 1.

**The MIG size metric is unverified** — see §4.2, footnote 1.

**Two more runtime dependencies and a heavier image.** Five `@google-cloud/*` packages pull 79
transitive packages. They are pure JavaScript and need no native compilation, which was the
criterion the earlier design used, but they are not free: the admin image grows, and the admin
service runs at `ADMIN_MIN_INSTANCES=0` by default, so that growth is paid as cold-start latency by
whoever opens the console first. Raising the admin service's own floor is now a control on the
Fleet page, which is a pleasing but genuine answer.

## 9. Testing

| Area | Tests |
|---|---|
| GCP wrappers | Each client faked at the client-object boundary; assert the request shape, the pagination handling and the error path |
| Autoscaler write | Asserts the read-modify-write preserves `customMetricUtilizations` — the silent-CPU-fallback failure in §4.4 |
| Server actions | min > max refused; ceiling above the cap refused; unknown instance name refused; audit entry emitted with the verified actor; action returns without awaiting the operation |
| IAP assertion | A request with a valid email header but no valid assertion is refused for writes and permitted for reads |
| Deploy carve-out | Fake-gcloud tests in `tests/unit/deploy/`, as the existing stage tests do: `create-worker-fleet.sh` issues `set-autoscaling` when no autoscaler exists and does not when one does; each Cloud Run script omits `--min-instances` when the service exists and passes it when it does not |
| Costs honesty | Renders the no-export state naming the setting, with no empty chart, when the export is absent |
| Billing | Every panel renders its unavailable state; no panel renders invented figures |

## 10. Delivery

Three PRs, in order. Each is independently shippable and independently useful.

| PR | Contents | Why this boundary |
|---|---|---|
| **A — Fleet, read-only** | The `@google-cloud/*` access layer, the read IAM roles, and the Fleet page's live state, graphs, worker profile and instance table | Delivers the whole observability half with no mutate authority and no deploy change. If the MIG size metric turns out to be wrong (§4.2 fn 1), it is discovered here, before anything depends on it |
| **B — Fleet, controls** | The five Server Actions, the IAP assertion verification, the audit trail, the guardrails, the custom IAM role, and the four deploy-script carve-outs | The mutate authority and the ownership change land together. Shipping the carve-outs without the controls would leave the knobs owned by nothing |
| **C — Costs and Billing** | Both pages, including the no-export path | Shares only the access layer with A and B; nothing in it blocks or is blocked by the fleet work |

PR B is the one that changes the security posture, and it is the one to review hardest.

## 11. Open questions

1. **Is a BigQuery billing export enabled on either project?** Carried forward unanswered from the
   previous design. It cannot be backfilled, so enabling it is worth doing now regardless of when
   the Costs page ships — every day it is off is a day of history that cannot be recovered.
2. **What is the ceiling cap?** §7 requires a constant; the number is a cost decision. The current
   ceiling is 5 `e2-standard-4` instances.
3. **Should `scaleInControl` have a default this design sets, or start unset?** It is unset today.
   Setting it protects long mesh jobs from aggressive scale-in, which matters more given there is
   no drain contract — but it is a behaviour change to a live fleet made by a console page, not by
   a deploy.
