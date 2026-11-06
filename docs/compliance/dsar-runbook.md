# Data-subject request runbook

GDPR gives one month to respond (Article 12(3)), extendable by two months for complex requests
with notice. Log every request in the evidence log: received date, type, identity check, completed
date. Never log the requester's email in the audit trail.

## Self-service (preferred)

- **Access / portability:** Console → Settings → Account → *Download my data*
  (`GET /api/v1/account/export`, JSON). Stored files are listed by key and downloadable from the
  console while the account exists.
- **Erasure:** Console → Settings → Account → *Delete my account*
  (`POST /api/v1/account/erasure`). Immediately: API keys revoked, conversations deleted, profile
  and memberships deleted, every `owner_id` pseudonymised to `erased:<organisation>`, organisation
  renamed. Within the next hourly sweep (after any running job ends): uploaded files and results
  deleted from object storage. The console then deletes the Identity Platform user.

## By email or support ticket

1. **Verify identity**: the request must come from the account's address, or the requester must
   sign in. Do not act on an unverified request.
2. **Access**: ask them to use self-service; if they cannot, run the export on their behalf and
   send it through an access-controlled link, not an attachment.
3. **Erasure**: if they cannot sign in, an operator signs in to the admin tooling, confirms
   ownership, and runs `privacy_service.request_erasure` for their organisation; then delete the
   Identity Platform user in the Google Cloud console.
4. **Refusals**: an active subscription must be cancelled first (billing would continue
   otherwise). A truncated export (`truncated_tables` non-empty) is completed by hand.
5. **Rectification**: display name is self-service; an email change requires a new account today.
6. **Objection / restriction**: the only legitimate-interest processing of customer data is the
   audit trail and the signup claim digest; explain both, and escalate to the Founder.
7. Record the completion date in the evidence log.

## What is not deleted, and the reply to say so

Billing and ledger records (legal obligation), the signup-claim digest (fraud prevention), audit
entries (security, 400 days) and backups until they rotate out. Tell the requester this explicitly.
