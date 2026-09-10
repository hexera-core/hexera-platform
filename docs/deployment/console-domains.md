# Giving the consoles a custom hostname

How `dev.console.hexera.ai`/`dev.admin.hexera.ai` and, as of this document, `console.hexera.ai`/
`admin.hexera.ai` reach their Cloud Run services, what the first run of each leaves half-finished
on purpose, and the manual steps that finish it.

Read from `deploy/gcp/scripts/create-edge.sh`, `deploy/gcp/scripts/bootstrap-env.sh` and
`.github/workflows/deploy.yml` on **2026-09-09**. Where this disagrees with a comment in the
repository, this is the observation and the comment is the claim.

---

## 1. Which hostname is which service, in which project

| Hostname | Cloud Run service | Project |
| --- | --- | --- |
| `dev.console.hexera.ai` | `dev-console` | `hexera-dev` |
| `dev.admin.hexera.ai` | `dev-admin` | `hexera-dev` |
| `console.hexera.ai` | `prod-console` | `hexera-prod` |
| `admin.hexera.ai` | `prod-admin` | `hexera-prod` |

Prod's row is new: `.github/workflows/deploy.yml`'s `target` job used to emit `console_domain=`
and `admin_domain=` empty for prod, deliberately, for exactly as long as hexera-prod had no
`prod-console`/`prod-admin` service accounts and no secret containers for them to read — naming a
hostname before then would have let a release tag provision a billed load balancer, address and
certificate nobody asked for. Both service accounts now exist in `hexera-prod`, each holding
`secretmanager.secretAccessor` on `console-auth-secret`, `console-auth-users`, `mesh-api-key` and
`user-token-secret` (every container with an enabled version), so a `v*` tag now pins both
hostnames and `create-edge.sh` provisions the edge for both consoles on the next release.

**What pinning the hostname does *not* finish, for prod specifically** — see §7 and §9:

- The IAP OAuth brand for `hexera-prod` does not exist yet and has to be created by hand in the
  Cloud Console; Google shut down the IAP OAuth Admin APIs in March 2026, so nothing here can
  script it. Until it exists, `--iap` on `prod-admin` will not work.
- The DNS A records for `console.hexera.ai` and `admin.hexera.ai` do not exist yet. §3 below is
  exactly as true for prod's first release as it always was for dev's first deploy.

## 2. Why a hostname means a load balancer at all

**Cloud Run hands out no IP address of its own.** Every service gets a `*.run.app` URL that
already works over HTTPS with a Google-managed certificate; what it cannot do is answer to a name
you chose. Fronting it with one means a global external Application Load Balancer:  a reserved
static IP, a serverless NEG bridging the classic LB backend model to a Cloud Run revision, a
backend service, a URL map that routes by host, and a Google-managed certificate. `create-edge.sh`
assembles exactly that, once, for both consoles — **one** address serving two host-routed backends,
and **one certificate per hostname**.

### 2.1 One certificate per hostname, because a shared one couples them

A Google-managed certificate serves only once it is **wholly** `ACTIVE`. A certificate that names
two hostnames therefore has one fate for both: while either name is still failing validation, the
certificate as a whole is not `ACTIVE`, and **neither** name is reachable over HTTPS — including
one that validated hours earlier.

That is an observation, not a worry. Dev ran in exactly this state on **2026-09-09**:

```
overall:                PROVISIONING
dev.console.hexera.ai = ACTIVE
dev.admin.hexera.ai   = FAILED_NOT_VISIBLE
```

`dev.console.hexera.ai` had validated and was still unreachable over HTTPS, because
`dev.admin.hexera.ai` on the same certificate had not. Carried into prod, that shape means a
validation problem on `admin.hexera.ai` — an internal, IAP-gated console — takes
`console.hexera.ai`, the customer-facing name, down with it. The blast radius of the admin console's
DNS is the whole product.

So `create-edge.sh` now creates **one certificate per declared hostname**, each covering exactly
that one domain, and hands the HTTPS target proxy the **list** of them
(`--ssl-certificates=cert1,cert2`). The names are derived from the `EDGE_CERT` base the deployment
env already declares — `dev-edge-cert-console`, `dev-edge-cert-admin` — so they stay predictable
and greppable in the Cloud Console. `EDGE_CERT` itself is now a base name rather than a resource:
no certificate is created under it any more.

The fates are then independent. One hostname at `FAILED_NOT_VISIBLE` still fails the deploy — a
state that does not resolve itself is still a failure — but it fails **after** every hostname's
state has been read, reported and attached, so the healthy hostname is serving its own certificate
either way, and each hostname's state is reported on its own line.

### 2.2 What the migration onto per-host certificates does to a live edge

Dev already had the shared `dev-edge-cert` attached to `dev-edge-https-proxy`. The first run of the
new script against it creates the two per-host certificates and then **updates the proxy's
certificate list** — `create-edge.sh` reads the live list back and reconciles it rather than only
setting it at creation, because an existing proxy is never created and would otherwise go on
serving the shared certificate forever while the new ones sat provisioned, billed and unused.

Two things that update deliberately does **not** do:

- **It does not delete the old certificate.** `target-https-proxies update --ssl-certificates`
  rewrites the proxy's `sslCertificates` field and nothing else. `dev-edge-cert` survives,
  unreferenced, until an operator removes it — this script never deletes anything.
- **It does not change the address.** The IP belongs to the *forwarding rule*
  (`dev-edge-https-rule` / `dev-edge-http-rule` on `35.186.202.188`), which names the proxy and is
  not rewritten at all. DNS that already points at that address keeps working across the swap.

There is a window during the swap in which the new certificates are `PROVISIONING`. They validate
against A records that already exist, so it is the short end of the 15–60 minutes in §4 rather than
a first run's wait — but HTTPS on both hostnames is degraded until they go `ACTIVE`.

## 3. The first run reserves an address and leaves each certificate `PROVISIONING` — by design

Run `create-edge.sh` (`components=edge`, or anything that includes it) against a project with no
edge yet, and it ends with every certificate reporting `PROVISIONING`. **That is the passing
outcome for a first run, not a failure to fix.** A Google-managed certificate validates by DNS: it
polls the hostname its `--domains` list names, waiting for an A record that resolves to the
certificate's own load balancer. That address cannot exist before this run reserves it — so on
the run that reserves it, no A record naming it can exist yet either, and the certificate has
nothing to validate against. `create-edge.sh` treats `PROVISIONING` (and the transient
`PROVISIONING_FAILED_TEMPORARILY`) as success and logs the address anyway; only a state that does
not resolve itself (`FAILED*`) stops the script. Each hostname is judged on its own certificate
(§2.1), so `console` at `ACTIVE` beside `admin` at `PROVISIONING` is a passing run that says so on
two lines.

**What the operator owes it:** the two A records, pointed at the address the run just printed.
The deploy workflow now does this printing for you — see §5.

## 4. `PROVISIONING` becomes `ACTIVE` on its own

Once both A records exist and have propagated, Google's own validation polling picks them up
without anything further from this repository. Expect **roughly 15–60 minutes** between an A
record landing and its certificate reporting `ACTIVE`; the two hostnames now get there separately.
Re-running `components=edge` at any point is safe and cheap. Nothing is deleted or recreated: the
address, the NEGs, the backend services, the certificates, the proxies, the forwarding rules and
the HTTP redirect map are all describe-then-create-only-if-absent, so a re-run against an
already-`ACTIVE` certificate reads its state back and leaves it alone. Two exceptions, both writes
rather than creations:

- The **serving URL map** is written by an unconditional full-state `url-maps import`, which issues
  an Update on every run. The content is identical, so the routing never changes, but the API call
  is a write and the resource's update timestamp moves; a re-run is not literally a no-op.
- The **HTTPS proxy's certificate list** is read back and updated when it disagrees with the
  hostnames this deployment declares (§2.2). Comparison is by set, not by literal string, so a
  proxy already carrying the right certificates is left untouched however they happen to be
  ordered — a converged edge issues no `create`, no `delete` and no proxy update.

Use a re-run to check progress rather than to force it; nothing about the check itself makes
validation happen sooner.

## 5. Where the address is: the run summary, not an 18-stage log

A deploy that reconciled the edge writes a step named **"Surface the edge address for DNS"** into
the run's own summary (`$GITHUB_STEP_SUMMARY`) — a table of hostname → A record → address, and the
certificate states, generated from `create-edge.sh`'s own `edge_ip` / `edge_cert_state` outputs.
`edge_cert_state` now carries **one `hostname=STATE` pair per declared hostname**, space-separated
(`dev.console.hexera.ai=ACTIVE dev.admin.hexera.ai=PROVISIONING`), because there is no longer a
single state to report — the states being separable is the point (§2.1). That step runs only when
`edge_ip` is non-empty, i.e. only when this run's `deploy` step
actually reconciled an edge with at least one hostname declared — a run whose components never
touched the edge leaves no table rather than an empty one. Both environments now pin
`CONSOLE_DOMAIN`/`ADMIN_DOMAIN`, so the first `v*` tag is expected to print this table for prod
exactly as every dev deploy has since Task 4 — with both certificates reporting `PROVISIONING` until
an operator points DNS at the address it prints (§3). Before this table existed, the address was
findable only by reading the deploy step's raw log output, which is measured in tens of minutes
and dozens of stages for the same run.

## 6. Why the script reuses the address instead of recreating it

**A recreated forwarding rule is handed a new address.** GCP does not let a forwarding rule keep
its IP across a delete-and-recreate; the replacement gets whatever the pool hands out next, which
is essentially never the one just released. Any DNS record still pointing at the old address then
resolves to nothing. `create-edge.sh`'s `_ensure` helper describes every resource — the address
first among them — before ever creating it, so a second and every subsequent run reads the same
reserved IP back rather than replacing it. This is the one rule in the script everything else is
structured around: recreating is not an alternate code path that a flag turns on, it does not
exist here at all.

## 7. IAP stays on Cloud Run, not the load balancer

The admin console's only gate is IAP, enabled directly on the `dev-admin`/`prod-admin` Cloud Run
service (`docs/deployment/admin-console-access.md`) — not on the load balancer this document
describes. `create-edge.sh` deliberately touches no `--iap` flag on anything it creates: fronting a
service with a load balancer changes how a request *reaches* Cloud Run, not what Cloud Run does
with it once it arrives, and IAP already authenticates every path in — the auto-assigned
`*.run.app` URL and any load-balancer URL alike, per Google's own documentation cited in
`admin-console-access.md` §1. So each admin hostname is protected on both paths it can be reached
by, by the same one binding, and this document adds no second gate for it to drift out of sync
with.

**Prod's binding is not live yet.** IAP on Cloud Run is gated on an OAuth consent screen — the
"brand" — existing for the project, and `hexera-prod` does not have one. Creating it used to be
scriptable through the IAP OAuth Admin APIs; Google shut those down in March 2026, so the brand now
has to be created by hand, once, in the Cloud Console, before `--iap` against `prod-admin` will
succeed. Nothing in `deploy.yml` or `create-admin-service.sh` can substitute for this — it is a
one-time manual prerequisite, the same class of gap the service accounts and secret containers
were before this document's §1 was updated, not a bug to fix in either script.

## 8. What the first prod release still leaves outstanding

- **The IAP OAuth brand for `hexera-prod`.** §7 above. Until a human creates it, `prod-admin` is
  reachable at its load-balancer hostname without the gate this whole document exists to describe
  — check IAP's own enforcement state on the service before treating the console as protected.
- **DNS for `console.hexera.ai` and `admin.hexera.ai`.** §3 above applies to prod's first release
  exactly as it always has to dev's: each hostname's certificate will sit at `PROVISIONING`
  until an operator reads the address the run prints (§5) and points both A records at it.

## 9. What this does not cover

- **Cloud CDN, Cloud Armor, or path-based routing.** The URL map here routes by host only — one
  path matcher per hostname, each with a single default backend.
- **A hostname for the product API.** `<env>-api` is unaffected by any of this.
