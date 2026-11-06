# Incident response

## Severity

| Sev | Example | Response |
|---|---|---|
| 1 | Customer data exposed, credential leaked, production down | Page immediately; Founder informed within 1 hour |
| 2 | Suspected unauthorised access, abuse wave, partial outage | Same business day |
| 3 | Vulnerability with no evidence of exploitation | Normal change process, within patch SLA |

## Steps

1. **Declare.** Open an incident record (date/time, reporter, summary). Name an incident lead.
2. **Contain.** Rotate the affected secret (Secret Manager, then redeploy the tier), revoke API
   keys, close signup (`CONSOLE_SIGNUP_ENABLED=false` and redeploy the API), or scale the service
   to zero as appropriate.
3. **Investigate.** Query the audit bucket:
   `gcloud logging read 'labels.kind="audit"' --bucket=hexera-audit --location=global --view=_AllLogs`.
   Preserve findings in the incident record.
4. **Assess the GDPR clock.** If personal data was accessed, altered, lost or disclosed without
   authorisation, it is a personal data breach. **The supervisory authority must be notified
   within 72 hours of becoming aware** (Article 33) unless the breach is unlikely to risk
   individuals' rights. As processor, notify affected customers (controllers) without undue delay
   so they can meet their own clock. Notify individuals directly when the risk is high (Article 34).
5. **Recover.** Restore service; if data was lost, follow backup-and-restore.md.
6. **Review.** Within 10 business days: root cause, timeline, what fixed it, follow-up actions.
   Record the review and every notification in the evidence log.

## Contacts

Fill in before use: incident lead rota, Founder phone, lead supervisory authority, customer
security contacts, Google Cloud support case link, Stripe support.

## Exercise

Run one tabletop exercise per year and record it in the evidence log.
