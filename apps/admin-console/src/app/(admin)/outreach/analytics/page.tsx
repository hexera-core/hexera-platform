import {
  kpis, funnel, dailyActivity, bySegment, byStep,
  verificationBreakdown, timeToReply, replyMix,
  cumulativeSends, verificationScoreDistribution, coverageByIndustry,
} from "@/lib/outreach/analytics/queries";
import {
  Panel, PageHeader, Stat, Chip, ActivityChart, CumulativeChart, CoverageBars,
  Funnel, BarList, Empty, pct,
} from "@/app/_components/outreach/ui";
import { SegmentExplorer } from "@/app/_components/outreach/segment-explorer";

export const dynamic = "force-dynamic";

/**
 * Grouped into four questions rather than one long stack of panels:
 * how much went out, what came back, how healthy the list is, and which
 * slice is working. Anything that answered the same question twice has been
 * merged, and the long tail of segment tables is collapsed by default.
 */

async function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="mb-9">
      <header className="mb-3 pb-2 border-b border-[var(--color-line)]">
        <h2 className="sec-eyebrow">{title}</h2>
        {note ? <p className="text-[0.8125rem] text-[var(--color-muted)] mt-1.5 max-w-[70ch]">{note}</p> : null}
      </header>
      {children}
    </section>
  );
}

/** Rates sit next to their raw counts on purpose. A 100% reply rate off one
 *  contacted person is noise, and only the denominator says so. */
async function SegmentTable({ rows }: { rows: ReturnType<typeof bySegment> }) {
  return (
    <div className="scroll-x">
      <table className="tbl">
        <thead>
          <tr>
            <th>Segment</th><th>People</th><th>Contacted</th>
            <th>Replies</th><th>Rate</th><th>Positive</th>
          </tr>
        </thead>
        <tbody>
          {(await rows).map((row) => (
            <tr key={row.segment}>
              <td className="max-w-[22rem] truncate" title={row.segment}>{row.segment}</td>
              <td className="t-num">{row.contacts}</td>
              <td className="t-num">{row.contacted}</td>
              <td className="t-num">{row.replies}</td>
              <td className="t-num">
                {row.contacted > 0 ? pct(row.replyRate, 0) : <span className="text-[var(--color-faint)]">-</span>}
              </td>
              <td className="t-num">
                {row.positive > 0
                  ? <span className="text-[var(--color-positive)]">{row.positive}</span>
                  : <span className="text-[var(--color-faint)]">0</span>}
              </td>
            </tr>
          ))}
          {!(await rows).length && <tr><td colSpan={6}><Empty>No data.</Empty></td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export default async function AnalyticsPage() {
  const k = await kpis();
  const activity = await dailyActivity(60);
  const cumulative = await cumulativeSends(60);
  const steps = byStep();
  const verification = verificationBreakdown();
  const scores = await verificationScoreDistribution();
  const coverage = await coverageByIndustry();
  const ttr = timeToReply();
  const mix = replyMix();

  return (
    <>
      <PageHeader
        title="Analytics"
        description="Reply rates use the number of people actually contacted, not the size of the list."
      />

      <div
        className="grid gap-3 mb-9"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(155px, 100%), 1fr))" }}
      >
        <Stat
          label="Delivered"
          value={(await k).liveSent}
          tone={(await k).liveSent > 0 ? "accent" : "default"}
          sub={(await k).liveSent === 0 ? "Nothing sent yet" : `${(await k).liveContactsReached} people`}
        />
        <Stat label="Reply rate" value={pct(k.replyRate)} sub={`${(await k).humanReplies} replies`} />
        <Stat label="Positive share" value={pct(k.positiveRate)} tone="positive" sub="Of human replies" />
        <Stat
          label="Bounce rate"
          value={pct(k.bounceRate)}
          tone={(await k).bounceRate > 3 ? "danger" : "default"}
          sub="Keep this under 3%"
        />
        <Stat label="Suppressed" value={(await k).suppressed} sub="Opt-outs and bounces" />
      </div>

      <Section
        title="Volume"
        note="The running total is the one to watch on a new sending address, since a steady ramp matters more than any single day."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(400px, 100%), 1fr))" }}>
          <Panel title="Per day, last 60">
            <ActivityChart data={activity} height={190} />
          </Panel>
          <Panel title="Running total">
            <CumulativeChart data={cumulative} height={190} />
          </Panel>
        </div>
      </Section>

      <Section
        title="Response"
        note="Where people drop out of the sequence, which touch earns the replies, and how long they take to answer."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(300px, 100%), 1fr))" }}>
          <Panel title="Funnel">
            <Funnel stages={await funnel()} />
          </Panel>

          <Panel title="By sequence step">
            {(await steps).length ? (
              <div className="scroll-x">
                <table className="tbl">
                  <thead>
                    <tr><th>Step</th><th>Template</th><th>Sent</th><th>Replies</th><th>Rate</th></tr>
                  </thead>
                  <tbody>
                    {(await steps).map((step) => (
                      <tr key={step.step}>
                        <td className="t-num">{step.step}</td>
                        <td className="max-w-[12rem] truncate text-[0.75rem]" title={step.templateName ?? ""}>
                          {step.templateName ?? "-"}
                        </td>
                        <td className="t-num">{step.sent}</td>
                        <td className="t-num">{step.replies}</td>
                        <td className="t-num">{pct(step.replyRate, 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty>Nothing sent yet.</Empty>
            )}
          </Panel>

          <Panel title="Reply mix and timing">
            {(await mix).length ? (
              <>
                <ul className="list-none m-0 p-0 flex flex-col gap-2 mb-4">
                  {(await mix).map((row) => (
                    <li key={row.classification} className="flex items-center justify-between gap-3">
                      <Chip value={row.classification} />
                      <span className="t-num text-[0.8125rem]">{row.count}</span>
                    </li>
                  ))}
                </ul>
                {(await ttr).length ? (
                  <>
                    <div className="t-label mb-2 pt-3 border-t border-[var(--color-line)]">
                      Time to reply
                    </div>
                    <BarList items={(await ttr).map((row) => ({ label: row.bucket, value: row.count }))} />
                  </>
                ) : null}
              </>
            ) : (
              <Empty>No replies classified yet.</Empty>
            )}
          </Panel>
        </div>
      </Section>

      <Section
        title="List health"
        note="Whether the addresses are good, and how much of the list is still untouched."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(320px, 100%), 1fr))" }}>
          <Panel title="Verification">
            <ul className="list-none m-0 p-0 flex flex-col gap-2 mb-4">
              {(await verification).map((row) => (
                <li key={row.status} className="flex items-center justify-between gap-3">
                  <Chip value={row.status} />
                  <span className="t-num text-[0.8125rem]">
                    {row.count}
                    <span className="text-[var(--color-faint)] text-[0.6875rem] ml-2">
                      avg {row.avgScore}
                    </span>
                  </span>
                </li>
              ))}
              {!(await verification).length && <li><Empty>Not verified yet.</Empty></li>}
            </ul>
            {(await scores).length ? (
              <>
                <div className="t-label mb-2 pt-3 border-t border-[var(--color-line)]">
                  Score distribution
                </div>
                <BarList items={(await scores).map((s) => ({ label: s.bucket, value: s.count }))} />
              </>
            ) : null}
          </Panel>

          <Panel title="Coverage by industry" subtitle="Filled is contacted. The rest is still to do.">
            <CoverageBars rows={coverage} />
          </Panel>
        </div>
      </Section>

      <Section
        title="Segments"
        note="Which slice of the list actually answers. Role and industry are the two worth checking first, since they decide who to write to next and what to say."
      >
        <Panel
          title="Explore"
          subtitle="Pick a breakdown and a measure. Percentages carry their raw counts, and thinly sampled segments are drawn hollow so a flattering number off two people cannot lead the chart."
        >
          <SegmentExplorer
            data={{
              role_group: await bySegment("role_group"),
              industry: await bySegment("industry"),
              tier: await bySegment("tier"),
              source_sheet: await bySegment("source_sheet"),
            }}
          />
        </Panel>

        <div className="grid gap-4 my-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(340px, 100%), 1fr))" }}>
          <Panel title="By role" flush>
            <SegmentTable rows={bySegment("role_group")} />
          </Panel>
          <Panel title="By industry" flush>
            <SegmentTable rows={bySegment("industry")} />
          </Panel>
        </div>

        <details className="panel">
          <summary className="t-label cursor-pointer px-4 py-3">
            By tier and source sheet
          </summary>
          <div className="grid gap-4 p-4 pt-0" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(340px, 100%), 1fr))" }}>
            <div>
              <div className="t-label mb-2">Source sheet</div>
              <SegmentTable rows={bySegment("source_sheet")} />
            </div>
            <div>
              <div className="t-label mb-2">Tier</div>
              <SegmentTable rows={bySegment("tier")} />
            </div>
          </div>
        </details>
      </Section>
    </>
  );
}
