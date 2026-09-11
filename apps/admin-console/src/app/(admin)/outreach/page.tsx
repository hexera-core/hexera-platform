import Link from "next/link";
import {
  funnel, kpis, dailyActivity, replyMix, enrollmentStates,
  campaignSummaries, verificationFlags,
} from "@/lib/outreach/analytics/queries";
import { capacitySnapshot, upcomingQueue } from "@/lib/outreach/engine/scheduler";
import { dispatchMode, sendableNow } from "@/lib/outreach/engine/dispatch";
import { recentEvents, parsePayload } from "@/lib/outreach/core/events";
import { connectionStatus } from "@/lib/outreach/mail/gmail";
import { formatRelative, formatDateTime } from "@/lib/outreach/core/time";
import {
  Panel, PageHeader, Stat, Chip, ActivityChart, Funnel, Empty, pct,
} from "@/app/_components/outreach/ui";


// NEVER PRERENDERED. These pages read the outreach database, which does not exist at build time -
// and a cached contact list or queue is the answer to "what was true when this was built", which
// is never the question anyone is asking of them.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export default async function OverviewPage() {
  const k = await kpis();
  const stages = await funnel();
  const activity = await dailyActivity(30);
  const capacity = await capacitySnapshot();
  // In manual mode nothing has a scheduled time, so upcomingQueue would come
  // back empty and the panel would claim the queue was empty when 17 people
  // are sitting there ready.
  const manual = (await dispatchMode()) === "manual";
  const queue = manual
    ? (await sendableNow())
        .slice(0, 8)
        .map((item) => ({
          enrollment_id: item.enrollment.id,
          company: item.contact.company,
          email: item.contact.email_normalized,
          step: item.enrollment.current_step + 1,
          next_send_at: item.enrollment.next_send_at ?? "",
        }))
    : await upcomingQueue(8);
  const events = recentEvents(14);
  const gmail = await connectionStatus();
  const flags = await verificationFlags();
  const campaigns = campaignSummaries();


  return (
    <>
      <PageHeader
        title="Overview"
        description="Every contact, message and reply in one place."
      />

      <div className="grid gap-3 mb-6" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(160px, 100%), 1fr))" }}>
        <Stat
          label="Delivered"
          value={(await k).liveSent}
          sub={
            // A bounce leaves delivered at zero while messages have in fact
            // gone out, and "nothing has been sent yet" would then be a lie
            // that hides the only thing worth knowing.
            (await k).liveSent === 0
              ? (await k).bounces > 0
                ? `${(await k).bounces} sent, ${(await k).bounces === 1 ? "it" : "all"} bounced`
                : "Nothing has been sent yet"
              : `${(await k).liveContactsReached} ${(await k).liveContactsReached === 1 ? "person" : "people"} reached` +
                (k.bounces > 0 ? `, ${(await k).bounces} bounced` : "")
          }
          tone={(await k).liveSent > 0 ? "accent" : "default"}
        />
        <Stat label="Reply rate" value={pct(k.replyRate)} sub={`${(await k).humanReplies} human replies`} />
        <Stat label="Interested" value={(await k).positiveReplies} sub={`${pct(k.positiveRate)} of replies`} tone="positive" />
        <Stat label="Declined" value={(await k).negativeReplies} sub={`${(await k).suppressed} on suppression list`} tone="danger" />
        <Stat label="Needs review" value={(await k).awaitingReview} sub="Replies a human should read" tone={(await k).awaitingReview > 0 ? "caution" : "default"} />
        <Stat label="In flight" value={(await k).activeSequences} sub={`${(await capacity).globalUsed} sent today`} />
      </div>

      <div className="grid gap-4 mb-4" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)" }}>
        <Panel title="Last 30 days" subtitle="Messages sent per day, with replies on the same count axis.">
          <ActivityChart data={activity} />
        </Panel>

        <Panel title="Funnel" subtitle="Where the list is losing people.">
          <Funnel stages={stages} />
        </Panel>
      </div>

      <div className="grid gap-4 mb-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(300px, 100%), 1fr))" }}>
        <Panel title="Reply mix">
          {(await replyMix()).length ? (
            <ul className="list-none m-0 p-0 flex flex-col gap-2">
              {(await replyMix()).map((row) => (
                <li key={row.classification} className="flex items-center justify-between gap-3">
                  <Chip value={row.classification} />
                  <span className="t-num text-[0.8125rem]">
                    {row.count}
                    <span className="text-[var(--color-faint)] text-[0.6875rem] ml-2">
                      conf {row.avgConfidence}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No replies yet. They appear here as the worker reads the inbox.</Empty>
          )}
        </Panel>

        <Panel title="Sequence states">
          {(await enrollmentStates()).length ? (
            <ul className="list-none m-0 p-0 flex flex-col gap-2">
              {(await enrollmentStates()).map((row) => (
                <li key={row.status} className="flex items-center justify-between gap-3">
                  <Chip value={row.status} />
                  <span className="t-num text-[0.8125rem]">{row.count}</span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>Nobody is in a sequence yet.</Empty>
          )}
        </Panel>

        <Panel title="System">
          <ul className="list-none m-0 p-0 flex flex-col gap-2 text-[0.8125rem]">
            <li className="flex justify-between gap-3">
              <span className="text-[var(--color-muted)]">Gmail</span>
              {gmail.connected ? (
                <span className="text-[var(--color-positive)] truncate" title={gmail.account ?? ""}>
                  {gmail.account}
                </span>
              ) : (
                <span className="text-[var(--color-faint)]">not connected</span>
              )}
            </li>
            <li className="flex justify-between gap-3">
              <span className="text-[var(--color-muted)]">Unverified</span>
              <span className="t-num">{flags?.unverified ?? 0}</span>
            </li>
            <li className="flex justify-between gap-3">
              <span className="text-[var(--color-muted)]">Role mailboxes</span>
              <span className="t-num">{flags?.role ?? 0}</span>
            </li>
            <li className="flex justify-between gap-3">
              <span className="text-[var(--color-muted)]">Catch-all domains</span>
              <span className="t-num">{flags?.catchAll ?? 0}</span>
            </li>
            <li className="flex justify-between gap-3">
              <span className="text-[var(--color-muted)]">Bounce rate</span>
              <span className="t-num">{pct(k.bounceRate)}</span>
            </li>
          </ul>
          {!gmail.connected && (
            <p className="text-[0.6875rem] text-[var(--color-faint)] mt-3 leading-snug">
              Run <code className="t-mono">npm run gmail:auth</code> to connect a sending mailbox.
              Everything else works without it.
            </p>
          )}
        </Panel>
      </div>

      <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(320px, 100%), 1fr))" }}>
        <Panel
          title="Campaigns"
          actions={<Link href="/settings" className="btn">Manage</Link>}
          flush
        >
          <div className="scroll-x">
            <table className="tbl">
              <thead>
                <tr><th>Campaign</th><th>Status</th><th>In flight</th><th>Sent</th><th>Replies</th></tr>
              </thead>
              <tbody>
                {(await campaigns).map((c) => (
                  <tr key={c.id}>
                    <td className="max-w-[16rem] truncate">{c.name}</td>
                    <td><Chip value={c.status} /></td>
                    <td className="t-num">{c.enrolled}</td>
                    <td className="t-num">{c.sent}</td>
                    <td className="t-num">
                      {c.replies}
                      {c.positive > 0 && (
                        <span className="text-[var(--color-positive)] ml-1.5 text-[0.6875rem]">
                          +{c.positive}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {!(await campaigns).length && (
                  <tr><td colSpan={5}><Empty>No campaigns. Run <code className="t-mono">npm run seed</code>.</Empty></td></tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title={manual ? "Ready to send" : "Next to send"}
          subtitle={
            (await queue).length
              ? manual
                ? "Waiting on you. Nothing here is on a timer."
                : undefined
              : "Nothing queued."
          }
          actions={<Link href="/queue" className="btn">Full queue</Link>}
          flush
        >
          <div className="scroll-x">
            <table className="tbl">
              <tbody>
                {(await queue).map((row) => (
                  <tr key={row.enrollment_id}>
                    <td className="max-w-[12rem] truncate">
                      <div>{row.company}</div>
                      <div className="text-[0.6875rem] text-[var(--color-faint)] truncate">{row.email}</div>
                    </td>
                    <td className="t-num text-[0.75rem]">step {row.step}</td>
                    {!manual && (
                      <td className="t-mono text-[0.75rem] text-[var(--color-muted)] whitespace-nowrap">
                        {formatRelative(row.next_send_at)}
                      </td>
                    )}
                  </tr>
                ))}
                {!(await queue).length && <tr><td><Empty>Queue is empty.</Empty></td></tr>}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Recent activity" flush>
          <ul className="list-none m-0 p-0 max-h-[22rem] overflow-y-auto scroll-y">
            {(await events).map((event) => {
              const payload = parsePayload<Record<string, unknown>>(event);
              return (
                <li key={event.id} className="px-4 py-2 border-b border-[var(--color-line)] last:border-0">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="t-mono text-[0.6875rem] text-[var(--color-accent-soft)]">{event.type}</span>
                    <span className="t-mono text-[0.625rem] text-[var(--color-faint)] whitespace-nowrap">
                      {formatDateTime(event.created_at)}
                    </span>
                  </div>
                  {payload && (
                    <div className="text-[0.6875rem] text-[var(--color-muted)] truncate mt-0.5">
                      {Object.entries(payload).slice(0, 3).map(([key, value]) => `${key}: ${String(value)}`).join(" · ")}
                    </div>
                  )}
                </li>
              );
            })}
            {!(await events).length && <li><Empty>No activity recorded yet.</Empty></li>}
          </ul>
        </Panel>
      </div>
    </>
  );
}
