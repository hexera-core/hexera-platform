import type { protos as computeProtos } from "@google-cloud/compute";
import type { protos as runProtos } from "@google-cloud/run";

import type { AutoscalerWriter, ManagerWriter, RunWriter } from "./clients";
import type { FleetTarget } from "./config";
import { serviceResourceName } from "./run";

// THE MUTATIONS. Everything that changes infrastructure passes through this module, so the
// guardrails are in one place rather than repeated at each call site.
//
// TWO MECHANICS THAT ARE EASY TO GET WRONG, AND ARE THEREFORE THE SHAPE OF THIS FILE:
//
// 1. THE AUTOSCALER UPDATE IS A READ-MODIFY-WRITE OF THE WHOLE RESOURCE. `autoscalingPolicy` is
//    replaced wholesale, not merged. Sending a policy containing only minNumReplicas DELETES the
//    customMetricUtilizations entry the fleet scales on, and the failure is silent: the autoscaler
//    keeps working, but on CPU utilization instead of queue depth. Every write here GETs the live
//    resource first, mutates the object it got back, and sends the whole thing.
//
// 2. COMPUTE MUTATIONS ARE LONG-RUNNING ZONAL OPERATIONS. resize, deleteInstances and
//    recreateInstances return an operation, not a result. Nothing here awaits it. The caller
//    returns as soon as the operation is accepted and the page reflects progress through the
//    group's own currentActions on the next read - which is how the group itself reports the work.
//    Blocking a Server Action on a VM deletion would hold a request open for minutes.

export type ScalingLimits = { maxAllowedReplicas: number };

export type ScalingUpdate = {
  cooldownSeconds?: number;
  jobsPerInstance?: number;
  maxReplicas?: number;
  minReplicas?: number;
  scaleInMaxReplicas?: number;
  scaleInWindowSeconds?: number;
};

export type ServiceScalingUpdate = {
  maxInstances?: number;
  minInstances?: number;
};

type AutoscalingPolicy = computeProtos.google.cloud.compute.v1.IAutoscalingPolicy;
type RevisionScaling = runProtos.google.cloud.run.v2.IRevisionScaling;

export type OperationResult = { operationId: string | null };

// A Compute instance name: a lowercase letter, then letters, digits or hyphens, up to 63. The name
// is interpolated into a resource URL, and there is no legitimate value here with a slash, a dot
// or a space in it - so anything else is refused rather than escaped.
const INSTANCE_NAME = /^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$/;

function requireWholeNumber(label: string, value: number | undefined): number | undefined {
  if (value === undefined) return undefined;
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${label} must be a whole number of zero or more; got ${value}.`);
  }
  return value;
}

function requireInstanceName(name: string): string {
  if (!INSTANCE_NAME.test(name)) {
    throw new Error(
      `'${name}' is not a Compute instance name. This value is interpolated into a resource URL, ` +
        `so anything outside a plain instance name is refused.`,
    );
  }
  return name;
}

function operationIdOf(operation: { name?: string | null } | undefined): string | null {
  return operation?.name ?? null;
}

export async function updateScalingPolicy(
  clients: { autoscalers: AutoscalerWriter },
  target: FleetTarget,
  update: ScalingUpdate,
  limits: ScalingLimits,
): Promise<{ after: AutoscalingPolicy; before: AutoscalingPolicy; operationId: string | null }> {
  for (const [label, value] of Object.entries(update)) {
    requireWholeNumber(label, value);
  }

  const [live] = await clients.autoscalers.get({
    autoscaler: target.migName,
    project: target.projectId,
    zone: target.migZone,
  });

  const before: AutoscalingPolicy = structuredClone(live.autoscalingPolicy ?? {});
  const after: AutoscalingPolicy = structuredClone(before);

  if (update.minReplicas !== undefined) after.minNumReplicas = update.minReplicas;
  if (update.maxReplicas !== undefined) after.maxNumReplicas = update.maxReplicas;
  if (update.cooldownSeconds !== undefined) after.coolDownPeriodSec = update.cooldownSeconds;

  if (update.jobsPerInstance !== undefined) {
    // Written onto the EXISTING utilization rather than as a new one. The filter must select a
    // single time series - that is the contract singleInstanceAssignment is defined against - so
    // appending a second entry would break the policy rather than extend it.
    const utilizations = after.customMetricUtilizations ?? [];
    if (utilizations.length === 0) {
      throw new Error(
        "This autoscaler has no custom metric utilization, so there is nothing for " +
          "jobs-per-instance to apply to. The fleet is not scaling on queue depth.",
      );
    }
    utilizations[0].singleInstanceAssignment = update.jobsPerInstance;
    after.customMetricUtilizations = utilizations;
  }

  if (update.scaleInMaxReplicas !== undefined || update.scaleInWindowSeconds !== undefined) {
    const control = after.scaleInControl ?? {};
    if (update.scaleInMaxReplicas !== undefined) {
      control.maxScaledInReplicas = { fixed: update.scaleInMaxReplicas };
    }
    if (update.scaleInWindowSeconds !== undefined) {
      control.timeWindowSec = update.scaleInWindowSeconds;
    }
    after.scaleInControl = control;
  }

  const floor = after.minNumReplicas ?? 0;
  const ceiling = after.maxNumReplicas ?? 0;
  if (ceiling > limits.maxAllowedReplicas) {
    throw new Error(
      `A ceiling of ${ceiling} exceeds this deployment's cap of ${limits.maxAllowedReplicas}. ` +
        `Every replica is a VM running until something scales it back down; raise ` +
        `ADMIN_MAX_ALLOWED_REPLICAS if the cap itself is wrong.`,
    );
  }
  if (floor > ceiling) {
    throw new Error(`A floor of ${floor} is above the ceiling of ${ceiling}.`);
  }

  // ONLY THE WRITABLE FIELDS GO BACK. `live` also carries id, selfLink, creationTimestamp, status,
  // statusDetails and recommendedSize - all output-only. Echoing them into a PUT is at best
  // ignored and at worst rejected, and none of them is anything this write means to change.
  // `target` is not optional: it is the group this autoscaler drives, and an update that omits it
  // is an autoscaler pointed at nothing.
  const [operation] = await clients.autoscalers.update({
    autoscaler: target.migName,
    autoscalerResource: {
      autoscalingPolicy: after,
      description: live.description,
      name: live.name,
      target: live.target,
    },
    project: target.projectId,
    zone: target.migZone,
  });

  return { after, before, operationId: operationIdOf(operation) };
}

export async function resizeFleet(
  clients: { instanceGroupManagers: ManagerWriter },
  target: FleetTarget,
  size: number,
  limits: ScalingLimits,
): Promise<OperationResult> {
  requireWholeNumber("size", size);
  if (size > limits.maxAllowedReplicas) {
    throw new Error(
      `A size of ${size} exceeds this deployment's cap of ${limits.maxAllowedReplicas}.`,
    );
  }

  const [operation] = await clients.instanceGroupManagers.resize({
    instanceGroupManager: target.migName,
    project: target.projectId,
    size,
    zone: target.migZone,
  });

  return { operationId: operationIdOf(operation) };
}

function instanceUrl(target: FleetTarget, name: string): string {
  return `projects/${target.projectId}/zones/${target.migZone}/instances/${requireInstanceName(name)}`;
}

export async function deleteFleetInstance(
  clients: { instanceGroupManagers: ManagerWriter },
  target: FleetTarget,
  name: string,
): Promise<OperationResult> {
  const url = instanceUrl(target, name);

  const [operation] = await clients.instanceGroupManagers.deleteInstances({
    instanceGroupManager: target.migName,
    instanceGroupManagersDeleteInstancesRequestResource: { instances: [url] },
    project: target.projectId,
    zone: target.migZone,
  });

  return { operationId: operationIdOf(operation) };
}

export async function recreateFleetInstance(
  clients: { instanceGroupManagers: ManagerWriter },
  target: FleetTarget,
  name: string,
): Promise<OperationResult> {
  const url = instanceUrl(target, name);

  const [operation] = await clients.instanceGroupManagers.recreateInstances({
    instanceGroupManager: target.migName,
    instanceGroupManagersRecreateInstancesRequestResource: { instances: [url] },
    project: target.projectId,
    zone: target.migZone,
  });

  return { operationId: operationIdOf(operation) };
}

export async function updateServiceScaling(
  client: RunWriter,
  args: { projectId: string; region: string; service: string },
  update: ServiceScalingUpdate,
  limits: ScalingLimits,
): Promise<{ after: RevisionScaling; before: RevisionScaling; operationId: string | null }> {
  for (const [label, value] of Object.entries(update)) {
    requireWholeNumber(label, value);
  }

  const name = serviceResourceName(args);
  const [live] = await client.getService({ name });

  const before: RevisionScaling = structuredClone(live.template?.scaling ?? {});
  const after: RevisionScaling = structuredClone(before);
  if (update.minInstances !== undefined) after.minInstanceCount = update.minInstances;
  if (update.maxInstances !== undefined) after.maxInstanceCount = update.maxInstances;

  const floor = after.minInstanceCount ?? 0;
  const ceiling = after.maxInstanceCount ?? 0;
  if (ceiling > limits.maxAllowedReplicas) {
    throw new Error(
      `A ceiling of ${ceiling} exceeds this deployment's cap of ${limits.maxAllowedReplicas}.`,
    );
  }
  if (floor > ceiling) {
    throw new Error(`A floor of ${floor} is above the ceiling of ${ceiling}.`);
  }

  // THE FIELD MASK IS NOT OPTIONAL. A Service update without one replaces the whole resource,
  // which would drop the container, its environment and its service account - a scaling change
  // that silently un-deploys the service.
  const [operation] = await client.updateService({
    service: { ...live, template: { ...live.template, scaling: after } },
    updateMask: { paths: ["template.scaling"] },
  });

  return { after, before, operationId: operationIdOf(operation as { name?: string | null }) };
}
