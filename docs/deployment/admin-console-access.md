# Locking the admin console behind IAP

How to make `admin-console` reachable only by named Google accounts, and how to prove it.

Read from Google's Cloud Run and IAP documentation on **2026-09-08**. Where this disagrees with a
comment in the repository, this is the observation and the comment is the claim.

---

## 1. Why IAP, and what it costs

The admin console shows fleet state, spend, customer usage and the outreach list. It is the one
surface where "reachable by anyone who finds the URL" is unacceptable.

**Identity-Aware Proxy authenticates a Google identity before the request reaches the container.**
That is a stronger control than the usual edge patterns — a signed cookie, a WAF IP allowlist, or
basic auth at a CDN — because those authenticate a *request*. IAP authenticates a *person*: access
is an IAM binding, revocation is removing it, and every entry is attributable.

**It is free, and it needs no load balancer.** IAP can be enabled directly on a Cloud Run service,
which is what avoids the cost:

> Enable IAP directly on your Cloud Run services. This enables IAP to protect all ingress paths to
> Cloud Run, including the auto-assigned URL and any configured load balancer URL.

> Identity-Aware Proxy includes a number of features that can be used to protect access to Google
> Cloud-hosted resources … at no charge. However, networking and compute charges apply for required
> load balancing.

We use no load balancer, so no such charge applies. The paid tier is BeyondCorp Enterprise —
protecting non-Google-Cloud resources, customising the IAP screens, and device attributes in access
levels. None of that is needed here. The running cost of the admin console is therefore the Cloud
Run cost alone, which at `min-instances=0` for a page a handful of people open is negligible.

**This is the opposite posture from the public console.** `hexera-<env>-console` is deliberately
`allUsers`-invokable because its Auth.js sign-in page must be reachable before anyone can
authenticate (`deploy/gcp/scripts/create-console-service.sh`, step 6). The admin console has no
public entry point and must not be granted that binding.

## 2. The failure that makes this pointless

**Enabling IAP on a service that is still `allUsers`-invokable protects nothing.** The Cloud Run
IAM binding and the IAP policy are separate checks, and the public invoker binding lets a request
in regardless of what IAP thinks. The service must be `--no-allow-unauthenticated`.

Check it before and after every change in this document:

```bash
gcloud run services get-iam-policy admin-console \
  --project hexera-dev --region us-central1 \
  --format='value(bindings.members)' | tr ';' '\n' | grep -x allUsers \
  && echo "PUBLIC - IAP is not protecting this" || echo "no public binding"
```

## 3. One-time project setup

Per project. `hexera-dev` is project number **224734058693**; `hexera-prod` is **688073002171**.

```bash
PROJECT=hexera-dev
gcloud services enable iap.googleapis.com --project "${PROJECT}"
```

Then configure the OAuth consent screen once, in the console under **OAuth Branding**. It cannot be
done from `gcloud` on a first-time project. Choose the audience type that matches your setup —
*Internal* if every admin is in your Google Workspace domain, which is the tighter choice;
*External* only if you must admit an account outside it. Note the Client ID and Secret, and add the
redirect URI:

```
https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect
```

If the admin console is deployed before this is done, the first attempt to enable IAP in the console
UI will offer to generate these credentials for you.

## 4. Enable IAP on the service

On a service that already exists:

```bash
PROJECT=hexera-dev
REGION=us-central1
SERVICE=admin-console

gcloud run services update "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --no-allow-unauthenticated \
  --iap
```

On first deploy, the same two flags apply to `gcloud run deploy`.

Then grant the **IAP service agent** permission to invoke the service. IAP terminates the request
and calls Cloud Run itself, so without this every request fails with a 403 that looks like your
policy is wrong when it is not:

```bash
PROJECT_NUMBER=224734058693      # hexera-dev
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
  --role roles/run.invoker
```

## 5. Grant a person access

The role is `roles/iap.httpsResourceAccessor`. It is granted on the IAP resource, not on the Cloud
Run service.

**Project Owner does not grant this, and the error does not say so.** An Owner who opens the
hostname gets "You don't have access" with their own address in the troubleshooting box, which reads
like a broken deployment. It is not. `roles/owner` includes `iap.tunnelDestGroups.accessViaIAP` and
`iap.tunnelInstances.accessViaIAP` — the *tunnel* permissions, for SSH and TCP forwarding. Web
resources are gated by `iap.webServiceVersions.accessViaIAP`, which Owner does not hold. Verified
against `gcloud iam roles describe` on 2026-09-09. This separation is deliberate: administering a
project and being admitted to an application it hosts are different questions, so every admin must
be granted access explicitly, including the person who created the project.

```bash
gcloud iap web add-iam-policy-binding \
  --project "${PROJECT}" --region "${REGION}" \
  --resource-type=cloud-run --service="${SERVICE}" \
  --member "user:you@hexera.ai" \
  --role roles/iap.httpsResourceAccessor
```

Prefer a **group** over individual users once there is more than one admin — then joining and
leaving the team is a group membership change rather than an IAM edit per service per environment:

```bash
  --member "group:ops@hexera.ai"
```

Revoke with the same command and `remove-iam-policy-binding`. Revocation takes effect on the next
request; it does not depend on an app-side session expiring.

List who currently has access:

```bash
gcloud iap web get-iam-policy \
  --project "${PROJECT}" --region "${REGION}" \
  --resource-type=cloud-run --service="${SERVICE}"
```

## 6. Prove it works

Three checks. The first is the one people skip, and it is the one that catches the §2 failure.

```bash
URL=$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')

# 1) An anonymous request must NOT reach the app. Expect 302 to accounts.google.com, or 401/403.
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "${URL}/"

# 2) A granted identity gets in. USE A BROWSER, not curl -- see the note below.

# 3) No public invoker binding survives.
gcloud run services get-iam-policy "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format='value(bindings.members)' | tr ';' '\n' | grep -qx allUsers \
  && echo "FAIL: still public" || echo "OK: not public"
```

A `200` from check 1 means the app served an anonymous request. Stop and fix §2 before going
further.

**Check 2 has to be a browser.** `gcloud auth print-identity-token` mints a token whose audience is
the default one, and IAP requires the audience to be its own OAuth client ID — so that token yields
`401` even when access is correctly granted, and reads as a failure that is not one. Curl can prove
the *negative* (check 1: anonymous does not get in); only the browser's OAuth round trip proves the
positive. To script it, mint a token for the right audience instead:

```bash
# CLIENT_ID is the IAP OAuth client, visible in the check-1 redirect URL.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences="${CLIENT_ID}")" "${URL}/"
```

## 7. Reading who the user is, inside the app

IAP puts a signed assertion on every request it forwards:

```
X-Goog-IAP-JWT-Assertion
```

**The header alone is not proof of anything.** It must be verified — signature, issuer, and the
audience matching this exact service — before it is trusted. An unverified header is trivially
forged by anything that can reach the container directly, which is precisely what IAP is stopping
at the edge; trusting it unverified moves the trust boundary back inside.

**The admin console now verifies it, and here is the reason.** It is not to distinguish one admin
from another — it still does not, and everyone IAP admits is an admin. It is because the console
can now *change infrastructure*, and every change writes an audit entry naming who made it. That
record must not rest on `X-Goog-Authenticated-User-Email`, which is plain text and trustworthy only
for as long as no `allUsers` invoker binding exists. §2 is the whole argument for why that
invariant is worth holding and how quietly it can break; an audit trail should not depend on it.

So: **reads are gated by IAP alone. Writes verify the assertion** — signature, issuer
`https://cloud.google.com/iap`, and audience — and take the actor's identity from the verified
claims. Direct IAP on Cloud Run signs for the service, so the audience is
`/projects/<PROJECT_NUMBER>/locations/<REGION>/services/<SERVICE>`; the
`/projects/<n>/global/backendServices/<id>` form belongs to the load-balancer arrangement §1
declines. `GCP_PROJECT_NUMBER` is therefore load-bearing for writes: a console deployed without it
cannot compute an audience, and refuses every mutation rather than falling back to the header.

There is no bypass switch. Failing closed means a misconfigured console stops changing things,
which is the correct direction; the alternative is accepting an unverified actor, which is the
entire risk this closes.

Server Functions are reachable by direct `POST`, not only through the form that renders them, so
this verification runs inside every action rather than in the page that displays it.

## 8. What this does not cover

- **The public console** (`hexera-<env>-console`) is deliberately not behind IAP. Its Auth.js
  session is its gate, and an IAP screen in front of the sign-in page would lock out every user.
- **The product API** (`<env>-api`) is invokable by `allUsers` today in both environments. That is
  recorded, with its consequences, in [environments and delivery](environments-and-delivery.md).
  IAP is not the fix for it: the browser and the WebSocket must reach it without a Google identity.
- **Prod.** Every command above names `hexera-dev` and its project number. Prod has no admin
  console service yet; when it gets one, repeat §3-§6 with `hexera-prod` / `688073002171` and grant
  access separately. Access to dev must not imply access to prod.

## 9. How the service gets there

The admin console has a Cloud Run service now, deployed by its own `admin` component:
`DEPLOY_COMPONENTS=admin` (or `all`) runs `deploy/gcp/scripts/create-admin-service.sh`, which is
the script that applies every mutation §4 describes by hand — `--no-allow-unauthenticated`,
`--iap`, and the IAP service agent's invoker grant — on every run, not once. That script is the
public console's own tier with the two inversions §4 names; it never grants `allUsers` and
actively removes the binding if it finds one, so the failure in §2 cannot survive a rerun.

Its name in `hexera-dev` is `dev-admin`, not the `admin-console` used as a placeholder in the
commands above — substitute it when running §2, §4-§6 for real. `hexera-prod`'s `admin_service` is
left deliberately empty in `.github/workflows/deploy.yml`, exactly as `console_service` is (§8):
a release tag reconciles every tier it is told about, so naming one there would provision a
billed production service nobody asked for. Prod gets an admin console only when a human pins a
name there in a reviewed diff, and then repeats §3-§6 against `hexera-prod`.


## 10. What the console reads, and what it can change

This section is the authority on the admin service account's authority. It is granted by
`create-admin-service.sh` on every run; a grant that fails there is reported with the command that
fixes it, and the console itself is the verdict — a page that cannot read its metric says so.

**Reads** — four predefined viewer roles on the project:

| Role | Serves |
|---|---|
| `roles/compute.viewer` | the managed instance group, its autoscaler and its instance template |
| `roles/monitoring.viewer` | every graph on the Fleet page |
| `roles/run.viewer` | Cloud Run revisions and current scaling |
| `roles/billing.viewer` | the Costs page's account state |

Budgets live on the **billing account**, not the project, and a deploy identity has no authority
there. Grant that one by hand, once:

```bash
gcloud billing accounts add-iam-policy-binding <BILLING_ACCOUNT_ID> \
  --member serviceAccount:<DEPLOYMENT_ID>-admin@<PROJECT>.iam.gserviceaccount.com \
  --role roles/billing.viewer
```

`roles/bigquery.jobUser` is granted **only** when `BILLING_EXPORT_TABLE` is set. A grant for a
billing export the deployment does not have is authority nobody asked for.

**Writes** — a project **custom role**, not a predefined one. `roles/compute.instanceAdmin.v1`
would work and would also grant disk and instance *creation* this console never performs;
`roles/run.admin` would let an IAP-gated web page deploy arbitrary revisions. The custom role
carries exactly the verbs the Fleet page's controls issue: `compute.autoscalers.get/update`,
`compute.instanceGroupManagers.get/update`, `compute.instanceTemplates.get`,
`compute.instances.get/list`, `compute.zoneOperations.get`, `compute.zones.get`,
`run.services.get/update`, `run.revisions.get/list`, `run.operations.get`.

**What this costs, stated plainly.** Before this, the worst a stolen admin session could do was
read. It can now raise the fleet's ceiling to its cap, resize the group, and delete workers. Two
things bound the damage and neither bounds it to a *subset of admins*, because per-admin
permissions remain a non-goal:

- `ADMIN_MAX_ALLOWED_REPLICAS` (default 12) caps any ceiling or resize the console will submit.
- Deleting an instance requires typing its name back.

**The delete confirmation is blind, and the page says so.** The console holds no database
connection, so it cannot read job leases and cannot tell you the worker you are about to remove is
four hours into a mesh job. There is also no drain contract yet — the worker is not asked to finish
first. Closing that gap needs either the read-only database connection or the drain work in
build-out item 6; neither is in place.

**Who owns the scaling knobs.** The floor, ceiling, cooldown, jobs-per-instance and scale-in
control belong to **this console**, not to the deploy. `create-worker-fleet.sh` sets them when it
creates the autoscaler and does not reconcile them afterwards;
`create-queue-depth-publisher.sh` still repoints the metric filter but reads the live numbers back
and passes them through unchanged; the three Cloud Run scripts pass `--min-instances` /
`--max-instances` only when creating a service. The `WORKER_MIG_*` and `*_MIN_INSTANCES` values in
`generated.<env>.env` are therefore **creation defaults** — they stop describing the running system
the moment anyone changes it here. The Fleet page is the authority after that.


## 11. What the first real dev deploy found

Recorded 2026-09-10, from run 34456974668 against `hexera-dev`. Every item here is a thing the
deploy could not do for itself, so each is a command a human runs once.

**The deployer cannot grant IAM, by design.** `github-deployer` holds neither
`resourcemanager.projectIamAdmin` nor `iam.roles.create` — deliberately, and
[the build-out plan](platform-buildout-plan.md) §1 explains what its eleven roles do and do not
include. So `create-admin-service.sh` attempts each grant, warns with the exact fix, and carries
on. **A green deploy therefore does not mean the console has any authority.** The three project
viewer roles and the billing-account role were granted by hand after the first deploy; the custom
role is below.

**Creating the custom role.** The console's controls return `PERMISSION_DENIED` until this exists
and is bound. Run once per project, as an owner:

```bash
PROJECT=hexera-dev      # or hexera-prod
ROLE=dev_admin_console  # or prod_admin_console — it is <DEPLOYMENT_ID>_admin_console
SA=dev-admin@hexera-dev.iam.gserviceaccount.com   # or prod-admin@hexera-prod...

gcloud iam roles create "$ROLE" --project "$PROJECT" \
  --title 'Hexera admin console fleet operator' --stage GA \
  --permissions compute.autoscalers.get,compute.autoscalers.update,\
compute.instanceGroupManagers.get,compute.instanceGroupManagers.update,\
compute.instanceTemplates.get,compute.instances.get,compute.instances.list,\
compute.zoneOperations.get,compute.zones.get,\
run.services.get,run.services.update,run.revisions.get,run.revisions.list,run.operations.get

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$SA" --role "projects/$PROJECT/roles/$ROLE" --condition None
```

Granting it is what turns an IAP session into infrastructure authority — §10 states that cost.
Until then the console reads and reports; it changes nothing.

**`roles/billing.viewer` is a billing-ACCOUNT role.** It cannot be bound to a project at all — the
API answers `Role roles/billing.viewer is not supported for this resource` — so the script does not
try. `hexera-dev`'s billing account is `01EFBB-8FF368-9E335F`; the grant command is in §10.

**The console points at `dev-workers`, which does not exist yet, and that is deliberate.**
`deploy.yml` pins `worker_mig=dev-workers`; the only managed instance group in `hexera-dev` today
is `hexera-dev-workers` (size 1, stable, `us-central1-a`), which is the hand-made group from before
the fleet had a provisioning script.

**`dev-workers` is the name the platform is moving to**, and separate work renames the fleet onto
it. The admin console is therefore pointed at the DESTINATION name rather than the current one, so
that the rename needs no change here and no redeploy of this tier: the Fleet page starts reporting
the moment a group by that name exists. `hexera-prod` already follows the convention — its group is
`prod-workers` — so prod needed no equivalent decision.

Until the rename lands, the Fleet page reports `NOT_FOUND` for `dev-workers` and names the two
environment variables it read. That is the page working, not failing: the alternative — quietly
falling back to a group with a similar name — is how an operator ends up reading one fleet's
numbers and acting on another's.

If the rename is deferred and dev needs live fleet numbers sooner, the stopgap is a one-line change
of `worker_mig` to `hexera-dev-workers` in `deploy.yml` plus an `admin`-tier deploy. It is a
stopgap and not the destination, which is why it is written here rather than done.

**The autoscaler is not named after its group.** `hexera-dev-workers` is driven by an autoscaler
called `hexera-dev-workers-9k6e`. Everything that asks "does this group have an autoscaler" must
read the group's own `status.autoscaler` field rather than assuming the names match — a name guess
returns "no autoscaler" for a group that has one, and for `create-worker-fleet.sh` that would mean
re-applying a policy the console owns. Both it and `create-queue-depth-publisher.sh` read the
field.


## 12. The billing export

The Costs page's per-service breakdown needs a **BigQuery billing export**, which is the one piece
of this system that cannot be provisioned from code: there is no `gcloud` subcommand and no method
on the Cloud Billing API. It is enabled in the Cloud Console under **Billing → Billing export →
BigQuery export**, and only **Standard usage cost** is turned on — that is the export whose table
carries `service.description`, `project.id`, `cost`, `credits[].amount` and `usage_start_time`,
which is everything the query reads. Detailed usage cost, Pricing, FOCUS and the CUD export are
deliberately off: each is a second copy of the same money in a shape nothing here reads, and each
costs storage and scan.

**The dataset is `hexera-prod:billing_export`, and it is in prod on purpose.** An export
accumulates from the day it is switched on and cannot be backfilled, so its history is
irreplaceable. `hexera-dev` is a shared sandbox that may be rebuilt; a table that disappears with it
takes every month recorded up to that point. The table name follows the account id with its hyphens
turned to underscores: `gcp_billing_export_v1_01EFBB_8FF368_9E335F`.

**One billing account bills both projects, so one table holds both.** Three reads had to be scoped
because of it, and the third was only caught by looking at the deployed page:

| Read | Scope |
|---|---|
| `getProjectBillingInfo` | already per-project |
| `listBudgets` | per billing ACCOUNT — filtered to budgets whose `budgetFilter.projects` names this project, plus account-wide ones |
| the spend query | per billing ACCOUNT — filtered on `project.id` |

Without the last two, dev's Costs page would have shown prod's budget beside its own and summed
prod's spend into its total.

**Dev's console reads a table in prod, and that is the only cross-project read in this design.**
§10's rule is otherwise "own project only". It is accepted here because the exposure was already
granted: the dev identity holds `roles/billing.viewer` on the shared billing account, so it can
already see account-wide billing data. The BigQuery grant is dataset-scoped `READER` on this one
table and reaches nothing that account-level role did not.

**Until the first table is written** — hours after enabling — the page says so, and distinguishes
"the export is not switched on" from "it is, and the first write is pending". It needs no redeploy
when the table appears.
