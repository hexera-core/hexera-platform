#!/usr/bin/env bash
# Responsibility: Write the digest of the validated, published image into this deployment's configuration.
# Boundaries: it builds nothing and resolves nothing from a tag; a record that is not promotable is refused.

# Promote the validated, published release artifact into this deployment's configuration.
#
# This IS the deploy flow's image step, and it builds nothing. The release record - written by
# Gate C, completed by release-publish - is read, its promotability is checked by the same
# authority Gate D uses, and the DIGEST-QUALIFIED reference is written into the deployment env.
#
# A tag is never resolved here, because a tag can be moved between validation and rollout: only a
# digest still names the bytes that were validated.
#
#   mesh -> MESH_IMAGE
#
# INPUTS   deploy/output/release.json, promotable
# OUTPUT   MESH_IMAGE written into the deployment env file as
#          registry/component@sha256:... references
# NETWORK  none
# MUTATES  the deployment env file only
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env

REC="${RELEASE_RECORD:-${REPO_ROOT}/deploy/output/release.json}"
PY="${REPO_ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY=python3

[ -f "${REC}" ] || die "no release record at ${REC}
       Deployment promotes an artifact that was already built and validated. Produce one:
         make release-validate     # build once, validate those exact images
         make release-publish      # push them and record the immutable digests"

if ! "${PY}" "${REPO_ROOT}/devtools/release/record.py" check --record "${REC}" > /tmp/.promote.$$ 2>&1; then
  warn "the release record is not promotable:"
  sed 's/^/       /' /tmp/.promote.$$ >&2
  rm -f /tmp/.promote.$$
  die "refusing to deploy an artifact that was not validated and published"
fi
rm -f /tmp/.promote.$$

HEAD_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo none)"
REC_COMMIT="$("${PY}" -c 'import json,sys;print(json.load(open(sys.argv[1]))["commit"])' "${REC}")"
[ "${HEAD_SHA}" = "${REC_COMMIT}" ] || [ "${HEAD_SHA}" = "none" ] \
  || die "the release record is for ${REC_COMMIT:0:12} but HEAD is ${HEAD_SHA:0:12}
       Deploy the commit that was validated, or re-run Gate C on this one."

MESH_REF="$("${PY}" -c 'import json,sys;print(json.load(open(sys.argv[1]))["components"]["mesh"]["reference"])' "${REC}")"
case "${MESH_REF}" in
  *@sha256:*) ;;
  *) die "the record carries a tag-only reference (${MESH_REF}) - deployment identity must be a digest" ;;
esac

ENV_TARGET="${DEPLOY_ENV_FILE:-${DEPLOY_DIR}/generated.env}"
"${PY}" - "${ENV_TARGET}" "${MESH_REF}" <<'PY'
import pathlib, re, sys
path, mesh = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""
for key, value in (("MESH_IMAGE", mesh),):
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + f"{key}={value}\n"
p.write_text(text)
PY

info "Promoting the validated release artifact (no build)"
log "commit:        ${REC_COMMIT}"
log "mesh (mesh job): ${MESH_REF}"
log "written to:    ${ENV_TARGET}"
