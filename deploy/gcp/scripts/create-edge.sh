#!/usr/bin/env bash
# Responsibility: Provision the global external Application Load Balancer that gives the console
# and admin console their custom hostnames.
# Owns: the reserved static IP, one serverless NEG and backend service per Cloud Run service, the
# host-routed URL map, the Google-managed certificate, and the HTTP-to-HTTPS redirect.
# Boundaries: it never deletes or recreates a live resource; DNS itself is the operator's, not this
# script's - it only prints what belongs in it.

# Provision the EDGE for the consoles. Idempotent and RECONCILING: every resource here is
# describe-then-create-only-if-absent, never recreated.
#
#   bash deploy/gcp/scripts/create-edge.sh
#
# WHY A LOAD BALANCER AT ALL. Cloud Run hands out no IP address of its own - a custom hostname
# means routing through a global external Application Load Balancer, which is what everything
# below assembles: a reserved global static IP, a serverless NEG per Cloud Run service (the bridge
# from a classic LB backend to a Cloud Run revision), a backend service per NEG, a URL map that
# routes by host, and a Google-managed certificate covering every declared hostname.
#
# WHY REUSE, NEVER RECREATE, IS THE ONE RULE THAT MATTERS HERE. A recreated forwarding rule is
# handed a NEW address - silently breaking any DNS that already pointed at the old one - and a
# recreated certificate goes back to PROVISIONING even if it was already ACTIVE. Every resource
# below is therefore described first and created only when that describe fails, through one
# `_ensure` helper rather than eight hand-rolled describe/create pairs, so the rule is expressed
# once.
#
# WHY A PROVISIONING CERTIFICATE IS A SUCCESS, NOT A FAILURE. A Google-managed certificate cannot
# validate until DNS points at the address this run may have just reserved - which cannot have
# happened before this run exists. So the FIRST run always leaves the certificate PROVISIONING.
# Failing on that would make this pipeline unusable on the day it is introduced; only a state that
# does not resolve itself (FAILED*) is treated as a failure.
#
# WHY THE HTTP REDIRECT IS ITS OWN URL MAP. Pointing the HTTP target proxy at the same URL map the
# HTTPS proxy uses would SERVE the app over plain HTTP rather than redirect it - the documentation
# examples that show one URL map shared between both proxies are misleading for that reason. A
# redirect is a URL map whose action is `defaultUrlRedirect`, which `gcloud` can only write by
# importing a small YAML document; there is no flag-only way to create one.
#
# WHY IAP IS NOT TOUCHED HERE. IAP already gates the admin console at the Cloud Run service itself
# (create-admin-service.sh); this script only gives both services a custom hostname to be reached
# through. No `--iap` flag belongs on anything below.
#
# INPUTS   the deployment env (CONSOLE_DOMAIN, ADMIN_DOMAIN, EDGE_IP_NAME, EDGE_URL_MAP, EDGE_CERT,
#          CLOUDRUN_CONSOLE_SERVICE, CLOUDRUN_ADMIN_SERVICE, GCP_REGION).
# MUTATES  a global static IP, a serverless NEG and backend service per declared console, two URL
#          maps (serving and redirect), a managed certificate, two target proxies, and two
#          forwarding rules. It deletes nothing.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

CONSOLE_DOMAIN="${CONSOLE_DOMAIN:-}"
ADMIN_DOMAIN="${ADMIN_DOMAIN:-}"

# A deployment may genuinely serve no custom hostname - Cloud Run's own *.run.app URL works fine
# without this tier. That is a skip, and it is stated, exactly as the admin and API tiers state
# theirs.
if [ -z "${CONSOLE_DOMAIN}" ] && [ -z "${ADMIN_DOMAIN}" ]; then
  info "No custom domain declared (CONSOLE_DOMAIN and ADMIN_DOMAIN both empty) - skipping the edge"
  log "set CONSOLE_DOMAIN and/or ADMIN_DOMAIN to give the consoles a custom hostname"
  exit 0
fi

require_vars EDGE_IP_NAME EDGE_URL_MAP EDGE_CERT

# HOST/SERVICE/LABEL triples, in a plain indexed array - bash 3.2 has no associative arrays.
# Console-then-admin order, matching the order the certificate's --domains list must agree with.
# A domain with no service behind it is refused here defensively; validate-config.sh already
# refuses it earlier in the pipeline, but this script does not rely on always running after it.
TRIPLES=()
if [ -n "${CONSOLE_DOMAIN}" ]; then
  require_vars CLOUDRUN_CONSOLE_SERVICE
  TRIPLES+=("${CONSOLE_DOMAIN}|${CLOUDRUN_CONSOLE_SERVICE}|console")
fi
if [ -n "${ADMIN_DOMAIN}" ]; then
  require_vars CLOUDRUN_ADMIN_SERVICE
  TRIPLES+=("${ADMIN_DOMAIN}|${CLOUDRUN_ADMIN_SERVICE}|admin")
fi

DOMAIN_LIST=()
for triple in "${TRIPLES[@]}"; do
  DOMAIN_LIST+=("${triple%%|*}")
done

info "Edge for $(IFS=,; printf '%s' "${DOMAIN_LIST[*]}") in ${GCP_PROJECT_ID}"

# _ensure LABEL  describe-args...  --  create-args...
# Runs the describe first; on success the resource is reused and nothing is mutated. Only when
# the describe fails does it create. This is the whole "reuse, never recreate" rule, expressed
# once and used for every resource below instead of being open-coded eight times. Sets
# ENSURE_DISPOSITION to "reused" or "created" so a caller that must not repeat a one-shot mutation
# (attaching a NEG to a backend service) can tell which happened.
_ensure() {
  local label="$1"; shift
  local -a describe_args=()
  local -a create_args=()
  local mode=describe a
  for a in "$@"; do
    if [ "${mode}" = describe ] && [ "${a}" = "--" ]; then
      mode=create
      continue
    fi
    if [ "${mode}" = describe ]; then describe_args+=("${a}"); else create_args+=("${a}"); fi
  done
  if gc "${describe_args[@]}" >/dev/null 2>&1; then
    log "  ${label}  (exists)"
    ENSURE_DISPOSITION=reused
    return 0
  fi
  info "Creating ${label}"
  gc "${create_args[@]}"
  ENSURE_DISPOSITION=created
}

# 1) THE STATIC IP. Reserved once; every subsequent run reads the same address back. Recreating
#    this is the one mistake that breaks DNS silently, because the new address differs from the
#    one an operator already pointed a hostname at.
_ensure "static IP ${EDGE_IP_NAME}" \
  compute addresses describe "${EDGE_IP_NAME}" --global -- \
  compute addresses create "${EDGE_IP_NAME}" --global --network-tier=PREMIUM --ip-version=IPV4

EDGE_IP_ADDRESS="$(gc compute addresses describe "${EDGE_IP_NAME}" --global \
  --format='value(address)')"
export EDGE_IP_ADDRESS
log "  address        ${EDGE_IP_ADDRESS}"

# 2) ONE SERVERLESS NEG AND BACKEND SERVICE PER CLOUD RUN SERVICE. The NEG is the bridge from a
#    classic load-balancer backend to a Cloud Run revision; the backend service is what the URL
#    map actually routes to.
BACKENDS=()
for triple in "${TRIPLES[@]}"; do
  host="${triple%%|*}"
  rest="${triple#*|}"
  service="${rest%%|*}"
  label="${rest#*|}"
  neg="${service}-neg"
  backend="${service}-be"

  _ensure "serverless NEG ${neg} (${label})" \
    compute network-endpoint-groups describe "${neg}" --region="${GCP_REGION}" -- \
    compute network-endpoint-groups create "${neg}" --region="${GCP_REGION}" \
      --network-endpoint-type=serverless --cloud-run-service="${service}"

  _ensure "backend service ${backend} (${label})" \
    compute backend-services describe "${backend}" --global -- \
    compute backend-services create "${backend}" --global \
      --load-balancing-scheme=EXTERNAL_MANAGED
  # Attaching the NEG is itself a one-shot create, not a describable property this fake (or real
  # gcloud, re-run against an already-attached NEG) can be asked about cheaply - so it runs only
  # the run that just created the backend service, never against one this run merely reused.
  if [ "${ENSURE_DISPOSITION}" = created ]; then
    gc compute backend-services add-backend "${backend}" --global \
      --network-endpoint-group="${neg}" --network-endpoint-group-region="${GCP_REGION}"
  fi

  BACKENDS+=("${host}|${backend}")
  log "  ${label} routed to ${backend} <- ${neg} <- ${service}"
done

# 3) THE SERVING URL MAP - one map, one host rule per hostname, each to its own backend. The
#    default service is the first declared backend (console, when present); every other hostname
#    reaches its backend only through its own host rule below.
default_backend="${BACKENDS[0]#*|}"
_ensure "URL map ${EDGE_URL_MAP}" \
  compute url-maps describe "${EDGE_URL_MAP}" --global -- \
  compute url-maps create "${EDGE_URL_MAP}" --default-service "${default_backend}"

for pair in "${BACKENDS[@]}"; do
  host="${pair%%|*}"
  backend="${pair#*|}"
  pm="${backend}-pm"
  # add-path-matcher adds-or-replaces the named path matcher and its host rule - an idempotent
  # update, not a create, so it runs every time rather than being gated by _ensure.
  gc compute url-maps add-path-matcher "${EDGE_URL_MAP}" \
    --path-matcher-name="${pm}" --default-service="${backend}" --new-hosts="${host}"
done

# 4) THE CERTIFICATE, covering exactly the declared hostnames, console-then-admin so its list
#    agrees with the order TRIPLES was built in.
CERT_DOMAINS="$(IFS=,; printf '%s' "${DOMAIN_LIST[*]}")"
_ensure "managed certificate ${EDGE_CERT}" \
  compute ssl-certificates describe "${EDGE_CERT}" --global -- \
  compute ssl-certificates create "${EDGE_CERT}" --domains="${CERT_DOMAINS}" --global

CERT_STATE="$(gc compute ssl-certificates describe "${EDGE_CERT}" --global \
  --format='value(managed.status)')"
case "${CERT_STATE}" in
  ACTIVE)        log "  certificate    ACTIVE" ;;
  PROVISIONING|PROVISIONING_FAILED_TEMPORARILY)
    # NOT A FAILURE. A managed certificate cannot validate until DNS points at the address this
    # run may have just reserved, so the first run always lands here. Failing would make the
    # pipeline unusable the day it is introduced.
    warn "certificate ${EDGE_CERT} is ${CERT_STATE} - it stays that way until the A records below
       exist and propagate, then becomes ACTIVE on its own within roughly 15-60 minutes." ;;
  *)             die "certificate ${EDGE_CERT} is ${CERT_STATE} - this does not resolve itself.
   Check the domains on the certificate against the A records that exist." ;;
esac

# 5) HTTPS: proxy + forwarding rule on port 443.
HTTPS_PROXY="${EDGE_URL_MAP}-https-proxy"
_ensure "HTTPS proxy ${HTTPS_PROXY}" \
  compute target-https-proxies describe "${HTTPS_PROXY}" --global -- \
  compute target-https-proxies create "${HTTPS_PROXY}" \
    --ssl-certificates="${EDGE_CERT}" --url-map="${EDGE_URL_MAP}"

HTTPS_RULE="${EDGE_URL_MAP}-https-rule"
_ensure "HTTPS forwarding rule ${HTTPS_RULE}" \
  compute forwarding-rules describe "${HTTPS_RULE}" --global -- \
  compute forwarding-rules create "${HTTPS_RULE}" --global \
    --load-balancing-scheme=EXTERNAL_MANAGED --network-tier=PREMIUM \
    --address="${EDGE_IP_NAME}" --target-https-proxy="${HTTPS_PROXY}" --ports=443

# 6) THE HTTP REDIRECT - its OWN url map. Pointing the HTTP proxy at the serving map above would
#    serve the app over plain HTTP rather than redirect it; a redirect is a url map whose action
#    is defaultUrlRedirect, which gcloud can only write by importing a small YAML document.
REDIRECT_MAP="${EDGE_URL_MAP}-redirect"
REDIRECT_FILE="$(mktemp -t edge-redirect-XXXXXX).yaml"
trap 'rm -f "${REDIRECT_FILE}"' EXIT
cat > "${REDIRECT_FILE}" <<YAML
name: ${REDIRECT_MAP}
defaultUrlRedirect:
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
YAML

_ensure "HTTP redirect map ${REDIRECT_MAP}" \
  compute url-maps describe "${REDIRECT_MAP}" --global -- \
  compute url-maps import "${REDIRECT_MAP}" --global --source="${REDIRECT_FILE}" --quiet

HTTP_PROXY="${EDGE_URL_MAP}-http-proxy"
_ensure "HTTP proxy ${HTTP_PROXY}" \
  compute target-http-proxies describe "${HTTP_PROXY}" --global -- \
  compute target-http-proxies create "${HTTP_PROXY}" --url-map="${REDIRECT_MAP}"

HTTP_RULE="${EDGE_URL_MAP}-http-rule"
_ensure "HTTP forwarding rule ${HTTP_RULE}" \
  compute forwarding-rules describe "${HTTP_RULE}" --global -- \
  compute forwarding-rules create "${HTTP_RULE}" --global \
    --load-balancing-scheme=EXTERNAL_MANAGED --network-tier=PREMIUM \
    --address="${EDGE_IP_NAME}" --target-http-proxy="${HTTP_PROXY}" --ports=80

log "edge ${EDGE_URL_MAP}"
log "  address        ${EDGE_IP_ADDRESS}"
log "  certificate    ${EDGE_CERT} (${CERT_STATE})"

info "DNS - add these A records, then nothing else is required:"
for pair in ${DOMAIN_LIST[@]+"${DOMAIN_LIST[@]}"}; do
  log "  ${pair}  A  ${EDGE_IP_ADDRESS}"
done

# THE ADDRESS AND CERTIFICATE STATE, for the caller that runs after this stage. Task 4 records the
# address in the manifest; a no-op outside a GitHub Actions step, so nothing here touches a local
# run or a unit test.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    printf 'edge_ip=%s\n' "${EDGE_IP_ADDRESS}"
    printf 'edge_cert_state=%s\n' "${CERT_STATE}"
  } >> "${GITHUB_OUTPUT}"
fi
log "done"
