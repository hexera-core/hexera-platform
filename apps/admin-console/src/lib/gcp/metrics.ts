import type { protos } from "@google-cloud/monitoring";

import type { MetricsReader } from "./clients";
import type { FleetTarget } from "./config";
import { toNumber, type Numeric } from "./fleet";

// Time series, and the filters that select them.
//
// THE FILTERS ARE COPIES, NOT INVENTIONS. The queue-depth filter below is the same five clauses
// create-worker-fleet.sh writes into the autoscaler policy. Graphing a series that is merely
// similar to the one the fleet scales on would be worse than graphing nothing: it would look
// authoritative and disagree with the autoscaler's own reading.

export type GraphWindow = "1h" | "6h" | "24h" | "7d";

export type Point = { at: string; value: number };
export type Series = { label: string; points: readonly Point[] };

// The aligner and the reducer are proto ENUMS, not free strings. Typing them as `string` is not
// merely loose: it makes TypeScript select the callback overload of listTimeSeries, and the call
// then returns `Promise<...> & void`, which cannot be destructured. Taking the types from the
// proto keeps the promise overload and rejects a misspelled aligner at compile time.
type Aggregation = protos.google.monitoring.v3.IAggregation;

export type SeriesRequest = {
  crossSeriesReducer?: Aggregation["crossSeriesReducer"];
  filter: string;
  groupByFields?: readonly string[];
  perSeriesAligner?: Aggregation["perSeriesAligner"];
  projectId: string;
  window: GraphWindow;
};

const WINDOW_SECONDS: Record<GraphWindow, number> = {
  "1h": 3600,
  "6h": 6 * 3600,
  "24h": 24 * 3600,
  "7d": 7 * 24 * 3600,
};

export const GRAPH_WINDOWS: readonly GraphWindow[] = ["1h", "6h", "24h", "7d"];

export function isGraphWindow(value: string | undefined): value is GraphWindow {
  return GRAPH_WINDOWS.includes(value as GraphWindow);
}

// Roughly 120 points per series at every window. Enough to see shape, few enough to draw.
export function alignmentSecondsFor(window: GraphWindow): number {
  return Math.max(60, Math.round(WINDOW_SECONDS[window] / 120 / 60) * 60);
}

export function windowSecondsFor(window: GraphWindow): number {
  return WINDOW_SECONDS[window];
}

export function queueDepthFilter(target: FleetTarget): string {
  return [
    'metric.type = "custom.googleapis.com/hexera/queue_depth"',
    'resource.type = "generic_task"',
    `resource.labels.location = "${target.migZone}"`,
    `resource.labels.namespace = "${target.deploymentId}"`,
    'resource.labels.job = "queue-depth"',
    `resource.labels.task_id = "${target.queueName}"`,
  ].join(" AND ");
}

// UNVERIFIED against the live project. If this series is empty, confirm the metric type and its
// resource labels before changing anything else:
//   gcloud monitoring time-series list --project <p> \
//     --filter 'metric.type = "compute.googleapis.com/instance_group/size"'
// The page renders an explicit "no series" state when it is empty, naming this metric, so a wrong
// guess here is visible rather than silent.
export function instanceGroupSizeFilter(target: FleetTarget): string {
  return [
    'metric.type = "compute.googleapis.com/instance_group/size"',
    `resource.labels.instance_group_name = "${target.migName}"`,
  ].join(" AND ");
}

export function requestCountFilter(service: string): string {
  return [
    'metric.type = "run.googleapis.com/request_count"',
    `resource.labels.service_name = "${service}"`,
  ].join(" AND ");
}

// Latency is a DISTRIBUTION, not a gauge. It is read with ALIGN_PERCENTILE_50 /
// ALIGN_PERCENTILE_95 as the per-series aligner, which is what turns the distribution into a
// number; asking for ALIGN_MEAN here returns the mean of the bucket counts, which is meaningless.
export function requestLatencyFilter(service: string): string {
  return [
    'metric.type = "run.googleapis.com/request_latencies"',
    `resource.labels.service_name = "${service}"`,
  ].join(" AND ");
}

export function runInstanceCountFilter(service: string): string {
  return [
    'metric.type = "run.googleapis.com/container/instance_count"',
    `resource.labels.service_name = "${service}"`,
  ].join(" AND ");
}

type RawPoint = protos.google.monitoring.v3.IPoint;

function asNumber(value: Numeric): number {
  return toNumber(value) ?? 0;
}

function valueOf(point: RawPoint): number {
  const value = point.value ?? {};
  if (value.doubleValue !== null && value.doubleValue !== undefined) return value.doubleValue;
  if (value.int64Value !== null && value.int64Value !== undefined) return asNumber(value.int64Value);
  return 0;
}

// The series label is whichever grouped label distinguishes it. With no grouping there is one
// series, and it takes the empty label the caller supplies a title for.
function labelOf(
  series: {
    metric?: { labels?: Record<string, string> | null } | null;
    resource?: { labels?: Record<string, string> | null } | null;
  },
  groupByFields: readonly string[],
): string {
  const parts = groupByFields
    .map((field) => {
      const key = field.split(".").pop() ?? "";
      return series.metric?.labels?.[key] ?? series.resource?.labels?.[key] ?? null;
    })
    .filter((part): part is string => Boolean(part));
  return parts.join(" ") || "";
}

export async function readSeries(client: MetricsReader, args: SeriesRequest): Promise<Series[]> {
  const endSeconds = Math.floor(Date.now() / 1000);
  const groupByFields = args.groupByFields ?? [];

  const [timeSeries] = await client.listTimeSeries({
    aggregation: {
      alignmentPeriod: { seconds: alignmentSecondsFor(args.window) },
      crossSeriesReducer: args.crossSeriesReducer ?? "REDUCE_SUM",
      groupByFields: [...groupByFields],
      perSeriesAligner: args.perSeriesAligner ?? "ALIGN_MEAN",
    },
    filter: args.filter,
    interval: {
      endTime: { seconds: endSeconds },
      startTime: { seconds: endSeconds - WINDOW_SECONDS[args.window] },
    },
    name: `projects/${args.projectId}`,
    view: "FULL",
  });

  return (timeSeries ?? []).map((series) => ({
    label: labelOf(series, groupByFields),
    points: (series.points ?? [])
      .map((point) => ({
        at: new Date(asNumber(point.interval?.endTime?.seconds) * 1000).toISOString(),
        value: valueOf(point),
      }))
      // Monitoring returns points newest first. Charts read left to right.
      .reverse(),
  }));
}

// Two derivations the request graphs need, kept here rather than in the page so they are testable
// and so "total" means the same thing everywhere.
//
// A REQUEST GRAPH SPLIT BY STATUS CLASS DOES NOT WORK AS ONE CHART. Four status classes need four
// reserved status colours, and that set fails the colour standard's CVD and normal-vision floors -
// warning and serious are 13.6 apart against this surface, below the floor of 15. The form is
// wrong, not the palette: total volume and error volume answer different questions and read better
// as two charts, which is what these two helpers feed.

export function sumSeries(series: readonly Series[], label = ""): Series[] {
  if (series.length === 0) return [];

  const totals = new Map<string, number>();
  for (const one of series) {
    for (const point of one.points) {
      totals.set(point.at, (totals.get(point.at) ?? 0) + point.value);
    }
  }

  return [
    {
      label,
      points: [...totals.entries()]
        .map(([at, value]) => ({ at, value }))
        .sort((a, b) => a.at.localeCompare(b.at)),
    },
  ];
}

export function pickSeries(series: readonly Series[], labels: readonly string[]): Series[] {
  // Ordered by the requested labels, not by what Monitoring happened to return, so the legend
  // reads the same way on every load.
  return labels
    .map((label) => series.find((one) => one.label === label))
    .filter((one): one is Series => Boolean(one));
}
