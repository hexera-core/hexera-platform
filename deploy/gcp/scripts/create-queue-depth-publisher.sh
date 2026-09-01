#!/usr/bin/env bash
# Responsibility: Provision the one writer of the queue-depth metric, and point the fleet's autoscaler at it.
# Owns: the publisher job, its schedule, its two narrow grants, and the autoscaling policy that reads its series.
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
QUEUE_NAME="${QUEUE_NAME:-simulation_jobs}"
METRIC="custom.googleapis.com/hexera/queue_depth"
PROGRAM="${DEPLOY_DIR}/worker/queue_depth_publisher.py"

# A deployment without a worker fleet has nothing to scale and nothing to publish for. That is a
# skip and it is stated - unlike the migration, a missing signal here cannot corrupt anything.
if [ -z "${WORKER_MIG:-}" ] || [ -z "${WORKER_MIG_ZONE:-}" ] || [ -z "${REDIS_URL:-}" ]; then
  info "No worker fleet configured - skipping the queue-depth publisher"
  log "set WORKER_MIG, WORKER_MIG_ZONE and REDIS_URL to publish the depth this deployment scales on"
  exit 0
fi
require_digest_reference APP_IMAGE "${APP_IMAGE:-}"

info "Queue-depth publisher for ${WORKER_MIG} (${WORKER_MIG_ZONE}), queue ${QUEUE_NAME}"

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

export QUEUE_DEPTH_PROGRAM_B64 QD_SA_EMAIL APP_IMAGE QUEUE_NAME
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
#    THE MINIMUM IS NOT LOWERED HERE. Scale-to-zero is now possible: the depth is published whether
#    or not an instance exists. It is not yet SAFE, because this metric counts queued work and not
#    work in flight, so a fleet allowed to reach zero can delete an instance that is mid-job. The
#    drain contract in item 6 - graceful shutdown, lease-aware deletion, scale-in control - is what
#    makes the floor a free choice. Until then the floor stays where the deployment set it.
FILTER="resource.type = \"generic_task\""
FILTER="${FILTER} AND resource.labels.location = \"${WORKER_MIG_ZONE}\""
FILTER="${FILTER} AND resource.labels.namespace = \"${DEPLOYMENT_ID}\""
FILTER="${FILTER} AND resource.labels.job = \"queue-depth\""
FILTER="${FILTER} AND resource.labels.task_id = \"${QUEUE_NAME}\""
info "Reconciling autoscaler for ${WORKER_MIG}"
gc compute instance-groups managed set-autoscaling "${WORKER_MIG}" \
  --zone "${WORKER_MIG_ZONE}" \
  --min-num-replicas "${WORKER_MIG_MIN_REPLICAS:-1}" \
  --max-num-replicas "${WORKER_MIG_MAX_REPLICAS:-5}" \
  --cool-down-period "${WORKER_MIG_COOLDOWN_SECONDS:-180}" \
  --update-stackdriver-metric "${METRIC}" \
  --stackdriver-metric-filter "${FILTER}" \
  --stackdriver-metric-single-instance-assignment "${WORKER_JOBS_PER_INSTANCE:-1}"
log "autoscaler: ${WORKER_MIG_MIN_REPLICAS:-1}..${WORKER_MIG_MAX_REPLICAS:-5} instances,"
log "  one instance per ${WORKER_JOBS_PER_INSTANCE:-1} queued job, cooldown ${WORKER_MIG_COOLDOWN_SECONDS:-180}s"
log "done"
