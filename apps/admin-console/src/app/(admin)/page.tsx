import { EmptyState, Panel } from "@/app/_components/panel";
import { TimeSeriesChart } from "@/app/_components/time-series-chart";
import { getFleetClients, getMetricsReader, getRunReader } from "@/lib/gcp/clients";
import { readAdminTargets } from "@/lib/gcp/config";
import { readFleetState } from "@/lib/gcp/fleet";
import { listFleetInstances } from "@/lib/gcp/instances";
import {
  instanceGroupSizeFilter,
  pickSeries,
  queueDepthFilter,
  readSeries,
  requestCountFilter,
  requestLatencyFilter,
  runInstanceCountFilter,
  sumSeries,
  type GraphWindow,
} from "@/lib/gcp/metrics";
import { readServiceScaling } from "@/lib/gcp/run";
import { readWorkerProfile } from "@/lib/gcp/worker-profile";

import { InstanceTable } from "./_fleet/instance-table";
import { ScalingPolicyPanel } from "./_fleet/scaling-policy";
import { ServiceScalingPanel } from "./_fleet/service-scaling";
import { WorkerProfilePanel } from "./_fleet/worker-profile";

// Every read on this page is live. Caching a fleet view is caching the answer to "what is it doing
// right now", which is the only question it is asked.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const WINDOW: GraphWindow = "24h";

export default async function FleetPage() {
  const targets = readAdminTargets(process.env);

  if (!targets.fleet) {
    return (
      <>
        <h1>Fleet</h1>
        <EmptyState note="This deployment declares no worker fleet. create-worker-fleet.sh skips the fleet when WORKER_MIG and WORKER_MIG_ZONE are unset, and pipeline work then runs on the operator's machine." />
      </>
    );
  }

  const fleetTarget = targets.fleet;
  const clients = getFleetClients();
  const metrics = getMetricsReader();
  const api = targets.apiService;

  // The fleet state is read first because the worker profile needs the template name it returns.
  const state = await readFleetState(clients, fleetTarget);

  const [profile, instances, workers, queue, byStatus, latency, runInstances, apiScaling] =
    await Promise.all([
      state.templateName
        ? readWorkerProfile(clients, fleetTarget.projectId, state.templateName)
        : Promise.resolve(null),
      listFleetInstances(clients, fleetTarget),
      readSeries(metrics, {
        filter: instanceGroupSizeFilter(fleetTarget),
        projectId: targets.projectId,
        window: WINDOW,
      }),
      readSeries(metrics, {
        filter: queueDepthFilter(fleetTarget),
        projectId: targets.projectId,
        window: WINDOW,
      }),
      api
        ? readSeries(metrics, {
            filter: requestCountFilter(api),
            groupByFields: ["metric.labels.response_code_class"],
            perSeriesAligner: "ALIGN_RATE",
            projectId: targets.projectId,
            window: WINDOW,
          })
        : Promise.resolve([]),
      // p50 and p95 are two reads of the same distribution, because one listTimeSeries call carries
      // one aligner. They are labelled here rather than by a metric label, since nothing in the
      // series itself distinguishes them.
      api
        ? Promise.all([
            readSeries(metrics, {
              filter: requestLatencyFilter(api),
              perSeriesAligner: "ALIGN_PERCENTILE_50",
              projectId: targets.projectId,
              window: WINDOW,
            }),
            readSeries(metrics, {
              filter: requestLatencyFilter(api),
              perSeriesAligner: "ALIGN_PERCENTILE_95",
              projectId: targets.projectId,
              window: WINDOW,
            }),
          ]).then(([p50, p95]) => [
            ...p50.map((one) => ({ ...one, label: "p50" })),
            ...p95.map((one) => ({ ...one, label: "p95" })),
          ])
        : Promise.resolve([]),
      api
        ? readSeries(metrics, {
            filter: runInstanceCountFilter(api),
            groupByFields: ["metric.labels.state"],
            projectId: targets.projectId,
            window: WINDOW,
          })
        : Promise.resolve([]),
      api
        ? readServiceScaling(getRunReader(), {
            projectId: targets.projectId,
            region: targets.region,
            service: api,
          })
        : Promise.resolve(null),
    ]);

  // The floor and the ceiling are what turn a worker count into a reading: "we ran five" becomes
  // "we sat at the ceiling for forty minutes", which is the sentence that justifies raising it.
  const bounds = [
    ...(state.policy?.minReplicas != null
      ? [{ label: "floor", value: state.policy.minReplicas }]
      : []),
    ...(state.policy?.maxReplicas != null
      ? [{ label: "ceiling", value: state.policy.maxReplicas }]
      : []),
  ];

  const requests = sumSeries(byStatus);
  const errors = pickSeries(byStatus, ["4xx", "5xx"]);
  const perSecond = (value: number) => `${value.toFixed(value < 1 ? 2 : 0)}/s`;
  const milliseconds = (value: number) => `${Math.round(value)}ms`;

  return (
    <>
      <h1>Fleet</h1>

      <ScalingPolicyPanel maxAllowedReplicas={targets.maxAllowedReplicas} state={state} />

      <div className="admin-grid-2">
        <Panel heading="last 24h" title="Workers up">
          <TimeSeriesChart
            emptyNote="No data for compute.googleapis.com/instance_group/size on this group. The instance table below is live regardless; this graph needs that metric to be published."
            kind="area"
            markers={bounds}
            series={workers}
          />
        </Panel>

        <Panel heading="last 24h" title="Queue depth">
          <TimeSeriesChart
            emptyNote="No data for custom.googleapis.com/hexera/queue_depth. The queue-depth publisher writes this series; an autoscaler reporting CUSTOM_METRIC_INVALID above is the same absence seen from the other side."
            markers={bounds}
            series={queue}
          />
        </Panel>

        <Panel heading="last 24h, per second" title="API requests">
          <TimeSeriesChart
            emptyNote="No request data. Either this deployment runs no API service, or it has served nothing in the window."
            format={perSecond}
            series={requests}
          />
        </Panel>

        <Panel heading="last 24h, per second" title="API errors">
          <TimeSeriesChart
            emptyNote="No 4xx or 5xx responses in the window."
            format={perSecond}
            series={errors}
          />
        </Panel>

        <Panel heading="last 24h" title="API latency">
          <TimeSeriesChart
            emptyNote="No latency data. A service that has served no requests reports no distribution to take a percentile of."
            format={milliseconds}
            series={latency}
          />
        </Panel>

        <Panel heading="last 24h, active vs idle" title="API instances">
          <TimeSeriesChart
            emptyNote="No instance-count data. A service at a floor of zero reports nothing while idle, which is itself the answer: every request in that state pays a container start."
            kind="area"
            series={runInstances}
          />
        </Panel>
      </div>

      {apiScaling ? (
        <ServiceScalingPanel
          maxAllowedReplicas={targets.maxAllowedReplicas}
          scaling={apiScaling}
        />
      ) : null}

      {profile ? <WorkerProfilePanel profile={profile} /> : null}

      <InstanceTable rows={instances} />
    </>
  );
}
