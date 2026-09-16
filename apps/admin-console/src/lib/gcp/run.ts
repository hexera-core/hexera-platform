import type { RunReader } from "./clients";
import { lastSegment } from "./fleet";

// A Cloud Run service's warm floor, its ceiling, and what it is currently running.
//
// The FLOOR is the cold-start control for the request-serving tier, the same way the MIG's
// minNumReplicas is for the worker fleet.

export type ServiceScaling = {
  imageDigest: string | null;
  latestRevision: string | null;
  maxInstances: number | null;
  minInstances: number;
  name: string;
};

export function serviceResourceName(args: {
  projectId: string;
  region: string;
  service: string;
}): string {
  return `projects/${args.projectId}/locations/${args.region}/services/${args.service}`;
}

export async function readServiceScaling(
  client: RunReader,
  args: { projectId: string; region: string; service: string },
): Promise<ServiceScaling> {
  const [service] = await client.getService({ name: serviceResourceName(args) });

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
