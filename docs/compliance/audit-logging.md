# Audit logging

`src/meshpipeline/application/audit.py` writes one JSON line per security-relevant decision to
stdout. Cloud Run ingests it into Cloud Logging with the label `kind=audit`; the admin console's
lines carry `kind=admin-action`. `deploy/gcp/scripts/configure-audit-log-bucket.sh` routes both into
the `hexera-audit` bucket, kept 400 days and, once locked, impossible to shorten or delete.

## Events

| Action | Outcomes | Written by |
|---|---|---|
| `auth.session` | success, refused (`invalid_token`, `link_refused`, `signup_disabled`); success carries `verified` and `provisioned` | `api/auth.py` |
| `account.unverified_registration_replaced` | success | `account_service` |
| `credits.signup_grant` | success, withheld (`withheld_domain`, `already_claimed`) | `credit_service` |
| `api_key.created`, `api_key.revoked` | success | `api/v1/api_keys.py` |
| `admin.authenticate` | refused | `api/v1/admin_billing.py` |
| `admin.credit_grant` | success | `api/v1/admin_ops.py` |
| `privacy.export` | success | `privacy_service` |
| `privacy.erasure_requested`, `privacy.erasure_completed` | success | `privacy_service` |
| admin console actions | — | `apps/admin-console/src/lib/audit.ts` |

## What a line never contains

Email addresses, tokens, passwords, API keys or signatures: the `actor` is an opaque id and the
detail is scrubbed by key name. The trail outlives erasure, so it must not identify a person by
anything but an id that erasure has already removed from the database.

## Setting it up

```bash
GCP_PROJECT_ID=hexera-prod bash deploy/gcp/scripts/configure-audit-log-bucket.sh
gcloud logging read 'labels.kind="audit"' --bucket=hexera-audit --location=global \
  --view=_AllLogs --limit 5 --project hexera-prod    # confirm entries arrive
GCP_PROJECT_ID=hexera-prod AUDIT_BUCKET_LOCK=true bash deploy/gcp/scripts/configure-audit-log-bucket.sh
```

Record the lock date in the evidence log. Suggested alerts (Cloud Monitoring log-based metrics):
any `admin.authenticate` refusal; more than 50 `auth.session` refusals in 5 minutes; more than 20
`credits.signup_grant` withheld in an hour.
