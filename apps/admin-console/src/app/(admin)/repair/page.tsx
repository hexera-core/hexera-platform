import Link from "next/link";

import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import {
  compactDate,
  formatDuration,
  formatRate,
  groupRepairQueue,
  readRepairQueue,
  readRepairThroughput,
  repairStatusLabel,
  type RepairJobRow,
} from "@/lib/admin/read";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// THE QUEUE, GROUPED BY WHO IS HOLDING EACH JOB UP. An operator opening this page needs to know
// what is theirs to move before anything else; alphabetical order and raw status answer neither.
// The grouping comes from the flags the API sends per row, never from this page re-deciding what a
// status means - that is how a screen and the service metrics come to disagree.

function QueueTable({ jobs }: { jobs: readonly RepairJobRow[] }) {
  return (
    <table className="admin-table">
      <thead>
        <tr>
          <th>Job</th>
          <th>Customer</th>
          <th>Status</th>
          <th>Inspection</th>
          <th>Strategy</th>
          <th>Engine</th>
          <th>Priority</th>
          <th>Operator</th>
          <th>Opened</th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((job) => (
          <tr key={job.id}>
            <td>
              <Link href={`/repair/${job.id}`}>{job.id.slice(0, 8)}</Link>
            </td>
            <td>{job.owner_id}</td>
            <td>
              {repairStatusLabel(job.status)}
              {/* WHY it is stopped, beside the fact that it is - the operator should not have to
                  open the job to find out whether it is waiting on them or on the customer. */}
              {job.blocked_reason ? (
                <span className="admin-muted"> — {job.blocked_reason}</span>
              ) : null}
            </td>
            <td>{job.repair_status ? repairStatusLabel(job.repair_status) : "not inspected"}</td>
            <td>{job.current_strategy || "—"}</td>
            <td>{job.target_engine || "—"}</td>
            <td>{job.service_priority}</td>
            <td>{job.assigned_operator ?? "unclaimed"}</td>
            <td>{compactDate(job.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default async function RepairQueuePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const status = typeof params.status === "string" ? params.status : "";
  const operator = typeof params.operator === "string" ? params.operator : "";
  const unassigned = params.unassigned === "true";

  const [queue, throughput] = await Promise.all([
    readRepairQueue({ limit: 100, operator, status, unassigned }),
    readRepairThroughput(),
  ]);
  const jobs = queue.data?.jobs ?? [];
  const groups = groupRepairQueue(jobs);

  const ours = jobs.filter((j) => !j.settled && !j.blocked && !j.awaiting_human).length;
  const waiting = jobs.filter((j) => j.awaiting_human).length;
  const blocked = jobs.filter((j) => j.blocked).length;
  const unclaimed = jobs.filter((j) => !j.settled && j.assigned_operator === null).length;

  return (
    <>
      <h1>Repair</h1>
      {queue.error ? <Alert>{queue.error}</Alert> : null}

      {throughput.data ? (
        <Panel
          title="What the service is delivering"
          heading="counted from the same rows this queue is worked from"
        >
          <StatRow
            stats={[
              { label: "Jobs", value: String(throughput.data.throughput.jobs_total) },
              {
                label: "Delivered",
                value: formatRate(throughput.data.throughput.delivery_rate),
              },
              {
                label: "Blocked",
                value: formatRate(throughput.data.throughput.blocked_rate),
              },
              {
                label: "Mean time to delivery",
                value: formatDuration(throughput.data.throughput.mean_seconds_to_delivery),
              },
              {
                label: "Operator decisions",
                value: String(throughput.data.throughput.operator_decisions_total),
              },
            ]}
          />
        </Panel>
      ) : null}

      <Panel title="The queue right now">
        <StatRow
          stats={[
            { label: "Ours to move", value: String(ours) },
            { label: "Waiting on a person", value: String(waiting) },
            { label: "Blocked", value: String(blocked) },
            { label: "Unclaimed", value: String(unclaimed) },
          ]}
        />
        <p className="admin-muted">
          <Link href="/repair">All</Link> ·{" "}
          <Link href="/repair?unassigned=true">Unclaimed</Link> ·{" "}
          <Link href="/repair?status=awaiting_strategy">Awaiting a strategy</Link> ·{" "}
          <Link href="/repair?status=repair_review,mesh_review">Awaiting review</Link> ·{" "}
          <Link href="/repair?status=waiting_customer">Waiting on a customer</Link>
        </p>
      </Panel>

      {groups.length === 0 ? (
        <Panel title="Nothing queued">
          <EmptyState
            note={
              queue.error
                ? "The queue could not be read, so this is not a statement that there is no work."
                : status || operator || unassigned
                  ? "No repair job matches this filter. The filter is applied by the API, so an empty list here means no match rather than no work."
                  : "No customer CAD is waiting on repair."
            }
          />
        </Panel>
      ) : null}

      {groups.map((group) => (
        <Panel key={group.key} title={group.title} heading={`${group.jobs.length} job(s)`}>
          <QueueTable jobs={group.jobs} />
        </Panel>
      ))}
    </>
  );
}
