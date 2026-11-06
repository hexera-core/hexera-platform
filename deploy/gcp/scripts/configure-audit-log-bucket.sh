#!/usr/bin/env bash
# Responsibility: Route every audit line the API and admin console write into a dedicated, retention-bound log bucket.
# Owns: the audit bucket, its retention, its optional lock, and the sink that fills it.
# Boundaries: project-wide, so it is an OPERATOR step, not a deploy stage. What is written to the trail
#             is src/meshpipeline/application/audit.py and apps/admin-console/src/lib/audit.ts.

# Provision the AUDIT TRAIL's storage. Idempotent: an existing bucket and sink are updated in place.
#
#   GCP_PROJECT_ID=hexera-prod bash deploy/gcp/scripts/configure-audit-log-bucket.sh
#   GCP_PROJECT_ID=hexera-prod AUDIT_BUCKET_LOCK=true bash deploy/gcp/scripts/configure-audit-log-bucket.sh
#
# WHY A BUCKET OF ITS OWN. The _Default bucket keeps 30 days and any project owner can shorten it.
# A dedicated bucket keeps the trail for AUDIT_RETENTION_DAYS (default 400 - a SOC 2 observation
# window plus margin), and once LOCKED its retention can be neither shortened nor the bucket deleted
# by anyone, including us. That lock is what makes the trail tamper-resistant; application identities
# hold no logging.buckets permission at all.
#
# THE LOCK IS IRREVERSIBLE, so it is never the default: run once unlocked, confirm entries arrive
# (docs/compliance/audit-logging.md), then re-run with AUDIT_BUCKET_LOCK=true.
#
# INPUTS   GCP_PROJECT_ID; optional AUDIT_BUCKET (default hexera-audit), AUDIT_RETENTION_DAYS
#          (default 400), AUDIT_BUCKET_LOCK (default false)
# MUTATES  one log bucket and one log sink in the project.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require_vars GCP_PROJECT_ID

BUCKET="${AUDIT_BUCKET:-hexera-audit}"
RETENTION="${AUDIT_RETENTION_DAYS:-400}"
SINK="${BUCKET}-sink"
# The two producers' labels: the API's audit lines and the admin console's action lines.
FILTER='labels.kind="audit" OR labels.kind="admin-action"'

if gc logging buckets describe "${BUCKET}" --location=global >/dev/null 2>&1; then
  info "Updating audit bucket ${BUCKET} (retention ${RETENTION} days)"
  gc logging buckets update "${BUCKET}" --location=global --retention-days="${RETENTION}"
else
  info "Creating audit bucket ${BUCKET} (retention ${RETENTION} days)"
  gc logging buckets create "${BUCKET}" --location=global --retention-days="${RETENTION}" \
    --description="Hexera audit trail - see docs/compliance/audit-logging.md"
fi

DESTINATION="logging.googleapis.com/projects/${GCP_PROJECT_ID}/locations/global/buckets/${BUCKET}"
if gc logging sinks describe "${SINK}" >/dev/null 2>&1; then
  gc logging sinks update "${SINK}" "${DESTINATION}" --log-filter="${FILTER}"
else
  gc logging sinks create "${SINK}" "${DESTINATION}" --log-filter="${FILTER}"
fi
log "sink ${SINK} -> ${BUCKET}  filter: ${FILTER}"

if [ "${AUDIT_BUCKET_LOCK:-false}" = "true" ]; then
  confirm "Lock ${BUCKET}? Its retention can never be reduced and it cannot be deleted before entries expire."
  gc logging buckets update "${BUCKET}" --location=global --locked
  log "bucket ${BUCKET} is LOCKED"
else
  warn "bucket ${BUCKET} is not locked - re-run with AUDIT_BUCKET_LOCK=true once entries are confirmed"
fi
