import assert from "node:assert/strict";
import test from "node:test";

import { recordAdminAction } from "./audit";

test("emits one structured line naming the actor, the resource and the change", () => {
  const lines: string[] = [];
  recordAdminAction(
    {
      action: "fleet.scaling.update",
      actor: "operator@hexera.ai",
      after: { minReplicas: 2 },
      before: { minReplicas: 1 },
      resource: "hexera-dev-workers",
    },
    (line) => lines.push(line),
  );

  assert.equal(lines.length, 1);
  const entry = JSON.parse(lines[0]);
  assert.equal(entry.severity, "NOTICE");
  assert.equal(entry.actor, "operator@hexera.ai");
  assert.equal(entry.action, "fleet.scaling.update");
  assert.equal(entry.resource, "hexera-dev-workers");
  assert.deepEqual(entry.before, { minReplicas: 1 });
  assert.deepEqual(entry.after, { minReplicas: 2 });
  assert.ok(entry.message.includes("operator@hexera.ai"));
  assert.ok(entry.timestamp);
});

test("a failure to write the audit line does not stop the mutation", () => {
  // Refusing an infrastructure change because logging is degraded is the wrong failure. The
  // logging failure is itself reported, and the caller carries on.
  const reported: unknown[] = [];
  assert.doesNotThrow(() =>
    recordAdminAction(
      { action: "fleet.resize", actor: "operator@hexera.ai", resource: "hexera-dev-workers" },
      () => {
        throw new Error("stdout closed");
      },
      (error) => reported.push(error),
    ),
  );
  assert.equal(reported.length, 1);
});

test("carries the operation id when the call returned one", () => {
  const lines: string[] = [];
  recordAdminAction(
    {
      action: "fleet.instance.delete",
      actor: "operator@hexera.ai",
      operationId: "operation-1757-abc",
      resource: "hexera-dev-workers-a1b2",
    },
    (line) => lines.push(line),
  );

  assert.equal(JSON.parse(lines[0]).operationId, "operation-1757-abc");
});
