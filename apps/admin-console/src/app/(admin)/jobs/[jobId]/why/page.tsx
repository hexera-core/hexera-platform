import { Alert, EmptyState, Panel, StatRow } from "@/app/_components/panel";
import { compactDate, readJobWhy } from "@/lib/admin/read";

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
  const why = await readJobWhy(jobId);
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

          <Panel title="In plain words" heading="one sentence per decision">
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
    </>
  );
}
