# Retention schedule

| Data | Where | Kept for | Deleted by |
|---|---|---|---|
| Uploaded geometry bytes | Object store | `UPLOAD_RETENTION_DAYS` (30) after upload, once no active job needs them | `purge-expired-geometry-sources` sweep (hourly) |
| Job records, results metadata | Postgres | Life of the account | Account erasure |
| Result artifacts (meshes, reports) | Object store | Life of the account | Erasure sweep |
| Failed job workspaces | Worker disk | `FAILED_JOB_RETENTION_HOURS` (24) | Worker / workspace purge |
| Conversations | Postgres `chat_sessions` | Life of the account | Deleted at erasure request |
| Account profile | Postgres `users`, Identity Platform | Life of the account | Erasure request (DB); console deletes the Identity Platform user |
| API keys | Postgres `api_keys` (hash only) | Life of the account | Revoked at erasure; row pseudonymised |
| Credit ledger, Stripe customer id, plan | Postgres `credit_ledger`, `organizations` | Statutory accounting period (confirm per jurisdiction; typically 6–10 years) | Kept through erasure; organisation renamed |
| Signup-grant claim digest | Postgres `signup_grant_claims` | Indefinitely | Not deleted; it is a one-way digest kept to stop repeat free-credit claims (legitimate interest). Review yearly. |
| Audit trail | Cloud Logging bucket `hexera-audit` | 400 days | Bucket retention |
| Operational logs | Cloud Logging `_Default` | 30 days | Bucket retention |
| Job event replay log | Redis | `EVENT_LOG_TTL_SECONDS` (24 h) | TTL |
| Database backups | Cloud SQL | `CLOUDSQL_RETAINED_BACKUPS` backups + PITR window | Rotation. An erased account remains in backups until they rotate out; a restore must re-apply pending erasures (see backup-and-restore.md). |
| Training corpus | `CORPUS_DIR` | Not collected (`DATA_COLLECTION_ENABLED=false`) | — |

**Legal hold.** If litigation or a regulator requires data to be preserved, the Founder records
the hold in the evidence log and suspends erasure for the named organisation by not running the
request (and telling the requester why, where lawful). There is no in-product hold switch.
