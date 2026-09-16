import Link from "next/link";
import { listReplies, replyCounts } from "@/lib/outreach/inbox/poller";
import { replyMix, kpis } from "@/lib/outreach/analytics/queries";
import { listSuppressions } from "@/lib/outreach/core/suppression";
import { formatDateTime, formatRelative } from "@/lib/outreach/core/time";
import { Panel, PageHeader, Stat, Chip, Empty, Field } from "@/app/_components/outreach/ui";
import { ActionForm, SubmitButton } from "@/app/_components/outreach/action-form";
import { reviewReply, dismissReview, addSuppression, removeSuppression } from "@/app/(admin)/outreach/actions";

export const dynamic = "force-dynamic";

const CLASSIFICATIONS = ["positive", "negative", "neutral", "ooo", "auto", "bounce"] as const;

const VIEWS = [
  { key: "review", label: "Needs review" },
  { key: "all", label: "Everything" },
  { key: "positive", label: "Interested" },
  { key: "negative", label: "Declined" },
  { key: "bounce", label: "Bounced" },
  { key: "ooo", label: "Out of office" },
  { key: "auto", label: "Automated" },
  { key: "neutral", label: "Neutral" },
] as const;

export default async function InboxPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const raw = typeof params.show === "string" ? params.show : "review";
  const view = VIEWS.some((v) => v.key === raw) ? raw : "review";

  const replies = await listReplies({
    needsReviewOnly: view === "review",
    classification: view === "review" || view === "all" ? undefined : view,
  }) as Record<string, string | number | null>[];

  const counts = await replyCounts();
  const countOf = async (key: string) => counts.find((c) => c.classification === key)?.n ?? 0;

  const mix = replyMix();
  const k = await kpis();
  const suppressions = listSuppressions();

  return (
    <>
      <PageHeader
        title="Replies"
        description="Every reply matched to a sequence, with the reasoning behind its label. Anything uncertain sits at the top. A sequence stays stopped until you decide."
      />

      {/* One row of filters. Each is a plain link, so any view is a URL you
          can bookmark or send to someone. */}
      <div className="flex flex-wrap gap-1.5 mb-4">
        {VIEWS.map((v) => {
          // "Everything" counts people, so it counts only human replies.
          // OOO autoresponders, bounce reports, and machine mail keep their
          // own tabs but do not inflate the headline.
          const HUMAN = ["positive", "negative", "neutral"];
          const n =
            v.key === "all"
              ? counts.reduce((sum, c) => sum + (HUMAN.includes(String(c.classification)) ? c.n : 0), 0)
              : v.key === "review"
                ? counts.reduce((sum, c) => sum + c.unreviewed, 0)
                : countOf(v.key);
          const active = v.key === view;
          return (
            <Link
              key={v.key}
              href={v.key === "review" ? "/inbox" : `/inbox?show=${v.key}`}
              className="t-label px-2.5 py-1.5 rounded-sm whitespace-nowrap transition-colors"
              style={{
                background: active ? "rgba(255,79,0,0.12)" : "rgba(238,235,225,0.04)",
                color: active ? "var(--color-accent-soft)" : "var(--color-muted)",
              }}
            >
              {v.label}
              <span className="t-num ml-1.5 text-[var(--color-faint)]">{n}</span>
            </Link>
          );
        })}
      </div>

      <div className="grid gap-3 mb-6" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(160px, 100%), 1fr))" }}>
        <Stat label="Needs review" value={(await k).awaitingReview} tone={(await k).awaitingReview ? "caution" : "default"} sub="Uncertain or disputed" />
        <Stat label="Interested" value={(await k).positiveReplies} tone="positive" sub="Handed off, sequence stopped" />
        <Stat label="Declined" value={(await k).negativeReplies} tone="danger" sub="Stopped and suppressed" />
        <Stat label="Bounces" value={(await k).bounces} sub="Address is undeliverable" />
        <Stat label="Suppressed" value={(await suppressions).length} sub="Will never be contacted" />
      </div>

      <div className="grid gap-4" style={{ gridTemplateColumns: "minmax(0, 3fr) minmax(0, 1fr)" }}>
        <div className="flex flex-col gap-4">
          {replies.map((reply) => {
            const rules = reply.matched_rules ? (JSON.parse(String(reply.matched_rules)) as string[]) : [];
            const needsReview = Number(reply.requires_review) === 1;

            return (
              <section
                key={reply.id as number}
                className="panel p-4"
                style={
                  needsReview
                    ? { borderColor: "rgba(201,162,39,0.4)", background: "rgba(201,162,39,0.03)" }
                    : undefined
                }
              >
                <header className="flex flex-wrap items-start justify-between gap-3 mb-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Chip value={reply.classification as string} />
                      {needsReview && <span className="chip chip-caution">needs review</span>}
                      <span className="t-mono text-[0.625rem] text-[var(--color-faint)]">
                        {reply.classifier as string} · confidence {Number(reply.confidence).toFixed(2)}
                      </span>
                    </div>
                    <h3 className="text-[0.9375rem] mt-2 mb-0.5 font-medium">
                      {(reply.full_name as string) ?? "Unknown"}{" "}
                      <span className="text-[var(--color-muted)] font-normal">· {reply.company as string}</span>
                    </h3>
                    <div className="t-mono text-[0.6875rem] text-[var(--color-faint)]">
                      {reply.email as string} · {(reply.role as string) ?? "role unknown"}
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="t-mono text-[0.6875rem] text-[var(--color-muted)]">
                      {formatRelative(reply.received_at as string)}
                    </div>
                    <div className="t-mono text-[0.625rem] text-[var(--color-faint)]">
                      {formatDateTime(reply.received_at as string)}
                    </div>
                  </div>
                </header>

                <div className="t-label mb-1">{(reply.subject as string) ?? "(no subject)"}</div>
                <blockquote className="m-0 pl-3 border-l-2 border-[var(--color-line-2)] text-[0.8125rem] leading-relaxed text-[var(--color-muted)] whitespace-pre-wrap max-h-56 overflow-y-auto scroll-y">
                  {(reply.body as string) || (reply.snippet as string) || "(empty)"}
                </blockquote>

                {rules.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {rules.map((rule) => (
                      <span key={rule} className="chip chip-neutral" title="Rule that fired during classification">
                        {rule.length > 48 ? `${rule.slice(0, 48)}…` : rule}
                      </span>
                    ))}
                  </div>
                )}

                <footer className="mt-4 pt-3 border-t border-[var(--color-line)] flex flex-wrap items-end gap-3">
                  <ActionForm action={reviewReply} className="flex items-end gap-2">
                    <input type="hidden" name="reply_id" value={reply.id as number} />
                    <Field label="Correct classification">
                      <select name="classification" defaultValue={reply.classification as string} className="select">
                        {CLASSIFICATIONS.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    </Field>
                    <SubmitButton variant="primary">Confirm</SubmitButton>
                  </ActionForm>

                  <ActionForm action={dismissReview}>
                    <input type="hidden" name="reply_id" value={reply.id as number} />
                    <SubmitButton>Clear</SubmitButton>
                  </ActionForm>

                  <span className="t-label ml-auto">
                    {(reply.campaign_name as string) ?? "-"} · sequence{" "}
                    <Chip value={(reply.enrollment_status as string) ?? undefined} />
                  </span>
                </footer>
              </section>
            );
          })}

          {!replies.length && (
            <Panel title={VIEWS.find((v) => v.key === view)?.label ?? "Replies"}>
              <Empty>
                {view === "review"
                  ? "Nothing waiting on you. Replies the system resolved by itself are under the other filters."
                  : `No replies filed as ${VIEWS.find((v) => v.key === view)?.label.toLowerCase()}. The worker reads the connected mailbox each tick and files anything that matches a sequence here.`}
              </Empty>
            </Panel>
          )}
        </div>

        <div className="flex flex-col gap-4">
          <Panel title="Reply mix">
            {(await mix).length ? (
              <ul className="list-none m-0 p-0 flex flex-col gap-2">
                {(await mix).map((row) => (
                  <li key={row.classification} className="flex items-center justify-between gap-2">
                    <Chip value={row.classification} />
                    <span className="t-num text-[0.8125rem]">{row.count}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>Nothing classified yet.</Empty>
            )}
          </Panel>

          <Panel
            title="Suppression list"
            subtitle="Checked immediately before every send. Domain entries block everyone at that company."
          >
            <ActionForm action={addSuppression} className="flex flex-col gap-2 mb-4">
              <Field label="Add">
                <input name="value" placeholder="person@company.com" className="input" />
              </Field>
              <div className="flex gap-2 items-end">
                <select name="scope" className="select" defaultValue="email">
                  <option value="email">This address</option>
                  <option value="domain">Whole domain</option>
                </select>
                <SubmitButton>Add</SubmitButton>
              </div>
              <input type="hidden" name="reason" value="added from the dashboard" />
            </ActionForm>

            <ul className="list-none m-0 p-0 flex flex-col gap-1.5 max-h-72 overflow-y-auto scroll-y">
              {(await suppressions).map((s) => (
                <li key={s.id} className="flex items-center justify-between gap-2 text-[0.75rem]">
                  <div className="min-w-0">
                    <div className="t-mono truncate" title={s.value}>{s.value}</div>
                    <div className="text-[0.625rem] text-[var(--color-faint)] truncate">
                      {s.scope} · {s.reason}
                    </div>
                  </div>
                  <ActionForm action={removeSuppression}>
                    <input type="hidden" name="value" value={s.value} />
                    <input type="hidden" name="scope" value={s.scope} />
                    <SubmitButton>Remove</SubmitButton>
                  </ActionForm>
                </li>
              ))}
              {!(await suppressions).length && <li><Empty>Empty.</Empty></li>}
            </ul>
          </Panel>
        </div>
      </div>
    </>
  );
}
