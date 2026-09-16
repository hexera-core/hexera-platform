import { recordAdminAction, type AdminAuditEntry } from "@/lib/audit";
import type { IapActor } from "@/lib/auth/iap";
import type { FleetWriteClients, RunWriter } from "@/lib/gcp/clients";
import type { AdminTargets } from "@/lib/gcp/config";
import {
  deleteFleetInstance,
  recreateFleetInstance,
  resizeFleet,
  updateScalingPolicy,
  updateServiceScaling,
} from "@/lib/gcp/fleet-writes";

// WHAT A CONTROL ON THE FLEET PAGE ACTUALLY DOES, as one pure function.
//
// This is deliberately NOT the "use server" module. Everything a Server Function must do that
// cannot be tested without a running request - reading headers, verifying the IAP assertion,
// revalidating the route - lives in actions.ts, and everything that decides what happens lives
// here, where it takes its clients as parameters and its tests hand it fakes.
//
// The caller has already established WHO. This function decides WHETHER and WHAT.

export type ActionResult = {
  error?: string;
  message?: string;
  ok: boolean;
};

export type FleetOperationDeps = {
  actor: IapActor;
  audit?: (entry: AdminAuditEntry) => void;
  clients: FleetWriteClients;
  form: FormData;
  runClient: RunWriter;
  targets: AdminTargets;
};

// An untouched number input posts as an empty string. Coercing that to 0 would set the fleet's
// floor to zero the first time anyone changed the cooldown, so blank means "leave this alone".
function optionalNumber(form: FormData, field: string): number | undefined {
  const raw = form.get(field);
  if (typeof raw !== "string" || raw.trim() === "") return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    throw new Error(`${field} must be a number; got '${raw}'.`);
  }
  return value;
}

function requiredString(form: FormData, field: string): string {
  const raw = form.get(field);
  if (typeof raw !== "string" || raw.trim() === "") {
    throw new Error(`${field} is required.`);
  }
  return raw.trim();
}

function definedOnly<T extends Record<string, number | undefined>>(update: T): T {
  return Object.fromEntries(
    Object.entries(update).filter(([, value]) => value !== undefined),
  ) as T;
}

export async function applyFleetOperation(deps: FleetOperationDeps): Promise<ActionResult> {
  const { actor, clients, form, runClient, targets } = deps;
  const audit = deps.audit ?? recordAdminAction;
  const operation = form.get("operation");
  const limits = { maxAllowedReplicas: targets.maxAllowedReplicas };

  try {
    switch (operation) {
      case "scaling": {
        const fleet = requireFleet(targets);
        const update = definedOnly({
          cooldownSeconds: optionalNumber(form, "cooldownSeconds"),
          jobsPerInstance: optionalNumber(form, "jobsPerInstance"),
          maxReplicas: optionalNumber(form, "maxReplicas"),
          minReplicas: optionalNumber(form, "minReplicas"),
          scaleInMaxReplicas: optionalNumber(form, "scaleInMaxReplicas"),
          scaleInWindowSeconds: optionalNumber(form, "scaleInWindowSeconds"),
        });
        if (Object.keys(update).length === 0) {
          return { error: "Nothing was changed.", ok: false };
        }

        const change = await updateScalingPolicy(clients, fleet, update, limits);
        audit({
          action: "fleet.scaling.update",
          actor: actor.email,
          after: change.after,
          before: change.before,
          operationId: change.operationId,
          resource: fleet.migName,
        });
        return {
          message: `Scaling policy updated. The autoscaler applies it on its next evaluation.`,
          ok: true,
        };
      }

      case "resize": {
        const fleet = requireFleet(targets);
        const size = optionalNumber(form, "size");
        if (size === undefined) return { error: "A size is required.", ok: false };

        const result = await resizeFleet(clients, fleet, size, limits);
        audit({
          action: "fleet.resize",
          actor: actor.email,
          after: { targetSize: size },
          operationId: result.operationId,
          resource: fleet.migName,
        });
        return {
          message: `Resizing to ${size}. The autoscaler owns the size again after its cooldown, so this is a nudge rather than a setting.`,
          ok: true,
        };
      }

      case "instance.recreate": {
        const fleet = requireFleet(targets);
        const instance = requiredString(form, "instance");

        const result = await recreateFleetInstance(clients, fleet, instance);
        audit({
          action: "fleet.instance.recreate",
          actor: actor.email,
          operationId: result.operationId,
          resource: instance,
        });
        return { message: `Recreating ${instance}.`, ok: true };
      }

      case "instance.delete": {
        const fleet = requireFleet(targets);
        const instance = requiredString(form, "instance");
        const confirm = (form.get("confirm") ?? "").toString().trim();
        if (confirm !== instance) {
          return {
            error: `The typed name does not match ${instance}. Nothing was deleted.`,
            ok: false,
          };
        }

        const result = await deleteFleetInstance(clients, fleet, instance);
        audit({
          action: "fleet.instance.delete",
          actor: actor.email,
          operationId: result.operationId,
          resource: instance,
        });
        return {
          // The console holds no database connection and there is no drain contract, so it cannot
          // tell whether this instance held a job lease. Saying so is the only honest option.
          message: `Deleting ${instance}. This console cannot see whether it had work in flight, and the worker is not drained first — anything it was running is lost.`,
          ok: true,
        };
      }

      case "service.scaling": {
        const service = requiredString(form, "service");
        // The service name reaches a resource path. Only the services this deployment declares are
        // addressable, so a forged POST cannot aim the console at another service in the project.
        const declared = [targets.adminService, targets.apiService, targets.consoleService].filter(
          (name): name is string => Boolean(name),
        );
        if (!declared.includes(service)) {
          return {
            error: `'${service}' is not a service this deployment declares.`,
            ok: false,
          };
        }

        const update = definedOnly({
          maxInstances: optionalNumber(form, "maxInstances"),
          minInstances: optionalNumber(form, "minInstances"),
        });
        if (Object.keys(update).length === 0) {
          return { error: "Nothing was changed.", ok: false };
        }

        const change = await updateServiceScaling(
          runClient,
          { projectId: targets.projectId, region: targets.region, service },
          update,
          limits,
        );
        audit({
          action: "service.scaling.update",
          actor: actor.email,
          after: change.after,
          before: change.before,
          operationId: change.operationId,
          resource: service,
        });
        return { message: `${service} scaling updated. It applies to the next revision.`, ok: true };
      }

      default:
        return { error: `'${String(operation)}' is not an operation this page performs.`, ok: false };
    }
  } catch (error) {
    // A refused or failed change is NOT audited as an action: nothing happened, and a log of
    // attempts that did nothing would dilute the record of changes that did.
    return { error: error instanceof Error ? error.message : String(error), ok: false };
  }
}

function requireFleet(targets: AdminTargets) {
  if (!targets.fleet) {
    throw new Error(
      "This deployment declares no worker fleet, so there is nothing to scale. " +
        "create-worker-fleet.sh skips the fleet when WORKER_MIG is unset.",
    );
  }
  return targets.fleet;
}
