#!/usr/bin/env bash
# Responsibility: Prove a personal environment's DEPLOYED repair path works, over HTTPS, from a laptop.
# Owns: the checks and their pass criteria. It mutates nothing in the cloud and creates no resource.
# Boundaries: it asserts what an HTTP client can prove. The checks that need gcloud are NAMED and
#             skipped rather than silently omitted - see "WHAT THIS CANNOT PROVE" below.

# WHY THIS EXISTS.
#
# "Deployed successfully" is a statement about a workflow, not about a change. A green deploy with
# an unexercised code path proves the images rolled out and nothing else. This script is the
# smallest thing that proves the repair work is actually RUNNING in a personal environment, and it
# is a script rather than a paragraph in a plan so the same checks run the same way next time.
#
# NO gcloud REQUIRED. Cloud Run's URL for a service is derived from the service name and the
# project NUMBER, both of which are literals this repository already holds, so the environment can
# be reached with curl alone. That matters because an agent or a reviewer without the Google Cloud
# SDK installed could otherwise verify nothing at all.
#
# WHAT A PASSING RUN PROVES
#   - the API image rolled out and serves
#   - it reached PostgreSQL, Redis and the object store from inside the environment
#   - MIGRATIONS APPLIED. The API runs them at start-up and REFUSES to boot against a schema
#     missing a table its models declare (runtime/migrate.assert_schema_is_complete), so a ready
#     API is proof the repair revisions landed on the real Cloud SQL database. This is the check
#     that would have caught the search_path bug in revision 0011.
#   - the operator repair surface is mounted and gated by the admin credential
#
# WHAT THIS CANNOT PROVE, and deliberately does not pretend to:
#   - that a worker consumed a repair task (needs the Redis queue or the MIG; gcloud)
#   - that artifacts landed in the artifacts bucket rather than exchange (gcloud/storage)
#   - that the Cloud Run mesh job executed (gcloud run jobs describe)
#   - end-to-end repair of a real customer file (needs an upload and an API key; see --api-key)
set -euo pipefail

SLUG="${1:-}"
PROJECT_NUMBER="${PROJECT_NUMBER:-224734058693}"   # hexera-dev; deploy.yml holds the same literal
REGION="${REGION:-us-central1}"
ADMIN_KEY="${ADMIN_API_KEY:-}"

if [ -z "${SLUG}" ]; then
  echo "usage: $0 <slug>   (e.g. $0 pranav)" >&2
  echo "       ADMIN_API_KEY=<key> $0 <slug>   to include the operator-surface checks" >&2
  exit 2
fi

API="https://dev-${SLUG}-api-${PROJECT_NUMBER}.${REGION}.run.app"
CONSOLE="https://dev-${SLUG}-console-${PROJECT_NUMBER}.${REGION}.run.app"

pass=0
fail=0

ok()   { printf '  PASS  %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }
skip() { printf '  SKIP  %s\n' "$1"; }

# A FAILED REQUEST MUST READ AS ONE STATUS. `curl -w` prints 000 on a connection failure AND
# exits non-zero, so a `|| echo 000` fallback printed "000000" and the message became unreadable.
code() {
  local out
  if [ -n "${3:-}" ]; then
    out=$(curl -s -m 30 -o "$2" -w '%{http_code}' -H "$3" "$1" 2>/dev/null) || true
  else
    out=$(curl -s -m 30 -o "$2" -w '%{http_code}' "$1" 2>/dev/null) || true
  fi
  [ -n "${out}" ] && printf '%s' "${out}" || printf '000'
}

echo "dev-${SLUG} at ${API}"
echo

# 1: THE API SERVES. Not "the workflow was green" - a request got an answer.
body=$(mktemp)
status=$(code "${API}/health" "${body}")
if [ "${status}" = "200" ]; then ok "GET /health -> 200 $(tr -d '\n' < "${body}")"
else bad "GET /health -> ${status} (expected 200)"; fi

# 2: IT REACHED ITS DEPENDENCIES, from inside the environment. A deploy can be green while the
#    service cannot see Cloud SQL at all, which is the failure this catches.
status=$(code "${API}/readyz" "${body}")
if [ "${status}" = "200" ]; then
  ok "GET /readyz -> 200 $(tr -d '\n' < "${body}")"
  for dep in postgres redis object_store; do
    if grep -q "\"${dep}\":\"ok\"" "${body}"; then ok "readyz: ${dep} ok"
    else bad "readyz: ${dep} is NOT ok"; fi
  done
  # MIGRATIONS. The API refuses to start against an incomplete schema, so a ready service that
  # reached PostgreSQL is the hosted proof that every revision this build ships applied.
  ok "migrations applied (a ready API cannot boot against an incomplete schema)"
else
  bad "GET /readyz -> ${status} (expected 200)"
fi

# 3: THE OPERATOR SURFACE IS MOUNTED AND GATED. Checked without a credential first: the refusal
#    itself is the evidence the route exists and is not open.
status=$(code "${API}/api/v1/admin/repair/queue" "${body}")
case "${status}" in
  403) ok "operator queue refuses an unauthenticated caller (403)" ;;
  404)
    # AMBIGUOUS, AND THE API MEANS IT TO BE. admin_dep answers 404 rather than 503 when
    # ADMIN_API_KEY is unset, precisely so a deployment with no cross-tenant access does not
    # advertise that the capability exists. A personal environment usually has no admin key, so
    # this is the expected answer there - but it is indistinguishable from the route being
    # absent, which is exactly why it cannot be scored as a pass.
    skip "operator queue -> 404: either ADMIN_API_KEY is unset on this API (expected for a personal environment) or the route is missing. Set ADMIN_API_KEY on the service to tell these apart." ;;
  200) bad "operator queue answered 200 WITHOUT a credential - it is open" ;;
  *)   bad "operator queue -> ${status} (expected 403, or 404 when no admin key is configured)" ;;
esac

# 4: WITH the credential, it answers and its shape is the one this build serves.
if [ -n "${ADMIN_KEY}" ]; then
  status=$(code "${API}/api/v1/admin/repair/queue" "${body}" "X-Admin-Key: ${ADMIN_KEY}")
  if [ "${status}" = "200" ] && grep -q '"jobs"' "${body}"; then
    ok "operator queue answers the admin credential with a job list"
  else
    bad "operator queue with credential -> ${status}"
  fi
  status=$(code "${API}/api/v1/admin/repair/throughput" "${body}" "X-Admin-Key: ${ADMIN_KEY}")
  if [ "${status}" = "200" ] && grep -q '"delivery_rate"' "${body}"; then
    ok "throughput answers with the service metrics $(tr -d '\n' < "${body}" | head -c 200)"
  else
    bad "throughput -> ${status}"
  fi
  # A MISSPELLED FILTER MUST BE REFUSED, not answered with every row - the deployed build has to
  # carry that behaviour, not just the tests.
  status=$(code "${API}/api/v1/admin/repair/queue?status=notastatus" "${body}" \
                "X-Admin-Key: ${ADMIN_KEY}")
  if [ "${status}" = "422" ]; then ok "a misspelled status filter is refused (422)"
  else bad "a misspelled status filter -> ${status} (expected 422)"; fi
else
  skip "operator queue and throughput contents (set ADMIN_API_KEY to include them)"
fi

# 5: THE CONSOLE SERVES. Its own URL, because a personal environment has no custom hostname.
status=$(code "${CONSOLE}/" "${body}")
case "${status}" in
  # 307/308 is how a console behind IAP or an HTTPS redirect answers an unauthenticated GET;
  # the point of this check is that something is SERVING, not that it is open.
  200|301|302|307|308|401|403) ok "console responds (${status})" ;;
  *) bad "console -> ${status}" ;;
esac

rm -f "${body}"

echo
echo "${pass} passed, ${fail} failed"
# The checks that need gcloud are reported as missing EVERY time, so a passing run is never
# mistaken for complete hosted verification.
cat <<'NOTE'

Still unproven here (needs gcloud, or a real upload):
  - a worker consumed a repair task from the slug-prefixed Redis queue
  - the Cloud Run mesh job executed for this environment
  - repaired CAD and the report landed in the ARTIFACTS bucket, not exchange
  - one real customer file inspected, repaired, promoted and meshed end to end
NOTE
[ "${fail}" -eq 0 ]
