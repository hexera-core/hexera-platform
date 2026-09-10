#!/usr/bin/env bash
# Responsibility: Provision the global external Application Load Balancer that gives the console
# and admin console their custom hostnames.
# Owns: the reserved static IP, one serverless NEG and backend service per Cloud Run service, the
# host-routed URL map, one Google-managed certificate per hostname, and the HTTP-to-HTTPS redirect.
# Boundaries: it never deletes or recreates a live resource; DNS itself is the operator's, not this
# script's - it only prints what belongs in it.

# Provision the EDGE for the consoles. Idempotent and RECONCILING: every resource here is either
# describe-then-create-only-if-absent or a full-state `import` that says the same thing every run.
# Nothing is recreated.
#
#   bash deploy/gcp/scripts/create-edge.sh
#
# WHY A LOAD BALANCER AT ALL. Cloud Run hands out no IP address of its own - a custom hostname
# means routing through a global external Application Load Balancer, which is what everything
# below assembles: a reserved global static IP, a serverless NEG per Cloud Run service (the bridge
# from a classic LB backend to a Cloud Run revision), a backend service per NEG, a URL map that
# routes by host, and a Google-managed certificate per declared hostname.
#
# WHY ONE CERTIFICATE PER HOSTNAME AND NOT ONE COVERING BOTH. A Google-managed certificate serves
# only once it is WHOLLY ACTIVE, so a single name that fails validation holds the whole certificate
# at PROVISIONING and takes every OTHER name on it off HTTPS with it. Dev showed exactly that:
#   overall PROVISIONING / dev.console.hexera.ai ACTIVE / dev.admin.hexera.ai FAILED_NOT_VISIBLE
# - the console had validated and was still unreachable over HTTPS because of its neighbour. In
# prod that shape lets a validation problem on admin.hexera.ai take down console.hexera.ai, the
# customer-facing name. A certificate each makes the fates independent.
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
# happened before this run exists. So the FIRST run always leaves every certificate PROVISIONING.
# Failing on that would make this pipeline unusable on the day it is introduced; only a state that
# does not resolve itself (FAILED*) is treated as a failure - and that failure is reported and
# raised per hostname, AFTER every hostname's state has been read and printed, so one name's
# trouble never decides what is said about another's.
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
#          maps (serving and redirect), a managed certificate per declared hostname, two target
#          proxies, and two forwarding rules. It deletes nothing.
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
# Console-then-admin order, matching the order the HTTPS proxy's certificate list is built in.
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
# once and used for every resource below instead of being open-coded eight times. It reports
# nothing about WHICH branch it took, deliberately: the one caller that ever needed to know
# (attaching a NEG to a backend service) asks the backend service itself instead, because a run
# that created a resource and then died leaves "it existed already" and "it is complete" as two
# different questions.
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
    return 0
  fi
  info "Creating ${label}"
  gc "${create_args[@]}"
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
  # ATTACH THE NEG WHEN IT IS NOT ALREADY ATTACHED - decided by READING THE BACKEND LIST, never by
  # whether _ensure just created the backend service. Gating on the disposition assumed that a
  # backend service that exists must already have its NEG, and any run that created the backend
  # service and then died before this line (the certificate `die` below, a transient API error, or
  # simply a run that failed halfway) falsifies that: every later run then reuses the empty backend
  # service, never attaches, and the load balancer serves 502 forever with no error anywhere. The
  # backend list is the actual state, so ask for it.
  attached_groups="$(gc compute backend-services describe "${backend}" --global \
    --format='value(backends[].group)' 2>/dev/null | tr '\n' ';' || true)"
  # Delimited on both ends so a NEG whose name is a prefix of another ("x-neg" vs "x-neg-2") does
  # not read as attached. gcloud joins a repeated field with ';'.
  case ";${attached_groups};" in
    *"/${neg};"*)
      log "  ${neg} already attached to ${backend}" ;;
    *)
      gc compute backend-services add-backend "${backend}" --global \
        --network-endpoint-group="${neg}" --network-endpoint-group-region="${GCP_REGION}" ;;
  esac

  BACKENDS+=("${host}|${backend}")
  log "  ${label} routed to ${backend} <- ${neg} <- ${service}"
done

# 3) THE SERVING URL MAP - one map, one host rule per hostname, each to its own backend. The
#    default service is the first declared backend (console, when present); every other hostname
#    reaches its backend only through its own host rule.
#
#    WRITTEN AS ONE FULL-STATE `url-maps import`, NOT create + add-path-matcher. `add-path-matcher`
#    is not the idempotent update it reads as: gcloud's own add_path_matcher surface refuses a host
#    that already belongs to a host rule -
#      "Cannot create a new host rule with host [X] because the host is already part of a host rule
#       that references the path matcher [Y]"
#    - and otherwise APPENDS a path matcher rather than replacing one. So the first run succeeded
#    and every run after it died right here, before the certificate was read, before the DNS block
#    printed and before $GITHUB_OUTPUT was written; deploy.sh is `set -euo pipefail`, so the whole
#    deploy aborted at this stage and never reached the manifest write. `import` is a full-state
#    write - gcloud's import inserts when the map is absent and replaces it when present - so it
#    says the same thing on every run and needs no describe-then-create gate. It is also the idiom
#    the redirect map below already uses, so this file has one way of writing a URL map, not two.
default_backend="${BACKENDS[0]#*|}"
# Backend services are named by their full resource URL in an imported UrlMap: the API accepts a
# selfLink there, not the bare name the flag-based `--default-service` would have resolved.
BE_URL="https://www.googleapis.com/compute/v1/projects/${GCP_PROJECT_ID}/global/backendServices"
# No .yaml suffix: appending one AFTER mktemp returns names a path mktemp never created, so
# the trap below removed nothing and left the real temp file behind on every run. `--source`
# sniffs the content, not the extension.
SERVING_FILE="$(mktemp -t edge-urlmap-XXXXXX)"
REDIRECT_FILE=""
trap 'rm -f "${SERVING_FILE}" ${REDIRECT_FILE:+"${REDIRECT_FILE}"}' EXIT

{
  printf 'name: %s\n' "${EDGE_URL_MAP}"
  printf 'defaultService: %s/%s\n' "${BE_URL}" "${default_backend}"
  printf 'hostRules:\n'
  for pair in "${BACKENDS[@]}"; do
    printf -- '- hosts:\n'
    printf -- '  - %s\n' "${pair%%|*}"
    printf -- '  pathMatcher: %s-pm\n' "${pair#*|}"
  done
  printf 'pathMatchers:\n'
  for pair in "${BACKENDS[@]}"; do
    printf -- '- name: %s-pm\n' "${pair#*|}"
    printf -- '  defaultService: %s/%s\n' "${BE_URL}" "${pair#*|}"
  done
} > "${SERVING_FILE}"

info "Reconciling URL map ${EDGE_URL_MAP}"
gc compute url-maps import "${EDGE_URL_MAP}" --global --source="${SERVING_FILE}" --quiet

# One sorted, space-separated set out of a list gcloud joined with ';' or this script joined with
# ',', with any resource-URL prefix stripped so a certificate's bare NAME compares against the bare
# name declared here. Both sides are normalised because neither the order gcloud returns nor the
# order this deployment declares is meaningful - only the set is.
# Split on the two separators one at a time: gcloud joins a repeated field with ';', the declared
# lists are comma-joined, and a two-character SET2 for a two-character SET1 is what `tr` calls a
# duplicate rather than a mapping. A hostname contains no '/', so the same helper serves both.
_name_set() {
  printf '%s' "$1" | tr ';' '\n' | tr ',' '\n' | sed -e 's#.*/##' -e '/^[[:space:]]*$/d' \
    | LC_ALL=C sort | tr '\n' ' '
}

# The HTTPS proxy is NAMED here rather than at section 5 because the drift refusal below has to
# print the `target-https-proxies update` an operator would run to finish the swap by hand.
HTTPS_PROXY="${EDGE_URL_MAP}-https-proxy"

# 4) THE CERTIFICATES - ONE PER HOSTNAME, each covering exactly its own name, console-then-admin so
#    the proxy's list below agrees with the order TRIPLES was built in.
#
#    WHY NOT ONE CERTIFICATE COVERING BOTH NAMES, WHICH IS WHAT THIS USED TO DO. A managed
#    certificate serves only once it is WHOLLY ACTIVE: one name failing validation holds the whole
#    certificate at PROVISIONING and takes every other name on it off HTTPS too, however long ago
#    that name validated. That is not hypothetical - dev ran in exactly that state, console ACTIVE
#    and unreachable behind an admin name at FAILED_NOT_VISIBLE (see the header). The names are
#    derived from EDGE_CERT, which stays the BASE name this deployment declares, so they remain
#    predictable and greppable in the console: ${EDGE_CERT}-console, ${EDGE_CERT}-admin.
CERT_NAMES=()
for triple in "${TRIPLES[@]}"; do
  rest="${triple#*|}"
  CERT_NAMES+=("${EDGE_CERT}-${rest#*|}")
done
# The proxy takes a LIST; this is it, in the one stable order this script uses everywhere.
CERT_LIST="$(IFS=,; printf '%s' "${CERT_NAMES[*]}")"

cert_i=0
for triple in "${TRIPLES[@]}"; do
  host="${triple%%|*}"
  cert="${CERT_NAMES[${cert_i}]}"
  cert_i=$((cert_i + 1))

  _ensure "managed certificate ${cert} (${host})" \
    compute ssl-certificates describe "${cert}" --global -- \
    compute ssl-certificates create "${cert}" --domains="${host}" --global

  # DRIFT IS NOTICED, NEVER SILENTLY REPAIRED. A managed certificate's domain list cannot be
  # edited, and "reuse, never recreate" forbids replacing it here - but it does not forbid saying
  # so. A certificate named for this hostname that covers some OTHER name (an older shared
  # certificate left under a name this scheme now claims, a hand-made one) serves a certificate
  # for a different name, and every browser hard-fails on it with nothing in this run's output
  # suggesting why. So read the list back and refuse. An unreadable list is refused too: an
  # unchecked certificate is exactly the state this guard exists to rule out.
  cert_domains_live="$(gc compute ssl-certificates describe "${cert}" --global \
    --format='value(managed.domains)')"
  cert_domains_live_set="$(_name_set "${cert_domains_live}")"
  cert_domains_want_set="$(_name_set "${host}")"
  if [ "${cert_domains_live_set}" != "${cert_domains_want_set}" ]; then
    # The remediation swaps THIS hostname's certificate and leaves the others attached, because
    # the proxy takes the whole list on every update: dropping the others would take their
    # hostnames off HTTPS, which is the coupling this whole section exists to remove.
    cert_remedy=""
    for other in "${CERT_NAMES[@]}"; do
      [ "${other}" != "${cert}" ] || other="${cert}-v2"
      cert_remedy="${cert_remedy:+${cert_remedy},}${other}"
    done
    die "certificate ${cert} covers [${cert_domains_live_set% }] but it must cover exactly
   [${cert_domains_want_set% }], the one hostname it is for. A hostname whose certificate does not
   name it serves a mismatched one and browsers refuse it outright.
   A managed certificate's domains cannot be edited and this script never deletes a live resource,
   so replacing it is an operator's deliberate act:
     gcloud compute ssl-certificates create ${cert}-v2 --domains=${host} --global \\
       --project ${GCP_PROJECT_ID}
     gcloud compute target-https-proxies update ${HTTPS_PROXY} \\
       --ssl-certificates=${cert_remedy} --global --project ${GCP_PROJECT_ID}
   then rename or remove ${cert} so this stage can create it again under the same name, and run
   this stage again. The new certificate is PROVISIONING until it validates, so make the swap when
   a brief window of the OLD certificate still being served is acceptable."
  fi
done

# 5) HTTPS: proxy + forwarding rule on port 443.
#
#    THE CERTIFICATE LIST IS RECONCILED, NOT MERELY SET AT CREATION. `_ensure` creates a proxy that
#    is absent and otherwise leaves it exactly as it stands - which is the point for every other
#    resource here and a trap for this one field. A deployment that already has an HTTPS proxy
#    pointing at the single shared certificate would go on pointing at it forever: the per-hostname
#    certificates above would be created, provisioned, billed and never served, and no run would
#    ever say so. So the live list is read back and updated whenever it disagrees with this
#    deployment's. Comparing SETS, not the literal string, keeps a re-run from rewriting a proxy
#    that already carries the right certificates in a different order.
#
#    UPDATING THE LIST DETACHES THE OLD CERTIFICATE; IT NEITHER DELETES IT NOR MOVES THE ADDRESS.
#    `target-https-proxies update --ssl-certificates` writes the proxy's sslCertificates field and
#    nothing else - the old certificate resource survives, unreferenced, for an operator to remove
#    deliberately, because this script never deletes. And the IP belongs to the FORWARDING RULE,
#    which names the proxy and is not rewritten here at all, so the address already in DNS is
#    untouched by the swap.
_ensure "HTTPS proxy ${HTTPS_PROXY}" \
  compute target-https-proxies describe "${HTTPS_PROXY}" --global -- \
  compute target-https-proxies create "${HTTPS_PROXY}" \
    --ssl-certificates="${CERT_LIST}" --url-map="${EDGE_URL_MAP}"

PROXY_CERTS_LIVE="$(gc compute target-https-proxies describe "${HTTPS_PROXY}" --global \
  --format='value(sslCertificates)')"
PROXY_CERTS_LIVE_SET="$(_name_set "${PROXY_CERTS_LIVE}")"
CERT_LIST_SET="$(_name_set "${CERT_LIST}")"
if [ "${PROXY_CERTS_LIVE_SET}" != "${CERT_LIST_SET}" ]; then
  info "Attaching ${CERT_LIST} to ${HTTPS_PROXY} (it serves [${PROXY_CERTS_LIVE_SET% }])"
  gc compute target-https-proxies update "${HTTPS_PROXY}" --global \
    --ssl-certificates="${CERT_LIST}"
else
  log "  ${HTTPS_PROXY} serves ${CERT_LIST}"
fi

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
REDIRECT_FILE="$(mktemp -t edge-redirect-XXXXXX)"   # removed by the trap set alongside the
                                                    # serving map's own temp file above; no
                                                    # .yaml suffix, for the reason stated there
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

# 7) THE CERTIFICATE STATES, ONE HOSTNAME PER LINE - read here, at the end, rather than before the
#    proxy above, and that placement is the fix, not an accident of layout. Refusing on a FAILED
#    certificate BEFORE reconciling the proxy would mean a deployment whose admin name cannot
#    validate never gets its console certificate attached at all, and no re-run ever would either -
#    the same "one hostname's problem is every hostname's problem" deadlock that splitting the
#    certificate exists to end, just moved into this script's control flow. So everything is
#    reconciled first, then every hostname's state is read and reported, and only then does a
#    FAILED one fail the run.
CERT_STATES=()
CERT_FAILED=""
cert_i=0
for triple in "${TRIPLES[@]}"; do
  host="${triple%%|*}"
  cert="${CERT_NAMES[${cert_i}]}"
  cert_i=$((cert_i + 1))
  cert_state="$(gc compute ssl-certificates describe "${cert}" --global \
    --format='value(managed.status)')"
  CERT_STATES+=("${host}=${cert_state}")
  case "${cert_state}" in
    ACTIVE) ;;
    PROVISIONING|PROVISIONING_FAILED_TEMPORARILY)
      # NOT A FAILURE. A managed certificate cannot validate until DNS points at the address this
      # run may have just reserved, so the first run always lands here. Failing would make the
      # pipeline unusable the day it is introduced. It is also strictly this hostname's state:
      # the other hostname's certificate is a different resource with a validation of its own.
      warn "certificate ${cert} for ${host} is ${cert_state} - it stays that way until that
       hostname's A record below exists and propagates, then becomes ACTIVE on its own within
       roughly 15-60 minutes." ;;
    *) CERT_FAILED="${CERT_FAILED}
     ${host}  ${cert}  ${cert_state}" ;;
  esac
done

log "edge ${EDGE_URL_MAP}"
log "  address        ${EDGE_IP_ADDRESS}"
cert_i=0
for triple in "${TRIPLES[@]}"; do
  host="${triple%%|*}"
  log "  certificate    ${CERT_NAMES[${cert_i}]}  ${CERT_STATES[${cert_i}]#*=}  (${host})"
  cert_i=$((cert_i + 1))
done

info "DNS - add these A records, then nothing else is required:"
for pair in ${DOMAIN_LIST[@]+"${DOMAIN_LIST[@]}"}; do
  log "  ${pair}  A  ${EDGE_IP_ADDRESS}"
done

# The A records are printed above this refusal on purpose: a FAILED certificate is very often a
# missing or wrong A record, so the run that refuses is the run that says which records it wanted.
if [ -n "${CERT_FAILED}" ]; then
  die "these certificates are in a state that does not resolve itself:${CERT_FAILED}
   Check each certificate's one domain against the A records that actually exist. Every OTHER
   hostname above is unaffected - its certificate is a separate resource and stays attached."
fi

# THE ADDRESS AND CERTIFICATE STATES, for the caller that runs after this stage. Task 4 records the
# address in the manifest; a no-op outside a GitHub Actions step, so nothing here touches a local
# run or a unit test. `edge_cert_state` now carries one `hostname=STATE` per declared hostname,
# space-separated, because there is no longer a single state to report - the states are the whole
# point of the change.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    printf 'edge_ip=%s\n' "${EDGE_IP_ADDRESS}"
    printf 'edge_cert_state=%s\n' "$(IFS=' '; printf '%s' "${CERT_STATES[*]}")"
  } >> "${GITHUB_OUTPUT}"
fi
log "done"
