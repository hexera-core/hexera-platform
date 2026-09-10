# Custom domains for the consoles, and standing up prod — design

Date: 2026-09-09
Status: proposed
Scope: three sub-projects — **Edge-0** (harden prod's API), **Edge-1** (the edge, proven in dev),
**Edge-2** (prod console and admin, on their real names). Edge-1 does not depend on Edge-0;
Edge-2 depends on both.

## 1. Why this exists

Three consoles are reachable only at generated `run.app` URLs, and two of them do not exist yet.
The product needs `console.hexera.ai`, the operations console needs `admin.hexera.ai`, and both
need dev equivalents so a change can be seen before it is released.

Cloud Run does not hand out an IP address, so "point DNS at it" is not a configuration step — it is
a small subsystem, and it has to be provisioned by the same pipeline as everything else rather than
clicked together once and forgotten.

### Verified starting facts

Read from Google's documentation and from this repository on **2026-09-09**.

| Fact | Evidence |
|---|---|
| Cloud Run offers three ways to take a custom domain | Google's mapping-custom-domains doc: a global external Application Load Balancer (recommended), Cloud Run domain mappings, or Firebase Hosting |
| Domain mappings are **not production-ready** | Same doc: "in preview… not production-ready and are not supported at General Availability"; SSL takes 15 min–24 h; TLS 1.0/1.1 cannot be disabled; 64-character domain limit |
| Google recommends the load balancer | Same doc, for custom TLS certificates, path routing, Cloud CDN and Cloud Armor |
| IAP on Cloud Run already covers the load-balancer path | IAP overview: enabling it directly on the service "protects all ingress paths to Cloud Run, including the auto-assigned URL and any configured load balancer URL" |
| IAP must not be enabled twice | Same doc: "If a Cloud Run service is behind a load balancer, don't enable IAP on both the load balancer and the Cloud Run service" |
| Load-balancer data processing costs $0.008–0.012/GB | Google's network pricing, region-dependent |
| The per-forwarding-rule charge is **unverified** | Google's load-balancing pricing page would not render; not stated here rather than guessed |
| Core IAP is free | IAP pricing: "at no charge. However, networking and compute charges apply for required load balancing" |
| Prod's API would deploy with authentication disabled | `generated.prod.env:116` `API_ALLOW_UNAUTHENTICATED=1`; `:124-125` both auth secret containers empty; no `APP_ENV` anywhere, so `create-api-service.sh:75` defaults `ENV=dev`, and `settings/policy.py:24` exempts `dev` from every hardening guard |
| That combination is self-asserted identity | `settings/policy.py:22-23`: "with both secrets unset, any caller could act as any user (IDOR)" |
| A hardened environment forces non-wildcard CORS | `runtime/startup.py`: `ENV` outside the dev allowlist refuses to start with `CORS_ORIGINS == ["*"]` |
| Prod's console and admin are deliberately unpinned | `.github/workflows/deploy.yml:223,229` — `console_service=` and `admin_service=` empty, with the reasoning in the file |
| The deploy identity cannot create service accounts | Observed on the dev admin deploy: `Permission 'iam.serviceAccounts.create' denied`; the scripts document this as an owner action |
| The dev console is live | `https://dev-console-s7toqcfd3a-uc.a.run.app` — `/` 307s to `/sign-in`, health returns `{"status":"ok"}` |

## 2. Goals and non-goals

**Goals**

- Four hostnames, TLS-terminated, provisioned by the deploy pipeline rather than by hand.
- Prod's console and admin standing up on a release, on their real names.
- Prod's API refusing to start rather than serving self-asserted identities.
- One place to read what the IP is, so DNS is a copy-paste rather than an investigation.

**Non-goals for this cycle**

- A hostname for the product API. It keeps its `run.app` URL; the browser's WebSocket dials it
  directly and only needs CORS to name the console's origin.
- Cloud CDN, Cloud Armor, or any WAF. The load balancer exists to terminate TLS for a name.
- Path-based routing. Host rules only — one service per hostname.
- Apex `hexera.ai`. Four subdomains; the apex is someone else's concern.
- Moving IAP to the load balancer. It stays on Cloud Run, where it already covers this path.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | A global external Application Load Balancer, not domain mappings | Domain mappings are Preview and explicitly not production-ready. `console.hexera.ai` is production. |
| 2 | One load balancer per project, serving both of that project's hostnames | Host rules in one URL map. Two IPs instead of four, and a project boundary is a hard boundary for backends. |
| 3 | IAP stays on Cloud Run; the load balancer is plain | It already protects the load-balancer path, and enabling it on both is documented as wrong. Nothing built for the admin console changes. |
| 4 | An `edge` deploy component with `create-edge.sh` | The repository provisions with idempotent, reconciling bash selected by `DEPLOY_COMPONENTS`. A second idiom (Terraform) would be a bigger change than the thing it provisions. |
| 5 | A `PROVISIONING` certificate exits 0 | A managed certificate cannot validate until DNS points at an IP that cannot exist before the first run. Failing on it would make the pipeline unusable on the day it is introduced. A `FAILED` certificate still fails. |
| 6 | Keep the `:80` → HTTPS redirect | A second forwarding rule per project. `http://console.hexera.ai` hanging is worse than the cost. |
| 7 | Prod's empty service names are rewritten, not deleted | They are empty deliberately, with the reasoning in the file. The answer changes; the reasoning is replaced by the new one rather than removed. |
| 8 | Edge-0 before Edge-2 | `ENV=production` forces a non-wildcard `CORS_ORIGINS`, which is where the console's origin has to be listed. Hardening first gives the domain work somewhere valid to point. |

## 4. Edge-0 — harden prod's API

`APP_ENV=prod` in the prod deployment env, and `MESH_API_KEY_SECRET` / `USER_TOKEN_SECRET_SECRET`
naming real containers. `CORS_ORIGINS` gains `https://console.hexera.ai`.

The effect is not cosmetic. Today a prod deploy would produce an API that accepts any `X-User-Id`
a caller sends. With `ENV` outside the dev allowlist, `settings/policy.py` refuses the import
unless both secrets are set, and `runtime/startup.py` refuses a wildcard `CORS_ORIGINS`. Prod stops
being able to start in the broken configuration.

`validate-config.sh` gains a check: a deployment whose `DEPLOYMENT_ID` is `prod` and whose
`APP_ENV` is unset or in the dev allowlist is refused before any mutation. The failure mode this
prevents is silent, and the environment it affects is the one that matters.

## 5. Edge-1 — the edge

**`deploy/gcp/scripts/create-edge.sh`**, selected by a new `edge` component, running after the
service stages because a serverless NEG requires its Cloud Run service to exist.

Per project it reconciles, in order: a reserved global static IP; one serverless network endpoint
group per Cloud Run service; one backend service per NEG; one URL map whose host rules map each
hostname to its backend; one Google-managed certificate covering that project's hostnames; an
HTTPS target proxy and a forwarding rule on `:443`; and an HTTP target proxy and forwarding rule on
`:80` that redirect to HTTPS.

Existing resources are validated and reused. Nothing is deleted and recreated — a forwarding rule
that is recreated changes the IP, which breaks DNS that was already correct.

**The first run cannot finish the job, and says so.** It reserves the IP, prints it as the one
thing an operator must act on, creates the certificate, and exits 0 reporting the certificate as
`PROVISIONING` and DNS as not yet pointed. The operator adds the `A` records; the certificate
becomes `ACTIVE` within roughly 15–60 minutes without further action; the next run reports
`ACTIVE`. A `FAILED` certificate is a failure and is reported as one.

The IP is also written into the deployment-state manifest, so the value survives the run that
printed it.

**Hostnames, by project:**

| Hostname | Project | Cloud Run service |
|---|---|---|
| `dev.console.hexera.ai` | hexera-dev | `dev-console` |
| `dev.admin.hexera.ai` | hexera-dev | `dev-admin` |
| `console.hexera.ai` | hexera-prod | `prod-console` |
| `admin.hexera.ai` | hexera-prod | `prod-admin` |

Hostnames are declared in the deployment env — `CONSOLE_DOMAIN` and `ADMIN_DOMAIN` — and an empty
value means that hostname is not served, in the same shape every other optional tier uses. A
project with neither declared skips the stage.

## 6. Edge-2 — prod

`console_service=prod-console` and `admin_service=prod-admin` on the workflow's prod branch,
replacing the empty values and the comment explaining why they were empty. `CONSOLE_DOMAIN` and
`ADMIN_DOMAIN` set for prod. The `edge` component joins the release tag's reconcile set.

A release tag then provisions: two Cloud Run services, two serverless NEGs, two backend services, a
URL map, a certificate, and two forwarding rules — all of them billed. That is the point of the
sub-project, and it is why the decision is recorded rather than implied.

## 7. Owner prerequisites

None of these can be done by the deploy identity, which deliberately lacks
`iam.serviceAccountAdmin` and cannot create secret values:

- `prod-console` and `prod-admin` service accounts in `hexera-prod`.
- `create-secrets.sh` run in `hexera-prod`, and a real version added to each container:
  `AUTH_SECRET`, `CONSOLE_AUTH_USERS`, `MESH_API_KEY`, `USER_TOKEN_SECRET`.
- The IAP OAuth brand in `hexera-prod` — see `docs/deployment/admin-console-access.md` §3.
- Four DNS `A` records, once the IPs are known.

The same list already blocks the dev admin console: `dev-admin` needs its service account created.

## 8. Testing

| Sub-project | Tests |
|---|---|
| Edge-0 | a prod-shaped config with `APP_ENV` unset is refused by `validate-config.sh`; a hardened one passes |
| Edge-1 | fake-gcloud stage tests: the NEG names its service, host rules map each declared hostname to the right backend, the certificate covers exactly the declared hostnames, a `PROVISIONING` certificate exits 0, a `FAILED` one does not, a second run reuses and never recreates the forwarding rule, and a project with no hostnames states its skip |
| Edge-2 | the workflow pins both prod services and both prod hostnames; `edge` is in the release tag's component set |

Post-deploy: each declared hostname resolves to the reserved IP, serves 200 or a redirect to
`/sign-in`, and the admin hostname refuses an anonymous request.

## 9. Risks

**The certificate is the slow part and the part that looks broken.** Between the first run and DNS
propagating, `https://console.hexera.ai` serves a certificate error. That is expected and
temporary, and it is why decision 5 exists — but anyone watching will read it as a failed deploy.
The run's own output has to say what state it is in and what the operator still owes it.

**Recreating a forwarding rule changes the IP.** DNS that was correct becomes wrong silently, and
the certificate goes back to `PROVISIONING`. The script must reuse; a test asserts it.

**Prod is being stood up for the first time by this pipeline.** Every stage before the edge —
service accounts, secrets, the schema, the API — has never run against `hexera-prod` from the
workflow. Edge-2 should follow a successful `components=images,migrate` release, not be the first
thing a tag does.

**Cost is not fully known.** Four forwarding rules across two projects. The per-rule charge could
not be verified; it should be read from the billing console before Edge-2, not after.

## 10. Open questions

1. Who owns DNS for `hexera.ai`, and is it somewhere `A` records can be added quickly? The
   certificate cannot provision until they exist, so a slow answer here is the critical path.
2. Should `dev.console.hexera.ai` be reachable publicly at all, or restricted? It is a sandbox
   whose API accepts self-asserted identity; a public name makes it easier to find. Not blocking,
   but worth an answer before it is advertised.
