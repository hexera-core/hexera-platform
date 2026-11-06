# SOC 2 control matrix

Maps implemented controls to the 2017 Trust Services Criteria (revised points of focus, 2022).
**Status** is one of: *Operating* (implemented and producing evidence), *Implemented* (in code or
config, evidence starts at production deploy), *Procedure* (written, not yet performed), *Gap*.

Owner roles: **Eng** = engineering lead, **Ops** = whoever holds production access, **Founder** =
company officer accountable for the program. Assign named people before an audit window opens.

## Security (Common Criteria)

| TSC | Control | Implementation | Owner | Evidence | Frequency | Status |
|---|---|---|---|---|---|---|
| CC1.1–1.5 | Security responsibilities are assigned and acknowledged | This matrix; named owners | Founder | Signed policy acknowledgement | Annual + on hire | Gap: no HR policies or acknowledgements yet |
| CC2.2 | Customers are told how their data is handled | Privacy notice, retention schedule | Founder | Published notice, version history | On change | Gap: public privacy notice to be published from records-of-processing.md |
| CC3.2 | Risks are identified and assessed | Risk table in this PR's description; this matrix's gaps | Founder | Dated risk register | Annual | Procedure |
| CC5.3 | Policies are deployed through procedures | docs/compliance/* | Eng | Git history | On change | Implemented |
| CC6.1 | Logical access uses unique identities and verified credentials | Identity Platform ID tokens verified server-side (`contracts/firebase_token.py`); API keys hashed (`api_keys.key_hash`); HMAC-signed console identity (`USER_TOKEN_SECRET`) | Eng | Code, config | Continuous | Implemented |
| CC6.1 | Tenant isolation | `tenant_scope.scope` on every read; integration suite `test_owner_isolation_matrix.py` | Eng | CI results | Every change | Operating |
| CC6.1 | Cross-tenant admin access needs its own credential | `ADMIN_API_KEY`, constant-time compare, admin console behind IAP | Eng | Config, audit trail | Continuous | Implemented |
| CC6.2 | New accounts are provisioned only after the address is verified | `account_service` step 4; `spend_gate` refuses organisation-less accounts | Eng | Audit `account.provisioned` | Continuous | Implemented |
| CC6.3 | Access is removed when no longer needed | API key revocation; account erasure revokes all keys | Eng | Audit `api_key.revoked`, `privacy.erasure_requested` | On event | Implemented |
| CC6.3 | Production access is reviewed | [access-review.md](access-review.md) | Ops | Signed review record in evidence log | Quarterly | Procedure |
| CC6.6 | Boundary protection | Cloud Run ingress, internal `/auth/session` gate (`MESH_API_KEY`), CORS allow-list, rate limits (`RATE_LIMIT_*`) | Eng | Config | Continuous | Implemented |
| CC6.6 | Automated abuse is resisted at sign-up | reCAPTCHA Enterprise + email-enumeration protection (`configure-identity-platform.sh`); one grant per normalised mailbox (`signup_claims`); disposable domains withheld | Eng | Identity Platform config, audit `credits.signup_grant` | Continuous | Implemented (reCAPTCHA starts in AUDIT) |
| CC6.7 | Data in transit is encrypted | HTTPS-only Cloud Run; Cloud SQL TLS required (`create-data-tier.sh`) | Eng | Config | Continuous | Implemented |
| CC6.7 | Data at rest is encrypted | Google-managed encryption for Cloud SQL, GCS, Memorystore | Eng | GCP documentation | Continuous | Implemented (provider) |
| CC6.8 | Untrusted code is contained | `run_python` seccomp/Landlock sandbox; OpenFOAM directive rejection (security-and-privacy.md) | Eng | Tests | Every change | Operating |
| CC7.1 | Vulnerabilities are detected | CodeQL, dependency audit, workflow audit in CI ([vulnerability-management.md](vulnerability-management.md)) | Eng | CI results | Every change | Operating |
| CC7.2 | Security events are logged | Structured audit trail ([audit-logging.md](audit-logging.md)) | Eng | Audit bucket | Continuous | Implemented |
| CC7.2 | Audit records are tamper-resistant | Dedicated, retention-locked log bucket (`configure-audit-log-bucket.sh`) | Ops | Bucket config | Continuous | Implemented (lock is an operator step) |
| CC7.3–7.5 | Incidents are handled and communicated | [incident-response.md](incident-response.md) | Founder | Incident records | On event + annual tabletop | Procedure |
| CC8.1 | Changes are reviewed, tested and approved | PR review, CI gates, prod only from a `v*` tag with approval ([change-management.md](change-management.md)) | Eng | GitHub PRs, workflow runs | Every change | Operating |
| CC9.2 | Vendors are assessed | [subprocessors.md](subprocessors.md) | Founder | DPA copies, review dates | Annual | Procedure |

## Availability

| TSC | Control | Implementation | Owner | Evidence | Frequency | Status |
|---|---|---|---|---|---|---|
| A1.2 | Data is backed up | Cloud SQL scheduled backups + PITR, deletion protection (prod) | Ops | `gcloud sql backups list` | Daily (automatic) | Operating (prod) |
| A1.3 | Recovery is tested | [backup-and-restore.md](backup-and-restore.md) | Ops | Restore test record | Semi-annual | Procedure |
| A1.1 | Capacity is monitored | Worker autoscaling, queue-depth publisher | Eng | Cloud Monitoring | Continuous | Implemented |

## Confidentiality

| TSC | Control | Implementation | Owner | Evidence | Frequency | Status |
|---|---|---|---|---|---|---|
| C1.1 | Confidential data is identified and retained only as needed | [retention-schedule.md](retention-schedule.md); upload purge (`UPLOAD_RETENTION_DAYS`) | Eng | Sweep logs | Hourly | Operating |
| C1.2 | Confidential data is disposed of | Account erasure + erasure sweep (`privacy_service`) | Eng | Audit `privacy.erasure_completed` | On request | Implemented |
| C1.1 | Customer data is not used for training by default | `DATA_COLLECTION_ENABLED=false` by default | Eng | Config | Continuous | Implemented |

## Processing integrity

| TSC | Control | Implementation | Owner | Evidence | Frequency | Status |
|---|---|---|---|---|---|---|
| PI1.3 | Billing is accurate and idempotent | Append-only credit ledger; deterministic grant ids; Stripe webhook dedupe (`stripe_events`) | Eng | Ledger, tests | Continuous | Operating |

## Privacy

| TSC | Control | Implementation | Owner | Evidence | Frequency | Status |
|---|---|---|---|---|---|---|
| P1.1 | Notice is given | Privacy notice | Founder | Published notice | On change | Gap |
| P4.2 | Personal data is retained only as long as needed | [retention-schedule.md](retention-schedule.md) | Eng | Sweep logs | Continuous | Implemented |
| P5.1 | Subjects can access their data | `GET /api/v1/account/export`, console Settings → Account | Eng | Audit `privacy.export` | On request | Implemented |
| P4.3 | Subjects can have their data deleted | `POST /api/v1/account/erasure` + sweep | Eng | Audit `privacy.erasure_*` | On request | Implemented |
| P6.1 | Disclosures to third parties are limited and recorded | [subprocessors.md](subprocessors.md) | Founder | Register | Annual | Procedure |
| P6.3 | Breaches are notified | [incident-response.md](incident-response.md) | Founder | Incident records | On event | Procedure |

## Known gaps (not addressed in code)

- EU data residency (EU region deployment).
- HR controls: background checks, security training, policy acknowledgements, offboarding checklist.
- Endpoint security for staff laptops (disk encryption, screen lock, MDM).
- Formal risk assessment and vendor risk reviews performed and dated.
- Public privacy notice and DPA template for customers.
- Selecting an auditor and a compliance automation tool, and starting the observation window.
