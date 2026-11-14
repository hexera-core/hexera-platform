#!/usr/bin/env bash
# Responsibility: Name the personal environments in hexera-dev that nobody has deployed for IDLE_DAYS.
# Owns: what "idle" means for the weekly sweep, and the keep list that exempts a slug from it.
# Boundaries: read-only - it deletes nothing; the sweep workflow hands each slug it names to destroy-env.sh.
# Collaborates with: .github/workflows/sweep-personal-envs.yml, deploy.yml's run-name, destroy-env.sh.

# IDLE MEANS "NO DEPLOY RUN NAMED IT". Every personal environment is created and updated by a
# deploy.yml dispatch, and every such run is titled `Deploy (<ref> -> <slug>)`, so the newest run
# carrying a slug is the last time anybody touched that environment on purpose. A run that is still
# queued or in progress counts as fresh. An environment with NO run on record is idle: GitHub keeps
# runs for months, so a slug missing from the history has not been deployed in at least that long.
#
# WHAT EXISTS COMES FROM THE CLOUD, NOT FROM THE HISTORY. A slug is a candidate only if something in
# the project still carries `deployment-id=dev-<slug>`. Matching on the label rather than on names
# is what keeps shared dev out: its label is the bare `dev`, which no `dev-<slug>` can equal.
#
# INPUTS   IDLE_DAYS (default 14). KEEP_SLUGS - slugs never named, comma or space separated.
#          SLUGS - judge only these and skip discovery (the destroy job's re-check before it deletes).
#          GH_TOKEN/GH_REPO for `gh run list`; an authenticated gcloud for discovery.
# OUTPUT   a report on stdout (and in the step summary); `idle=<json array>` in $GITHUB_OUTPUT.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

PROJECT_ID="hexera-dev"
GCP_REGION="${GCP_REGION:-us-central1}"
IDLE_DAYS="${IDLE_DAYS:-14}"
NOW="${NOW:-$(date -u +%s)}"
case "${IDLE_DAYS}" in
  ''|*[!0-9]*) die "IDLE_DAYS='${IDLE_DAYS}' is not a whole number of days" ;;
esac
[ "${IDLE_DAYS}" -ge 3 ] || die "IDLE_DAYS=${IDLE_DAYS} would sweep environments in active use; the floor is 3"
CUTOFF=$(( NOW - IDLE_DAYS * 86400 ))

# The same slug rules deploy.yml's picker and destroy-env.sh enforce. A label that fails them is not
# an environment this sweep may name, whatever it carries.
_valid_slug() {
  case "$1" in
    *[!a-z0-9-]*|[!a-z]*|*-|'') return 1 ;;
    dev|prod|production|shared|main|staging) return 1 ;;
  esac
  [ "${#1}" -le 14 ]
}

# ---------------------------------------------------------------------------------------------
# 1) the last deploy of every slug, from deploy.yml's run history
_history="$(gh run list --workflow deploy.yml --event workflow_dispatch --limit 2000 \
  --json displayTitle,updatedAt --jq '.[] | [.displayTitle, .updatedAt] | @tsv')" \
  || die "could not read deploy.yml's run history - without it every environment looks idle"
# AN EMPTY HISTORY IS A FAILURE, NOT AN ANSWER. Read as "nobody has deployed anything", it would
# name every environment in the project for deletion.
[ -n "${_history}" ] || die "deploy.yml has no manual runs on record - refusing to call everything idle"

declare -A LAST=()
while IFS=$'\t' read -r _title _at; do
  [[ "${_title}" =~ ^Deploy\ \(.*\ -\>\ ([a-z][a-z0-9-]*)\)$ ]] || continue
  _slug="${BASH_REMATCH[1]}"
  _ts="$(date -u -d "${_at}" +%s 2>/dev/null)" || continue
  if [ -z "${LAST[${_slug}]:-}" ] || [ "${_ts}" -gt "${LAST[${_slug}]}" ]; then
    LAST[${_slug}]="${_ts}"
  fi
done <<< "${_history}"

# ---------------------------------------------------------------------------------------------
# 2) the environments that exist, by the label every create-*.sh stamps
_discover() {
  {
    gcloud run services list --project "${PROJECT_ID}" --region "${GCP_REGION}" --format=json \
      | jq -r '.[].metadata.labels["deployment-id"] // empty'
    gcloud run jobs list --project "${PROJECT_ID}" --region "${GCP_REGION}" --format=json \
      | jq -r '.[].metadata.labels["deployment-id"] // empty'
    gcloud compute instance-templates list --project "${PROJECT_ID}" --format=json \
      | jq -r '.[].properties.labels["deployment-id"] // empty'
    gcloud storage buckets list --project "${PROJECT_ID}" --format=json \
      | jq -r '.[].labels["deployment-id"] // empty'
  } | sed -n 's/^dev-\(..*\)$/\1/p' | sort -u
}

if [ -n "${SLUGS:-}" ]; then
  # shellcheck disable=SC2086  # word-split on purpose: SLUGS is a list
  _found="$(printf '%s\n' ${SLUGS//,/ } | sort -u)"
else
  _found="$(_discover)" || die "could not list hexera-dev's resources"
fi

_keep=" $(printf '%s' "${KEEP_SLUGS:-}" | tr ',' ' ') "

# ---------------------------------------------------------------------------------------------
# 3) the verdict, per environment
_idle=()
_report="| slug | last deploy | verdict |
| --- | --- | --- |"
while read -r _slug; do
  [ -n "${_slug}" ] || continue
  if ! _valid_slug "${_slug}"; then
    _verdict="skipped - not a personal environment name"
    _when="-"
  else
    _ts="${LAST[${_slug}]:-}"
    if [ -n "${_ts}" ]; then
      _when="$(date -u -d "@${_ts}" +%Y-%m-%dT%H:%MZ) ($(( (NOW - _ts) / 86400 ))d ago)"
    else
      _when="none on record"
    fi
    if [[ "${_keep}" == *" ${_slug} "* ]]; then
      _verdict="kept - listed in PERSONAL_ENV_KEEP"
    elif [ -n "${_ts}" ] && [ "${_ts}" -ge "${CUTOFF}" ]; then
      _verdict="active"
    else
      _verdict="IDLE - will be destroyed"
      _idle+=("${_slug}")
    fi
  fi
  _report="${_report}
| ${_slug} | ${_when} | ${_verdict} |"
done <<< "${_found}"

info "Personal environments in ${PROJECT_ID} (idle = no deploy for ${IDLE_DAYS} days)"
printf '%s\n' "${_report}"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '### Personal environments (idle after %s days)\n\n%s\n' "${IDLE_DAYS}" "${_report}" \
    >> "${GITHUB_STEP_SUMMARY}"
fi

_json="$(printf '%s\n' "${_idle[@]+"${_idle[@]}"}" | jq -Rsc 'split("\n") | map(select(length > 0))')"
log "idle: ${_json}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'idle=%s\n' "${_json}" >> "${GITHUB_OUTPUT}"
fi
