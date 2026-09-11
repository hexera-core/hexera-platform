import { allSettings, sendMode, getBoolSetting, SETTING_KEYS } from "@/lib/outreach/core/settings";
import { dispatchMode, dispatchTime, sendableNow } from "@/lib/outreach/engine/dispatch";
import { DispatchModeForm } from "@/app/_components/outreach/dispatch-mode-form";
import type { Campaign } from "@/lib/outreach/core/types";
import { all, get } from "@/lib/outreach/db";
import type { Template } from "@/lib/outreach/core/types";
import { connectionStatus } from "@/lib/outreach/mail/gmail";
import { isLlmClassifierAvailable } from "@/lib/outreach/inbox/classify-llm";
import { Panel, PageHeader, Field } from "@/app/_components/outreach/ui";
import { ActionForm, SubmitButton } from "@/app/_components/outreach/action-form";
import {
  setLiveSending,
  setEnvSending,
  updateDispatch,
  updateSetting,
  setTestSending,
  sendTestEmail,
  updateCampaign,
} from "@/app/(admin)/outreach/actions";

export const dynamic = "force-dynamic";

function SettingRow({
  settingKey,
  label,
  hint,
  value,
  type = "text",
}: {
  settingKey: string;
  label: string;
  hint?: string;
  value: string;
  type?: "text" | "number" | "textarea" | "boolean";
}) {
  return (
    <ActionForm action={updateSetting} className="flex flex-col gap-2">
      <input type="hidden" name="key" value={settingKey} />
      <Field label={label} hint={hint}>
        {type === "textarea" ? (
          <textarea name="value" defaultValue={value} rows={3} className="textarea" />
        ) : type === "boolean" ? (
          <select name="value" defaultValue={value} className="select">
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        ) : (
          <input name="value" type={type} defaultValue={value} className="input" />
        )}
      </Field>
      <div><SubmitButton>Save</SubmitButton></div>
    </ActionForm>
  );
}

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  // The mailbox-connect routes hand their outcome back in the query string, because the browser
  // arrives here as a fresh navigation from Google rather than as a response to a form post.
  const params = await searchParams;
  const justConnected = typeof params.mailbox_connected === "string" ? params.mailbox_connected : null;
  const connectError = typeof params.mailbox_error === "string" ? params.mailbox_error : null;
  const settings = await allSettings();
  const mode = await sendMode();
  const gmail = await connectionStatus();
  const llm = isLlmClassifierAvailable();
  const testOn = await getBoolSetting(SETTING_KEYS.testSending);
  const dispatch = await dispatchMode();
  const releaseAt = (await dispatchTime()).text;
  const readyCount = (await sendableNow()).length;
  const templates = await all<Template>(`SELECT * FROM templates ORDER BY kind DESC, name`);
  // One campaign, so the per-campaign caps are shown inline under Limits
  // rather than behind a page of their own.
  const campaign = await get<Campaign>(`SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 1`);

  return (
    <>
      <PageHeader
        title="Settings"
        description="Sending safety, mailbox connection, and the thresholds the engine enforces."
      />

      {/* ── The interlock ────────────────────────────────────────────── */}
      <Panel
        title="Sending safety"
        subtitle="Two switches must agree before any message is delivered. Either one off keeps the system in dry run."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(260px, 100%), 1fr))" }}>
          <div className="panel p-3" style={{ background: "var(--color-bg-2)" }}>
            <div className="t-label mb-2">Switch 1 · this machine</div>
            <div className="flex items-center gap-2 mb-3">
              <span
                className="inline-block w-2 h-2 rounded-full"
                style={{ background: (await mode).envAllows ? "var(--color-accent)" : "var(--color-faint)" }}
              />
              <span className="t-mono text-[0.8125rem]">
                Sending <strong>{(await mode).envAllows ? "ON" : "OFF"}</strong>
              </span>
            </div>

            <ActionForm action={setEnvSending}>
              <input type="hidden" name="enable" value={(await mode).envAllows ? "false" : "true"} />
              <SubmitButton variant={(await mode).envAllows ? "danger" : "primary"}>
                {(await mode).envAllows ? "Block sending" : "Allow sending"}
              </SubmitButton>
            </ActionForm>

            <p className="text-[0.6875rem] text-[var(--color-faint)] mt-3 leading-snug m-0">
              Whether this machine may put mail on the wire at all. Takes effect immediately,
              in the worker too, with no restart. On its own it sends nothing.
            </p>
          </div>

          <div className="panel p-3" style={{ background: "var(--color-bg-2)" }}>
            <div className="t-label mb-2">Switch 2 · this campaign</div>
            <div className="flex items-center gap-2 mb-3">
              <span
                className="inline-block w-2 h-2 rounded-full"
                style={{ background: (await mode).dbAllows ? "var(--color-accent)" : "var(--color-faint)" }}
              />
              <span className="t-mono text-[0.8125rem]">
                Armed <strong>{(await mode).dbAllows ? "ON" : "OFF"}</strong>
              </span>
            </div>

            {(await mode).dbAllows ? (
              <ActionForm action={setLiveSending}>
                <input type="hidden" name="enable" value="false" />
                <SubmitButton variant="danger">Switch off</SubmitButton>
              </ActionForm>
            ) : (
              <ActionForm action={setLiveSending} className="flex flex-col gap-2">
                <input type="hidden" name="enable" value="true" />
                <Field label="Type SEND LIVE to confirm">
                  <input name="confirm" className="input" placeholder="SEND LIVE" autoComplete="off" />
                </Field>
                <div><SubmitButton variant="primary">Arm live sending</SubmitButton></div>
              </ActionForm>
            )}
          </div>

          <div
            className="panel p-3 flex flex-col justify-center"
            style={{
              background: (await mode).live ? "rgba(255,79,0,0.08)" : "var(--color-bg-2)",
              borderColor: (await mode).live ? "rgba(255,79,0,0.4)" : undefined,
            }}
          >
            <div className="t-label mb-2">Current state</div>
            <div
              className="t-display text-[1.5rem] leading-none"
              style={{ color: (await mode).live ? "var(--color-accent)" : "var(--color-muted)" }}
            >
              {(await mode).live ? "LIVE" : "NOT SENDING"}
            </div>
            <p className="text-[0.6875rem] text-[var(--color-faint)] mt-2 leading-snug m-0">{(await mode).detail}</p>
          </div>
        </div>
      </Panel>


      {/* ── Dispatch: when, as opposed to whether ──────────────────── */}
      <Panel
        title="When mail goes out"
        subtitle="The switches above decide whether sending is possible at all. This decides when it actually happens."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(300px, 100%), 1fr))" }}>
          <div className="panel p-3" style={{ background: "var(--color-bg-2)" }}>
            <div className="t-label mb-2">Mode</div>
            <DispatchModeForm mode={dispatch} time={releaseAt} action={updateDispatch} />
          </div>

          <div
            className="panel p-3 flex flex-col justify-center"
            style={{
              background: "var(--color-bg-2)",
              borderColor: dispatch === "automatic" ? "rgba(255,79,0,0.35)" : undefined,
            }}
          >
            <div className="t-label mb-2">Currently</div>
            <div
              className="t-display text-[1.5rem] leading-none mb-2"
              style={{ color: dispatch === "automatic" ? "var(--color-accent)" : "var(--color-muted)" }}
            >
              {dispatch === "automatic" ? "AUTOMATIC" : "MANUAL"}
            </div>
            <p className="text-[0.8125rem] leading-snug m-0">
              {dispatch === "manual"
                ? "Nothing goes out on its own and nothing is on a timer. The worker keeps verifying addresses and reading replies, and that is all it does."
                : `The worker sends by itself from ${releaseAt} each day, campaign time, whenever something is due.`}
            </p>
          </div>

          <div className="panel p-3 flex flex-col justify-center" style={{ background: "var(--color-bg-2)" }}>
            <div className="t-label mb-2">{dispatch === "manual" ? "Ready to send" : "Due now"}</div>
            <div className="flex items-baseline gap-2 mb-2">
              <span
                className="t-display text-[1.5rem] leading-none"
                style={{ color: readyCount ? "var(--color-accent)" : "var(--color-muted)" }}
              >
                {readyCount}
              </span>
              <span className="text-[0.75rem] text-[var(--color-muted)]">
                {readyCount === 1 ? "message" : "messages"}
              </span>
            </div>
            <p className="text-[0.6875rem] text-[var(--color-faint)] leading-snug m-0">
              You cannot send from this page, on purpose. Sending means choosing who,
              and choosing who means seeing the list. It lives on the{" "}
              <a href="/outreach/queue" className="underline">Queue</a> page.
            </p>
          </div>
        </div>
      </Panel>


      {(justConnected || connectError) && (
        <p
          className="text-[0.75rem] mt-4 mb-0 leading-snug"
          style={{ color: justConnected ? "var(--color-muted)" : "var(--color-crimson-soft)" }}
          role="status"
        >
          {justConnected ? `Mailbox connected: ${justConnected}` : connectError}
        </p>
      )}

      <div className="grid gap-4 mt-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(300px, 100%), 1fr))" }}>
        <Panel title="Mailbox">
          {gmail.connected ? (
            <>
              <div className="flex items-center gap-2 mb-2">
                <span className="inline-block w-2 h-2 rounded-full" style={{ background: "var(--color-positive)" }} />
                <span className="t-mono text-[0.8125rem]">{gmail.account}</span>
              </div>
              <p className="text-[0.75rem] text-[var(--color-muted)] leading-snug mt-0 mb-3">
                Connected with send and read-only scopes. Replies to this mailbox are what stop
                sequences, so it must be the same address the outreach goes out from.
              </p>
              <a href="/outreach/auth/start" className="btn">Reconnect</a>
            </>
          ) : (
            <>
              <div className="flex items-center gap-2 mb-2">
                <span className="inline-block w-2 h-2 rounded-full" style={{ background: "var(--color-faint)" }} />
                <span className="text-[0.8125rem] text-[var(--color-muted)]">Not connected</span>
              </div>
              <p className="text-[0.75rem] text-[var(--color-muted)] leading-snug mt-0 mb-3">
                Everything except sending and reply-reading works without it.
              </p>
              <a href="/outreach/auth/start" className="btn btn-primary">Connect mailbox</a>
              {gmail.error && (
                <p className="text-[0.6875rem] text-[var(--color-crimson-soft)] m-0">{gmail.error}</p>
              )}
            </>
          )}
        </Panel>

        <Panel title="Reading replies">
          <p className="text-[0.75rem] text-[var(--color-muted)] leading-snug mt-0 mb-3">
            When someone writes back, something has to decide what they said. That decision
            is what stops the follow-ups, so it matters more than it sounds: read a
            &quot;not interested&quot; as a yes and the person gets two more emails.
          </p>

          <div className="t-label mb-2">What it sorts replies into</div>
          <ul className="list-none m-0 mb-3 p-0 flex flex-col gap-1 text-[0.75rem]">
            <li><span className="chip chip-positive">interested</span> <span className="text-[var(--color-muted)]">stops the sequence and lands in Replies for you</span></li>
            <li><span className="chip chip-danger">not interested</span> <span className="text-[var(--color-muted)]">stops the sequence, and an explicit opt-out suppresses the address</span></li>
            <li><span className="chip chip-neutral">out of office</span> <span className="text-[var(--color-muted)]">not a reply at all, so the sequence is rescheduled rather than cancelled</span></li>
            <li><span className="chip chip-danger">bounce</span> <span className="text-[var(--color-muted)]">stops and suppresses, before it damages the sending address</span></li>
            <li><span className="chip chip-caution">not sure</span> <span className="text-[var(--color-muted)]">stops and waits for you. Guessing is the one thing it will not do</span></li>
          </ul>

          <div className="t-label mb-2">How it decides</div>
          <div className="flex items-center gap-2 mb-2">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ background: llm ? "var(--color-positive)" : "var(--color-faint)" }}
            />
            <span className="text-[0.8125rem]">
              {llm ? "Keyword rules, with Claude as a second opinion" : "Keyword rules only"}
            </span>
          </div>
          <p className="text-[0.75rem] text-[var(--color-muted)] leading-snug m-0">
            Keyword rules run first on every reply, with your own quoted pitch stripped out
            first so a two-word &quot;no thanks&quot; is not outvoted by the enthusiastic
            email underneath it.{" "}
            {llm
              ? "Replies the rules are unsure about go to Claude for a second read, and if the two disagree the reply goes to you rather than being quietly resolved."
              : "Add ANTHROPIC_API_KEY to .env.local to have Claude take a second look at the unclear ones. Without it they go straight to your review queue, which is safe, just more work for you."}{" "}
            An opt-out or a bounce is never reinterpreted by either path.
          </p>
        </Panel>
      </div>

      {/* ── One-off test send ────────────────────────────────────────── */}
      <Panel
        title="Send a test"
        subtitle="Checks that Gmail is actually wired up. One message, to one address you type, delivered immediately."
      >
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(300px, 100%), 1fr))" }}>
          <div className="panel p-3" style={{ background: "var(--color-bg-2)" }}>
            <div className="t-label mb-2">Test sending</div>
            <div className="flex items-center gap-2 mb-3">
              <span
                className="inline-block w-2 h-2 rounded-full"
                style={{ background: testOn ? "var(--color-accent)" : "var(--color-faint)" }}
              />
              <span className="text-[0.8125rem]">{testOn ? "On" : "Off"}</span>
            </div>

            <ActionForm action={setTestSending}>
              <input type="hidden" name="enable" value={testOn ? "false" : "true"} />
              <SubmitButton variant={testOn ? "danger" : "primary"}>
                {testOn ? "Switch off" : "Switch on"}
              </SubmitButton>
            </ActionForm>

            <p className="text-[0.6875rem] text-[var(--color-faint)] mt-3 leading-snug m-0">
              This bypasses both campaign switches on purpose. Those exist to stop an
              accidental blast at the real list; a single message to an address you typed
              by hand is a different risk, and arming the whole engine to check Gmail works
              would be a worse trade. Suppressed addresses are still refused, and every
              test is written to the activity log.
            </p>
          </div>

          <div>
            {gmail.connected ? (
              <ActionForm action={sendTestEmail} className="flex flex-col gap-3">
                <Field label="Send to" hint="Any address. Nothing is stored against it.">
                  <input name="to" type="email" placeholder="you@example.com" className="input" required />
                </Field>

                <div className="grid gap-3" style={{ gridTemplateColumns: "1fr 1fr" }}>
                  <Field label="Greet as" hint='Blank uses "there".'>
                    <input name="first_name" placeholder="there" className="input" />
                  </Field>
                  <Field label="Company" hint='Blank uses "your team".'>
                    <input name="company" placeholder="your team" className="input" />
                  </Field>
                </div>

                <Field label="Template">
                  <select name="template_id" className="select" required>
                    {templates.map((t) => (
                      <option key={t.id} value={t.id}>{t.name}</option>
                    ))}
                  </select>
                </Field>

                <div>
                  <SubmitButton variant="primary" disabled={!testOn}>
                    {testOn ? "Send it" : "Switch test sending on first"}
                  </SubmitButton>
                </div>
              </ActionForm>
            ) : (
              <p className="text-[0.8125rem] text-[var(--color-muted)] leading-snug">
                No mailbox connected, so there is nothing to test yet. Connect one under Mailbox.
              </p>
            )}
          </div>
        </div>
      </Panel>

      <div className="grid gap-4 mt-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(280px, 100%), 1fr))" }}>
        <Panel title="Identity">
          <div className="flex flex-col gap-4">
            <SettingRow
              settingKey={SETTING_KEYS.sendingName}
              label="From name"
              value={settings[SETTING_KEYS.sendingName] ?? ""}
            />
            <SettingRow
              settingKey={SETTING_KEYS.signature}
              label="Signature"
              hint="Inserted wherever a template uses {{signature}}."
              type="textarea"
              value={settings[SETTING_KEYS.signature] ?? ""}
            />
            <SettingRow
              settingKey={SETTING_KEYS.unsubscribeLine}
              label="Opt-out line"
              hint="Give people an easy way out. It protects the sending domain as much as the recipient."
              type="textarea"
              value={settings[SETTING_KEYS.unsubscribeLine] ?? ""}
            />
          </div>
        </Panel>

        <Panel title="Limits">
          <div className="flex flex-col gap-4">
            {/* The campaign's own caps. They live on the campaign row rather
                than in settings, so they need their own form, but with one
                campaign there is no reason to make you go looking for them. */}
            {campaign && (
              <ActionForm action={updateCampaign} className="flex flex-col gap-4">
                <input type="hidden" name="campaign_id" value={campaign.id} />
                {/* Carried through untouched: updateCampaign writes the whole
                    row, so anything omitted here would be reset. */}
                <input type="hidden" name="send_window_start" value={campaign.send_window_start} />
                <input type="hidden" name="send_window_end" value={campaign.send_window_end} />
                <input type="hidden" name="send_days" value={campaign.send_days} />
                <input type="hidden" name="timezone" value={campaign.timezone} />

                <Field
                  label="Daily cap"
                  hint="Messages a day. The only volume limit there is; ramp slowly on a new sending address."
                >
                  <input
                    name="daily_cap"
                    type="number"
                    min={1}
                    max={500}
                    defaultValue={campaign.daily_cap}
                    className="input"
                  />
                </Field>
                <Field
                  label="Per company / day"
                  hint="Four people at one company hearing from you the same morning reads as a blast."
                >
                  <input
                    name="per_domain_daily_cap"
                    type="number"
                    min={1}
                    max={20}
                    defaultValue={campaign.per_domain_daily_cap}
                    className="input"
                  />
                </Field>
                {/* This one button saves BOTH caps above it. It says so
                    because a stack of identical "Save" buttons cost a real
                    edit: a new daily cap typed up there, then the score's
                    Save pressed down here, submits nothing. */}
                <div><SubmitButton>Save both caps</SubmitButton></div>
              </ActionForm>
            )}
            <SettingRow
              settingKey={SETTING_KEYS.minVerificationScore}
              label="Minimum verification score"
              hint="Below this, a contact is never auto-enrolled. Catch-all and role mailboxes score 55."
              type="number"
              value={settings[SETTING_KEYS.minVerificationScore] ?? "60"}
            />
          </div>
        </Panel>

        <Panel title="Behaviour">
          <div className="flex flex-col gap-4">
            <SettingRow
              settingKey={SETTING_KEYS.allowRiskySends}
              label="Send to risky addresses"
              hint="Catch-all domains and role mailboxes."
              type="boolean"
              value={settings[SETTING_KEYS.allowRiskySends] ?? "false"}
            />
            <SettingRow
              settingKey={SETTING_KEYS.allowPlaceholderNames}
              label="Send without a real first name"
              hint='Off by default. "Hi CEO," reads as automated and is worse than not sending.'
              type="boolean"
              value={settings[SETTING_KEYS.allowPlaceholderNames] ?? "false"}
            />
            <SettingRow
              settingKey={SETTING_KEYS.autoStopOnNegative}
              label="Stop on a negative reply"
              type="boolean"
              value={settings[SETTING_KEYS.autoStopOnNegative] ?? "true"}
            />
            <SettingRow
              settingKey={SETTING_KEYS.autoSuppressDomainOnOptOut}
              label="Suppress the whole company on an opt-out"
              hint='For explicit requests like "take us off your list", as opposed to a simple "not interested".'
              type="boolean"
              value={settings[SETTING_KEYS.autoSuppressDomainOnOptOut] ?? "true"}
            />
          </div>
        </Panel>
      </div>
    </>
  );
}
