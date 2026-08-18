#!/usr/bin/env bash
# Responsibility: Push the exact images Gate C validated and record the immutable digest each push produced.
# Boundaries: it never builds; a record that is not a clean, matching pass is refused rather than published.

# RELEASE PUBLICATION - push the exact images Gate C validated, and record their digests.
#
# This is the step that makes a release record deployable. It never builds. It takes the LOCAL
# IMAGE IDs from a Gate C PASS record, confirms those exact images are still present unchanged,
# tags and pushes THOSE image objects, resolves the immutable registry digest each push produced,
# and folds the mapping back into the record.
#
# Why it exists: Cloud Build previously rebuilt the application from source at deploy time, so the
# bytes that shipped were never the bytes that were validated - a different builder, a different
# base-image resolution, a different layer cache. `docker push` of an already-built image object
# transfers those exact layers, so the registry digest identifies the validated artifact.
#
# The provenance chain it completes:
#     clean commit -> built once -> validated -> pushed without rebuilding -> registry digest
#     -> recorded local image ID <-> digest -> deployed by digest
#
# REFUSALS (each is a way an unvalidated artifact could otherwise ship):
#   * the record is absent, malformed, or its verdict is not `passed`
#   * the record is for a different commit than HEAD, or the tree is dirty
#   * a component's local image is gone, or its ID no longer matches the record
#   * the registry already has this tag pointing at a DIFFERENT digest
#   * a push produced no digest
#
# INPUTS   deploy/output/release.json in state=validated with verdict=passed
# OUTPUT   the same record, state=published, each component carrying registry_digest + reference
# NETWORK  yes - it pushes to a registry
# MUTATES  the registry (by adding a new tag), and the release record
#
#   RELEASE_REGISTRY=host/repo   publish somewhere other than the configured Artifact Registry.
#                                Used by the test suite against an ephemeral local registry.
#   RELEASE_PUBLISH_TAG=<tag>    override the publication tag (default: the record's commit sha12)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${REPO_ROOT}"
REC="${RELEASE_RECORD:-${REPO_ROOT}/deploy/output/release.json}"
PY="${REPO_ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY=python3

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
stage(){ printf '\n%s── %s ──%s\n' "${c_bold}" "$1" "${c_off}"; }
ok(){ printf '   %s✓%s %s\n' "${c_grn}" "${c_off}" "$1"; }
die(){ printf '   %s✗%s %s\n\n' "${c_red}" "${c_off}" "$1"; exit 1; }
note(){ printf '   %s%s%s\n' "${c_dim}" "$1" "${c_off}"; }

# the record must be a PASS
stage "the record being published"
[ -f "${REC}" ] || die "no release record at ${REC} - run 'make release-validate' first"

eval "$("${PY}" - "${REC}" <<'PY'
import json, shlex, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"REC_ERROR={shlex.quote('malformed release record: ' + str(exc))}"); raise SystemExit(0)
print(f"REC_SCHEMA={shlex.quote(str(r.get('record_schema')))}")
print(f"REC_STATE={shlex.quote(str(r.get('state')))}")
print(f"REC_VERDICT={shlex.quote(str(r.get('verdict')))}")
print(f"REC_COMMIT={shlex.quote(str(r.get('commit')))}")
print(f"REC_TREE={shlex.quote(str(r.get('tree')))}")
print(f"REC_TAG={shlex.quote(str(r.get('local_image_tag')))}")
print(f"REC_COMPONENTS={shlex.quote(' '.join(sorted((r.get('components') or {}))))}")
PY
)"
[ -n "${REC_ERROR:-}" ] && die "${REC_ERROR}"
[ "${REC_SCHEMA}" = "2" ] || die "record_schema ${REC_SCHEMA} is not supported by this tool"
[ "${REC_VERDICT}" = "passed" ] || die "the record's verdict is '${REC_VERDICT}' - only a PASS may be published"
case "${REC_STATE}" in
  validated) ;;
  published) note "the record is already published; re-publishing re-pushes and re-verifies" ;;
  *) die "unexpected record state '${REC_STATE}'" ;;
esac
ok "verdict passed, state ${REC_STATE}, components: ${REC_COMPONENTS}"

HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
HEAD_TREE="$(git rev-parse HEAD^{tree} 2>/dev/null || echo none)"
[ "${HEAD_SHA}" = "${REC_COMMIT}" ] \
  || die "the record is for ${REC_COMMIT:0:12} but HEAD is ${HEAD_SHA:0:12} - re-run Gate C"
[ "${HEAD_TREE}" = "${REC_TREE}" ] || die "the record's tree does not match the current tree"
if ! git diff --quiet HEAD || [ -n "$(git diff --cached --name-only)" ]; then
  die "the working tree is dirty - publish only from the exact commit that was validated"
fi
ok "record describes HEAD (${HEAD_SHA:0:12}), tree matches, working tree clean"

# where to publish
stage "publication target"
if [ -n "${RELEASE_REGISTRY:-}" ]; then
  REGISTRY="${RELEASE_REGISTRY}"
  note "RELEASE_REGISTRY override in use: ${REGISTRY}"
else
  # The deployment environment is machine-owned state that discovery writes. It is never the
  # application's root .env: that file holds live API keys, and sourcing it here would export
  # them into every gcloud and docker child process.
  ENV_FILE="${DEPLOY_ENV_FILE:-${REPO_ROOT}/deploy/gcp/generated.env}"
  [ -f "${ENV_FILE}" ] || die "no deployment environment at ${ENV_FILE#"${REPO_ROOT}/"} and no RELEASE_REGISTRY.
       Discovery writes it; run it once for this project:
         cd deploy/gcp && make bootstrap
       Or publish somewhere else explicitly:  RELEASE_REGISTRY=host/repo make release-publish"
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
  for v in GCP_REGION GCP_PROJECT_ID ARTIFACT_REGISTRY_REPOSITORY; do
    [ -n "${!v:-}" ] || die "${v} is unset in ${ENV_FILE#"${REPO_ROOT}/"} - rerun: cd deploy/gcp && make bootstrap"
  done
  REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${ARTIFACT_REGISTRY_REPOSITORY}"
fi
PUBLISH_TAG="${RELEASE_PUBLISH_TAG:-${REC_TAG}}"
[ -n "${PUBLISH_TAG}" ] && [ "${PUBLISH_TAG}" != "None" ] || die "no publication tag in the record"
ok "registry ${REGISTRY}, tag ${PUBLISH_TAG}"

# push each validated image
stage "publishing the exact validated images (no build)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/amp-publish-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
PUBLISHED="${WORK}/published.json"
echo '{}' > "${PUBLISHED}"

for comp in ${REC_COMPONENTS}; do
  eval "$("${PY}" - "${REC}" "${comp}" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))["components"][sys.argv[2]]
print(f"C_TAG={shlex.quote(str(c.get('local_tag') or ''))}")
print(f"C_ID={shlex.quote(str(c.get('local_image_id') or ''))}")
PY
)"
  [ -n "${C_ID}" ] || die "${comp}: the record carries no local image ID"
  # The image object the record named must still be here, unchanged. A tag that now points
  # somewhere else is exactly the substitution this step exists to prevent.
  actual="$(docker image inspect "${C_TAG}" --format '{{.Id}}' 2>/dev/null)" \
    || die "${comp}: the validated image ${C_TAG} is no longer present locally - re-run Gate C"
  [ "${actual}" = "${C_ID}" ] \
    || die "${comp}: ${C_TAG} now resolves to ${actual:0:19}, but the record validated ${C_ID:0:19}
       a different image is wearing the validated tag - re-run Gate C"
  ok "${comp}: local image ${C_ID:0:19} matches the record"

  target="${REGISTRY}/${comp}:${PUBLISH_TAG}"

  # If the registry already carries this tag, it must already be THIS image. Overwriting it
  # silently is how a validated tag starts pointing at something nobody validated.
  #
  # The comparison is the manifest's CONFIG digest, which for a given image equals the local
  # image ID - so "is the remote tag already this exact image?" is answerable BEFORE pushing.
  #
  # FAIL CLOSED. `docker manifest inspect` prints "no such manifest" both when the tag is
  # genuinely absent AND when the registry could not be reached at all (a plain-HTTP registry
  # without --insecure, for one), so a failed probe is NOT evidence that the tag is free.
  # Treating it as evidence is fail-OPEN, and it let a conflicting tag be overwritten in
  # testing. If the remote state cannot be established, publication stops.
  # Probed WITHOUT a pipeline: `set -o pipefail` makes `docker ... | grep -q` inherit docker's
  # non-zero exit even when grep matched, which silently collapsed "absent" into "unknown".
  remote_manifest=""; probe=unknown
  if remote_manifest="$(docker manifest inspect "${target}" 2>/dev/null)" \
       && [ -n "${remote_manifest}" ]; then
    probe=ok
  elif remote_manifest="$(docker manifest inspect --insecure "${target}" 2>/dev/null)" \
       && [ -n "${remote_manifest}" ]; then
    probe=ok                      # plain-HTTP registry (local test registries)
  else
    probe_err="$(docker manifest inspect --insecure "${target}" 2>&1 || true)"
    registry_host="${REGISTRY%%/*}"
    case "${probe_err}" in
      *"no such manifest"*|*"manifest unknown"*|*"not found"*)
        # The message alone is ambiguous - an unreachable registry says the same thing - so it
        # only means ABSENT if the registry itself answered.
        # ANY HTTP status proves the registry answered, and that is the whole question here.
        # A real registry replies 401 to an unauthenticated /v2/, so a success requirement would
        # read every healthy hosted registry as unreachable.
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
                 "https://${registry_host}/v2/" 2>/dev/null || echo 000)"
        [ "${code}" = "000" ] && code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
                 "http://${registry_host}/v2/" 2>/dev/null || echo 000)"
        if [ "${code}" != "000" ]; then
          probe=absent
        fi ;;
    esac
  fi

  case "${probe}" in
    ok)
      remote_config="$(printf '%s' "${remote_manifest}" | "${PY}" -c \
        'import json,sys; print(json.load(sys.stdin).get("config",{}).get("digest",""))' 2>/dev/null)"
      if [ -n "${remote_config}" ] && [ "${remote_config}" != "${C_ID}" ]; then
        die "${comp}: ${target} already exists and points at ${remote_config:0:19},
       which is NOT the validated image ${C_ID:0:19}. Refusing to overwrite a published tag.
       Publish under a different tag (RELEASE_PUBLISH_TAG=...) or investigate the conflict."
      fi
      note "${comp}: the registry already carries this exact image under ${PUBLISH_TAG}" ;;
    absent)
      note "${comp}: ${PUBLISH_TAG} is free in the registry" ;;
    unknown)
      if [ "${RELEASE_ALLOW_UNVERIFIED_TAG:-0}" = "1" ]; then
        note "${comp}: WARNING - could not read the remote tag; proceeding because
       RELEASE_ALLOW_UNVERIFIED_TAG=1. If ${PUBLISH_TAG} holds another image it is now lost."
      else
        _HOST="${target%%/*}"
        if ! grep -q "${_HOST}" "${HOME}/.docker/config.json" 2>/dev/null; then
          note "${comp}: docker has no credential helper for ${_HOST}.
       Fix it with: gcloud auth configure-docker ${_HOST}"
        fi
        die "${comp}: could not determine what ${target} currently points at.
       A failed registry read is not proof that the tag is free, and pushing over an unknown
       tag can destroy a published artifact. Fix registry access, or set
       RELEASE_ALLOW_UNVERIFIED_TAG=1 if you accept overwriting whatever is there."
      fi ;;
  esac

  docker tag "${C_TAG}" "${target}" || die "${comp}: could not tag ${C_TAG} as ${target}"
  if ! docker push "${target}" >"${WORK}/push-${comp}.log" 2>&1; then
    sed 's/^/       /' "${WORK}/push-${comp}.log"
    die "${comp}: push failed"
  fi

  # The digest the registry assigned to what we just pushed.
  digest="$(docker image inspect "${target}" --format '{{range .RepoDigests}}{{println .}}{{end}}' \
             2>/dev/null | grep -F "${REGISTRY}/${comp}@" | head -1 | sed 's/.*@//')"
  if [ -z "${digest}" ]; then
    digest="$(grep -oE 'sha256:[0-9a-f]{64}' "${WORK}/push-${comp}.log" | tail -1)"
  fi
  [ -n "${digest}" ] || die "${comp}: the push produced no digest - refusing to record a tag-only reference"
  ok "${comp}: pushed -> ${digest}"

  "${PY}" - "${PUBLISHED}" "${comp}" "${REGISTRY}" "${PUBLISH_TAG}" "${digest}" <<'PY'
import json, pathlib, sys
out, comp, registry, tag, digest = sys.argv[1:6]
p = pathlib.Path(out); d = json.loads(p.read_text())
d[comp] = {"registry_repository": f"{registry}/{comp}",
           "publication_tag": tag,
           "registry_digest": digest,
           "reference": f"{registry}/{comp}@{digest}"}
p.write_text(json.dumps(d, indent=2))
PY
done

# fold into the record
stage "release record"
if "${PY}" "${REPO_ROOT}/devtools/release/record.py" publish-record \
     --record "${REC}" --published "${PUBLISHED}"; then
  printf '\n%sPUBLISHED%s - the record is promotable\n' "${c_bold}" "${c_off}"
  "${PY}" - "${REC}" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
for name, c in sorted(r["components"].items()):
    print(f"   {name:6} {c['local_image_id'][:19]}  ->  {c['reference']}")
PY
  printf '\n   next: %smake mesh-preflight%s\n\n' "${c_bold}" "${c_off}"
  exit 0
fi
printf '\n%sPUBLICATION INCOMPLETE%s - the record is still not promotable (see above)\n\n' \
  "${c_red}" "${c_off}"
exit 1
