# Compliance readiness

This directory is Hexera's working record for SOC 2 and GDPR **readiness**. It is not a
certification, an attestation or a legal opinion, and nothing in it may be quoted to a customer as
one. A SOC 2 report is issued by an independent CPA firm after it has tested controls over an
observation period; GDPR compliance is an ongoing obligation, not a status.

## The rule about dates

**Evidence starts when a control actually operates.** No record in this directory, in the
evidence log, or in any system it points to may be created, edited or dated to suggest a control
operated before it did. Where an earlier period is genuinely covered by records that already
exist — Cloud Audit Logs, Git history, Cloud SQL backup history, Stripe records — those records may
be cited for that period *as they are*. Nothing is reconstructed after the fact.

The controls added in this change begin operating on the date they are deployed to production.
That date is the first entry in [`evidence-log.md`](evidence-log.md), written when it happens.

## Contents

| Document | Purpose |
|---|---|
| [control-matrix.md](control-matrix.md) | SOC 2 Trust Services Criteria → control → owner → evidence → frequency → gap |
| [records-of-processing.md](records-of-processing.md) | GDPR Article 30 record of processing activities |
| [subprocessors.md](subprocessors.md) | Every third party that processes customer data, and the transfer basis |
| [retention-schedule.md](retention-schedule.md) | How long each class of data is kept, and what deletes it |
| [dsar-runbook.md](dsar-runbook.md) | Access, export, rectification and erasure requests |
| [incident-response.md](incident-response.md) | Security incidents and the GDPR 72-hour breach clock |
| [audit-logging.md](audit-logging.md) | What the audit trail records and where it is kept |
| [access-review.md](access-review.md) | Quarterly review of who can reach production |
| [backup-and-restore.md](backup-and-restore.md) | Backup configuration and the restore test |
| [vulnerability-management.md](vulnerability-management.md) | Dependency, code and image scanning, and patch timelines |
| [change-management.md](change-management.md) | How a change reaches production |
| [evidence-log.md](evidence-log.md) | Dated record of each control operation, appended as it happens |

## Hosting decision

Customer data is hosted in Google Cloud `us-central1`. European customers are served under the
EU Standard Contractual Clauses in Google's and each subprocessor's data processing terms (see
[subprocessors.md](subprocessors.md)). EU data residency is a later infrastructure project and is
listed as a gap in the control matrix.
