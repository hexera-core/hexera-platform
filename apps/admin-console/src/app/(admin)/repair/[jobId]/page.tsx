import Link from "next/link";

import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import {
  compactDate,
  readRepairJob,
  repairDecisionLabel,
  repairStatusLabel,
  REPAIR_DECISIONS_NEEDING_REASON,
  type RepairAttemptRow,
  type RepairDecisionRow,
} from "@/lib/admin/read";

import { claimRepair, decideRepair } from "../actions";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// ONE JOB, AND WHAT MAY BE DONE TO IT.
//
// The buttons are built from `available_decisions`, which the API derives from the one transition
// table. A screen that composed its own menu would offer moves the database then refuses, and the
// operator would learn the difference from a 409.

function defectSummary(report: Record<string, unknown> | null): string {
  if (!report) return "";
  // The attempt's report is the typed RepairResult payload. Read defensively: an older attempt may
  // predate a field, and a half-shown report is better than a page that will not render.
  const inner = (report.report ?? report) as Record<string, unknown>;
  const summary = typeof inner.summary === "string" ? inner.summary : "";
  const defects = Array.isArray(inner.defects) ? inner.defects : [];
  const codes = defects
    .map((d) => (d as { code?: unknown }).code)
    .filter((c): c is string => typeof c === "string");
  if (codes.length === 0) return summary;
  return summary ? `${summary} (${codes.join(", ")})` : codes.join(", ");
}

function AttemptTable({ attempts }: { attempts: readonly RepairAttemptRow[] }) {
  return (
    <table className="admin-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Mode</th>
          <th>Profile</th>
          <th>Tool</th>
          <th>Result</th>
          <th>Bytes in</th>
          <th>Bytes out</th>
          <th>What it found</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody>
        {attempts.map((a) => (
          <tr key={a.attempt_no}>
            <td>{a.attempt_no}</td>
            <td>{a.mode}</td>
            <td>{a.profile || "—"}</td>
            <td>{a.tool_version || "—"}</td>
            <td>{a.status ? repairStatusLabel(a.status) : "—"}</td>
            {/* THE AUDIT PAIR, on the screen. An inspection shows no output because it produced
                none; a repair that shows the same digest in both columns changed nothing. */}
            <td title={a.input_sha256}>{a.input_sha256.slice(0, 12)}</td>
            <td title={a.output_sha256 ?? ""}>
              {a.output_sha256 ? a.output_sha256.slice(0, 12) : "no new geometry"}
            </td>
            <td>{defectSummary(a.report) || "—"}</td>
            <td>{compactDate(a.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DecisionHistory({ decisions }: { decisions: readonly RepairDecisionRow[] }) {
  return (
    <table className="admin-table">
      <thead>
        <tr>
          <th>Decision</th>
          <th>From</th>
          <th>Who</th>
          <th>Why</th>
          <th>Notes</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody>
        {decisions.map((d, index) => (
          <tr key={`${d.created_at}-${index}`}>
            <td>{repairDecisionLabel(d.decision)}</td>
            <td>{d.from_status ? repairStatusLabel(d.from_status) : "—"}</td>
            <td>{d.actor}</td>
            <td>{d.reason ?? "—"}</td>
            <td>{d.notes ?? "—"}</td>
            <td>{compactDate(d.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default async function RepairJobPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = await params;
  const detail = await readRepairJob(jobId);

  if (detail.error || !detail.data) {
    return (
      <>
        <h1>Repair job</h1>
        <Alert>{detail.error ?? "This repair job could not be read."}</Alert>
      </>
    );
  }

  const { attempts, available_decisions: available, decisions, job } = detail.data;

  return (
    <>
      <h1>Repair job {job.id.slice(0, 8)}</h1>
      <p className="admin-muted">
        <Link href="/repair">← the queue</Link>
      </p>

      <Panel title="Where it stands">
        <StatRow
          stats={[
            { label: "Status", value: repairStatusLabel(job.status) },
            {
              label: "Inspection",
              value: job.repair_status ? repairStatusLabel(job.repair_status) : "not inspected",
            },
            { label: "Strategy", value: job.current_strategy || "not chosen" },
            { label: "Target engine", value: job.target_engine || "—" },
            { label: "Customer", value: job.owner_id },
            { label: "Priority", value: String(job.service_priority) },
            { label: "Operator", value: job.assigned_operator ?? "unclaimed" },
            { label: "Opened", value: compactDate(job.created_at) },
          ]}
        />
        {job.blocked_reason ? <Alert>{job.blocked_reason}</Alert> : null}
      </Panel>

      <Panel title={job.assigned_operator ? "Claimed" : "Unclaimed"}>
        {/* CLAIM FOR MYSELF, RELEASE BACK. The operator is the IAP-verified identity, not a typed
            field, so nobody can decide on somebody else's behalf by editing a form. */}
        <form action={claimRepair}>
          <input name="job_id" type="hidden" value={job.id} />
          <input name="release" type="hidden" value={job.assigned_operator ? "1" : "0"} />
          <button type="submit">{job.assigned_operator ? "Release it" : "Claim it"}</button>
        </form>
      </Panel>

      <Panel title="Decide" heading="What this job may do next, from the transition table">
        {available.length === 0 ? (
          <EmptyState
            note={
              job.settled
                ? "This job has settled. Nothing further happens to it on its own."
                : "No decision is available from this state. It is waiting on something else to move it."
            }
          />
        ) : (
          <div className="admin-actions">
            {available.map((decision) => (
              <form action={decideRepair} key={decision}>
                <input name="job_id" type="hidden" value={job.id} />
                <input name="decision" type="hidden" value={decision} />
                {decision === "choose_strategy" ? (
                  <label>
                    Strategy
                    <select name="strategy" defaultValue="conservative">
                      <option value="conservative">conservative</option>
                      <option value="mesh_ready">mesh_ready</option>
                      <option value="manual_review">manual_review</option>
                    </select>
                  </label>
                ) : null}
                {REPAIR_DECISIONS_NEEDING_REASON.includes(decision) ? (
                  <label>
                    Reason (the customer is told this)
                    <input maxLength={2000} name="reason" required type="text" />
                  </label>
                ) : null}
                <label>
                  Notes
                  <input maxLength={4000} name="notes" type="text" />
                </label>
                <button type="submit">{repairDecisionLabel(decision)}</button>
              </form>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="What was measured" heading={`${attempts.length} attempt(s)`}>
        {attempts.length === 0 ? (
          <EmptyState note="Nothing has inspected or repaired this geometry yet." />
        ) : (
          <AttemptTable attempts={attempts} />
        )}
      </Panel>

      <Panel title="What people decided" heading={`${decisions.length} decision(s)`}>
        {decisions.length === 0 ? (
          <EmptyState note="No decision has been recorded on this job." />
        ) : (
          <DecisionHistory decisions={decisions} />
        )}
      </Panel>
    </>
  );
}
