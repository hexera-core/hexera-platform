#!/usr/bin/env bash
# Responsibility: Let the deployment's own pages PUT a large geometry file straight into the artifacts bucket.
# Owns: the artifacts bucket's CORS rule for that PUT - which origins may send it, and nothing else.
# Boundaries: it grants no access (the signed URL is the permission) and touches no object, IAM binding or other bucket setting.

# Apply the BROWSER-UPLOAD CORS RULE to the artifacts bucket.
#
#   bash deploy/gcp/scripts/apply-upload-cors.sh
#
# WHY THIS EXISTS. Cloud Run refuses an HTTP/1 request body over 32 MiB ("413 Request Entity Too
# Large") before the application sees it, and the console relays uploads through a service with the
# same cap - so a 40-500 MB CAD file cannot reach the API in a request body at all. A large file is
# therefore sent by the browser STRAIGHT to the bucket, to a signed PUT URL the API issues for one
# object (src/meshpipeline/api/v1/upload_direct.py), and the API reads it back and checks it.
#
# That PUT is cross-origin: the page is the console's (or the API's) origin, the bucket is
# storage.googleapis.com. Without a CORS rule on the bucket the browser's preflight is refused and
# the upload fails with a bare network error. The rule lists the deployment's own origins, allows
# PUT with a Content-Type header, and nothing more.
#
# WHAT IT DOES NOT DO. CORS is not access. The bucket stays private and public access prevention is
# untouched; a browser holding no signed URL can write nothing, whatever its origin. The signing
# identity (the HMAC key's service account) already holds objectAdmin on this bucket for the
# multipart upload, so no IAM changes.
#
# RECONCILING, NOT OVERWRITING. The rule this script owns is recognised by its shape (method PUT,
# response header Content-Type); any other CORS entry already on the bucket is kept as it is. An
# unchanged rule is reported as reused and not rewritten.
#
# INPUTS   the deployment env (MINIO_BUCKET or GCP_ARTIFACTS_BUCKET, MINIO_ENDPOINT,
#          CLOUDRUN_CONSOLE_SERVICE, CLOUDRUN_API_SERVICE, CONSOLE_DOMAIN, CONSOLE_BASE_URL)
# MUTATES  the artifacts bucket's CORS configuration, only when it differs from the one stated here
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION

BUCKET="${MINIO_BUCKET:-${GCP_ARTIFACTS_BUCKET:-}}"
if [ -z "${BUCKET}" ]; then
  info "No artifacts bucket declared - skipping the browser-upload CORS rule"
  exit 0
fi
# Only Google Cloud Storage is configured here. The local stack's MinIO allows every origin by
# default, and another S3-compatible store is configured by whoever runs it.
case "${MINIO_ENDPOINT:-storage.googleapis.com}" in
  storage.googleapis.com|*.googleapis.com) ;;
  *) info "The object store is not Google Cloud Storage (${MINIO_ENDPOINT}) - skipping the CORS rule"
     exit 0 ;;
esac
if ! bucket_exists "${BUCKET}"; then
  warn "cannot see gs://${BUCKET} - the browser-upload CORS rule was NOT applied, so files over
       20 MB cannot be uploaded from the console until it is. Once the bucket exists, rerun:
         bash deploy/gcp/scripts/apply-upload-cors.sh"
  exit 0
fi

# THE ORIGINS: every address the deployment's pages are served from. The console by its custom
# domain and by both of its run.app addresses; the API too, because it serves the same page.
service_urls() {  # SERVICE -> its URLs, one line (status.url and the run.googleapis.com/urls list)
  [ -n "$1" ] || return 0
  gc run services describe "$1" --region "${GCP_REGION}" \
    --format="value(status.url,metadata.annotations['run.googleapis.com/urls'])" 2>/dev/null || true
}
CANDIDATES="$(service_urls "${CLOUDRUN_CONSOLE_SERVICE:-}") $(service_urls "${CLOUDRUN_API_SERVICE:-}")"
[ -n "${CONSOLE_DOMAIN:-}" ] && CANDIDATES="${CANDIDATES} https://${CONSOLE_DOMAIN}"
[ -n "${CONSOLE_BASE_URL:-}" ] && CANDIDATES="${CANDIDATES} ${CONSOLE_BASE_URL}"

LIVE_CORS="$(gcloud storage buckets describe "gs://${BUCKET}" --project "${GCP_PROJECT_ID}" \
  --format='json(cors_config)' 2>/dev/null || echo '{}')"

CORS_FILE="$(mktemp)"
trap 'rm -f "${CORS_FILE}"' EXIT
# Prints "unchanged", "empty" or "changed"; writes the merged rule to CORS_FILE when changed.
DECISION="$(python3 - "${CORS_FILE}" "${CANDIDATES}" "${LIVE_CORS}" <<'PY'
import json, re, sys
path, candidates, live = sys.argv[1], sys.argv[2], sys.argv[3]
origins = sorted({m.group(0).rstrip("/") for m in
                  re.finditer(r"https://[A-Za-z0-9.-]+(?::\d+)?", candidates)})
if not origins:
    print("empty"); sys.exit(0)
for o in origins:
    print(f"  allowed origin  {o}", file=sys.stderr)
ours = {"origin": origins, "method": ["PUT"], "responseHeader": ["Content-Type"],
        "maxAgeSeconds": 3600}
# A bucket with no CORS rule describes as JSON null (or {"cors_config": null}), not as {}.
try:
    doc = json.loads(live or "{}")
except ValueError:
    doc = {}
current = (doc.get("cors_config") if isinstance(doc, dict) else None) or []
if not isinstance(current, list):
    current = []
def is_ours(entry):
    return (sorted(entry.get("method") or []) == ["PUT"]
            and sorted(entry.get("responseHeader") or []) == ["Content-Type"])
kept = [e for e in current if not is_ours(e)]
wanted = kept + [ours]
def norm(entries):
    return sorted(json.dumps({k: (sorted(v) if isinstance(v, list) else v)
                              for k, v in e.items()}, sort_keys=True) for e in entries)
if norm(current) == norm(wanted):
    print("unchanged"); sys.exit(0)
with open(path, "w") as fh:
    json.dump(wanted, fh)
print("changed")
PY
)"

case "${DECISION}" in
  empty)
    warn "no console or API address could be discovered, so the browser-upload CORS rule was NOT
       applied - files over 20 MB cannot be uploaded from the console. Set CLOUDRUN_CONSOLE_SERVICE
       or CONSOLE_DOMAIN and rerun: bash deploy/gcp/scripts/apply-upload-cors.sh" ;;
  unchanged)
    log "gs://${BUCKET} browser-upload CORS rule  (reused - unchanged)" ;;
  changed)
    if gc storage buckets update "gs://${BUCKET}" --cors-file="${CORS_FILE}" >/dev/null; then
      log "gs://${BUCKET} browser-upload CORS rule  (applied)"
    else
      warn "could not set the CORS rule on gs://${BUCKET} (this identity needs storage.buckets.update,
       which roles/storage.admin carries). Until it is set, files over 20 MB cannot be uploaded from
       the console. As an owner, run once:
         bash deploy/gcp/scripts/apply-upload-cors.sh"
    fi ;;
esac
log "done"
