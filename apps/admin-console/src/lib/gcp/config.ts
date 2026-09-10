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
  adminService: string | null;
  apiService: string | null;
  consoleService: string | null;
  deploymentId: string;
  fleet: FleetTarget | null;
  maxAllowedReplicas: number;
  projectId: string;
  region: string;
};

// THE COST CEILING'S CEILING. The Fleet page lets an operator raise the group's maxNumReplicas,
// and every replica is an e2-standard-4 running until something scales it back down. This bound is
// what stops a fat-fingered "50" from becoming a fifty-VM bill. It is deliberately a small number:
// the live fleet's ceiling is 5, so 12 is headroom for a bad day rather than a licence.
const DEFAULT_MAX_ALLOWED_REPLICAS = 12;

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

  // A deployment may raise the cap, but a missing, zero or unparseable value falls back to the
  // default rather than to "no limit" - an unreadable setting must never widen authority.
  const declaredCap = Number(optional(env, "ADMIN_MAX_ALLOWED_REPLICAS"));
  const maxAllowedReplicas =
    Number.isInteger(declaredCap) && declaredCap > 0 ? declaredCap : DEFAULT_MAX_ALLOWED_REPLICAS;

  return {
    adminService: optional(env, "CLOUDRUN_ADMIN_SERVICE"),
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
    maxAllowedReplicas,
    projectId,
    region,
  };
}
