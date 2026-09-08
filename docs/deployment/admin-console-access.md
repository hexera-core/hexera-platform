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
Run service:

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

# 2) A granted identity gets in.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" "${URL}/"

# 3) No public invoker binding survives.
gcloud run services get-iam-policy "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format='value(bindings.members)' | tr ';' '\n' | grep -qx allUsers \
  && echo "FAIL: still public" || echo "OK: not public"
```

A `200` from check 1 means the app served an anonymous request. Stop and fix §2 before going
further.

## 7. Reading who the user is, inside the app

IAP puts a signed assertion on every request it forwards:

```
X-Goog-IAP-JWT-Assertion
```

**The header alone is not proof of anything.** It must be verified — signature, issuer, and the
audience matching this exact service — before it is trusted. An unverified header is trivially
forged by anything that can reach the container directly, which is precisely what IAP is stopping
at the edge; trusting it unverified moves the trust boundary back inside.

Until the admin console needs per-admin permissions, it does not have to read this at all: IAP
either let the request through or it did not, and everyone who gets through is an admin. Add
verification at the point you first need to distinguish one admin from another, not before.

## 8. What this does not cover

- **The public console** (`hexera-<env>-console`) is deliberately not behind IAP. Its Auth.js
  session is its gate, and an IAP screen in front of the sign-in page would lock out every user.
- **The product API** (`<env>-api`) is invokable by `allUsers` today in both environments. That is
  recorded, with its consequences, in [environments and delivery](environments-and-delivery.md).
  IAP is not the fix for it: the browser and the WebSocket must reach it without a Google identity.
- **Prod.** Every command above names `hexera-dev` and its project number. Prod has no admin
  console service yet; when it gets one, repeat §3-§6 with `hexera-prod` / `688073002171` and grant
  access separately. Access to dev must not imply access to prod.

## 9. Prerequisite

None of this can run yet: **`admin-console` has no Cloud Run service.** It is a stub app with a
health route, deliberately left undeployed (`docs/superpowers/specs/2026-09-07-saas-console-design.md`,
non-goals). Deploying it is the step before §4 — the public console's own tier
(`deploy/gcp/scripts/create-console-service.sh`) is the pattern, with two deliberate inversions:
`--no-allow-unauthenticated` instead of the public invoker binding, and `--iap`.
