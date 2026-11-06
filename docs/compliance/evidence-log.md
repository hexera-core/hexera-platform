# Evidence log

Append-only. One entry per control operation, written **when it happens**, with the date it
happened. Never backdate an entry; never edit a past entry except to add a dated correction below
it. Link the supporting artefact (workflow run, export, ticket) rather than pasting it.

| Date | Control | What was done | By | Evidence |
|---|---|---|---|---|
| 2026-11-04 | CC6.6 signup abuse controls | configure-identity-platform.sh applied to hexera-dev: reCAPTCHA Enterprise AUDIT (block below 0.3), email-enumeration protection, 8-char password floor | pranav@hexera.ai (via agent) | Identity Platform config, hexera-dev |
| 2026-11-04 | CC7.2 audit trail storage | configure-audit-log-bucket.sh applied to hexera-dev: bucket hexera-audit (400-day retention, unlocked) + sink on kind=audit/admin-action | pranav@hexera.ai (via agent) | gcloud logging buckets describe hexera-audit --location=global |
| 2026-11-04 | C1.2 / P4.3 erasure, CC6.6 grant-once | Live verification on dev-pranav (13/13): unverified sign-in provisions nothing; verified sign-in provisions org + one 100-credit grant; +tag alias and erase-and-resignup get none; export excludes worker token; erasure revokes and kills the signed session; audit lines redacted in Cloud Logging | pranav@hexera.ai (via agent) | PR #404 verification transcript |
