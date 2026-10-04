import assert from "node:assert/strict";
import { test } from "node:test";

import type { AdminAuditEntry } from "@/lib/audit";

import { applyRepairAssignment, applyRepairDecision } from "./operations";

const ACTOR = { email: "ana@hexera.io", subject: "sub-1" };

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.append(key, value);
  return data;
}

function recorder() {
  const entries: AdminAuditEntry[] = [];
  return { audit: (entry: AdminAuditEntry) => entries.push(entry), entries };
}

function decideSpy(result: { error?: string } = {}) {
  const calls: Record<string, unknown>[] = [];
  const decide = async (input: Record<string, unknown>) => {
    calls.push(input);
    return result.error
      ? { data: null, error: result.error }
      : { data: { applied: "applied", decisions: [], job: {} }, error: null };
  };
  return { calls, decide: decide as never };
}

function assignSpy(result: { error?: string } = {}) {
  const calls: Record<string, unknown>[] = [];
  const assign = async (input: Record<string, unknown>) => {
    calls.push(input);
    return result.error
      ? { data: null, error: result.error }
      : { data: { job: {} }, error: null };
  };
  return { assign: assign as never, calls };
}

test("the decision's actor is the verified identity, never a form field", async () => {
  const { calls, decide } = decideSpy();
  const { audit, entries } = recorder();

  // an operator trying to decide as somebody else by adding the field
  const result = await applyRepairDecision(
    form({ actor: "someone-else@example.com", decision: "inspect", job_id: "job-1" }),
    { actor: ACTOR, audit, decide },
  );

  assert.equal(result.ok, true);
  assert.equal(calls[0].actor, ACTOR.email);
  assert.equal(entries[0].actor, ACTOR.email);
});

test("a decision that stops a customer's job is refused without a reason", async () => {
  const { calls, decide } = decideSpy();

  for (const decision of ["block", "ask_customer", "escalate", "manual_cleanup"]) {
    const result = await applyRepairDecision(
      form({ decision, job_id: "job-1" }),
      { actor: ACTOR, decide },
    );
    assert.equal(result.ok, false, decision);
    assert.match(result.error ?? "", /must state a reason/);
  }
  // refused HERE, so the API is never called and the operator keeps what they typed
  assert.deepEqual(calls, []);
});

test("a decision that does not stop the job needs no reason", async () => {
  const { calls, decide } = decideSpy();

  const result = await applyRepairDecision(
    form({ decision: "inspect", job_id: "job-1" }),
    { actor: ACTOR, decide },
  );

  assert.equal(result.ok, true);
  assert.equal(calls[0].reason, "");
});

test("the stated reason and strategy travel with the decision", async () => {
  const { calls, decide } = decideSpy();

  await applyRepairDecision(
    form({
      decision: "choose_strategy",
      job_id: "job-1",
      notes: "small gap near the flange",
      strategy: "conservative",
    }),
    { actor: ACTOR, decide },
  );

  assert.equal(calls[0].strategy, "conservative");
  assert.equal(calls[0].notes, "small gap near the flange");
});

test("a job id is required, because a decision has to be about something", async () => {
  const { calls, decide } = decideSpy();
  const result = await applyRepairDecision(form({ decision: "inspect" }), {
    actor: ACTOR,
    decide,
  });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /job_id is required/);
  assert.deepEqual(calls, []);
});

test("an API refusal is reported rather than audited as a success", async () => {
  const { decide } = decideSpy({ error: "Product API returned 409." });
  const { audit, entries } = recorder();

  const result = await applyRepairDecision(
    form({ decision: "inspect", job_id: "job-1" }),
    { actor: ACTOR, audit, decide },
  );

  assert.equal(result.ok, false);
  assert.equal(result.error, "Product API returned 409.");
  // nothing is written to the audit for a decision that did not take effect
  assert.deepEqual(entries, []);
});

test("claiming puts the verified operator's own name on the job", async () => {
  const { assign, calls } = assignSpy();
  const { audit, entries } = recorder();

  const result = await applyRepairAssignment(form({ job_id: "job-1", release: "0" }), {
    actor: ACTOR,
    assign,
    audit,
  });

  assert.equal(result.ok, true);
  assert.deepEqual(calls[0], { claim: true, jobId: "job-1", operator: ACTOR.email });
  assert.equal(entries[0].action, "repair.claim");
});

test("releasing clears the claim and does not re-claim it", async () => {
  const { assign, calls } = assignSpy();
  const { audit, entries } = recorder();

  await applyRepairAssignment(form({ job_id: "job-1", release: "1" }), {
    actor: ACTOR,
    assign,
    audit,
  });

  // claim:false, because a release is not a contended write - there is nothing to lose a race for
  assert.deepEqual(calls[0], { claim: false, jobId: "job-1", operator: "" });
  assert.equal(entries[0].action, "repair.release");
});

test("a lost claim is surfaced as the API's own refusal", async () => {
  const { assign } = assignSpy({ error: "Product API returned 409." });
  const result = await applyRepairAssignment(form({ job_id: "job-1" }), {
    actor: ACTOR,
    assign,
  });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /409/);
});
