import assert from "node:assert/strict";
import test from "node:test";

import {
  alignmentSecondsFor,
  instanceGroupSizeFilter,
  queueDepthFilter,
  readSeries,
  requestCountFilter,
  requestLatencyFilter,
  runInstanceCountFilter,
} from "./metrics";

const TARGET = {
  deploymentId: "hexera-dev",
  migName: "hexera-dev-workers",
  migZone: "us-central1-a",
  projectId: "hexera-dev",
  queueName: "simulation_jobs",
};

test("the queue-depth filter matches the series create-worker-fleet.sh publishes", () => {
  // These clauses are copied from the autoscaler's own filter. If they drift, the graph shows a
  // different series than the one the fleet actually scales on, which is worse than no graph.
  const filter = queueDepthFilter(TARGET);

  assert.match(filter, /metric\.type = "custom\.googleapis\.com\/hexera\/queue_depth"/);
  assert.match(filter, /resource\.type = "generic_task"/);
  assert.match(filter, /resource\.labels\.location = "us-central1-a"/);
  assert.match(filter, /resource\.labels\.namespace = "hexera-dev"/);
  assert.match(filter, /resource\.labels\.job = "queue-depth"/);
  assert.match(filter, /resource\.labels\.task_id = "simulation_jobs"/);
});

test("the group-size filter names the group", () => {
  const filter = instanceGroupSizeFilter(TARGET);
  assert.match(filter, /metric\.type = "compute\.googleapis\.com\/instance_group\/size"/);
  assert.match(filter, /resource\.labels\.instance_group_name = "hexera-dev-workers"/);
});

test("the request-count filter names one Cloud Run service", () => {
  const filter = requestCountFilter("hexera-dev-api");
  assert.match(filter, /metric\.type = "run\.googleapis\.com\/request_count"/);
  assert.match(filter, /resource\.labels\.service_name = "hexera-dev-api"/);
});

test("the latency filter names one Cloud Run service", () => {
  const filter = requestLatencyFilter("hexera-dev-api");
  assert.match(filter, /metric\.type = "run\.googleapis\.com\/request_latencies"/);
  assert.match(filter, /resource\.labels\.service_name = "hexera-dev-api"/);
});

test("the instance-count filter names one Cloud Run service", () => {
  const filter = runInstanceCountFilter("hexera-dev-api");
  assert.match(filter, /metric\.type = "run\.googleapis\.com\/container\/instance_count"/);
  assert.match(filter, /resource\.labels\.service_name = "hexera-dev-api"/);
});

test("longer windows align into coarser buckets", () => {
  // A week at one-minute resolution is ten thousand points per series, which no browser should be
  // asked to draw and no reader can see.
  assert.ok(alignmentSecondsFor("1h") < alignmentSecondsFor("24h"));
  assert.ok(alignmentSecondsFor("24h") < alignmentSecondsFor("7d"));
});

test("builds a request naming the project, the filter and the window", async () => {
  let seen: unknown = null;
  const client = {
    listTimeSeries: async (request: unknown) => {
      seen = request;
      return [[], null, {}] as never;
    },
  } as never;

  await readSeries(client, {
    filter: 'metric.type = "x"',
    projectId: "hexera-dev",
    window: "6h",
  });

  const request = seen as {
    aggregation: { alignmentPeriod: { seconds: number }; perSeriesAligner: string };
    filter: string;
    interval: { endTime: { seconds: number }; startTime: { seconds: number } };
    name: string;
  };

  assert.equal(request.name, "projects/hexera-dev");
  assert.equal(request.filter, 'metric.type = "x"');
  assert.equal(request.aggregation.perSeriesAligner, "ALIGN_MEAN");
  assert.equal(request.aggregation.alignmentPeriod.seconds, alignmentSecondsFor("6h"));
  assert.equal(request.interval.endTime.seconds - request.interval.startTime.seconds, 6 * 3600);
});

test("turns points into ascending plain values", async () => {
  // Monitoring returns points NEWEST FIRST. A chart drawn in that order runs backwards, and the
  // bug is invisible on a flat series.
  const client = {
    listTimeSeries: async () =>
      [
        [
          {
            metric: { labels: { response_code_class: "2xx" } },
            points: [
              { interval: { endTime: { seconds: 120 } }, value: { doubleValue: 5 } },
              { interval: { endTime: { seconds: 60 } }, value: { doubleValue: 3 } },
            ],
          },
        ],
        null,
        {},
      ] as never,
  } as never;

  const series = await readSeries(client, {
    filter: "f",
    groupByFields: ["metric.labels.response_code_class"],
    projectId: "hexera-dev",
    window: "1h",
  });

  assert.equal(series.length, 1);
  assert.equal(series[0].label, "2xx");
  assert.deepEqual(
    series[0].points.map((point) => point.value),
    [3, 5],
  );
  assert.equal(series[0].points[0].at, new Date(60_000).toISOString());
});

test("reads int64 values, which arrive as strings", async () => {
  const client = {
    listTimeSeries: async () =>
      [
        [{ points: [{ interval: { endTime: { seconds: 60 } }, value: { int64Value: "7" } }] }],
        null,
        {},
      ] as never,
  } as never;

  const series = await readSeries(client, { filter: "f", projectId: "p", window: "1h" });
  assert.equal(series[0].points[0].value, 7);
});

test("no data is an empty list, not an error", async () => {
  // The MIG size metric is unverified against the live project. The page must be able to say
  // "no series" rather than draw a flat line that reads as "we ran zero workers".
  const client = { listTimeSeries: async () => [[], null, {}] as never } as never;
  assert.deepEqual(await readSeries(client, { filter: "f", projectId: "p", window: "1h" }), []);
});
