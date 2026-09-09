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
backend service, a URL map that routes by host, and a Google-managed certificate covering the
declared hostnames. `create-edge.sh` assembles exactly that, once, for both consoles — one address
and one certificate serving two host-routed backends, not two of each.

## 3. The first run reserves an address and leaves the certificate `PROVISIONING` — by design

Run `create-edge.sh` (`components=edge`, or anything that includes it) against a project with no
edge yet, and it ends with the certificate reporting `PROVISIONING`. **That is the passing
outcome for a first run, not a failure to fix.** A Google-managed certificate validates by DNS: it
polls the hostnames its `--domains` list names, waiting for an A record that resolves to the
certificate's own load balancer. That address cannot exist before this run reserves it — so on
the run that reserves it, no A record naming it can exist yet either, and the certificate has
nothing to validate against. `create-edge.sh` treats `PROVISIONING` (and the transient
`PROVISIONING_FAILED_TEMPORARILY`) as success and logs the address anyway; only a state that does
not resolve itself (`FAILED*`) stops the script.

**What the operator owes it:** the two A records, pointed at the address the run just printed.
The deploy workflow now does this printing for you — see §5.

## 4. `PROVISIONING` becomes `ACTIVE` on its own

Once both A records exist and have propagated, Google's own validation polling picks them up
without anything further from this repository. Expect **roughly 15–60 minutes** between the A
records landing and the certificate reporting `ACTIVE`. Re-running `components=edge` at any point
is safe and cheap — every resource in `create-edge.sh` is describe-then-create-only-if-absent, so
a re-run against an already-`ACTIVE` certificate reads its state back and changes nothing. Use a
re-run to check progress rather than to force it; nothing about the check itself makes validation
happen sooner.

## 5. Where the address is: the run summary, not a 17-stage log

A deploy that reconciled the edge writes a step named **"Surface the edge address for DNS"** into
the run's own summary (`$GITHUB_STEP_SUMMARY`) — a table of hostname → A record → address, and the
certificate's current state, generated from `create-edge.sh`'s own `edge_ip` / `edge_cert_state`
outputs. That step runs only when `edge_ip` is non-empty, i.e. only when this run's `deploy` step
actually reconciled an edge with at least one hostname declared — a run whose components never
touched the edge leaves no table rather than an empty one. Both environments now pin
`CONSOLE_DOMAIN`/`ADMIN_DOMAIN`, so the first `v*` tag is expected to print this table for prod
exactly as every dev deploy has since Task 4 — with the certificate reporting `PROVISIONING` until
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
  exactly as it always has to dev's: the certificate will sit at `PROVISIONING` until an operator
  reads the address the run prints (§5) and points both A records at it.

## 9. What this does not cover

- **Cloud CDN, Cloud Armor, or path-based routing.** The URL map here routes by host only — one
  path matcher per hostname, each with a single default backend.
- **A hostname for the product API.** `<env>-api` is unaffected by any of this.
