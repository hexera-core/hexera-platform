#!/usr/bin/env bash
# Responsibility: Find a release record an earlier deploy run already published for THIS commit, and adopt it when the registry still serves exactly what it names.
# Boundaries: read-only - it builds nothing, pushes nothing and never moves a tag; anything it cannot prove falls back to Gate C.

# REUSE A RELEASE THAT IS ALREADY PUBLISHED.
#
# Why it exists: a commit is published once, by the push deploy that follows the merge, and the
# registry tag <component>:<sha12> then belongs to those bytes. A later manual run on the SAME
# commit (a fleet-only deploy, a re-run) used to run Gate C again, rebuild every image, and hand
# publish.sh a NEW image ID for a tag that already held the old one. The images are not
# bit-reproducible: buildx restores identical layers only when the shared registry layer cache
# still holds this commit's layers, and any other branch built into the same environment in
# between (a personal-environment deploy, for one) rewrites that cache. The rebuild then gets new
# timestamps, a new image ID, and publish.sh rightly refuses to overwrite the published tag - so
# every such run failed, and the workers could not get new code.
#
# What it does instead: look up the release record the earlier run uploaded for this commit, and
# adopt it only when ALL of these hold - otherwise it says why and Gate C runs as before:
#   * the run was a push or a manual run of deploy.yml in THIS repository (never a pull request)
#   * the record is promotable (record.py check: verdict passed, state published, digests present)
#   * the record names this exact commit AND tree, and the checkout is clean
#   * every component's reference lives in the registry this run publishes to
#   * the registry still serves that digest, and its config digest IS the validated image ID
#   * the publication tag still points at that same image (nobody moved it since)
#
# INPUTS   GITHUB_REPOSITORY, RELEASE_REGISTRY, RELEASE_ENVIRONMENT (the record's artifact name
#          is release-record-<environment>-<commit>), GH_TOKEN (actions: read), optional
#          GITHUB_RUN_ID (this run is never its own source), RELEASE_PYTHON, RELEASE_RECORD
# OUTPUT   deploy/output/release.json (the adopted record, byte for byte), and `reused=true|false`
#          on GITHUB_OUTPUT when that is set
# EXIT     0 whether or not a record was reused; non-zero only for a usage error
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${REPO_ROOT}"
REC="${RELEASE_RECORD:-${REPO_ROOT}/deploy/output/release.json}"
PY="${RELEASE_PYTHON:-}"
if [ -z "${PY}" ]; then
  PY="${REPO_ROOT}/.venv/bin/python"
  [ -x "${PY}" ] || PY=python3
fi

c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
stage(){ printf '\n%s── %s ──%s\n' "${c_bold}" "$1" "${c_off}"; }
ok(){ printf '   %s✓%s %s\n' "${c_grn}" "${c_off}" "$1"; }
note(){ printf '   %s%s%s\n' "${c_dim}" "$1" "${c_off}"; }
answer(){ [ -n "${GITHUB_OUTPUT:-}" ] && echo "reused=$1" >> "${GITHUB_OUTPUT}"; return 0; }
# Not reusing is never an error: Gate C builds and validates from scratch, exactly as before.
fallback(){ note "$1"; printf '\n%sNOT REUSED%s - Gate C will build and validate this commit\n\n' \
  "${c_bold}" "${c_off}"; answer false; exit 0; }

for v in GITHUB_REPOSITORY RELEASE_REGISTRY RELEASE_ENVIRONMENT; do
  [ -n "${!v:-}" ] || { echo "reuse.sh: ${v} is required" >&2; answer false; exit 2; }
done
REGISTRY="${RELEASE_REGISTRY}"

stage "is this commit already published?"
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null)" || fallback "not a git checkout"
HEAD_TREE="$(git rev-parse 'HEAD^{tree}' 2>/dev/null)" || fallback "cannot read HEAD's tree"
if ! git diff --quiet HEAD || [ -n "$(git diff --cached --name-only)" ]; then
  fallback "the working tree is dirty - only a clean checkout of the published commit may reuse it"
fi
NAME="release-record-${RELEASE_ENVIRONMENT}-${HEAD_SHA}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/amp-reuse-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Earlier runs of THIS workflow on THIS commit, newest first. Only push and manual runs from this
# repository: a pull_request run executes the pull request's own copy of the workflow, so a record
# it uploaded proves nothing.
gh api -X GET "repos/${GITHUB_REPOSITORY}/actions/workflows/deploy.yml/runs" \
     -f head_sha="${HEAD_SHA}" -f per_page=100 >"${WORK}/runs.json" 2>"${WORK}/gh.err" \
  || fallback "could not list earlier deploy runs: $(head -c 300 "${WORK}/gh.err")"
runs="$("${PY}" - "${WORK}/runs.json" "${GITHUB_REPOSITORY}" "${HEAD_SHA}" "${GITHUB_RUN_ID:-}" <<'PY'
import json, sys
path, repo, sha, me = sys.argv[1:5]
for r in json.load(open(path)).get("workflow_runs") or []:
    if (r.get("event") in ("push", "workflow_dispatch")
            and (r.get("head_repository") or {}).get("full_name") == repo
            and r.get("head_sha") == sha and str(r.get("id")) != me):
        print(r["id"])
PY
)"
[ -n "${runs}" ] || fallback "no earlier deploy run of ${HEAD_SHA:0:12}"

# The config digest of a manifest IS the local image ID, so this proves what image a reference
# serves without pulling it.
config_of(){
  local m
  m="$(docker manifest inspect "$1" 2>/dev/null </dev/null)" \
    || m="$(docker manifest inspect --insecure "$1" 2>/dev/null </dev/null)" || return 1
  printf '%s' "${m}" | "${PY}" -c \
    'import json,sys; print(json.load(sys.stdin).get("config",{}).get("digest",""))' 2>/dev/null
}

# WHAT IMAGE A REFERENCE SERVES, as either of the two IDs Docker gives a local image. The classic
# image store's ID is the image CONFIG digest; the containerd image store (Docker 29's default,
# on the hosted runners since 2026-10) gives the MANIFEST digest instead - which is also exactly
# the digest the registry assigns on push. A record written under either store names one of the
# two, so a reference is "this image" when EITHER digest equals the recorded ID. Comparing the
# config digest alone read every re-publish and every reuse of a commit published under the
# containerd store as a conflicting image (the fleet roll of 65c63ab28b1b, 2026-10-24).
manifest_digest_of(){
  local raw
  raw="$(docker buildx imagetools inspect --raw "$1" 2>/dev/null </dev/null)" || return 1
  [ -n "${raw}" ] || return 1
  printf 'sha256:%s' "$(printf '%s' "${raw}" | sha256sum | cut -d' ' -f1)"
}
# serves_image <reference> <config digest of it, or ""> <recorded image id>
serves_image(){
  local ref="$1" config="$2" want="$3" m
  [ -n "${want}" ] || return 1
  [ "${config}" = "${want}" ] && return 0
  case "${ref}" in *@"${want}") return 0 ;; esac
  m="$(manifest_digest_of "${ref}")" || return 1
  [ "${m}" = "${want}" ]
}

# usable <record> - every check, in order; prints the first reason it fails and returns 1.
usable(){
  local cand="$1" why bad comp image_id ref tagref got
  if ! why="$("${PY}" "${REPO_ROOT}/devtools/release/record.py" check --record "${cand}" 2>&1)"; then
    note "the record is not promotable: $(printf '%s' "${why}" | tr '\n' ' ' | head -c 300)"
    return 1
  fi
  # One line per component: name, local image id, digest reference, tag reference - or
  # `BAD <reason>` for the first thing that does not hold.
  "${PY}" - "${cand}" "${HEAD_SHA}" "${HEAD_TREE}" "${REGISTRY}" >"${WORK}/components" <<'PY'
import json, sys
path, sha, tree, registry = sys.argv[1:5]
r = json.load(open(path))
if r.get("commit") != sha:
    print(f"BAD the record is for {str(r.get('commit'))[:12]}, HEAD is {sha[:12]}"); raise SystemExit
if r.get("tree") != tree:
    print("BAD the record's tree does not match HEAD's tree"); raise SystemExit
for name, c in sorted(r["components"].items()):
    ref, dig = c["reference"], c["registry_digest"]
    if ref != f"{registry}/{name}@{dig}":
        print(f"BAD {name}: {ref} is not in {registry}"); raise SystemExit
    tag = c.get("publication_tag") or r.get("local_image_tag") or ""
    print(name, c["local_image_id"], ref, f"{registry}/{name}:{tag}" if tag else "-")
PY
  bad="$(grep -m1 '^BAD ' "${WORK}/components" | cut -c5-)"
  [ -z "${bad}" ] || { note "${bad}"; return 1; }
  [ -s "${WORK}/components" ] || { note "the record names no components"; return 1; }
  # The registry must serve the validated image under both the digest the deploy will pin and
  # the tag publish.sh guards.
  while read -r comp image_id ref tagref; do
    got="$(config_of "${ref}")" || { note "${comp}: the registry does not serve ${ref}"; return 1; }
    serves_image "${ref}" "${got}" "${image_id}" || {
      note "${comp}: ${ref} is image ${got:0:19}, the record validated ${image_id:0:19}"; return 1; }
    if [ "${tagref}" != "-" ]; then
      got="$(config_of "${tagref}")" || { note "${comp}: the registry does not serve ${tagref}"; return 1; }
      serves_image "${tagref}" "${got}" "${image_id}" || {
        note "${comp}: ${tagref} now points at ${got:0:19}, not the published ${image_id:0:19}"
        return 1; }
    fi
    ok "${comp}: ${ref#"${REGISTRY}/"} is the validated image ${image_id:0:19}"
  done <"${WORK}/components"
  return 0
}

# Newest first, and an unusable record is no reason to stop: a run that failed after Gate C (this
# very failure, for one) uploads a record that was never published, and the push deploy that DID
# publish the commit is older than it.
found=""
for run in ${runs}; do
  gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run}/artifacts" >"${WORK}/artifacts.json" \
    2>/dev/null || continue
  "${PY}" - "${WORK}/artifacts.json" "${NAME}" <<'PY' || continue
import json, sys
arts = json.load(open(sys.argv[1])).get("artifacts") or []
raise SystemExit(0 if any(a.get("name") == sys.argv[2] and not a.get("expired") for a in arts) else 1)
PY
  stage "run ${run} left ${NAME}"
  if ! gh run download "${run}" -R "${GITHUB_REPOSITORY}" -n "${NAME}" -D "${WORK}/${run}" \
         >/dev/null 2>"${WORK}/dl.err" || [ ! -f "${WORK}/${run}/release.json" ]; then
    note "it could not be downloaded: $(head -c 200 "${WORK}/dl.err")"
    continue
  fi
  if usable "${WORK}/${run}/release.json"; then found="${run}"; break; fi
done
[ -n "${found}" ] || fallback "no earlier run of ${HEAD_SHA:0:12} left a record this run can deploy"

# A record that did not land is no reuse: the next steps would skip Gate C with nothing to deploy.
mkdir -p "$(dirname "${REC}")" && cp "${WORK}/${found}/release.json" "${REC}" \
  && cmp -s "${WORK}/${found}/release.json" "${REC}" \
  || fallback "the adopted record could not be written to ${REC}"
printf '\n%sREUSED%s - deploying the images run %s validated and published for %s; nothing rebuilt\n\n' \
  "${c_bold}" "${c_off}" "${found}" "${HEAD_SHA:0:12}"
answer true
exit 0
