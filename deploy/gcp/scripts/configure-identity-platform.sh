#!/usr/bin/env bash
# Responsibility: Put the project's Identity Platform sign-up defences into the state this repository declares.
# Owns: reCAPTCHA Enterprise for email/password, email-enumeration protection, and the password policy.
# Boundaries: project-wide, so it is an OPERATOR step, not a deploy stage: every personal environment in
#             hexera-dev shares one Identity Platform config, and a personal deploy must not change it.

# Configure Identity Platform's abuse controls. Idempotent: it PATCHes the same fields every run.
#
#   GCP_PROJECT_ID=hexera-dev bash deploy/gcp/scripts/configure-identity-platform.sh
#   GCP_PROJECT_ID=hexera-prod IDP_RECAPTCHA_MODE=ENFORCE bash deploy/gcp/scripts/configure-identity-platform.sh
#
# WHAT EACH PART BUYS.
#   reCAPTCHA Enterprise  scores every email/password sign-up and sign-in; below the threshold the
#                         request is refused by Identity Platform before an account exists, which is
#                         the only layer that sees the request before our API does. AUDIT first,
#                         to read the score distribution, then ENFORCE (docs/deployment/identity-platform.md §2b).
#   enumeration protection makes "is this address registered?" unanswerable from the public API -
#                         the same discipline api/auth.py applies to its own refusals.
#   password policy       a floor of 8 characters, matching the console's form, enforced server-side.
#
# INPUTS   GCP_PROJECT_ID; optional IDP_RECAPTCHA_MODE (AUDIT|ENFORCE|OFF, default AUDIT) and
#          IDP_RECAPTCHA_BLOCK_BELOW (score, default 0.3)
# MUTATES  the project's Identity Platform config, and enables recaptchaenterprise.googleapis.com.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
require_vars GCP_PROJECT_ID

MODE="${IDP_RECAPTCHA_MODE:-AUDIT}"
case "${MODE}" in
  AUDIT|ENFORCE|OFF) ;;
  *) die "IDP_RECAPTCHA_MODE must be AUDIT, ENFORCE or OFF (got '${MODE}')" ;;
esac
BLOCK_BELOW="${IDP_RECAPTCHA_BLOCK_BELOW:-0.3}"

info "Enabling the reCAPTCHA Enterprise API on ${GCP_PROJECT_ID}"
gc services enable recaptchaenterprise.googleapis.com

BODY=$(cat <<JSON
{
  "emailPrivacyConfig": {"enableImprovedEmailPrivacy": true},
  "recaptchaConfig": {
    "emailPasswordEnforcementState": "${MODE}",
    "managedRules": [{"endScore": ${BLOCK_BELOW}, "action": "BLOCK"}],
    "useAccountDefender": false
  },
  "passwordPolicyConfig": {
    "passwordPolicyEnforcementState": "ENFORCE",
    "forceUpgradeOnSignin": false,
    "passwordPolicyVersions": [{"customStrengthOptions": {"minPasswordLength": 8}}]
  }
}
JSON
)
MASK="emailPrivacyConfig.enableImprovedEmailPrivacy,recaptchaConfig.emailPasswordEnforcementState,recaptchaConfig.managedRules,recaptchaConfig.useAccountDefender,passwordPolicyConfig"

info "Applying Identity Platform abuse controls (reCAPTCHA ${MODE}, block below ${BLOCK_BELOW})"
TOKEN="$(gcloud auth print-access-token)"
curl --fail-with-body -sS -X PATCH \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "x-goog-user-project: ${GCP_PROJECT_ID}" \
  -H "Content-Type: application/json" \
  "https://identitytoolkit.googleapis.com/admin/v2/projects/${GCP_PROJECT_ID}/config?updateMask=${MASK}" \
  --data "${BODY}" >/dev/null
log "Identity Platform config updated. Record the change in docs/compliance/evidence-log.md."
