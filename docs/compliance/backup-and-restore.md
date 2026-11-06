# Backup and restore

Production Cloud SQL (`hexera-prod-pg`) has scheduled backups, point-in-time recovery, deletion
protection and no public IP; `create-data-tier.sh` reports any of those missing. Object storage
holds uploads (purged after 30 days) and results.

## Restore test — semi-annual

1. Clone to a scratch instance at a recent point in time:
   `gcloud sql instances clone hexera-prod-pg restore-test-<date> --point-in-time=<RFC3339> --project hexera-prod`
2. Connect and check row counts for `organizations`, `simulation_jobs`, `credit_ledger` against
   production at that time; run `alembic current` and confirm the expected head.
3. **Re-apply erasures**: for every organisation with `erasure_requested_at` later than the restore
   point, re-run the erasure. A restore must never resurrect someone who asked to be deleted.
4. Delete the scratch instance.
5. Record in the evidence log: date, restore point, time to restore, checks passed, operator.
