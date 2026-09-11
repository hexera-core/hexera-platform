import { upcomingQueue, capacitySnapshot } from "@/lib/outreach/engine/scheduler";
import { previewStep } from "@/lib/outreach/engine/sender";
import { sendMode } from "@/lib/outreach/core/settings";
import { dispatchMode, dispatchTime, sendableNow } from "@/lib/outreach/engine/dispatch";
import { formatRelative, formatDateTime } from "@/lib/outreach/core/time";
import { Panel, PageHeader, Stat, Chip, Empty } from "@/app/_components/outreach/ui";
import { ActionForm, SubmitButton } from "@/app/_components/outreach/action-form";
import { SendPicker, type Sendable } from "@/app/_components/outreach/send-picker";
import { stopEnrollment, sendSelected } from "@/app/(admin)/outreach/actions";

export const dynamic = "force-dynamic";

export default async function QueuePage() {
  const mode = await dispatchMode();
  const manual = mode === "manual";
  const live = (await sendMode()).live;
  const capacity = await capacitySnapshot();

  const sendable = await sendableNow();

  // Render what the next few would actually say. previewStep has no side
  // effects: it stops before the send and before the ledger.
  const previews = await Promise.all(
    (await sendable).slice(0, 3).map(async (item) => ({
      company: item.contact.company,
      outcome: await previewStep(item),
    })),
  );

  const picker: Sendable[] = (await sendable).map((item) => ({
    enrollmentId: item.enrollment.id,
    company: item.contact.company,
    person: item.contact.full_name ?? "-",
    email: item.contact.email_normalized ?? "-",
    step: item.enrollment.current_step + 1,
    role: item.contact.role ?? "-",
  }));

  return (
    <>
      <PageHeader
        title="Send queue"
        description="What can go out, and exactly what it will say. Previews come from the live templates and the real contact record, so this is the message itself rather than a sample of it."
      />

      <div className="grid gap-3 mb-6" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(180px, 100%), 1fr))" }}>
        <Stat
          label="Mode"
          value={manual ? "Manual" : "Automatic"}
          sub={manual ? "Nothing sends on its own" : `From ${(await dispatchTime()).text} each day`}
          tone={manual ? "default" : "accent"}
        />
        <Stat
          label={manual ? "Ready to send" : "Due now"}
          value={(await sendable).length}
          sub={manual ? "Pick who you want" : "Waiting on the worker"}
          tone={(await sendable).length ? "accent" : "default"}
        />
        <Stat
          label="Today's usage"
          value={`${(await capacity).perCampaign[0]?.used ?? 0}/${(await capacity).perCampaign[0]?.limit ?? "-"}`}
          sub={`Day ${(await capacity).day}`}
        />
      </div>

      {manual ? (
        <Panel
          title="Ready to send"
          subtitle="Tick the ones you want, then send them or take them back out of the campaign. A sent contact leaves this list and returns when their follow-up is actually due. The daily caps, one person per company per day, and both safety switches still apply to a send."
          flush
        >
          {picker.length ? (
            <div className="p-3">
              <SendPicker items={picker} action={sendSelected} live={live} />
            </div>
          ) : (
            <div className="p-3">
              <Empty>
                Nothing is enrolled in an active campaign. Enroll from the People view on Partners & Contacts,
                page, and they appear here straight away.
              </Empty>
            </div>
          )}
        </Panel>
      ) : (
        <Panel
          title="Automatic"
          subtitle={`The worker sends on its own from ${(await dispatchTime()).text} each day, campaign time, whenever something is due. Switch to manual under Settings if you would rather pick each one.`}
        >
          <Empty>
            There is no send button in automatic mode. Everything below happens on the
            schedule.
          </Empty>
        </Panel>
      )}

      <div className="grid gap-4 mb-4 mt-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(340px, 100%), 1fr))" }}>
        {previews.map((preview, i) => (
          <Panel key={i} title={`Preview: ${preview.company}`}>
            {preview.outcome.kind === "preview" ? (
              <>
                <div className="t-label mb-1">To</div>
                <div className="t-mono text-[0.8125rem] mb-3">{preview.outcome.to}</div>
                <div className="t-label mb-1">Subject</div>
                <div className="text-[0.875rem] mb-3">{preview.outcome.subject}</div>
                <div className="t-label mb-1">Body</div>
                <pre className="text-[0.75rem] leading-relaxed whitespace-pre-wrap m-0 max-h-72 overflow-y-auto scroll-y text-[var(--color-muted)] font-[family-name:var(--font-mono)]">
                  {preview.outcome.body}
                </pre>
              </>
            ) : (
              <Empty>
                {preview.outcome.kind === "failed"
                  ? `Cannot render: ${preview.outcome.error}`
                  : `Would ${preview.outcome.kind.replace("_", " ")}.`}
              </Empty>
            )}
          </Panel>
        ))}
        {!previews.length && (
          <Panel title="Nothing to preview">
            <Empty>
              {manual
                ? "Nothing is enrolled in an active campaign yet."
                : "No enrollment is due right now. Sends only happen inside the campaign's window, and only while the campaign is active."}
            </Empty>
          </Panel>
        )}
      </div>

      <ScheduledTable manual={manual} />
    </>
  );
}

/**
 * Everyone currently enrolled, each with a Stop button. In automatic mode
 * the timing column is the schedule; in manual mode it is just an ordering,
 * but the list itself is what makes any enrollment reversible from here —
 * including ones that are mid-sequence and not due yet.
 */
async function ScheduledTable({ manual }: { manual: boolean }) {
  const queue = await upcomingQueue(200);

  return (
    <Panel
      title={manual ? "Everyone enrolled" : "Scheduled sends"}
      subtitle={manual ? "Stop takes anyone back out of their campaign, due or not." : undefined}
      flush
    >
      <div className="scroll-x">
        <table className="tbl">
          <thead>
            <tr>
              <th>When</th><th>Company</th><th>Person</th><th>Email</th>
              <th>Step</th><th>Campaign</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {(await queue).map((row) => (
              <tr key={row.enrollment_id}>
                <td className="whitespace-nowrap">
                  {row.next_send_at ? (
                    <>
                      <div className="t-mono text-[0.75rem]">{formatRelative(row.next_send_at)}</div>
                      <div className="text-[0.625rem] text-[var(--color-faint)]">
                        {formatDateTime(row.next_send_at)}
                      </div>
                    </>
                  ) : (
                    <div className="t-mono text-[0.75rem] text-[var(--color-faint)]">not scheduled</div>
                  )}
                </td>
                <td className="max-w-[14rem] truncate">{row.company}</td>
                <td className="max-w-[10rem] truncate">{row.full_name ?? "-"}</td>
                <td className="t-mono text-[0.75rem] max-w-[15rem] truncate">{row.email ?? "-"}</td>
                <td className="t-num">{row.step}</td>
                <td className="max-w-[12rem] truncate text-[0.75rem] text-[var(--color-muted)]">
                  {row.campaign_name}
                </td>
                <td><Chip value={row.status} /></td>
                <td>
                  <ActionForm action={stopEnrollment}>
                    <input type="hidden" name="enrollment_id" value={row.enrollment_id} />
                    <input type="hidden" name="reason" value="stopped from the queue" />
                    <SubmitButton variant="danger">Stop</SubmitButton>
                  </ActionForm>
                </td>
              </tr>
            ))}
            {!(await queue).length && (
              <tr>
                <td colSpan={8}>
                  <Empty>Nothing scheduled. Enroll from the People view on Partners & Contacts.</Empty>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
