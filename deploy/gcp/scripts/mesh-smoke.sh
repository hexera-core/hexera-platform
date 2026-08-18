#!/usr/bin/env bash
# Responsibility: Submit one real job to the deployed mesh job and report what came back.
# Boundaries: the only script here that mutates anything remote, so it refuses to run without stated authority.

# Submit ONE real job to the deployed mesh Cloud Run Job and report what came back.
#
# This is the only script here that mutates anything remote, so it refuses to run without an
# explicit statement of authority: rendering and validation must stay safe to run anywhere.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=lib.sh
source ./scripts/lib.sh
load_env

JOB_ID="${1:-}"
[ -n "${JOB_ID}" ] || die "usage: make smoke JOB_ID=<uuid> (a job already prepared by the local application)"
[ "${MESH_SMOKE_AUTHORIZED:-}" = "yes" ] || die \
  "refusing to submit a real mesh job. This spends money and mutates ${CLOUDRUN_JOB:-<unset>}.
   Re-run with MESH_SMOKE_AUTHORIZED=yes once you intend that."

require_vars GCP_PROJECT_ID GCP_REGION CLOUDRUN_JOB GCP_MESH_BUCKET

say "submitting ${JOB_ID} to ${CLOUDRUN_JOB} in ${GCP_REGION}"
gcloud run jobs execute "${CLOUDRUN_JOB}" \
  --project "${GCP_PROJECT_ID}" --region "${GCP_REGION}" \
  --args="--job-id=${JOB_ID}" --wait
say "execution finished; the local application reconciles the result from gs://${GCP_MESH_BUCKET}"
