import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import {
  type AdminRepairTraceSection,
  compactDate,
  readJobRepairTrace,
  readJobWhy,
} from "@/lib/admin/read";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// ONE RUN'S WHY RECORD. Every decision the run made on its own, the rule behind it (the rulebook,
// docs/RULES.md in the platform repo) and the facts that rule read. Read-only: the record is what
// the run wrote when it ended. Open wherever Customers is (dev included): the runs link here.

function shown(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export default async function JobWhyPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  const [why, trace] = await Promise.all([readJobWhy(jobId), readJobRepairTrace(jobId)]);
  const decisions = why.data?.decisions ?? [];

  return (
    <>
      <h1>Why this run did what it did</h1>
      <p className="admin-lede">
        Run {jobId}. Each line is one decision the run made on its own, and the written rule
        behind it.
      </p>

      {why.error ? (
        <Alert>{why.error}</Alert>
      ) : (
        <>
          <StatRow
            stats={[
              { label: "Rulebook version", value: why.data?.rules_version || "-" },
              { label: "Decisions", value: decisions.length.toLocaleString("en-US") },
              { label: "Recorded", value: compactDate(why.data?.recorded_at ?? null) },
            ]}
          />

          <Panel title="In plain words">
            {why.data?.sentences.length ? (
              <ol>
                {why.data.sentences.map((sentence, index) => (
                  <li key={index}>{sentence}</li>
                ))}
              </ol>
            ) : (
              <EmptyState note="The run recorded no decisions." />
            )}
          </Panel>

          <Panel title="Decisions" heading="the rule, its source and the facts it read">
            {decisions.length ? (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Stage</th>
                    <th>Decided</th>
                    <th>Rule</th>
                    <th>Source</th>
                    <th>Inputs</th>
                    <th>Alternatives</th>
                  </tr>
                </thead>
                <tbody>
                  {decisions.map((decision, index) => (
                    <tr key={index}>
                      <td>{decision.stage}</td>
                      <td>
                        {decision.decided}: {shown(decision.value)}
                      </td>
                      <td>
                        {decision.rule || "-"}
                        {decision.also?.length ? ` (with ${decision.also.join(", ")})` : ""}
                      </td>
                      <td>{decision.source}</td>
                      <td>
                        <code>{shown(decision.inputs)}</code>
                      </td>
                      <td>{shown(decision.alternatives)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState note="The run recorded no decisions." />
            )}
          </Panel>
        </>
      )}

      {/* THE REPAIR INVESTIGATION (dev diagnostics, CAD_REPAIR_INVESTIGATION_TRACE). Per region:
          how the investigator ended and why no package; the per-turn trace behind an expander.
          Absent (a 404) when the run traced nothing, which is the production default. */}
      {trace.data ? (
        <Panel title="Repair investigation" heading="per region: how it ended, and why no package">
          <TraceSection section={trace.data} />
        </Panel>
      ) : null}
      {trace.data?.handback ? (
        <Panel
          title="Repair investigation after the mesh failed"
          heading="the mesher handback's scoped repair"
        >
          <TraceSection section={trace.data.handback} />
        </Panel>
      ) : null}
    </>
  );
}

// ONE TRACE: the per-region outcome table, then the per-turn lines behind an expander.
function TraceSection({ section }: { section: AdminRepairTraceSection }) {
  const outcomes = section.outcomes ?? [];
  const lines = section.lines ?? [];
  return (
    <>
      {outcomes.length ? (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Region</th>
              <th>Tier</th>
              <th>Ending</th>
              <th>Tool / model calls</th>
              <th>Seconds</th>
              <th>Edits tried / kept</th>
              <th>Rolled back</th>
              <th>Why no package</th>
            </tr>
          </thead>
          <tbody>
            {outcomes.map((row, index) => (
              <tr key={index}>
                <td>{shown(row.region)}</td>
                <td>{row.tier || "-"}</td>
                <td>
                  {row.end || "-"}
                  {row.form ? ` (${row.form})` : ""}
                </td>
                <td>
                  {row.tool_calls} / {row.model_calls}
                </td>
                <td>{row.seconds.toFixed(1)}</td>
                <td>
                  {row.edits.tried} / {row.edits.kept}
                </td>
                <td>{shown(row.edits.rolled_back)}</td>
                <td>{row.why_no_package || "a package was concluded"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <EmptyState note="The investigator ran on no region." />
      )}
      <details>
        <summary>
          Per-turn trace ({lines.length.toLocaleString("en-US")} lines
          {section.truncated ? ", truncated" : ""}
          {section.transcript.object_key ? `, ${String(section.transcript.object_key)}` : ""})
        </summary>
        {section.transcript.error ? (
          <Alert>{`The trace was not stored: ${String(section.transcript.error)}`}</Alert>
        ) : null}
        <pre>{lines.map((line) => JSON.stringify(line)).join("\n")}</pre>
      </details>
    </>
  );
}
