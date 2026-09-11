# Admin fleet observability (PR A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the admin console's placeholder Fleet page into a live read-only view of the worker
fleet — its scaling policy and autoscaler health, four time-series graphs, the worker profile, and
every managed instance.

**Architecture:** A thin typed layer over the official `@google-cloud/*` client libraries lives in
`apps/admin-console/src/lib/gcp/`. Every reader is a pure async function taking an injected client
object, so tests pass hand-built fakes and never touch the network. The Fleet page is a React Server
Component that calls the readers in parallel and hands plain serialisable data to a small number of
client components. No database, no VPC connector, no Server Actions — writes are PR B.

**Tech Stack:** Next 16 (App Router, Server Components), React 19, TypeScript 6, Tailwind 4,
Recharts 3, `@google-cloud/compute` 7.3.0, `@google-cloud/monitoring` 6.1.0, `@google-cloud/run`
4.1.0, `node:test` + `tsx` for unit tests, bash + pytest for deploy tooling.

**Spec:** `docs/superpowers/specs/2026-09-10-admin-fleet-and-billing-design.md` — this plan is
**PR A** of the three in §10. It implements §4.1, §4.2, §4.3, the read half of §7's IAM table, and
the read rows of §9's test table. It implements none of §4.4, §5, §6.

## Global Constraints

- **This PR grants no mutate authority.** The admin service account receives viewer roles only. If
  a task tempts you toward `compute.instanceGroupManagers.update`, you are in PR B.
- **No secret value is rendered, logged, or returned from a reader.** Instance metadata is a public
  surface (`create-worker-fleet.sh` establishes this at length); only keys on the allowlist in
  Task 4 have their values read.
- **No network in unit tests.** Every reader takes its client as a parameter. A test that
  constructs a real `InstanceGroupManagersClient` is a broken test.
- **Absent infrastructure is a rendered state, never a crash.** A deployment may have no fleet
  (`create-worker-fleet.sh` skips when `WORKER_MIG` is unset) and a fleet may have no autoscaler.
  Both render an explicit message.
- **Object literal keys are alphabetised**, matching every existing module in `apps/admin-console`
  and `apps/console`.
- **Tests run with** `pnpm --filter @hexera/admin-console test`, which is
  `node --import tsx --test src/**/*.test.ts`.
- **Deploy scripts must run on bash 3.2** (what macOS ships), like their siblings.
- **New deploy behaviour states its skip**, using the existing `info`/`log`/`warn` helpers.

## Verified API surface

Read from the installed packages on 2026-09-10. Do not re-derive these from the online reference;
they were checked against the typings and the compiled protos.

| Call | Shape |
|---|---|
| `InstanceGroupManagersClient.get` | `get({instanceGroupManager, project, zone})` → `Promise<[IInstanceGroupManager, ...]>` — destructure `[manager]` |
| `InstanceGroupManagersClient.listManagedInstances` | returns `Promise<[IManagedInstance[], nextRequest | null, rawResponse]>` — destructure `[instances]` |
| `AutoscalersClient.get` | `get({autoscaler, project, zone})` → `Promise<[IAutoscaler, ...]>`. Throws with `code === 5` (NOT_FOUND) when absent |
| `InstanceTemplatesClient.get` | `get({instanceTemplate, project})` → `Promise<[IInstanceTemplate, ...]>`. Templates are **global**, so there is no `zone` field |
| `MetricServiceClient.listTimeSeries` | `listTimeSeries({aggregation, filter, interval, name, view})` → `Promise<[ITimeSeries[], ...]>` |
| `ServicesClient.getService` | `getService({name})` → `Promise<[IService, ...]>`, `name` = `projects/P/locations/R/services/S` |

Field names, likewise verified:

- `IInstanceGroupManager`: `currentActions`, `instanceTemplate`, `name`, `status`, `targetSize`, `versions`, `zone`
- `IInstanceGroupManagerActionsSummary`: `abandoning`, `creating`, `deleting`, `none`, `recreating`, `refreshing`, `restarting`, `verifying`
- `IInstanceGroupManagerStatus`: `autoscaler`, `isStable`, `versionTarget`
- `IAutoscaler`: `autoscalingPolicy`, `name`, `recommendedSize`, `status`, `statusDetails`, `target`
- `IAutoscalingPolicy`: `coolDownPeriodSec`, `customMetricUtilizations`, `maxNumReplicas`, `minNumReplicas`, `mode`, `scaleInControl`
- `IAutoscalingPolicyCustomMetricUtilization`: `filter`, `metric`, `singleInstanceAssignment`, `utilizationTarget`
- `IAutoscalingPolicyScaleInControl`: `maxScaledInReplicas` (a `FixedOrPercent`), `timeWindowSec`
- `IFixedOrPercent`: `calculated`, `fixed`, `percent` — the deploy sets `fixed`, so that is the field read
- `IManagedInstance`: `currentAction`, `instance`, `instanceStatus`, `lastAttempt`, `name`, `version`
- `IManagedInstanceVersion`: `instanceTemplate`, `name`
- `IInstanceTemplate`: `name`, `properties`
- `IInstanceProperties`: `disks`, `machineType`, `metadata`, `networkInterfaces`, `serviceAccounts`
- `IAttachedDisk`: `boot`, `initializeParams`; `IAttachedDiskInitializeParams`: `diskSizeGb`, `diskType`, `sourceImage`
- `IMetadata`: `items` (array of `{key, value}`); `IServiceAccount`: `email`, `scopes`
- `ListTimeSeriesRequest`: `aggregation`, `filter`, `interval`, `name`, `orderBy`, `view`
- `Aggregation`: `alignmentPeriod`, `crossSeriesReducer`, `groupByFields`, `perSeriesAligner`

---

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `apps/admin-console/src/lib/gcp/config.ts` | Read the deployment's GCP targets out of the environment; say what is absent |
| `apps/admin-console/src/lib/gcp/config.test.ts` | Its tests |
| `apps/admin-console/src/lib/gcp/clients.ts` | Construct and memoise the real clients; declare the narrow injectable client types |
| `apps/admin-console/src/lib/gcp/fleet.ts` | The MIG's size, actions and stability, plus the autoscaler's policy and health |
| `apps/admin-console/src/lib/gcp/fleet.test.ts` | Its tests |
| `apps/admin-console/src/lib/gcp/worker-profile.ts` | The instance template, reduced to what a worker is |
| `apps/admin-console/src/lib/gcp/worker-profile.test.ts` | Its tests, including the metadata-value assertion |
| `apps/admin-console/src/lib/gcp/instances.ts` | One row per managed instance |
| `apps/admin-console/src/lib/gcp/instances.test.ts` | Its tests |
| `apps/admin-console/src/lib/gcp/metrics.ts` | Monitoring filters, alignment, and the series reader |
| `apps/admin-console/src/lib/gcp/metrics.test.ts` | Its tests |
| `apps/admin-console/src/lib/gcp/run.ts` | Cloud Run per-service scaling and current revision |
| `apps/admin-console/src/lib/gcp/run.test.ts` | Its tests |
| `apps/admin-console/src/app/_components/time-series-chart.tsx` | The one chart component every graph uses |
| `apps/admin-console/src/app/_components/panel.tsx` | Section frame, empty state and stat row |
| `apps/admin-console/src/app/(admin)/_fleet/scaling-policy.tsx` | Renders `ScalingPolicy` + autoscaler health |
| `apps/admin-console/src/app/(admin)/_fleet/worker-profile.tsx` | Renders `WorkerProfile` |
| `apps/admin-console/src/app/(admin)/_fleet/instance-table.tsx` | Renders `ManagedInstanceRow[]` |
| `tests/unit/deploy/test_admin_fleet_read_access.py` | The admin service is handed its fleet targets and granted read roles |

**Modified**

| Path | Change |
|---|---|
| `apps/admin-console/package.json` | Three `@google-cloud/*` deps, `recharts` |
| `apps/admin-console/src/app/(admin)/page.tsx` | Placeholder → the composed Fleet page |
| `apps/admin-console/src/app/globals.css` | Panel, stat, table and chart styles |
| `deploy/gcp/scripts/create-admin-service.sh` | Fleet-target env vars; read-role grants on the admin identity |
| `docs/deployment/admin-console-access.md` | A section on what the console can now read, and with which roles |

---

## Task 1: Deployment targets from the environment

**Files:**
- Create: `apps/admin-console/src/lib/gcp/config.ts`
- Test: `apps/admin-console/src/lib/gcp/config.test.ts`
- Modify: `apps/admin-console/package.json`

**Interfaces:**
- Consumes: nothing.
- Produces: `type FleetTarget = {deploymentId: string; migName: string; migZone: string; projectId: string; queueName: string}`; `type AdminTargets = {apiService: string | null; consoleService: string | null; deploymentId: string; fleet: FleetTarget | null; projectId: string; region: string}`; `function readAdminTargets(env: Record<string, string | undefined>): AdminTargets`.

- [ ] **Step 1: Add the dependencies**

```bash
pnpm --filter @hexera/admin-console add @google-cloud/compute@7.3.0 @google-cloud/monitoring@6.1.0 @google-cloud/run@4.1.0 recharts@3.10.1
```

- [ ] **Step 2: Write the failing test**

Create `apps/admin-console/src/lib/gcp/config.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";

import { readAdminTargets } from "./config";

const BASE = {
  DEPLOYMENT_ID: "hexera-dev",
  GCP_PROJECT_ID: "hexera-dev",
  GCP_REGION: "us-central1",
};

test("reads the fleet target when the deployment declares one", () => {
  const targets = readAdminTargets({
    ...BASE,
    QUEUE_NAME: "simulation_jobs",
    WORKER_MIG: "hexera-dev-workers",
    WORKER_MIG_ZONE: "us-central1-a",
  });

  assert.deepEqual(targets.fleet, {
    deploymentId: "hexera-dev",
    migName: "hexera-dev-workers",
    migZone: "us-central1-a",
    projectId: "hexera-dev",
    queueName: "simulation_jobs",
  });
});

test("a deployment with no fleet is absent, not an error", () => {
  // create-worker-fleet.sh skips the fleet entirely when WORKER_MIG is unset. The console must
  // render that as "no fleet in this deployment" rather than failing to load.
  assert.equal(readAdminTargets(BASE).fleet, null);
});

test("a fleet declared without its zone is absent", () => {
  // validate-config.sh refuses this combination at deploy time; the console must not then behave
  // as though a zonal group existed.
  assert.equal(readAdminTargets({ ...BASE, WORKER_MIG: "hexera-dev-workers" }).fleet, null);
});

test("the queue name defaults to the one create-worker-fleet.sh defaults to", () => {
  const targets = readAdminTargets({
    ...BASE,
    WORKER_MIG: "hexera-dev-workers",
    WORKER_MIG_ZONE: "us-central1-a",
  });

  assert.equal(targets.fleet?.queueName, "simulation_jobs");
});

test("refuses to build targets without a project", () => {
  assert.throws(() => readAdminTargets({ GCP_REGION: "us-central1" }), /GCP_PROJECT_ID/);
});

test("refuses to build targets without a region", () => {
  assert.throws(() => readAdminTargets({ GCP_PROJECT_ID: "hexera-dev" }), /GCP_REGION/);
});

test("Cloud Run services this deployment does not run are absent", () => {
  const targets = readAdminTargets(BASE);
  assert.equal(targets.apiService, null);
  assert.equal(targets.consoleService, null);
});
```

- [ ] **Step 3: Run it and watch it fail**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — `Cannot find module './config'`.

- [ ] **Step 4: Implement**

Create `apps/admin-console/src/lib/gcp/config.ts`:

```ts
// What this deployment's admin console is allowed to look at, read from the environment that
// create-admin-service.sh hands the container. Nothing here reaches the network.
//
// ABSENCE IS A VALUE, NOT AN ERROR. create-worker-fleet.sh skips the fleet when WORKER_MIG is
// unset, and a deployment may run no console. Those are states the pages render, so they are
// modelled as null rather than thrown - a missing fleet must not take the whole page down.

export type FleetTarget = {
  deploymentId: string;
  migName: string;
  migZone: string;
  projectId: string;
  queueName: string;
};

export type AdminTargets = {
  apiService: string | null;
  consoleService: string | null;
  deploymentId: string;
  fleet: FleetTarget | null;
  projectId: string;
  region: string;
};

type Env = Record<string, string | undefined>;

function required(env: Env, name: string): string {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(
      `${name} is not set on the admin console. create-admin-service.sh declares it; a container ` +
        `without it cannot know which project to read.`,
    );
  }
  return value;
}

function optional(env: Env, name: string): string | null {
  return env[name]?.trim() || null;
}

export function readAdminTargets(env: Env): AdminTargets {
  const projectId = required(env, "GCP_PROJECT_ID");
  const region = required(env, "GCP_REGION");
  const deploymentId = optional(env, "DEPLOYMENT_ID") ?? projectId;

  const migName = optional(env, "WORKER_MIG");
  const migZone = optional(env, "WORKER_MIG_ZONE");

  return {
    apiService: optional(env, "CLOUDRUN_API_SERVICE"),
    consoleService: optional(env, "CLOUDRUN_CONSOLE_SERVICE"),
    deploymentId,
    // Both or neither: a group name without its zone cannot be addressed, and validate-config.sh
    // already refuses that combination at deploy time.
    fleet:
      migName && migZone
        ? {
            deploymentId,
            migName,
            migZone,
            projectId,
            // The same default create-worker-fleet.sh uses, so the metric filter this feeds
            // matches the one the autoscaler was built with.
            queueName: optional(env, "QUEUE_NAME") ?? "simulation_jobs",
          }
        : null,
    projectId,
    region,
  };
}
```

- [ ] **Step 5: Run the tests**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS, 7 tests.

- [ ] **Step 6: Typecheck and lint**

Run: `pnpm --filter @hexera/admin-console typecheck && pnpm --filter @hexera/admin-console lint`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add apps/admin-console/package.json apps/admin-console/src/lib/gcp/config.ts \
  apps/admin-console/src/lib/gcp/config.test.ts pnpm-lock.yaml
git commit -m "feat(admin): read the deployment's GCP targets from the environment"
```

---

## Task 2: The client factory and the injectable client types

**Files:**
- Create: `apps/admin-console/src/lib/gcp/clients.ts`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `type FleetClients = {autoscalers: AutoscalerReader; instanceGroupManagers: ManagerReader; instanceTemplates: TemplateReader}`, `type MetricsReader`, `type RunReader`, and the four factories `getFleetClients()`, `getMetricsReader()`, `getRunReader()`. Tasks 3–7 depend **only on the narrow reader types**, never on the concrete client classes.

There is no test for this file. It constructs SDK objects and nothing else; a test would assert
that the SDK's constructor was called. The types it exports are what the tested modules consume,
and Task 1's `typecheck` step is what proves they line up.

- [ ] **Step 1: Implement**

Create `apps/admin-console/src/lib/gcp/clients.ts`:

```ts
import {
  AutoscalersClient,
  InstanceGroupManagersClient,
  InstanceTemplatesClient,
} from "@google-cloud/compute";
import { MetricServiceClient } from "@google-cloud/monitoring";
import { ServicesClient } from "@google-cloud/run";

// The clients, and the NARROW TYPES the readers are written against.
//
// Every reader in this directory takes its client as a parameter typed as one of the `Pick<>`
// aliases below, never as the concrete class. That is what lets the tests hand over a four-line
// object literal instead of standing up an SDK client with fake credentials - and it is why no
// test in this directory can accidentally reach the network.
//
// The clients are memoised at module scope because a Cloud Run container serves many requests and
// each client opens its own auth and connection pool. Building one per render would spend a token
// exchange on every page load.

export type ManagerReader = Pick<
  InstanceGroupManagersClient,
  "get" | "listManagedInstances"
>;
export type AutoscalerReader = Pick<AutoscalersClient, "get">;
export type TemplateReader = Pick<InstanceTemplatesClient, "get">;
export type MetricsReader = Pick<MetricServiceClient, "listTimeSeries">;
export type RunReader = Pick<ServicesClient, "getService">;

export type FleetClients = {
  autoscalers: AutoscalerReader;
  instanceGroupManagers: ManagerReader;
  instanceTemplates: TemplateReader;
};

let fleetClients: FleetClients | null = null;
let metricsReader: MetricsReader | null = null;
let runReader: RunReader | null = null;

export function getFleetClients(): FleetClients {
  fleetClients ??= {
    autoscalers: new AutoscalersClient(),
    instanceGroupManagers: new InstanceGroupManagersClient(),
    instanceTemplates: new InstanceTemplatesClient(),
  };
  return fleetClients;
}

export function getMetricsReader(): MetricsReader {
  metricsReader ??= new MetricServiceClient();
  return metricsReader;
}

export function getRunReader(): RunReader {
  runReader ??= new ServicesClient();
  return runReader;
}
```

- [ ] **Step 2: Typecheck**

Run: `pnpm --filter @hexera/admin-console typecheck`
Expected: no errors. If `Pick<>` complains that a method is not on the class, the version pin in
Task 1 drifted — fix the pin, not the type.

- [ ] **Step 3: Commit**

```bash
git add apps/admin-console/src/lib/gcp/clients.ts
git commit -m "feat(admin): memoised GCP clients and the narrow reader types"
```

---

## Task 3: Fleet state — size, actions, autoscaler policy and health

**Files:**
- Create: `apps/admin-console/src/lib/gcp/fleet.ts`
- Test: `apps/admin-console/src/lib/gcp/fleet.test.ts`

**Interfaces:**
- Consumes: `FleetTarget` (Task 1); `AutoscalerReader`, `ManagerReader` (Task 2).
- Produces: `type ScalingPolicy`, `type AutoscalerHealth`, `type FleetState`, and
  `async function readFleetState(clients: {autoscalers: AutoscalerReader; instanceGroupManagers: ManagerReader}, target: FleetTarget): Promise<FleetState>`.

- [ ] **Step 1: Write the failing test**

Create `apps/admin-console/src/lib/gcp/fleet.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";

import { readFleetState } from "./fleet";

const TARGET = {
  deploymentId: "hexera-dev",
  migName: "hexera-dev-workers",
  migZone: "us-central1-a",
  projectId: "hexera-dev",
  queueName: "simulation_jobs",
};

const MANAGER = {
  currentActions: { creating: 2, deleting: 0, none: 3, recreating: 0, verifying: 1 },
  instanceTemplate:
    "https://www.googleapis.com/compute/v1/projects/hexera-dev/global/instanceTemplates/hexera-dev-worker-tpl-abc123-1445b8",
  name: "hexera-dev-workers",
  status: { isStable: false },
  targetSize: 5,
};

const AUTOSCALER = {
  autoscalingPolicy: {
    coolDownPeriodSec: 180,
    customMetricUtilizations: [
      {
        filter: 'resource.type = "generic_task"',
        metric: "custom.googleapis.com/hexera/queue_depth",
        singleInstanceAssignment: 1,
      },
    ],
    maxNumReplicas: 5,
    minNumReplicas: 1,
  },
  name: "hexera-dev-workers",
  status: "ACTIVE",
  statusDetails: [],
};

function clients(manager: unknown, autoscaler: unknown | Error) {
  return {
    autoscalers: {
      get: async () => {
        if (autoscaler instanceof Error) throw autoscaler;
        return [autoscaler] as never;
      },
    },
    instanceGroupManagers: { get: async () => [manager] as never },
  } as never;
}

function notFound() {
  return Object.assign(new Error("The resource was not found"), { code: 5 });
}

test("reads the group's size, actions and stability", async () => {
  const state = await readFleetState(clients(MANAGER, AUTOSCALER), TARGET);

  assert.equal(state.targetSize, 5);
  assert.equal(state.isStable, false);
  assert.equal(state.currentActions.creating, 2);
  assert.equal(state.templateName, "hexera-dev-worker-tpl-abc123-1445b8");
});

test("reads the scaling policy the fleet actually runs on", async () => {
  const state = await readFleetState(clients(MANAGER, AUTOSCALER), TARGET);

  assert.deepEqual(state.policy, {
    cooldownSeconds: 180,
    jobsPerInstance: 1,
    maxReplicas: 5,
    metricType: "custom.googleapis.com/hexera/queue_depth",
    minReplicas: 1,
    scaleInMaxReplicas: null,
    scaleInWindowSeconds: null,
  });
});

test("surfaces an autoscaler that cannot read its metric", async () => {
  // This is the failure the panel exists for: the fleet sits silently at its floor while the
  // queue grows, and nothing else in the system says so.
  const broken = {
    ...AUTOSCALER,
    status: "ERROR",
    statusDetails: [{ message: "The custom metric is invalid", type: "CUSTOM_METRIC_INVALID" }],
  };
  const state = await readFleetState(clients(MANAGER, broken), TARGET);

  assert.equal(state.health.status, "ERROR");
  assert.deepEqual(state.health.details, [
    { message: "The custom metric is invalid", type: "CUSTOM_METRIC_INVALID" },
  ]);
});

test("a group with no autoscaler reports no policy rather than failing", async () => {
  const state = await readFleetState(clients(MANAGER, notFound()), TARGET);

  assert.equal(state.policy, null);
  assert.equal(state.health.status, "ABSENT");
  assert.equal(state.targetSize, 5);
});

test("an error that is not NOT_FOUND is not swallowed", async () => {
  // A permission failure must reach the page as an error. Treating it as "no autoscaler" would
  // render a fleet with no scaling policy, which is a different and untrue statement.
  const denied = Object.assign(new Error("Permission denied"), { code: 7 });
  await assert.rejects(() => readFleetState(clients(MANAGER, denied), TARGET), /Permission denied/);
});

test("missing counters read as zero, not undefined", async () => {
  const bare = { ...MANAGER, currentActions: {} };
  const state = await readFleetState(clients(bare, AUTOSCALER), TARGET);

  assert.equal(state.currentActions.deleting, 0);
  assert.equal(state.currentActions.none, 0);
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — `Cannot find module './fleet'`.

- [ ] **Step 3: Implement**

Create `apps/admin-console/src/lib/gcp/fleet.ts`:

```ts
import type { AutoscalerReader, ManagerReader } from "./clients";
import type { FleetTarget } from "./config";

// The managed instance group and the autoscaler that sizes it, reduced to the numbers a person
// reads to answer "is the fleet doing the right thing".
//
// WHY THE AUTOSCALER'S STATUS IS A FIRST-CLASS FIELD. An autoscaler whose custom metric has no
// data reports CUSTOM_METRIC_INVALID and holds the group at its floor. Nothing fails, nothing
// pages, and the queue grows. create-worker-fleet.sh records the project having been through
// exactly that once. This is the only surface that says so.

export type ScalingPolicy = {
  cooldownSeconds: number | null;
  jobsPerInstance: number | null;
  maxReplicas: number | null;
  metricType: string | null;
  minReplicas: number | null;
  scaleInMaxReplicas: number | null;
  scaleInWindowSeconds: number | null;
};

export type AutoscalerHealth = {
  details: readonly { message: string; type: string }[];
  status: string;
};

export type FleetState = {
  currentActions: Record<string, number>;
  health: AutoscalerHealth;
  isStable: boolean;
  policy: ScalingPolicy | null;
  targetSize: number;
  templateName: string | null;
};

// The counters worth naming. Listing them explicitly - rather than spreading whatever the API
// returned - means a new field in the proto cannot silently appear in the UI unlabelled.
const ACTION_KEYS = [
  "abandoning",
  "creating",
  "deleting",
  "none",
  "recreating",
  "refreshing",
  "restarting",
  "verifying",
] as const;

// A NOT_FOUND from the autoscaler read means the group has none, which is a legitimate state -
// a fleet at a fixed size. Any other code is a real failure and is rethrown, because rendering a
// permission error as "no scaling policy" states something untrue about the fleet.
const NOT_FOUND = 5;

function isNotFound(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { code?: number }).code === NOT_FOUND;
}

// Compute returns resource references as full self-links. The last segment is the name, which is
// the only part anyone reads.
function lastSegment(selfLink: string | null | undefined): string | null {
  if (!selfLink) return null;
  const name = selfLink.split("/").pop();
  return name || null;
}

function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function readFleetState(
  clients: { autoscalers: AutoscalerReader; instanceGroupManagers: ManagerReader },
  target: FleetTarget,
): Promise<FleetState> {
  const [manager] = await clients.instanceGroupManagers.get({
    instanceGroupManager: target.migName,
    project: target.projectId,
    zone: target.migZone,
  });

  let policy: ScalingPolicy | null = null;
  let health: AutoscalerHealth = { details: [], status: "ABSENT" };

  try {
    const [autoscaler] = await clients.autoscalers.get({
      autoscaler: target.migName,
      project: target.projectId,
      zone: target.migZone,
    });

    const raw = autoscaler.autoscalingPolicy ?? {};
    // The fleet scales on exactly one custom metric - create-worker-fleet.sh builds a filter that
    // must select a single time series, which is the contract singleInstanceAssignment is defined
    // against. Reading the first entry is therefore reading the only entry.
    const custom = raw.customMetricUtilizations?.[0];

    policy = {
      cooldownSeconds: toNumber(raw.coolDownPeriodSec),
      jobsPerInstance: toNumber(custom?.singleInstanceAssignment),
      maxReplicas: toNumber(raw.maxNumReplicas),
      metricType: custom?.metric ?? null,
      minReplicas: toNumber(raw.minNumReplicas),
      scaleInMaxReplicas: toNumber(raw.scaleInControl?.maxScaledInReplicas?.fixed),
      scaleInWindowSeconds: toNumber(raw.scaleInControl?.timeWindowSec),
    };
    health = {
      details: (autoscaler.statusDetails ?? []).map((detail) => ({
        message: detail.message ?? "",
        type: detail.type ?? "UNKNOWN",
      })),
      status: autoscaler.status ?? "UNKNOWN",
    };
  } catch (error) {
    if (!isNotFound(error)) throw error;
  }

  const actions = manager.currentActions ?? {};

  return {
    currentActions: Object.fromEntries(
      ACTION_KEYS.map((key) => [key, toNumber(actions[key]) ?? 0]),
    ),
    health,
    isStable: manager.status?.isStable ?? false,
    policy,
    targetSize: toNumber(manager.targetSize) ?? 0,
    templateName: lastSegment(manager.instanceTemplate),
  };
}
```

- [ ] **Step 4: Run the tests**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS, 13 tests total.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/lib/gcp/fleet.ts apps/admin-console/src/lib/gcp/fleet.test.ts
git commit -m "feat(admin): read fleet size, actions and autoscaler health"
```

---

## Task 4: The worker profile, with metadata values withheld

**Files:**
- Create: `apps/admin-console/src/lib/gcp/worker-profile.ts`
- Test: `apps/admin-console/src/lib/gcp/worker-profile.test.ts`

**Interfaces:**
- Consumes: `TemplateReader` (Task 2); `templateName` from `FleetState` (Task 3).
- Produces: `type WorkerProfile`, `const RENDERABLE_METADATA_KEYS`, and
  `async function readWorkerProfile(clients: {instanceTemplates: TemplateReader}, projectId: string, templateName: string): Promise<WorkerProfile>`.

This task carries the spec's §4.3 rule and its one deliberate exception, so read both before
writing code: metadata **keys** are listed, metadata **values** are withheld — except
`worker-image`, which is the pinned application digest the page exists to show.

- [ ] **Step 1: Write the failing test**

Create `apps/admin-console/src/lib/gcp/worker-profile.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";

import { readWorkerProfile } from "./worker-profile";

const TEMPLATE = {
  name: "hexera-dev-worker-tpl-abc123-1445b8",
  properties: {
    disks: [
      {
        boot: true,
        initializeParams: {
          diskSizeGb: "100",
          diskType: "pd-balanced",
          sourceImage: "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts",
        },
      },
    ],
    machineType: "e2-standard-4",
    metadata: {
      items: [
        { key: "worker-image", value: "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee" },
        { key: "redis-url", value: "redis://10.62.0.9:6379" },
        { key: "postgres-password-secret", value: "hexera-dev-postgres-password" },
        { key: "startup-script", value: "#!/usr/bin/env bash\nexport SOMETHING=secret\n" },
      ],
    },
    networkInterfaces: [{ network: "projects/p/global/networks/default", subnetwork: "projects/p/regions/us-central1/subnetworks/default" }],
    serviceAccounts: [
      { email: "hexera-dev-worker@hexera-dev.iam.gserviceaccount.com", scopes: ["https://www.googleapis.com/auth/cloud-platform"] },
    ],
  },
};

function clients(template: unknown = TEMPLATE) {
  return { instanceTemplates: { get: async () => [template] as never } } as never;
}

test("reads the shape of a worker", async () => {
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);

  assert.equal(profile.machineType, "e2-standard-4");
  assert.equal(profile.bootDiskGb, 100);
  assert.equal(profile.bootDiskType, "pd-balanced");
  assert.equal(profile.osImage, "ubuntu-2204-lts");
  assert.equal(profile.serviceAccount, "hexera-dev-worker@hexera-dev.iam.gserviceaccount.com");
  assert.deepEqual(profile.scopes, ["https://www.googleapis.com/auth/cloud-platform"]);
  assert.equal(profile.network, "default");
  assert.equal(profile.subnetwork, "default");
});

test("shows the pinned application digest, which is the point of the panel", async () => {
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);
  assert.equal(profile.workerImage, "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee");
});

test("lists metadata keys", async () => {
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);
  assert.deepEqual(profile.metadataKeys, [
    "postgres-password-secret",
    "redis-url",
    "startup-script",
    "worker-image",
  ]);
});

test("withholds every metadata value except the allowlisted one", async () => {
  // Instance metadata is readable by anyone holding compute.instances.get, and this page is one
  // template change away from printing whatever lands there next. The allowlist is the control;
  // this test is what stops it being widened by accident.
  const profile = await readWorkerProfile(clients(), "hexera-dev", TEMPLATE.name);
  const serialised = JSON.stringify(profile);

  assert.ok(!serialised.includes("redis://10.62.0.9:6379"));
  assert.ok(!serialised.includes("hexera-dev-postgres-password"));
  assert.ok(!serialised.includes("export SOMETHING=secret"));
});

test("a template with nothing in it renders as unknowns, not a crash", async () => {
  const profile = await readWorkerProfile(clients({ name: "bare" }), "hexera-dev", "bare");

  assert.equal(profile.machineType, null);
  assert.equal(profile.bootDiskGb, null);
  assert.deepEqual(profile.metadataKeys, []);
  assert.deepEqual(profile.scopes, []);
});

test("a non-boot disk is not mistaken for the boot disk", async () => {
  const twoDisks = {
    ...TEMPLATE,
    properties: {
      ...TEMPLATE.properties,
      disks: [
        { boot: false, initializeParams: { diskSizeGb: "500", diskType: "pd-ssd" } },
        ...TEMPLATE.properties.disks,
      ],
    },
  };
  const profile = await readWorkerProfile(clients(twoDisks), "hexera-dev", TEMPLATE.name);

  assert.equal(profile.bootDiskGb, 100);
  assert.equal(profile.bootDiskType, "pd-balanced");
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — `Cannot find module './worker-profile'`.

- [ ] **Step 3: Implement**

Create `apps/admin-console/src/lib/gcp/worker-profile.ts`:

```ts
import type { TemplateReader } from "./clients";

// What a worker IS, read from the instance template the group runs.
//
// METADATA VALUES ARE WITHHELD, AND THE ONE EXCEPTION IS NAMED.
// create-worker-fleet.sh spends four paragraphs establishing that instance metadata is a public
// surface: it is readable by anyone holding compute.instances.get, which is why that script writes
// secret NAMES there and never secret values. The names are not themselves sensitive, but a page
// that prints instance metadata wholesale is one template change away from printing a credential,
// and nothing would catch it. So this reader lists KEYS and withholds VALUES.
//
// `worker-image` is the exception, allowlisted rather than special-cased: it is the pinned
// application digest, and "which build is this fleet running" is the question the panel exists to
// answer. Adding a key here is a deliberate act with a test in front of it.
export const RENDERABLE_METADATA_KEYS: readonly string[] = ["worker-image"];

export type WorkerProfile = {
  bootDiskGb: number | null;
  bootDiskType: string | null;
  machineType: string | null;
  metadataKeys: readonly string[];
  network: string | null;
  osImage: string | null;
  scopes: readonly string[];
  serviceAccount: string | null;
  subnetwork: string | null;
  templateName: string;
  workerImage: string | null;
};

function lastSegment(value: string | null | undefined): string | null {
  if (!value) return null;
  const name = value.split("/").pop();
  return name || null;
}

function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function readWorkerProfile(
  clients: { instanceTemplates: TemplateReader },
  projectId: string,
  templateName: string,
): Promise<WorkerProfile> {
  // Instance templates are GLOBAL resources - there is no zone on this request, and passing one
  // is an error rather than a no-op.
  const [template] = await clients.instanceTemplates.get({
    instanceTemplate: templateName,
    project: projectId,
  });

  const properties = template.properties ?? {};
  const boot = properties.disks?.find((disk) => disk.boot) ?? null;
  const account = properties.serviceAccounts?.[0] ?? null;
  const nic = properties.networkInterfaces?.[0] ?? null;
  const items = properties.metadata?.items ?? [];

  const allowed = new Map(
    items
      .filter((item) => item.key && RENDERABLE_METADATA_KEYS.includes(item.key))
      .map((item) => [item.key as string, item.value ?? null]),
  );

  return {
    bootDiskGb: toNumber(boot?.initializeParams?.diskSizeGb),
    bootDiskType: lastSegment(boot?.initializeParams?.diskType),
    machineType: lastSegment(properties.machineType),
    metadataKeys: items
      .map((item) => item.key)
      .filter((key): key is string => Boolean(key))
      .sort(),
    network: lastSegment(nic?.network),
    osImage: lastSegment(boot?.initializeParams?.sourceImage),
    scopes: account?.scopes ?? [],
    serviceAccount: account?.email ?? null,
    subnetwork: lastSegment(nic?.subnetwork),
    templateName: template.name ?? templateName,
    workerImage: allowed.get("worker-image") ?? null,
  };
}
```

- [ ] **Step 4: Run the tests**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS, 19 tests total.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/lib/gcp/worker-profile.ts \
  apps/admin-console/src/lib/gcp/worker-profile.test.ts
git commit -m "feat(admin): read the worker profile, withholding metadata values"
```

---

## Task 5: One row per managed instance

**Files:**
- Create: `apps/admin-console/src/lib/gcp/instances.ts`
- Test: `apps/admin-console/src/lib/gcp/instances.test.ts`

**Interfaces:**
- Consumes: `FleetTarget` (Task 1), `ManagerReader` (Task 2).
- Produces: `type ManagedInstanceRow`, and
  `async function listFleetInstances(clients: {instanceGroupManagers: ManagerReader}, target: FleetTarget): Promise<ManagedInstanceRow[]>`.

- [ ] **Step 1: Write the failing test**

Create `apps/admin-console/src/lib/gcp/instances.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";

import { listFleetInstances } from "./instances";

const TARGET = {
  deploymentId: "hexera-dev",
  migName: "hexera-dev-workers",
  migZone: "us-central1-a",
  projectId: "hexera-dev",
  queueName: "simulation_jobs",
};

const INSTANCES = [
  {
    currentAction: "NONE",
    instance:
      "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-a1b2",
    instanceStatus: "RUNNING",
    version: { instanceTemplate: "projects/p/global/instanceTemplates/tpl-old" },
  },
  {
    currentAction: "CREATING",
    instance:
      "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/instances/hexera-dev-workers-c3d4",
    instanceStatus: "PROVISIONING",
    lastAttempt: { errors: { errors: [{ code: "QUOTA_EXCEEDED", message: "CPUS quota exceeded" }] } },
    version: { instanceTemplate: "projects/p/global/instanceTemplates/tpl-new" },
  },
];

function clients(instances: unknown[]) {
  return {
    instanceGroupManagers: { listManagedInstances: async () => [instances, null, {}] as never },
  } as never;
}

test("names each instance and where it is", async () => {
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);

  assert.equal(rows.length, 2);
  assert.equal(rows[0].name, "hexera-dev-workers-a1b2");
  assert.equal(rows[0].zone, "us-central1-a");
  assert.equal(rows[0].status, "RUNNING");
  assert.equal(rows[0].currentAction, "NONE");
});

test("reports the template version each instance is on", async () => {
  // Two template names in one table is how a half-finished roll becomes legible. Nothing else
  // says whether a rotation is still in flight.
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);

  assert.deepEqual(
    rows.map((row) => row.templateName),
    ["tpl-old", "tpl-new"],
  );
});

test("surfaces why an instance failed to come up", async () => {
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);
  assert.deepEqual(rows[1].lastErrors, ["QUOTA_EXCEEDED: CPUS quota exceeded"]);
});

test("an instance with no errors reports none", async () => {
  const rows = await listFleetInstances(clients(INSTANCES), TARGET);
  assert.deepEqual(rows[0].lastErrors, []);
});

test("an empty fleet is an empty list, not a failure", async () => {
  assert.deepEqual(await listFleetInstances(clients([]), TARGET), []);
});

test("rows are ordered by name so the table does not reshuffle between reads", async () => {
  const shuffled = [INSTANCES[1], INSTANCES[0]];
  const rows = await listFleetInstances(clients(shuffled), TARGET);

  assert.deepEqual(
    rows.map((row) => row.name),
    ["hexera-dev-workers-a1b2", "hexera-dev-workers-c3d4"],
  );
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — `Cannot find module './instances'`.

- [ ] **Step 3: Implement**

Create `apps/admin-console/src/lib/gcp/instances.ts`:

```ts
import type { ManagerReader } from "./clients";
import type { FleetTarget } from "./config";

// Every instance the group manages, as a table row.
//
// `listManagedInstances` returns a PAGINATED TUPLE - [instances, nextRequest, rawResponse] - and
// the library resolves every page before returning the first element. A fleet is bounded by its
// ceiling, currently five, so there is no case here where that is the wrong trade.

export type ManagedInstanceRow = {
  currentAction: string;
  lastErrors: readonly string[];
  name: string;
  status: string;
  templateName: string | null;
  zone: string | null;
};

function lastSegment(value: string | null | undefined): string | null {
  if (!value) return null;
  const name = value.split("/").pop();
  return name || null;
}

// A self-link is .../zones/<zone>/instances/<name>. The zone is the segment before "instances".
function zoneOf(selfLink: string | null | undefined): string | null {
  if (!selfLink) return null;
  const parts = selfLink.split("/");
  const index = parts.indexOf("zones");
  return index >= 0 ? (parts[index + 1] ?? null) : null;
}

export async function listFleetInstances(
  clients: { instanceGroupManagers: ManagerReader },
  target: FleetTarget,
): Promise<ManagedInstanceRow[]> {
  const [instances] = await clients.instanceGroupManagers.listManagedInstances({
    instanceGroupManager: target.migName,
    project: target.projectId,
    zone: target.migZone,
  });

  return (instances ?? [])
    .map((instance) => ({
      currentAction: instance.currentAction ?? "UNKNOWN",
      // The errors are nested twice: lastAttempt.errors is a wrapper whose own `errors` field is
      // the list. Reading one level less yields an object that renders as "[object Object]".
      lastErrors: (instance.lastAttempt?.errors?.errors ?? []).map(
        (error) => `${error.code ?? "ERROR"}: ${error.message ?? ""}`.trim(),
      ),
      name: lastSegment(instance.instance) ?? instance.name ?? "unknown",
      status: instance.instanceStatus ?? "UNKNOWN",
      templateName: lastSegment(instance.version?.instanceTemplate),
      zone: zoneOf(instance.instance) ?? target.migZone,
    }))
    // Compute returns instances in no guaranteed order. Sorting means a page that refreshes every
    // few seconds does not reshuffle its rows under the reader's cursor.
    .sort((a, b) => a.name.localeCompare(b.name));
}
```

- [ ] **Step 4: Run the tests**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS, 25 tests total.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/lib/gcp/instances.ts apps/admin-console/src/lib/gcp/instances.test.ts
git commit -m "feat(admin): list the fleet's managed instances"
```

---

## Task 6: Monitoring time series and their filters

**Files:**
- Create: `apps/admin-console/src/lib/gcp/metrics.ts`
- Test: `apps/admin-console/src/lib/gcp/metrics.test.ts`

**Interfaces:**
- Consumes: `FleetTarget` (Task 1), `MetricsReader` (Task 2).
- Produces: `type GraphWindow = "1h" | "6h" | "24h" | "7d"`, `type Point`, `type Series`;
  the filter builders `queueDepthFilter`, `instanceGroupSizeFilter`, `requestCountFilter`,
  `requestLatencyFilter`, `runInstanceCountFilter`; and
  `async function readSeries(client: MetricsReader, args: SeriesRequest): Promise<Series[]>` where
  `type SeriesRequest = {crossSeriesReducer?: string; filter: string; groupByFields?: readonly string[]; perSeriesAligner?: string; projectId: string; window: GraphWindow}`.

- [ ] **Step 1: Write the failing test**

Create `apps/admin-console/src/lib/gcp/metrics.test.ts`:

```ts
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
  // These five clauses are copied from the autoscaler's own filter. If they drift, the graph
  // shows a different series than the one the fleet actually scales on, which is worse than no
  // graph at all.
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
  let seen: Record<string, never> | null = null;
  const client = {
    listTimeSeries: async (request: never) => {
      seen = request;
      return [[], null, {}] as never;
    },
  } as never;

  await readSeries(client, {
    filter: 'metric.type = "x"',
    projectId: "hexera-dev",
    window: "6h",
  });

  const request = seen as unknown as {
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
  // §4.2 footnote 1: the MIG size metric is unverified. The page must be able to say "no series"
  // rather than draw a flat line that reads as "we ran zero workers".
  const client = { listTimeSeries: async () => [[], null, {}] as never } as never;
  assert.deepEqual(await readSeries(client, { filter: "f", projectId: "p", window: "1h" }), []);
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — `Cannot find module './metrics'`.

- [ ] **Step 3: Implement**

Create `apps/admin-console/src/lib/gcp/metrics.ts`:

```ts
import type { MetricsReader } from "./clients";
import type { FleetTarget } from "./config";

// Time series, and the filters that select them.
//
// THE FILTERS ARE COPIES, NOT INVENTIONS. The queue-depth filter below is the same five clauses
// create-worker-fleet.sh writes into the autoscaler policy. Graphing a series that is merely
// similar to the one the fleet scales on would be worse than graphing nothing: it would look
// authoritative and disagree with the autoscaler's own reading.

export type GraphWindow = "1h" | "6h" | "24h" | "7d";

export type Point = { at: string; value: number };
export type Series = { label: string; points: readonly Point[] };

export type SeriesRequest = {
  crossSeriesReducer?: string;
  filter: string;
  groupByFields?: readonly string[];
  perSeriesAligner?: string;
  projectId: string;
  window: GraphWindow;
};

const WINDOW_SECONDS: Record<GraphWindow, number> = {
  "1h": 3600,
  "6h": 6 * 3600,
  "24h": 24 * 3600,
  "7d": 7 * 24 * 3600,
};

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

// UNVERIFIED against the live project - spec §4.2, footnote 1. If this series is empty, confirm
// the metric type and its resource labels before changing anything else:
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

function valueOf(point: { value?: { doubleValue?: number | null; int64Value?: number | string | null } | null }): number {
  const value = point.value ?? {};
  if (value.doubleValue !== null && value.doubleValue !== undefined) return value.doubleValue;
  // int64 arrives as a STRING over JSON, because it does not fit a double. Number() is safe at the
  // magnitudes these gauges reach; the alternative is a BigInt that no chart library accepts.
  if (value.int64Value !== null && value.int64Value !== undefined) return Number(value.int64Value);
  return 0;
}

// The series label is whichever grouped label distinguishes it. With no grouping there is one
// series, and it takes the empty label the caller supplies a title for.
function labelOf(
  series: { metric?: { labels?: Record<string, string> | null } | null; resource?: { labels?: Record<string, string> | null } | null },
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
        at: new Date(Number(point.interval?.endTime?.seconds ?? 0) * 1000).toISOString(),
        value: valueOf(point),
      }))
      // Monitoring returns points newest first. Charts read left to right.
      .reverse(),
  }));
}
```

- [ ] **Step 4: Run the tests**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS, 35 tests total.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/lib/gcp/metrics.ts apps/admin-console/src/lib/gcp/metrics.test.ts
git commit -m "feat(admin): monitoring series reader and the fleet's metric filters"
```

---

## Task 7: Cloud Run scaling per service

**Files:**
- Create: `apps/admin-console/src/lib/gcp/run.ts`
- Test: `apps/admin-console/src/lib/gcp/run.test.ts`

**Interfaces:**
- Consumes: `RunReader` (Task 2).
- Produces: `type ServiceScaling`, and
  `async function readServiceScaling(client: RunReader, args: {projectId: string; region: string; service: string}): Promise<ServiceScaling>`.

- [ ] **Step 1: Write the failing test**

Create `apps/admin-console/src/lib/gcp/run.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";

import { readServiceScaling } from "./run";

const SERVICE = {
  latestReadyRevision: "projects/p/locations/us-central1/services/hexera-dev-api/revisions/hexera-dev-api-00042-abc",
  name: "projects/p/locations/us-central1/services/hexera-dev-api",
  template: {
    containers: [{ image: "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee" }],
    scaling: { maxInstanceCount: 5, minInstanceCount: 1 },
  },
};

function client(service: unknown = SERVICE) {
  return { getService: async () => [service] as never } as never;
}

test("addresses the service by its fully qualified name", async () => {
  let seen: { name?: string } = {};
  const spy = {
    getService: async (request: { name?: string }) => {
      seen = request;
      return [SERVICE] as never;
    },
  } as never;

  await readServiceScaling(spy, { projectId: "p", region: "us-central1", service: "hexera-dev-api" });
  assert.equal(seen.name, "projects/p/locations/us-central1/services/hexera-dev-api");
});

test("reads the warm floor and the ceiling", async () => {
  const scaling = await readServiceScaling(client(), {
    projectId: "p",
    region: "us-central1",
    service: "hexera-dev-api",
  });

  assert.equal(scaling.minInstances, 1);
  assert.equal(scaling.maxInstances, 5);
  assert.equal(scaling.name, "hexera-dev-api");
  assert.equal(scaling.latestRevision, "hexera-dev-api-00042-abc");
  assert.equal(scaling.imageDigest, "us-central1-docker.pkg.dev/p/mesh/app@sha256:c0ffee");
});

test("an unset floor reads as zero, which is what Cloud Run means by it", async () => {
  // Cloud Run omits minInstanceCount when it is 0. Reporting null would render "unknown" for the
  // most common and most consequential state - a service that scales to zero and cold-starts.
  const scaleToZero = { ...SERVICE, template: { ...SERVICE.template, scaling: { maxInstanceCount: 3 } } };
  const scaling = await readServiceScaling(client(scaleToZero), {
    projectId: "p",
    region: "us-central1",
    service: "hexera-dev-api",
  });

  assert.equal(scaling.minInstances, 0);
  assert.equal(scaling.maxInstances, 3);
});

test("a service with no template renders unknowns rather than throwing", async () => {
  const scaling = await readServiceScaling(client({ name: "projects/p/locations/r/services/s" }), {
    projectId: "p",
    region: "us-central1",
    service: "s",
  });

  assert.equal(scaling.minInstances, 0);
  assert.equal(scaling.maxInstances, null);
  assert.equal(scaling.imageDigest, null);
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pnpm --filter @hexera/admin-console test`
Expected: FAIL — `Cannot find module './run'`.

- [ ] **Step 3: Implement**

Create `apps/admin-console/src/lib/gcp/run.ts`:

```ts
import type { RunReader } from "./clients";

// A Cloud Run service's warm floor, its ceiling, and what it is currently running.
//
// The FLOOR is the cold-start control for the request-serving tier, the same way the MIG's
// minNumReplicas is for the worker fleet. It is read here and made writable in PR B.

export type ServiceScaling = {
  imageDigest: string | null;
  latestRevision: string | null;
  maxInstances: number | null;
  minInstances: number;
  name: string;
};

function lastSegment(value: string | null | undefined): string | null {
  if (!value) return null;
  const name = value.split("/").pop();
  return name || null;
}

export async function readServiceScaling(
  client: RunReader,
  args: { projectId: string; region: string; service: string },
): Promise<ServiceScaling> {
  const [service] = await client.getService({
    name: `projects/${args.projectId}/locations/${args.region}/services/${args.service}`,
  });

  const scaling = service.template?.scaling ?? {};

  return {
    imageDigest: service.template?.containers?.[0]?.image ?? null,
    latestRevision: lastSegment(service.latestReadyRevision),
    maxInstances: scaling.maxInstanceCount ?? null,
    // Cloud Run omits minInstanceCount when it is zero. Zero is the meaningful reading - it is
    // what "this service cold-starts" looks like - so it is reported as a number, not as unknown.
    minInstances: scaling.minInstanceCount ?? 0,
    name: lastSegment(service.name) ?? args.service,
  };
}
```

- [ ] **Step 4: Run the tests**

Run: `pnpm --filter @hexera/admin-console test`
Expected: PASS, 39 tests total.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/lib/gcp/run.ts apps/admin-console/src/lib/gcp/run.test.ts
git commit -m "feat(admin): read Cloud Run per-service scaling and current revision"
```

---

## Task 8: The chart and the panel chrome

**Files:**
- Create: `apps/admin-console/src/app/_components/time-series-chart.tsx`
- Create: `apps/admin-console/src/app/_components/panel.tsx`
- Modify: `apps/admin-console/src/app/globals.css`

**Interfaces:**
- Consumes: `Series` (Task 6).
- Produces: `<TimeSeriesChart series={Series[]} kind="area" | "line" markers?={{label: string; value: number}[]} emptyNote={string} />` (client component) and `<Panel title heading? children>`, `<EmptyState note>`, `<StatRow stats={{label: string; value: string}[]}>` (server components).

**Before writing chart code, invoke the `dataviz` skill.** It is the project's authority on chart
form, colour and axis treatment, and this task creates the one chart component every graph on the
page reuses — a decision made once here propagates to all four.

- [ ] **Step 1: Build the panel chrome**

Create `apps/admin-console/src/app/_components/panel.tsx`:

```tsx
// The shared frame for every block on an admin page: a titled section, the empty state that says
// why a block has nothing to show, and a row of labelled figures.
//
// EMPTY IS A SENTENCE, NOT A BLANK. Spec §4.2 and §6 both turn on this: a chart with no series
// must say which metric it looked for, because "we ran zero workers" and "this metric is not
// published" look identical when both render as a flat line at zero.

export function Panel({
  children,
  heading,
  title,
}: {
  children: React.ReactNode;
  heading?: string;
  title: string;
}) {
  return (
    <section className="admin-panel">
      <header className="admin-panel-head">
        <h2 className="admin-panel-title">{title}</h2>
        {heading ? <p className="admin-panel-heading">{heading}</p> : null}
      </header>
      {children}
    </section>
  );
}

export function EmptyState({ note }: { note: string }) {
  return <p className="admin-empty">{note}</p>;
}

export function StatRow({ stats }: { stats: readonly { label: string; value: string }[] }) {
  return (
    <dl className="admin-stats">
      {stats.map((stat) => (
        <div className="admin-stat" key={stat.label}>
          <dt>{stat.label}</dt>
          <dd>{stat.value}</dd>
        </div>
      ))}
    </dl>
  );
}
```

- [ ] **Step 2: Build the chart**

Create `apps/admin-console/src/app/_components/time-series-chart.tsx`. It must be a client
component — Recharts measures the DOM — and it must handle the empty case before rendering
anything:

```tsx
"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { Series } from "@/lib/gcp/metrics";

// One chart for every graph on the Fleet page.
//
// The MARKERS are the reason this component takes more than a series: a worker-count line means
// little on its own and means a great deal against the floor and the ceiling it is bounded by.
// "We ran five workers" becomes "we sat at the ceiling for forty minutes", which is the reading
// that justifies raising it.

export type Marker = { label: string; value: number };

// Palette placeholder - replace with the values the dataviz skill's reference palette specifies.
const STROKES = ["#536071", "#8a6f4e", "#6b7f6a", "#7a5b6e", "#4f6b78"];

export function TimeSeriesChart({
  emptyNote,
  kind = "line",
  markers = [],
  series,
}: {
  emptyNote: string;
  kind?: "area" | "line";
  markers?: readonly Marker[];
  series: readonly Series[];
}) {
  const hasPoints = series.some((one) => one.points.length > 0);
  if (!hasPoints) {
    return <p className="admin-empty">{emptyNote}</p>;
  }

  // Recharts wants one row per timestamp with a column per series. The series are aligned to the
  // same alignmentPeriod by readSeries, so their timestamps line up and a union of keys is safe.
  const byTime = new Map<string, Record<string, number | string>>();
  for (const one of series) {
    for (const point of one.points) {
      const row = byTime.get(point.at) ?? { at: point.at };
      row[one.label || "value"] = point.value;
      byTime.set(point.at, row);
    }
  }
  const rows = [...byTime.values()].sort((a, b) => String(a.at).localeCompare(String(b.at)));
  const keys = series.map((one) => one.label || "value");

  const Chart = kind === "area" ? AreaChart : LineChart;

  return (
    <div className="admin-chart">
      <ResponsiveContainer height={220} width="100%">
        <Chart data={rows} margin={{ bottom: 4, left: 0, right: 8, top: 8 }}>
          <CartesianGrid stroke="#e6e2da" vertical={false} />
          <XAxis
            dataKey="at"
            minTickGap={48}
            stroke="#68615a"
            tickFormatter={(at: string) =>
              new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
            }
            tickLine={false}
          />
          <YAxis allowDecimals={false} stroke="#68615a" tickLine={false} width={40} />
          <Tooltip
            labelFormatter={(at: string) => new Date(at).toLocaleString()}
            wrapperClassName="admin-tooltip"
          />
          {keys.length > 1 ? <Legend /> : null}
          {markers.map((marker) => (
            <ReferenceLine
              key={marker.label}
              label={{ fill: "#68615a", fontSize: 11, position: "right", value: marker.label }}
              stroke="#b9b2a8"
              strokeDasharray="4 4"
              y={marker.value}
            />
          ))}
          {keys.map((key, index) =>
            kind === "area" ? (
              <Area
                dataKey={key}
                fill={STROKES[index % STROKES.length]}
                fillOpacity={0.16}
                key={key}
                stroke={STROKES[index % STROKES.length]}
                strokeWidth={2}
                type="stepAfter"
              />
            ) : (
              <Line
                dataKey={key}
                dot={false}
                key={key}
                stroke={STROKES[index % STROKES.length]}
                strokeWidth={2}
                type="monotone"
              />
            ),
          )}
        </Chart>
      </ResponsiveContainer>
    </div>
  );
}
```

- [ ] **Step 3: Add the styles**

Append to `apps/admin-console/src/app/globals.css` — the existing file's conventions are flat class
names and alphabetised declarations:

```css
.admin-panel {
  background: rgba(255, 255, 255, 0.6);
  border: 1px solid #ddd9d2;
  border-radius: 12px;
  margin: 0 0 1.5rem;
  padding: 1.25rem 1.5rem 1.5rem;
}

.admin-panel-head {
  align-items: baseline;
  display: flex;
  gap: 1rem;
  justify-content: space-between;
  margin: 0 0 1rem;
}

.admin-panel-title {
  font-size: 1rem;
  font-weight: 660;
  margin: 0;
}

.admin-panel-heading {
  color: #68615a;
  font-size: 0.85rem;
  margin: 0;
}

.admin-empty {
  color: #68615a;
  font-size: 0.9rem;
  line-height: 1.5;
  margin: 0;
}

.admin-stats {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  margin: 0;
}

.admin-stat dt {
  color: #68615a;
  font-size: 0.78rem;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.admin-stat dd {
  font-size: 1.5rem;
  font-weight: 660;
  margin: 0.15rem 0 0;
}

.admin-chart {
  margin: 0.5rem 0 0;
}

.admin-table {
  border-collapse: collapse;
  font-size: 0.88rem;
  width: 100%;
}

.admin-table th {
  border-bottom: 1px solid #ddd9d2;
  color: #68615a;
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  padding: 0.4rem 0.6rem 0.4rem 0;
  text-align: left;
  text-transform: uppercase;
}

.admin-table td {
  border-bottom: 1px solid #efece6;
  padding: 0.5rem 0.6rem 0.5rem 0;
  vertical-align: top;
}

.admin-alert {
  background: rgba(150, 60, 40, 0.08);
  border: 1px solid rgba(150, 60, 40, 0.3);
  border-radius: 8px;
  color: #7a3326;
  font-size: 0.9rem;
  margin: 0 0 1rem;
  padding: 0.75rem 1rem;
}

.admin-main {
  max-width: 72rem;
}

.admin-grid-2 {
  display: grid;
  gap: 1.5rem;
  grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr));
}
```

Note `.admin-main` already exists earlier in the file with `max-width: 960px`. Change that
declaration in place rather than adding a second rule — the fleet page needs the wider column.

- [ ] **Step 4: Typecheck and lint**

Run: `pnpm --filter @hexera/admin-console typecheck && pnpm --filter @hexera/admin-console lint`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/app/_components/panel.tsx \
  apps/admin-console/src/app/_components/time-series-chart.tsx \
  apps/admin-console/src/app/globals.css
git commit -m "feat(admin): shared panel chrome and the time-series chart"
```

---

## Task 9: The Fleet page

**Files:**
- Create: `apps/admin-console/src/app/(admin)/_fleet/scaling-policy.tsx`
- Create: `apps/admin-console/src/app/(admin)/_fleet/worker-profile.tsx`
- Create: `apps/admin-console/src/app/(admin)/_fleet/instance-table.tsx`
- Modify: `apps/admin-console/src/app/(admin)/page.tsx`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: the rendered page. Nothing later in this PR depends on it.

- [ ] **Step 1: The three presentational components**

Create `apps/admin-console/src/app/(admin)/_fleet/scaling-policy.tsx`:

```tsx
import { EmptyState, Panel, StatRow } from "@/app/_components/panel";
import type { FleetState } from "@/lib/gcp/fleet";

// The policy the fleet runs on, and - the reason this panel is first on the page - whether the
// autoscaler can actually read the metric it scales on. An autoscaler in ERROR holds the group at
// its floor while the queue grows, and nothing else in the system says so.
export function ScalingPolicyPanel({ state }: { state: FleetState }) {
  const { health, policy } = state;

  return (
    <Panel
      heading={state.isStable ? "stable" : "reconciling"}
      title="Scaling policy"
    >
      {health.status !== "ACTIVE" && health.status !== "ABSENT" ? (
        <p className="admin-alert">
          Autoscaler status <strong>{health.status}</strong>
          {health.details.length > 0
            ? `: ${health.details.map((detail) => `${detail.type} — ${detail.message}`).join("; ")}`
            : "."}{" "}
          While this persists the group holds at its floor regardless of queue depth.
        </p>
      ) : null}

      {policy ? (
        <StatRow
          stats={[
            { label: "Warm floor", value: String(policy.minReplicas ?? "—") },
            { label: "Ceiling", value: String(policy.maxReplicas ?? "—") },
            { label: "Target now", value: String(state.targetSize) },
            {
              label: "Cooldown",
              value: policy.cooldownSeconds === null ? "—" : `${policy.cooldownSeconds}s`,
            },
            { label: "Jobs / instance", value: String(policy.jobsPerInstance ?? "—") },
            {
              label: "Scale-in limit",
              value:
                policy.scaleInMaxReplicas === null
                  ? "unset"
                  : `${policy.scaleInMaxReplicas} / ${policy.scaleInWindowSeconds ?? "?"}s`,
            },
          ]}
        />
      ) : (
        <EmptyState note="This group has no autoscaler. Its size is fixed at its target and no metric moves it." />
      )}
    </Panel>
  );
}
```

Create `apps/admin-console/src/app/(admin)/_fleet/worker-profile.tsx`:

```tsx
import { Panel, StatRow } from "@/app/_components/panel";
import type { WorkerProfile } from "@/lib/gcp/worker-profile";

// What a worker is. Metadata KEYS are listed and their values are not - see worker-profile.ts for
// why, and for the single allowlisted exception this panel does render.
export function WorkerProfilePanel({ profile }: { profile: WorkerProfile }) {
  return (
    <Panel heading={profile.templateName} title="Worker profile">
      <StatRow
        stats={[
          { label: "Machine", value: profile.machineType ?? "—" },
          {
            label: "Boot disk",
            value: profile.bootDiskGb ? `${profile.bootDiskGb} GB ${profile.bootDiskType ?? ""}`.trim() : "—",
          },
          { label: "OS image", value: profile.osImage ?? "—" },
          { label: "Network", value: `${profile.network ?? "—"} / ${profile.subnetwork ?? "—"}` },
        ]}
      />
      <dl className="admin-stats" style={{ marginTop: "1rem" }}>
        <div className="admin-stat">
          <dt>Application image</dt>
          <dd style={{ fontSize: "0.82rem", fontWeight: 400, wordBreak: "break-all" }}>
            {profile.workerImage ?? "—"}
          </dd>
        </div>
        <div className="admin-stat">
          <dt>Identity</dt>
          <dd style={{ fontSize: "0.82rem", fontWeight: 400, wordBreak: "break-all" }}>
            {profile.serviceAccount ?? "—"}
          </dd>
        </div>
        <div className="admin-stat">
          <dt>Metadata keys</dt>
          <dd style={{ fontSize: "0.82rem", fontWeight: 400 }}>
            {profile.metadataKeys.join(", ") || "—"}
          </dd>
        </div>
      </dl>
      <p className="admin-empty" style={{ marginTop: "0.75rem" }}>
        Metadata values are not shown. Instance metadata is readable by anyone holding
        compute.instances.get, so this console lists what is set, not what it is set to.
      </p>
    </Panel>
  );
}
```

Create `apps/admin-console/src/app/(admin)/_fleet/instance-table.tsx`:

```tsx
import { EmptyState, Panel } from "@/app/_components/panel";
import type { ManagedInstanceRow } from "@/lib/gcp/instances";

// One row per instance. The TEMPLATE column is what makes a half-finished rotation legible: two
// template names in this table means the roll is still in flight.
export function InstanceTable({ rows }: { rows: readonly ManagedInstanceRow[] }) {
  const templates = new Set(rows.map((row) => row.templateName).filter(Boolean));

  return (
    <Panel
      heading={templates.size > 1 ? `${templates.size} template versions — rotation in flight` : undefined}
      title={`Instances (${rows.length})`}
    >
      {rows.length === 0 ? (
        <EmptyState note="The group is running no instances. At a floor of zero this is what idle looks like." />
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Instance</th>
              <th>Status</th>
              <th>Action</th>
              <th>Zone</th>
              <th>Template</th>
              <th>Last error</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.name}>
                <td>{row.name}</td>
                <td>{row.status}</td>
                <td>{row.currentAction}</td>
                <td>{row.zone ?? "—"}</td>
                <td>{row.templateName ?? "—"}</td>
                <td>{row.lastErrors.join("; ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
```

- [ ] **Step 2: Compose the page**

Replace `apps/admin-console/src/app/(admin)/page.tsx` entirely:

```tsx
import { EmptyState, Panel, StatRow } from "@/app/_components/panel";
import { TimeSeriesChart } from "@/app/_components/time-series-chart";
import { getFleetClients, getMetricsReader, getRunReader } from "@/lib/gcp/clients";
import { readAdminTargets } from "@/lib/gcp/config";
import { readFleetState } from "@/lib/gcp/fleet";
import { listFleetInstances } from "@/lib/gcp/instances";
import {
  instanceGroupSizeFilter,
  queueDepthFilter,
  readSeries,
  requestCountFilter,
  requestLatencyFilter,
  runInstanceCountFilter,
  type GraphWindow,
} from "@/lib/gcp/metrics";
import { readServiceScaling } from "@/lib/gcp/run";
import { readWorkerProfile } from "@/lib/gcp/worker-profile";

import { InstanceTable } from "./_fleet/instance-table";
import { ScalingPolicyPanel } from "./_fleet/scaling-policy";
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

  const state = await readFleetState(clients, fleetTarget);

  // Everything below is independent of everything else below it, so it is issued at once. The
  // fleet state above is not: the worker profile needs the template name it returns.
  const [profile, instances, workers, queue, requests, latency, runInstances, apiScaling] = await Promise.all([
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
    targets.apiService
      ? readSeries(metrics, {
          crossSeriesReducer: "REDUCE_SUM",
          filter: requestCountFilter(targets.apiService),
          groupByFields: ["metric.labels.response_code_class"],
          perSeriesAligner: "ALIGN_RATE",
          projectId: targets.projectId,
          window: WINDOW,
        })
      : Promise.resolve([]),
    // p50 and p95 are two reads of the same distribution, because a single listTimeSeries call
    // carries one aligner. They are labelled here rather than by a grouped metric label, since
    // nothing in the series itself distinguishes them.
    targets.apiService
      ? Promise.all([
          readSeries(metrics, {
            filter: requestLatencyFilter(targets.apiService),
            perSeriesAligner: "ALIGN_PERCENTILE_50",
            projectId: targets.projectId,
            window: WINDOW,
          }),
          readSeries(metrics, {
            filter: requestLatencyFilter(targets.apiService),
            perSeriesAligner: "ALIGN_PERCENTILE_95",
            projectId: targets.projectId,
            window: WINDOW,
          }),
        ]).then(([p50, p95]) => [
          ...p50.map((one) => ({ ...one, label: "p50" })),
          ...p95.map((one) => ({ ...one, label: "p95" })),
        ])
      : Promise.resolve([]),
    targets.apiService
      ? readSeries(metrics, {
          filter: runInstanceCountFilter(targets.apiService),
          groupByFields: ["metric.labels.state"],
          projectId: targets.projectId,
          window: WINDOW,
        })
      : Promise.resolve([]),
    targets.apiService
      ? readServiceScaling(getRunReader(), {
          projectId: targets.projectId,
          region: targets.region,
          service: targets.apiService,
        })
      : Promise.resolve(null),
  ]);

  const bounds = [
    ...(state.policy?.minReplicas != null
      ? [{ label: "floor", value: state.policy.minReplicas }]
      : []),
    ...(state.policy?.maxReplicas != null
      ? [{ label: "ceiling", value: state.policy.maxReplicas }]
      : []),
  ];

  return (
    <>
      <h1>Fleet</h1>

      <ScalingPolicyPanel state={state} />

      <div className="admin-grid-2">
        <Panel heading="last 24h" title="Workers up">
          <TimeSeriesChart
            emptyNote="No data for compute.googleapis.com/instance_group/size on this group. The instance table below is live regardless; this graph needs the metric to be published."
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
            emptyNote="No request data. Either this deployment runs no API service or it has served nothing in the window."
            series={requests}
          />
        </Panel>

        <Panel heading="last 24h, milliseconds" title="API latency">
          <TimeSeriesChart
            emptyNote="No latency data. A service that has served no requests in the window reports no distribution to take a percentile of."
            series={latency}
          />
        </Panel>

        <Panel
          heading={
            apiScaling
              ? `${apiScaling.name}: floor ${apiScaling.minInstances}, ceiling ${apiScaling.maxInstances ?? "—"}`
              : "no API service"
          }
          title="API instances"
        >
          <TimeSeriesChart
            emptyNote="No instance-count data. A service at a floor of zero reports nothing while it is idle, which is itself the answer: every request in that state pays a cold start."
            kind="area"
            series={runInstances}
          />
        </Panel>
      </div>

      {apiScaling ? (
        <Panel title="Request tier">
          <StatRow
            stats={[
              { label: "Service", value: apiScaling.name },
              { label: "Warm floor", value: String(apiScaling.minInstances) },
              { label: "Ceiling", value: String(apiScaling.maxInstances ?? "—") },
              { label: "Revision", value: apiScaling.latestRevision ?? "—" },
            ]}
          />
        </Panel>
      ) : null}

      {profile ? <WorkerProfilePanel profile={profile} /> : null}

      <InstanceTable rows={instances} />
    </>
  );
}
```

- [ ] **Step 3: Typecheck, lint and build**

Run:
```bash
pnpm --filter @hexera/admin-console typecheck
pnpm --filter @hexera/admin-console lint
pnpm --filter @hexera/admin-console build
```
Expected: all three clean. The build is the step that proves the Next standalone tracer picks up
the `@google-cloud/*` protos; a failure here is a tracing problem, not a code problem, and
`outputFileTracingIncludes` is the fix.

- [ ] **Step 4: Run the page against a deployment**

With application-default credentials for `hexera-dev`:

```bash
cd apps/admin-console
GCP_PROJECT_ID=hexera-dev GCP_REGION=us-central1 DEPLOYMENT_ID=hexera-dev \
  WORKER_MIG=hexera-dev-workers WORKER_MIG_ZONE=us-central1-a \
  CLOUDRUN_API_SERVICE=hexera-dev-api \
  pnpm dev
```

Open `http://localhost:3001`. Record which of the four graphs have data. **The workers-up graph is
the one spec §4.2 footnote 1 flags as unverified** — if it is empty, run the `gcloud monitoring
time-series list` command in the comment above `instanceGroupSizeFilter`, find the metric type and
resource labels that do return the group's size, correct the filter, and update the footnote in
the spec to say what it turned out to be.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-console/src/app/\(admin\)
git commit -m "feat(admin): live fleet page — policy, graphs, worker profile, instances"
```

---

## Task 10: Deploy wiring — targets and read roles

**Files:**
- Modify: `deploy/gcp/scripts/create-admin-service.sh`
- Test: `tests/unit/deploy/test_admin_fleet_read_access.py`

**Interfaces:**
- Consumes: the env var names Task 1 reads.
- Produces: nothing consumed by later tasks in this PR. PR B extends the same IAM block.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/deploy/test_admin_fleet_read_access.py`, following the fake-gcloud pattern in
`tests/unit/deploy/test_schema_and_scaling_stages.py`:

```python
# Responsibility: Verify the admin service is told which fleet to read and granted only read roles.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-admin-service.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "run services describe"; then exit 1; fi
exit 0
"""

_ENV = {
    "ADMIN_IMAGE": "us-central1-docker.pkg.dev/p/mesh/admin@sha256:c0ffee",
    "APP_ENV": "dev",
    "CLOUDRUN_ADMIN_SERVICE": "hexera-dev-admin",
    "CLOUDRUN_API_SERVICE": "hexera-dev-api",
    "CLOUDRUN_CONSOLE_SERVICE": "hexera-dev-console",
    "DEPLOYMENT_ID": "hexera-dev",
    "GCP_PROJECT_ID": "hexera-dev",
    "GCP_PROJECT_NUMBER": "123456789",
    "GCP_REGION": "us-central1",
    "QUEUE_NAME": "simulation_jobs",
    "WORKER_MIG": "hexera-dev-workers",
    "WORKER_MIG_ZONE": "us-central1-a",
}


def _run(tmp_path: Path, overrides: dict[str, str] | None = None) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    env = {
        **os.environ,
        **_ENV,
        **(overrides or {}),
        "FAKE_GCP_STATE": str(state),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    subprocess.run(["bash", str(SCRIPT)], check=True, env=env, capture_output=True)
    return (state / "calls.log").read_text()


def test_the_console_is_told_which_fleet_to_read(tmp_path: Path) -> None:
    calls = _run(tmp_path)
    deploy = next(line for line in calls.splitlines() if line.startswith("run deploy"))

    for expected in (
        "GCP_PROJECT_ID=hexera-dev",
        "GCP_REGION=us-central1",
        "WORKER_MIG=hexera-dev-workers",
        "WORKER_MIG_ZONE=us-central1-a",
        "QUEUE_NAME=simulation_jobs",
        "CLOUDRUN_API_SERVICE=hexera-dev-api",
    ):
        assert expected in deploy, f"{expected} missing from the admin service environment"


def test_a_deployment_with_no_fleet_still_deploys(tmp_path: Path) -> None:
    # create-worker-fleet.sh skips the fleet when WORKER_MIG is unset. The admin console must
    # still come up; its Fleet page renders that absence.
    calls = _run(tmp_path, {"WORKER_MIG": "", "WORKER_MIG_ZONE": ""})
    assert "run deploy hexera-dev-admin" in calls


def test_the_admin_identity_is_granted_read_roles_only(tmp_path: Path) -> None:
    calls = _run(tmp_path)
    granted = {
        line.split("--role ")[1].split()[0]
        for line in calls.splitlines()
        if "projects add-iam-policy-binding" in line and "--role " in line
    }

    assert "roles/monitoring.viewer" in granted
    assert "roles/compute.viewer" in granted
    assert "roles/run.viewer" in granted


def test_no_mutate_role_is_granted_in_this_pr(tmp_path: Path) -> None:
    # PR A is read-only by construction. This assertion is what makes that a property of the
    # deploy rather than an intention in a document.
    calls = _run(tmp_path)
    for forbidden in ("roles/compute.instanceAdmin", "roles/run.admin", "roles/editor", "roles/owner"):
        assert forbidden not in calls, f"{forbidden} is a mutate role and belongs to PR B"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/unit/deploy/test_admin_fleet_read_access.py -v`
Expected: FAIL — the env vars are not declared and no role bindings are made.

- [ ] **Step 3: Declare the fleet targets**

In `deploy/gcp/scripts/create-admin-service.sh`, replace the `ADMIN_ENV_PAIRS` block (step 2 of
that script) with:

```bash
# 2) THE NON-SECRET SETTINGS. No secret bindings here at all: IAP is this service's gate and it
#    holds no session of its own, so there is nothing for Secret Manager to hand it.
#
#    THE FLEET TARGETS. The console reads the managed instance group and the Cloud Run services by
#    name, and it learns those names here rather than discovering them. Discovery would mean
#    listing every group in the project and guessing which one is ours, which is both a wider IAM
#    grant and a worse failure mode - a renamed group would silently show a different fleet.
#
#    An empty value is not a hole. readAdminTargets() treats an absent WORKER_MIG as "this
#    deployment runs no fleet", which is exactly what create-worker-fleet.sh does with the same
#    variable, and the Fleet page renders that state rather than failing.
ADMIN_ENV_PAIRS=(
  "CLOUDRUN_API_SERVICE=${CLOUDRUN_API_SERVICE:-}"
  "CLOUDRUN_CONSOLE_SERVICE=${CLOUDRUN_CONSOLE_SERVICE:-}"
  "DEPLOYMENT_ID=${DEPLOYMENT_ID}"
  "ENV=${APP_ENV}"
  "GCP_PROJECT_ID=${GCP_PROJECT_ID}"
  "GCP_REGION=${GCP_REGION}"
  "NODE_ENV=production"
  "QUEUE_NAME=${QUEUE_NAME:-simulation_jobs}"
  "WORKER_MIG=${WORKER_MIG:-}"
  "WORKER_MIG_ZONE=${WORKER_MIG_ZONE:-}"
)
```

- [ ] **Step 4: Grant the read roles**

Insert this immediately after the identity block (step 1) of the same script, before the env pairs:

```bash
# 1b) WHAT THE CONSOLE MAY READ. Three viewer roles, and nothing that can change anything.
#
#     This is the narrowest set that serves the Fleet page: monitoring for the graphs, compute for
#     the group, its autoscaler and its template, run for per-service scaling and revisions. There
#     is deliberately no mutate role here - the scaling controls and the custom role they need are
#     a separate change, reviewed on its own, because granting an IAP-gated web service the
#     authority to delete VMs is not a detail to slip into a read-only page's deploy.
#
#     A deploy identity may not hold resourcemanager.projectIamAdmin. Each grant is attempted, a
#     failure is reported with the command that fixes it, and the console itself is the verdict: a
#     page that cannot read its metric says so.
for role in roles/compute.viewer roles/monitoring.viewer roles/run.viewer; do
  if gc projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
       --member "serviceAccount:${ADMIN_SA_EMAIL}" \
       --role "${role}" --condition None >/dev/null 2>&1; then
    log "project += ${role} -> ${ADMIN_SA_EMAIL}"
  else
    warn "could not grant ${role} to ${ADMIN_SA_EMAIL}. If the binding is already in place the
       console still reads; if it is not, its pages report the permission error and this is the
       command:
         gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \\
           --member serviceAccount:${ADMIN_SA_EMAIL} --role ${role} --condition None"
  fi
done
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/deploy/test_admin_fleet_read_access.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 6: Run the whole deploy suite for regressions**

Run: `pytest tests/unit/deploy -q`
Expected: PASS. `test_admin_config_declaration.py` and `test_admin_service_stage.py` both assert
things about this script; if either fails, it is asserting the old three-variable environment and
needs updating to the new list, not working around.

- [ ] **Step 7: Commit**

```bash
git add deploy/gcp/scripts/create-admin-service.sh tests/unit/deploy/test_admin_fleet_read_access.py
git commit -m "feat(deploy): hand the admin console its fleet targets and read-only roles"
```

---

## Task 11: Document what the console can now see

**Files:**
- Modify: `docs/deployment/admin-console-access.md`

- [ ] **Step 1: Add the section**

Append a section to `docs/deployment/admin-console-access.md` covering: the three viewer roles and
what each serves; that the console holds no mutate authority in this release and what that means
for an IAP compromise; the environment variables the service is given and what an empty
`WORKER_MIG` renders as; and the fact that instance metadata values are deliberately not shown.

Match the file's existing voice and heading depth. Read it first — do not assume its structure.

- [ ] **Step 2: Verify every claim you wrote**

For each factual statement, point at the code that makes it true. A documented role that no script
grants is worse than an undocumented one.

- [ ] **Step 3: Commit**

```bash
git add docs/deployment/admin-console-access.md
git commit -m "docs: what the admin console reads, and with which roles"
```

---

## Definition of done

- [ ] `pnpm --filter @hexera/admin-console test` — 39 tests pass
- [ ] `pnpm --filter @hexera/admin-console typecheck` — clean
- [ ] `pnpm --filter @hexera/admin-console lint` — clean
- [ ] `pnpm --filter @hexera/admin-console build` — clean, standalone output traces the protos
- [ ] `pytest tests/unit/deploy -q` — passes, including the four new assertions
- [ ] The page has been loaded against `hexera-dev` and each of the four graphs is recorded as
      having data or not
- [ ] Spec §4.2 footnote 1 has been resolved — either the metric works, or the spec now records
      what does
- [ ] `git grep -n "instanceAdmin\|run.admin" deploy/gcp/scripts/create-admin-service.sh` returns
      nothing. PR A grants no mutate authority
