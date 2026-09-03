#!/usr/bin/env bash
# Responsibility: Gate D - decide whether the published artifact may be promoted into this environment.
# Owns: the three distinct outcomes, kept apart so a local pass is never read as proven cloud readiness.
# Boundaries: read-only; it re-runs no source test, and it deploys nothing.

# GATE D - deployment readiness. READ-ONLY: this script deploys nothing and mutates nothing.
#
# It answers one question: may the artifact Gate C validated and `release-publish` published be
# promoted into this environment? It deliberately does NOT re-run source tests. Those gated the
# commit, and the commit produced the artifact; running them again proves nothing new about the
# thing being promoted and hides the checks that ARE specific to promotion.
#
# IT REPORTS THREE DISTINCT OUTCOMES, because collapsing them is how "preflight passed" comes to
# mean less than a reader assumes:
#
#   LOCAL PREFLIGHT PASSED     the record, the artifact identity and the manifests are coherent,
#                              but cloud-side checks could not run (no gcloud / no session / no
#                              configuration). NOT a statement that deployment will succeed.
#   CLOUD READINESS UNVERIFIED the same, said explicitly, with every unrun check named.
#   DEPLOYMENT READY           everything above AND the registry digest, IAM and rollback checks
#                              ran against the real project and passed.
#
# INPUTS   deploy/output/release.json (from release-validate + release-publish), the deployment
#          environment ($DEPLOY_ENV_FILE, else deploy/gcp/generated.env)
# OUTPUT   a printed report; exit 0 only when nothing failed
# NETWORK  the registry/IAM/rollback section makes READ-ONLY Google Cloud calls; skipped loudly
# MUTATES  nothing
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
REC="${RELEASE_RECORD:-${REPO_ROOT}/deploy/output/release.json}"
# WHICH interpreter runs the checks. The repository virtualenv when it exists, else python3 -
# but a caller may name one explicitly, and a test must, because "whatever python3 happens to be
# on this machine" is not a property of the release being gated. Without the override this gate
# reported "manifest render check failed" on any host whose python3 is not the project
# environment (a conda shell, CI's setup-python, a clone before `make setup`) - a FAILED verdict
# about the manifests that had nothing to do with the manifests.
PY="${PREFLIGHT_PYTHON:-}"
if [ -z "${PY}" ] || [ ! -x "${PY}" ]; then
  PY="${REPO_ROOT}/.venv/bin/python"
  [ -x "${PY}" ] || PY=python3
fi

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_off=$'\033[0m'
FAIL=0; CLOUD_RAN=0; SKIPPED=()
stage(){ printf '\n%s── %s ──%s\n' "${c_bold}" "$1" "${c_off}"; }
ok()  { printf '   %s✓%s %s\n' "${c_grn}" "${c_off}" "$1"; }
no()  { printf '   %s✗%s %s\n' "${c_red}" "${c_off}" "$1"; FAIL=1; }
skip(){ printf '   %s•%s SKIP  %s\n' "${c_yel}" "${c_off}" "$1"; SKIPPED+=("$1"); }

# the release record
stage "the release record"
if [ ! -f "${REC}" ]; then
  no "no release record at ${REC#"${REPO_ROOT}/"}"
  printf '     run: make release-validate && make release-publish\n'
else
  # One authority decides promotability - the same module Gate C and release-publish use, so the
  # three cannot disagree about what "ready" means. It fails CLOSED on a malformed or partial
  # record: every reason is printed.
  if PROMO="$("${PY}" "${REPO_ROOT}/devtools/release/record.py" check --record "${REC}" 2>&1)"; then
    ok "record is complete, coherent and promotable"
  else
    no "the release record is not promotable:"
    printf '%s\n' "${PROMO}" | sed 's/^/       /'
  fi

  eval "$("${PY}" - "${REC}" 2>/dev/null <<'PY' || echo 'REC_BAD=1'
import json, shlex, sys
r = json.load(open(sys.argv[1]))
def q(k, v): print(f"{k}={shlex.quote(str(v))}")
q("REC_COMMIT", r.get("commit")); q("REC_TREE", r.get("tree"))
q("REC_VERDICT", r.get("verdict")); q("REC_STATE", r.get("state"))
q("REC_VER", r.get("product_version")); q("REC_WHEEL", (r.get("wheel") or {}).get("filename"))
q("REC_WSHA", (r.get("wheel") or {}).get("sha256"))
q("REC_AT", r.get("generated_at")); q("REC_PUB_AT", r.get("published_at"))
q("REC_COMPONENTS", " ".join(sorted(r.get("components") or {})))
PY
)"
  if [ -n "${REC_BAD:-}" ]; then
    no "the release record could not be parsed"
  else
    printf '   validated %s   published %s\n   version   %s\n   wheel     %s\n             sha256 %s\n' \
      "${REC_AT}" "${REC_PUB_AT}" "${REC_VER}" "${REC_WHEEL}" "${REC_WSHA}"

    # Every MANDATORY tier must be named and passed. A record whose native-terminal check is
    # absent is as unacceptable as one where it failed: this gate must not be satisfiable by
    # simply not mentioning a required tier.
    if TIERS="$("${PY}" - "${REC}" <<'PY' 2>&1
import json, sys
r = json.load(open(sys.argv[1]))
checks = {c.get("check"): c for c in (r.get("checks") or [])}
need = ("native smoke", "native all", "native terminal")
problems = []
missing = [n for n in need if n not in checks]
if missing:
    problems.append(f"the record never mentions required tier(s): {missing}")
bad = {n: checks[n].get("status") for n in need if n in checks and checks[n].get("status") != "passed"}
if bad:
    problems.append(f"required tier(s) not passed: {bad}")
notpassed = [c.get("check") for c in (r.get("checks") or [])
             if c.get("required") and c.get("status") != "passed"]
if notpassed:
    problems.append(f"required check(s) not passed: {notpassed}")
if problems:
    print("; ".join(problems)); sys.exit(1)
print("native smoke, native all and native terminal all passed; "
      f"{sum(1 for c in r['checks'] if c.get('required'))} required checks, all passed")
PY
)"; then ok "${TIERS}"
    else no "${TIERS}"; fi
  fi
fi

# artifact identity vs HEAD
stage "the artifact this deployment would promote"
HEAD_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo none)"
HEAD_TREE="$(git -C "${REPO_ROOT}" rev-parse HEAD^{tree} 2>/dev/null || echo none)"
if [ -n "${REC_COMMIT:-}" ]; then
  if [ "${HEAD_SHA}" = "${REC_COMMIT}" ]; then ok "record describes HEAD (${HEAD_SHA:0:12})"
  else no "record is for ${REC_COMMIT:0:12}, HEAD is ${HEAD_SHA:0:12} - re-run Gate C"; fi
  if [ "${HEAD_TREE}" = "${REC_TREE}" ]; then ok "record tree matches the current tree"
  else no "record tree ${REC_TREE:0:12} != current tree ${HEAD_TREE:0:12}"; fi
fi
if git -C "${REPO_ROOT}" diff --quiet HEAD 2>/dev/null \
   && [ -z "$(git -C "${REPO_ROOT}" diff --cached --name-only 2>/dev/null)" ]; then
  ok "working tree and index clean"
else
  no "the tracked tree or index is dirty - what would deploy is not what was validated"
fi

# every component must carry a digest-qualified reference, and the local image must still match
if [ -n "${REC_COMPONENTS:-}" ]; then
  for comp in ${REC_COMPONENTS}; do
    eval "$("${PY}" - "${REC}" "${comp}" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))["components"][sys.argv[2]]
for k in ("local_tag", "local_image_id", "registry_digest", "reference"):
    print(f"C_{k.upper()}={shlex.quote(str(c.get(k) or ''))}")
PY
)"
    if [ -z "${C_REFERENCE}" ]; then
      no "${comp}: no published reference - run 'make release-publish'"
    elif [[ "${C_REFERENCE}" != *"@sha256:"* ]]; then
      no "${comp}: reference '${C_REFERENCE}' is tag-only, not digest-qualified"
    else
      ok "${comp}: ${C_REFERENCE}"
    fi
    if [ -n "${C_LOCAL_TAG}" ] && [ -n "${C_LOCAL_IMAGE_ID}" ]; then
      local_now="$(docker image inspect "${C_LOCAL_TAG}" --format '{{.Id}}' 2>/dev/null || true)"
      if [ -z "${local_now}" ]; then
        skip "${comp}: the validated local image is no longer on this host (not required to deploy)"
      elif [ "${local_now}" != "${C_LOCAL_IMAGE_ID}" ]; then
        no "${comp}: local tag ${C_LOCAL_TAG} now points at ${local_now:0:19}, not the validated ${C_LOCAL_IMAGE_ID:0:19}"
      else
        ok "${comp}: local image still matches the validated ID"
      fi
    fi
  done
fi

# configuration
stage "deployment configuration"
# The machine-owned deployment environment discovery wrote. Never the application root .env:
# that file carries live API keys and must not reach a gcloud subprocess.
ENV_FILE="${DEPLOY_ENV_FILE:-${REPO_ROOT}/deploy/gcp/generated.env}"
ENV_LABEL="${ENV_FILE#"${REPO_ROOT}/"}"
if [ -f "${ENV_FILE}" ]; then
  ok "${ENV_LABEL} present"
  missing=()
  for v in GCP_PROJECT_ID GCP_REGION ARTIFACT_REGISTRY_REPOSITORY \
           CLOUDRUN_MESH_JOB; do
    grep -qE "^${v}=.+" "${ENV_FILE}" || missing+=("${v}")
  done
  [ ${#missing[@]} -eq 0 ] && ok "every required deployment variable is set" \
                           || no "unset deployment variables: ${missing[*]}"
  # WHICH names are credentials is not decided here. devtools/quality/check_deploy_secrets.py reads
  # the `secret=True` flag off settings/inventory.py, so this gate and the CI step refuse exactly
  # the same set. The hand-written list this replaced named four of them and missed both
  # POSTGRES_PASSWORD and MINIO_SECRET_KEY - the two a live audit then found published as literal
  # values in a Cloud Run service spec. A second list is how that happens.
  SECRET_GATE="${REPO_ROOT}/devtools/quality/check_deploy_secrets.py"
  if [ ! -f "${SECRET_GATE}" ]; then
    skip "credential-value check not run - ${SECRET_GATE#"${REPO_ROOT}/"} is absent"
  else
    gate_out="$("${PY}" "${SECRET_GATE}" "${ENV_FILE}" 2>&1)"; gate_rc=$?
    case "${gate_rc}" in
      0) ok "no credential values in ${ENV_LABEL} (names only)" ;;
      1) no "${ENV_LABEL} carries a credential VALUE - the deployment environment holds names only"
         # Only the offending NAMES. The gate never prints a value it found, and neither does this.
         printf '%s\n' "${gate_out}" | sed -n 's/^  - /       /p'
         printf '       full report: %s %s\n' "${PY}" "${SECRET_GATE#"${REPO_ROOT}/"} ${ENV_LABEL}" ;;
      # An interpreter that cannot import the settings catalogue tells us nothing about the file.
      # Unrun, not clean: it still blocks DEPLOYMENT READY rather than reading as a pass.
      *) skip "credential-value check not run - ${PY} could not run it: $(printf '%s\n' "${gate_out}" | tail -1)" ;;
    esac
  fi
  # The image variables the manifests render from must be the RECORDED digest references.
  for pair in "MESH_IMAGE:mesh"; do
    var="${pair%%:*}"; comp="${pair##*:}"
    val="$(grep -E "^${var}=" "${ENV_FILE}" | tail -1 | cut -d= -f2-)"
    want="$("${PY}" - "${REC}" "${comp}" 2>/dev/null <<'PY' || true
import json, sys
print((json.load(open(sys.argv[1]))["components"].get(sys.argv[2]) or {}).get("reference") or "")
PY
)"
    if [ -z "${val}" ]; then
      no "${var} is unset - the manifest would render an empty image"
    elif [[ "${val}" != *"@sha256:"* ]]; then
      no "${var}=${val} is a TAG - deployment identity must be a digest"
    elif [ -n "${want}" ] && [ "${val}" != "${want}" ]; then
      no "${var} does not match the release record
       env:    ${val}
       record: ${want}"
    else
      ok "${var} is the recorded digest reference"
    fi
  done
else
  skip "${ENV_LABEL} absent (run: cd deploy/gcp && make bootstrap) - configuration, manifest-image and cloud checks not run"
fi

# manifests
stage "Cloud Run manifests"
if ! "${PY}" -c "import pytest" >/dev/null 2>&1; then
  # NOT the same as a broken manifest. An interpreter that cannot run the check tells us nothing
  # about the manifests, so this is an unrun check - which still blocks DEPLOYMENT READY - rather
  # than a failure that would send an operator hunting a manifest bug that does not exist.
  skip "manifest render check not run - ${PY} has no pytest. Run \`make setup\`, or set
       PREFLIGHT_PYTHON to an interpreter with the project's test dependencies"
elif "${PY}" -m pytest -q -p no:cacheprovider \
     "${REPO_ROOT}/tests/unit/deploy/test_manifest_render.py" >/dev/null 2>&1; then
  ok "manifests render with the variables the deploy scripts export, and parse as YAML"
else
  no "manifest render check failed - run: ${PY} -m pytest tests/unit/deploy"
fi
# The rendered manifests (if a previous deploy produced them) must carry digests, not tags.
RENDERED="${REPO_ROOT}/deploy/gcp/.rendered"
if [ -d "${RENDERED}" ]; then
  tagonly="$(grep -hE '^\s*-?\s*image:' "${RENDERED}"/*.yaml 2>/dev/null | grep -v '@sha256:' || true)"
  if [ -n "${tagonly}" ]; then
    no "a previously rendered manifest carries a tag-only image:"
    printf '%s\n' "${tagonly}" | sed 's/^/       /'
  else
    ok "previously rendered manifests are digest-qualified"
  fi
fi

# migration readiness
stage "database migration readiness"
if MIG="$("${PY}" - <<'PY' 2>&1
import pathlib, re
d = pathlib.Path("alembic/versions")
revs, downs = {}, {}
for p in sorted(d.glob("*.py")):
    t = p.read_text()
    m = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)', t, re.M)
    n = re.search(r'^down_revision(?::\s*\w[\w\[\], |]*)?\s*=\s*(?:["\']([^"\']+)|None)', t, re.M)
    if not m:
        continue
    revs[m.group(1)] = p.name
    downs[m.group(1)] = n.group(1) if (n and n.group(1)) else None
heads = set(revs) - {d for d in downs.values() if d}
roots = [r for r, d in downs.items() if d is None]
assert len(heads) == 1, f"expected ONE alembic head, found {sorted(heads)}"
assert len(roots) == 1, f"expected ONE baseline revision, found {roots}"
for r, d in downs.items():
    assert d is None or d in revs, f"{revs[r]}: down_revision {d!r} does not exist"
print(f"{len(revs)} revisions, one head ({sorted(heads)[0]}), chain resolves from the baseline")
PY
)"; then ok "${MIG}"; else no "alembic chain: ${MIG}"; fi

# cloud: registry, IAM, rollback
stage "target environment (read-only)"
if ! command -v gcloud >/dev/null 2>&1; then
  skip "gcloud not installed - registry digest, IAM and rollback checks not run"
elif ! gcloud auth print-access-token >/dev/null 2>&1; then
  skip "gcloud is not authenticated - registry digest, IAM and rollback checks not run"
elif [ ! -f "${ENV_FILE}" ]; then
  skip "no ${ENV_LABEL} - registry digest, IAM and rollback checks not run"
else
  CLOUD_RAN=1
  # shellcheck source=lib.sh
  source "${HERE}/lib.sh"; load_env
  # Every recorded digest must actually exist in the registry, and resolve to the same digest.
  for comp in ${REC_COMPONENTS:-}; do
    ref="$("${PY}" - "${REC}" "${comp}" <<'PY'
import json, sys
print((json.load(open(sys.argv[1]))["components"].get(sys.argv[2]) or {}).get("reference") or "")
PY
)"
    [ -n "${ref}" ] || continue
    if remote="$(gcloud artifacts docker images describe "${ref}" \
                   --format='value(image_summary.digest)' 2>/dev/null)"; then
      want="${ref##*@}"
      if [ -z "${remote}" ] || [ "${remote}" = "${want}" ]; then
        ok "${comp}: the recorded digest resolves in the registry"
      else
        no "${comp}: registry reports ${remote}, the record says ${want}"
      fi
    else
      no "${comp}: the recorded digest ${ref} cannot be resolved in the registry"
    fi
  done
  if ASSUME_YES=1 bash "${HERE}/preflight.sh" >/dev/null 2>&1; then
    ok "GCP preflight (session, project, region, IAM, reused resources)"
  else
    no "GCP preflight failed - run: bash deploy/gcp/scripts/preflight.sh"
  fi
  CUR="$(gcloud run jobs describe "${CLOUDRUN_MESH_JOB}" --region "${GCP_REGION}" \
          --format='value(spec.template.spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [ -n "${CUR}" ]; then
    ok "rollback target available: mesh job currently runs ${CUR}"
  else
    skip "no existing ready revision - first deployment, nothing to roll back to"
  fi
fi

# outcome
printf '\n%s' "${c_bold}"
if [ "${FAIL}" -ne 0 ]; then
  printf '%sGATE D FAILED - do not promote%s\n' "${c_red}" "${c_off}"
elif [ "${CLOUD_RAN}" -eq 1 ] && [ ${#SKIPPED[@]} -eq 0 ]; then
  printf 'DEPLOYMENT READY%s - artifact identity, configuration and cloud readiness all verified\n' \
    "${c_off}"
else
  printf '%sLOCAL PREFLIGHT PASSED / CLOUD READINESS UNVERIFIED%s\n' "${c_yel}" "${c_off}"
  printf '   The record, the artifact identity and the manifests are coherent.\n'
  printf '   This is NOT a statement that deployment will succeed.\n'
fi
if [ ${#SKIPPED[@]} -gt 0 ]; then
  printf '   checks NOT run (%d):\n' "${#SKIPPED[@]}"
  for s in "${SKIPPED[@]}"; do printf '     - %s\n' "${s}"; done
fi
printf '\n'
exit "${FAIL}"
