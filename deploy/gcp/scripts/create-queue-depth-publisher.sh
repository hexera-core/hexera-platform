#!/usr/bin/env bash
# Responsibility: Provision the one writer of the queue-depth metric, and point the fleet's autoscaler at it.
# Owns: the publisher job, its schedule, its two narrow grants, and the autoscaler's METRIC WIRING.
# Boundaries: the autoscaler's sizing - floor, ceiling, cooldown, jobs-per-instance - belongs to the
#             admin console and is read back from the live policy rather than re-applied from env.
# Boundaries: it publishes a signal and states a policy; it never resizes the group itself and never touches a job in flight.

# Provision the SCHEDULED QUEUE-DEPTH PUBLISHER and reconcile the worker fleet's autoscaler.
# Idempotent: every resource is replaced or updated from this configuration, never duplicated.
#
# WHY IT IS NOT ON THE FLEET. `deploy/gcp/worker/queue_depth_exporter.py` ran as a systemd unit on
# every MIG instance, which made the fleet the only writer of the number that wakes the fleet: at
# zero instances nobody reports a backlog and the group can never come back. That is why the group's
# minimum is 1 today, and Decision 4 of the build-out plan is scale-to-zero in dev.
#
# WHY THE METRIC CHANGES SHAPE AT THE SAME TIME. Every instance published the same group-wide total
# against its own `gce_instance` resource, and `--custom-metric-utilization` AVERAGES a per-instance
# series across the group - so N identical copies of "3 queued" read as "3 per worker" however many
# workers existed, and the group jumped to max on any backlog. One writer removes the duplicate
# series, which lets the autoscaler use `--stackdriver-metric-single-instance-assignment`: desired
# size = total depth / the work one instance carries. That is proportional scaling, and it is the
# arithmetic the per-instance arrangement could not do.
#
# The old per-instance series is left alone. It is a different monitored resource, and the filter
# below selects only the new one - so an instance still running the retired unit cannot influence
# scaling, and no metric descriptor has to be deleted for this to be correct.
#
# INPUTS   the deployment env (APP_IMAGE, REDIS_URL, WORKER_MIG, WORKER_MIG_ZONE, thresholds)
# MUTATES  the publisher identity, its two grants, the Cloud Run job, the Cloud Scheduler job, and
#          the MIG's autoscaling policy. It does NOT change the group's minimum size.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
require_vars GCP_PROJECT_ID GCP_REGION DEPLOYMENT_ID

QD_JOB="${CLOUDRUN_QUEUE_DEPTH_JOB:-${DEPLOYMENT_ID}-queue-depth}"
QD_SA="${QUEUE_DEPTH_SERVICE_ACCOUNT:-${DEPLOYMENT_ID}-queue-depth}"
QD_SA_EMAIL="${QD_SA}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
QD_SCHEDULER="${QUEUE_DEPTH_SCHEDULER_JOB:-${DEPLOYMENT_ID}-queue-depth}"
# THE QUEUE, UNDER THE KEY THE WORKERS ACTUALLY WRITE.
#
# Celery on a Redis broker stores a queue as a Redis LIST named after the queue - but under
# `global_keyprefix` when one is set, the real key is <prefix><queue name>. queue_depth_publisher.py
# does a bare LLEN of whatever it is handed, so handing it the unprefixed name in an environment
# that uses a prefix reads a key nobody writes: the depth publishes as 0 forever and the autoscaler
# never adds an instance, however long the real queue gets. Nothing errors, which is what makes it
# worth stating here.
#
# Empty prefix - shared dev, production, a local stack - leaves this exactly `simulation_jobs`.
QUEUE_NAME="${QUEUE_NAME:-${REDIS_KEY_PREFIX:-}simulation_jobs}"
# THE GEOMETRY-CHECK QUEUE RIDES ALONG. celery_app.py routes the scout and the naming to
# `geometry_checks`, and every worker instance drains it from a slot of its own (worker/startup.sh).
# Its depth is published as a second series of the same metric - the `task_id` label tells the two
# apart - so a backlog of checks is visible where the fleet's backlog is. THE AUTOSCALER STILL
# READS QUEUE_NAME ALONE (the filter in step 7): a check is seconds of work and an instance takes
# minutes to arrive, so a fleet sized on that queue would add machines to a backlog that had
# already drained. Under the same prefix, for the reason given above.
GEOMETRY_QUEUE_NAME="${GEOMETRY_QUEUE_NAME:-${REDIS_KEY_PREFIX:-}geometry_checks}"
QUEUE_NAMES="${QUEUE_NAME},${GEOMETRY_QUEUE_NAME}"
# THE METRIC THE AUTOSCALER READS IS DEMAND, NOT DEPTH. `queue_depth` counts jobs WAITING, so a
# fleet sized on it shrinks to its floor the moment a burst drains - deleting whichever VMs it
# likes, including the ones running jobs. Shared dev, 2026-09-29: three scale-ins to 1 in two hours
# (05:41, 06:39, 07:32 UTC) took three running mesh jobs down with them, each failed half an hour
# later as "worker lost". `worker_demand` is queued + running (queue_depth_publisher.py counts a
# running job by its execution fence), so the group never shrinks below the work in flight. The
# depth series is still published and still charted by the admin console.
METRIC="custom.googleapis.com/hexera/worker_demand"
PROGRAM="${DEPLOY_DIR}/worker/queue_depth_publisher.py"
# The keyspace each running job's fence lives under - the publisher counts them there and nowhere
# else, so two environments sharing one Memorystore never size each other's fleets.
export REDIS_KEY_PREFIX="${REDIS_KEY_PREFIX:-}"

# A deployment without a worker fleet has nothing to scale and nothing to publish for. That is a
# skip and it is stated - unlike the migration, a missing signal here cannot corrupt anything.
if [ -z "${WORKER_MIG:-}" ] || [ -z "${WORKER_MIG_ZONE:-}" ] || [ -z "${REDIS_URL:-}" ]; then
  info "No worker fleet configured - skipping the queue-depth publisher"
  log "set WORKER_MIG, WORKER_MIG_ZONE and REDIS_URL to publish the depth this deployment scales on"
  exit 0
fi
require_digest_reference APP_IMAGE "${APP_IMAGE:-}"

info "Queue-depth publisher for ${WORKER_MIG} (${WORKER_MIG_ZONE}), queues ${QUEUE_NAMES} - sized on ${QUEUE_NAME}"

# Cloud Scheduler and Cloud Monitoring are enabled HERE rather than in enable-apis.sh: that list is
# what every deployment needs, and a mesh-only deployment has neither a fleet nor a metric.
gc services enable cloudscheduler.googleapis.com monitoring.googleapis.com \
  || warn "could not enable the Cloud Scheduler / Monitoring APIs - if they are already on, the
       steps below still work; if they are not, they fail and this is the command:
         gcloud services enable cloudscheduler.googleapis.com monitoring.googleapis.com --project ${GCP_PROJECT_ID}"

# 1) the publisher identity. One account is both the job's runtime identity and the identity Cloud
#    Scheduler authenticates as, because splitting them buys nothing here: the only right the
#    scheduler side holds is "execute the job that publishes this number", which is what the runtime
#    side does anyway.
if sa_exists "${QD_SA_EMAIL}"; then
  log "publisher identity ${QD_SA_EMAIL} (exists)"
else
  info "Creating publisher identity ${QD_SA_EMAIL}"
  gc iam service-accounts create "${QD_SA}" --display-name "Hexera queue-depth publisher" \
    || die "could not create ${QD_SA_EMAIL}. Creating identities needs iam.serviceAccountAdmin,
   which a DEPLOY identity is deliberately not given. Create it once, as an owner:
     gcloud iam service-accounts create ${QD_SA} --project ${GCP_PROJECT_ID} \\
       --display-name 'Hexera queue-depth publisher'"
fi

# 2) writing a time series is a PROJECT-level permission - Cloud Monitoring has no per-metric scope
#    to grant instead. metricWriter is the narrowest role that allows it and reads nothing.
#    A DEPLOY IDENTITY MAY NOT BE ABLE TO GRANT IT - project-level IAM is exactly what the deployer
#    was deliberately not given. The grant is attempted, a failure is reported with the command that
#    fixes it, and the SMOKE RUN below is the verdict: a publisher that cannot write the metric fails
#    there, before the autoscaler is ever pointed at a series that would not exist.
if gc projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
     --member "serviceAccount:${QD_SA_EMAIL}" \
     --role roles/monitoring.metricWriter >/dev/null 2>&1; then
  log "project += roles/monitoring.metricWriter -> ${QD_SA_EMAIL}"
else
  warn "could not grant roles/monitoring.metricWriter to ${QD_SA_EMAIL}. If it is already granted
       the smoke run below still succeeds; if it is not, that run fails and this is the command:
         gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \\
           --member serviceAccount:${QD_SA_EMAIL} --role roles/monitoring.metricWriter"
fi

# 3) the job. The program is embedded from its ONE authority in the repository; the digest of the
#    file that was embedded is logged, so what is running in the spec can be checked against it.
[ -f "${PROGRAM}" ] || die "publisher program not found: ${PROGRAM}"
read -r QUEUE_DEPTH_PROGRAM_B64 QUEUE_DEPTH_PROGRAM_SHA256 QUEUE_DEPTH_PROGRAM_BYTES <<<"$(
  python3 - "${PROGRAM}" <<'PY'
import base64, hashlib, sys
raw = open(sys.argv[1], "rb").read()
print(base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest(), len(raw))
PY
)"
log "program: ${PROGRAM#"${REPO_ROOT}/"} (${QUEUE_DEPTH_PROGRAM_BYTES} bytes, sha256 ${QUEUE_DEPTH_PROGRAM_SHA256:0:12})"

export QUEUE_DEPTH_PROGRAM_B64 QD_SA_EMAIL APP_IMAGE QUEUE_NAME QUEUE_NAMES
export QUEUE_DEPTH_SA_EMAIL="${QD_SA_EMAIL}"
export CLOUDRUN_QUEUE_DEPTH_JOB="${QD_JOB}"
export VPC_NETWORK="${VPC_NETWORK:-default}"
export VPC_SUBNET="${VPC_SUBNET:-default}"
rendered="$(render_manifest "${DEPLOY_DIR}/cloud-run/queue-depth-job.yaml")"
gc run jobs replace "${rendered}" --region "${GCP_REGION}"

# 4) the right to execute it, scoped to this job rather than granted project-wide.
gc run jobs add-iam-policy-binding "${QD_JOB}" --region "${GCP_REGION}" \
  --member "serviceAccount:${QD_SA_EMAIL}" --role roles/run.invoker >/dev/null
log "job/${QD_JOB} += roles/run.invoker -> ${QD_SA_EMAIL}"

# 5) ONE RUN NOW, waited on. It proves the three things the schedule cannot report on for a minute:
#    the broker is reachable over the VPC path, the identity may write a time series, and the
#    embedded program is the program. It also puts the FIRST data point in place before step 7
#    points an autoscaler at the series - the live autoscaler went CUSTOM_METRIC_INVALID once
#    already, for exactly the gap between "policy references a metric" and "metric has a value".
info "Publishing once, to prove the path before the autoscaler depends on it"
if ! gc run jobs execute "${QD_JOB}" --region "${GCP_REGION}" --wait; then
  die "the queue-depth publisher failed its first run - the autoscaler was NOT repointed, so the
   fleet keeps scaling on whatever it scales on today. Read why:
     gcloud run jobs executions list --job ${QD_JOB} --region ${GCP_REGION} --project ${GCP_PROJECT_ID}"
fi

# 6) the schedule. TWO minutes, not the one Cloud Scheduler could offer: measured against
#    hexera-dev, an execution takes ~75 s end to end, nearly all of it pulling the multi-gigabyte
#    application image. At a one-minute cadence the publisher overlaps itself, which costs a
#    continuously-running container and lets two writes to the same series arrive out of order -
#    which Cloud Monitoring rejects. Two minutes plus the 180 s cooldown bounds how long a fleet at
#    its floor takes to notice a backlog; for jobs budgeted in hours that is not the slow part.
SCHEDULE="${QUEUE_DEPTH_SCHEDULE:-*/2 * * * *}"
RUN_URI="https://${GCP_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT_ID}/jobs/${QD_JOB}:run"
scheduler_args=(
  --location "${GCP_REGION}"
  --schedule "${SCHEDULE}"
  --time-zone UTC
  --uri "${RUN_URI}"
  --http-method POST
  --oauth-service-account-email "${QD_SA_EMAIL}"
  --attempt-deadline 60s
)
if gc scheduler jobs describe "${QD_SCHEDULER}" --location "${GCP_REGION}" >/dev/null 2>&1; then
  info "Updating schedule ${QD_SCHEDULER} (${SCHEDULE})"
  gc scheduler jobs update http "${QD_SCHEDULER}" "${scheduler_args[@]}" >/dev/null
else
  info "Creating schedule ${QD_SCHEDULER} (${SCHEDULE})"
  gc scheduler jobs create http "${QD_SCHEDULER}" "${scheduler_args[@]}" \
    --description "Publishes ${QUEUE_NAME} depth for the ${WORKER_MIG} autoscaler" >/dev/null
fi
log "schedule ${QD_SCHEDULER}  ${SCHEDULE}  -> ${QD_JOB}"

# 7) the autoscaling policy, STATED rather than left at a bare utilization target (build-out plan,
#    item 6). The filter must select exactly ONE time series - that is the contract
#    single-instance-assignment is defined against - so it names every label that identifies it.
#
#    THIS STEP OWNS THE METRIC WIRING, NOT THE SIZING. `set-autoscaling` replaces the whole policy,
#    so repointing the filter necessarily rewrites the numbers alongside it. The numbers belong to
#    the ADMIN CONSOLE once an autoscaler exists (see create-worker-fleet.sh), so they are READ BACK
#    from the live autoscaler and passed through unchanged. The deployment's WORKER_MIG_* values are
#    used only when there is no autoscaler yet to read.
#
#    Skipping this step outright when an autoscaler exists was the simpler option and is wrong: the
#    filter is derived from the publisher's own resource labels, so a publisher that moved zone or
#    queue would leave the autoscaler pointed at a series nobody writes - which presents as
#    CUSTOM_METRIC_INVALID and a fleet pinned silently at its floor.
#
#    THE MINIMUM IS NOT LOWERED HERE. Scale-to-zero is possible: demand is published whether or not
#    an instance exists, and it now counts work in flight, so the group is never sized below the
#    jobs it is running. Which instance an AUTOSCALER scale-in removes is the group's choice - a
#    Compute Engine group cannot be told which of its VMs is idle - and on 2026-09-30 it chose the
#    busy one twice in half an hour. So the autoscaler now only scales out, and an idle worker
#    removes its own VM (create-worker-fleet.sh WORKER_SELF_RETIRE, runtime/idle_retire.py); the
#    mode is carried through below. A job on a VM that goes away anyway still hands itself back
#    (deploy/gcp/worker/shutdown.sh). The floor belongs to the admin console like the rest.
FILTER="resource.type = \"generic_task\""
FILTER="${FILTER} AND resource.labels.location = \"${WORKER_MIG_ZONE}\""
FILTER="${FILTER} AND resource.labels.namespace = \"${DEPLOYMENT_ID}\""
FILTER="${FILTER} AND resource.labels.job = \"queue-depth\""
FILTER="${FILTER} AND resource.labels.task_id = \"${QUEUE_NAME}\""
# The live numbers, if there are any. Read OFF THE GROUP, not from `compute autoscalers describe`.
#
# THAT COMMAND NO LONGER EXISTS. `gcloud compute autoscalers` is absent from GA, beta and alpha
# (582.0.0 answers `Invalid choice: 'autoscalers'`), so every read of it failed into the `|| true`
# below and returned an empty string. LIVE_MIN..LIVE_ASSIGNMENT were therefore ALWAYS empty, the
# fallbacks on the next lines handed this deployment's env values to `set-autoscaling`, and because
# set-autoscaling replaces the whole policy the floor, ceiling, cooldown and jobs-per-instance the
# admin console owns were silently overwritten on every run - the precise outcome the paragraph
# above says must not happen. It was invisible because the fallbacks make a total failure to read
# look exactly like "there is no autoscaler yet".
#
# `instance-groups managed describe` annotates the group with the autoscaler that TARGETS it (the
# same link `status.autoscaler` names), so this still assumes nothing about the autoscaler's name.
as_field() {
  gc compute instance-groups managed describe "${WORKER_MIG}" --zone "${WORKER_MIG_ZONE}" \
    --format="value($1)" 2>/dev/null || true
}
LIVE_MIN=""; LIVE_MAX=""; LIVE_COOLDOWN=""; LIVE_ASSIGNMENT=""
LIVE_SCALE_IN_FIXED=""; LIVE_SCALE_IN_PERCENT=""; LIVE_SCALE_IN_WINDOW=""; LIVE_METRICS=""
LIVE_MODE=""
if [ -n "$(as_field autoscaler.name)" ]; then
  LIVE_MODE="$(as_field autoscaler.autoscalingPolicy.mode)"
  LIVE_METRICS="$(as_field 'autoscaler.autoscalingPolicy.customMetricUtilizations[].metric')"
  LIVE_MIN="$(as_field autoscaler.autoscalingPolicy.minNumReplicas)"
  LIVE_MAX="$(as_field autoscaler.autoscalingPolicy.maxNumReplicas)"
  LIVE_COOLDOWN="$(as_field autoscaler.autoscalingPolicy.coolDownPeriodSec)"
  LIVE_ASSIGNMENT="$(as_field 'autoscaler.autoscalingPolicy.customMetricUtilizations[0].singleInstanceAssignment')"
  LIVE_SCALE_IN_FIXED="$(as_field autoscaler.autoscalingPolicy.scaleInControl.maxScaledInReplicas.fixed)"
  LIVE_SCALE_IN_PERCENT="$(as_field autoscaler.autoscalingPolicy.scaleInControl.maxScaledInReplicas.percent)"
  LIVE_SCALE_IN_WINDOW="$(as_field autoscaler.autoscalingPolicy.scaleInControl.timeWindowSec)"
fi

AS_MIN="${LIVE_MIN:-${WORKER_MIG_MIN_REPLICAS:-1}}"
AS_MAX="${LIVE_MAX:-${WORKER_MIG_MAX_REPLICAS:-5}}"
AS_COOLDOWN="${LIVE_COOLDOWN:-${WORKER_MIG_COOLDOWN_SECONDS:-180}}"
AS_ASSIGNMENT="${LIVE_ASSIGNMENT:-${WORKER_JOBS_PER_INSTANCE:-1}}"

# THE SCALE-IN CONTROL, carried through like the four numbers above - `set-autoscaling` replaces the
# whole policy, so a control the admin console set (its Fleet page edits it) would otherwise be
# wiped by every fleet deploy. When the live policy has NONE, this deployment's default is applied:
# at most one instance removed per half hour. That is what stops the churn of 2026-09-29, where each
# burst of queued jobs grew the group to 5 and the drain dropped it back to 1 in one step ten
# minutes later - and it bounds how often a scale-in can land on a machine that is mid-job, which
# the worker then survives by handing its job back (deploy/gcp/worker/shutdown.sh). A percentage
# set in the console is kept as a percentage.
AS_SCALE_IN_WINDOW="${LIVE_SCALE_IN_WINDOW:-${WORKER_SCALE_IN_WINDOW_SECONDS:-1800}}"
if [ -n "${LIVE_SCALE_IN_FIXED}" ]; then
  AS_SCALE_IN="max-scaled-in-replicas=${LIVE_SCALE_IN_FIXED},time-window=${AS_SCALE_IN_WINDOW}"
elif [ -n "${LIVE_SCALE_IN_PERCENT}" ]; then
  AS_SCALE_IN="max-scaled-in-replicas-percent=${LIVE_SCALE_IN_PERCENT},time-window=${AS_SCALE_IN_WINDOW}"
else
  AS_SCALE_IN="max-scaled-in-replicas=${WORKER_SCALE_IN_MAX_REPLICAS:-1},time-window=${AS_SCALE_IN_WINDOW}"
fi

# THE MODE, carried through - and it matters more than anything above. `set-autoscaling` writes
# mode ON unless told otherwise, and ON is the autoscaler choosing which VM to delete, busy or not.
# create-worker-fleet.sh sets `only-scale-out` when the workers remove themselves once idle
# (WORKER_SELF_RETIRE, runtime/idle_retire.py); a rewrite here that dropped it would hand scale-in
# back to the autoscaler on every deploy of this stage. The scale-in control above is still carried
# through: it does nothing while the mode is only-scale-out, and is in place if the mode goes back.
case "${LIVE_MODE}" in
  ONLY_SCALE_OUT|ONLY_UP) AS_MODE=only-scale-out ;;
  OFF) AS_MODE=off ;;
  *) AS_MODE=on ;;
esac

# THE DEPTH METRIC IS RETIRED FROM THE POLICY, not left beside the demand metric. `set-autoscaling`
# MERGES custom metrics - `--update-stackdriver-metric` replaces the entry of the same name and
# keeps every other one - so repointing an autoscaler that still scales on queue_depth would leave
# it scaling on both. The admin console reads and edits the FIRST entry as "jobs per instance",
# which could then be the stale one.
RETIRED_METRIC="custom.googleapis.com/hexera/queue_depth"
AS_RETIRE=()
case ";${LIVE_METRICS};" in
  *";${RETIRED_METRIC};"*) AS_RETIRE=(--remove-stackdriver-metric "${RETIRED_METRIC}") ;;
esac

# THE GROUP HAS TO EXIST BEFORE A POLICY CAN BE ATTACHED TO IT, and on a first deploy it does not.
#
# This stage owns the autoscaler's METRIC WIRING and runs at stage 13; create-worker-fleet.sh
# creates the managed instance group at stage 17 and deliberately never reconciles the policy - "an
# existing autoscaler's policy belongs to the admin console and is not reconciled here". That split
# is right, and it quietly assumes the group already exists, which is true of every environment
# whose fleet predates this tooling and false of every environment built from nothing:
#
#   ERROR: The resource '.../instanceGroupManagers/dev-workers' was not found
#
# The publisher, its schedule and its identity are all established above and are the valuable half
# of this stage - they are what proves the metric path. Only the attachment is deferred, with the
# one command that completes it, rather than failing a deploy that has done everything it could.
if ! gc compute instance-groups managed describe "${WORKER_MIG}" \
       --zone "${WORKER_MIG_ZONE}" >/dev/null 2>&1; then
  warn "managed instance group ${WORKER_MIG} does not exist yet, so there is nothing to attach the
       autoscaling policy to. The publisher and its schedule ARE in place and the metric is being
       written. Create the fleet, then reconcile this stage again to attach the policy:
         DEPLOY_COMPONENTS=workers  (this deploy, stage 17 - or tick `workers`)
         DEPLOY_COMPONENTS=queue    (a later run, once the group exists)"
  log "autoscaler      deferred - ${WORKER_MIG} not created yet"
  exit 0
fi

if [ -n "${LIVE_MIN}" ]; then
  info "Repointing the autoscaler for ${WORKER_MIG} at the metric, preserving its sizing"
  log "the floor, ceiling, cooldown and jobs-per-instance below were READ FROM THE LIVE"
  log "AUTOSCALER, not from this deployment's env - they belong to the admin console"
else
  info "Reconciling autoscaler for ${WORKER_MIG}"
fi
gc compute instance-groups managed set-autoscaling "${WORKER_MIG}" \
  --zone "${WORKER_MIG_ZONE}" \
  --min-num-replicas "${AS_MIN}" \
  --max-num-replicas "${AS_MAX}" \
  --cool-down-period "${AS_COOLDOWN}" \
  --update-stackdriver-metric "${METRIC}" \
  --stackdriver-metric-filter "${FILTER}" \
  --stackdriver-metric-single-instance-assignment "${AS_ASSIGNMENT}" \
  --scale-in-control "${AS_SCALE_IN}" \
  --mode "${AS_MODE}" \
  ${AS_RETIRE[@]+"${AS_RETIRE[@]}"}
log "autoscaler: ${AS_MIN}..${AS_MAX} instances,"
log "  one instance per ${AS_ASSIGNMENT} job queued or running, cooldown ${AS_COOLDOWN}s"
log "  mode ${AS_MODE}, scale-in control ${AS_SCALE_IN}"
log "done"
