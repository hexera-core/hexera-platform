# Record of processing activities (GDPR Article 30)

Hexera acts as **controller** for account, billing and fraud-prevention data, and as **processor**
for the geometry, conversations and results a customer submits to run their jobs. Review this
record whenever a processing purpose, data category or subprocessor changes, and at least yearly.

Controller contact and, if appointed, the EU representative (Article 27) and DPO: *to be filled
by the company before publication.*

| # | Activity | Role | Data subjects | Personal data | Purpose | Lawful basis | Recipients | Retention |
|---|---|---|---|---|---|---|---|---|
| 1 | Account and sign-in | Controller | Customer users | Email, display name, Identity Platform uid, verification and login timestamps | Provide the service, authenticate | Contract (6(1)(b)) | Google Identity Platform | Until erasure |
| 2 | Abuse prevention at sign-up | Controller | Sign-up applicants | reCAPTCHA risk signals (held by Google); SHA-256 digest of normalised email | Stop automated sign-ups and repeat free-credit claims | Legitimate interest (6(1)(f)) | Google reCAPTCHA Enterprise | Digest: indefinitely, survives erasure (see retention schedule); reCAPTCHA: per Google terms |
| 3 | Running customer jobs | Processor | Customer users and anyone named in their files | Uploaded geometry, conversation text, job metadata, results | Perform the contracted meshing service | Customer's instructions (Art. 28) | Google Cloud; model providers (subprocessors.md) | Uploads 30 days; rest until erasure |
| 4 | Billing | Controller | Paying customers | Stripe customer id, plan, subscription status, credit ledger | Charge for the service, keep accounting records | Contract; legal obligation (6(1)(c)) | Stripe | Statutory accounting period (see retention schedule) |
| 5 | Security audit trail | Controller | Users, operators | Opaque user and key ids, organisation id, action, outcome | Detect and investigate misuse | Legitimate interest (6(1)(f)) | Google Cloud Logging | 400 days |
| 6 | Operational logs | Controller | Users | Request metadata, client address in transit | Operate and debug the service | Legitimate interest (6(1)(f)) | Google Cloud Logging | 30 days (_Default bucket) |
| 7 | Training corpus (off by default) | Controller | Customer users | Redacted run data | Improve the service | Consent or contract term — must be decided before enabling `DATA_COLLECTION_ENABLED` | — | Not collected unless enabled |
| 8 | Outreach (admin console) | Controller | Business prospects | Name, work email, company, message history | B2B sales outreach | Legitimate interest (6(1)(f)); suppression list honoured | Email provider | Until objection, then suppressed |

## Security measures (Article 32)

See [control-matrix.md](control-matrix.md) and `docs/deployment/security-and-privacy.md`.

## International transfers (Chapter V)

All hosting is in the United States (`us-central1`). Transfers rely on the EU Standard Contractual
Clauses incorporated in each subprocessor's data processing terms, and on the EU–US Data Privacy
Framework where the recipient is certified. Record each subprocessor's mechanism in
[subprocessors.md](subprocessors.md).
