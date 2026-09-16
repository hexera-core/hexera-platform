/**
 * Runtime settings, and the send-safety interlock.
 *
 * The interlock is the most important thing in this file. Live sending
 * requires TWO switches to agree:
 *
 *   1. DRY_RUN=false in `.env.local`     (the machine is allowed to send)
 *   2. live_sending=true in the database (this campaign is armed)
 *
 * Either one alone keeps the system in dry-run, and every change to either is
 * written to the audit log.
 *
 * Both can now be flipped from the dashboard. Switch 1 used to be file-edit
 * only, on the theory that two different mechanisms are harder to trip by
 * accident than two clicks. That theory does not survive contact with a
 * one-person operation: it mostly meant sitting in dry run and editing a file
 * in another window. The real protection is switch 2, which demands the words
 * SEND LIVE typed by hand, and that has not changed.
 *
 * Switch 1 is read from disk on every call rather than from `process.env`.
 * The dashboard and the worker are separate processes that each load the file
 * once at boot, so anything cached would mean a change in one is invisible to
 * the other until both restart. A button that says "stop sending" has to mean
 * it in the process that actually sends.
 */
import { all, get, run } from "../db";
import { readEnvFileValue, setEnvFileValue } from "./env-file";
import { nowIso } from "./time";
import { recordEvent, EVENT_TYPES } from "./events";

export interface SettingRow {
  key: string;
  value: string;
  updated_at: string;
}

export const SETTING_KEYS = {
  liveSending: "live_sending",
  sendingAddress: "sending_address",
  sendingName: "sending_name",
  signature: "signature",
  unsubscribeLine: "unsubscribe_line",
  minVerificationScore: "min_verification_score",
  allowRiskySends: "allow_risky_sends",
  allowPlaceholderNames: "allow_placeholder_names",
  autoStopOnNegative: "auto_stop_on_negative",
  autoSuppressDomainOnOptOut: "auto_suppress_domain_on_opt_out",
  replyPollMinutes: "reply_poll_minutes",
  testSending: "test_sending",
  dispatchMode: "dispatch_mode",
  dispatchTime: "dispatch_time",
} as const;

const DEFAULTS: Record<string, string> = {
  [SETTING_KEYS.liveSending]: "false",
  [SETTING_KEYS.sendingAddress]: "",
  [SETTING_KEYS.sendingName]: "Rehaan Kadhar",
  [SETTING_KEYS.signature]: "Rehaan\nHexera · hexera.so",
  [SETTING_KEYS.unsubscribeLine]:
    "If this isn't relevant, just reply \"no thanks\" and I won't follow up.",
  // Below this verification score a contact is never auto-sent to.
  [SETTING_KEYS.minVerificationScore]: "60",
  [SETTING_KEYS.allowRiskySends]: "false",
  // "Hi CEO," is worse than not sending at all.
  [SETTING_KEYS.allowPlaceholderNames]: "false",
  [SETTING_KEYS.autoStopOnNegative]: "true",
  [SETTING_KEYS.autoSuppressDomainOnOptOut]: "true",
  [SETTING_KEYS.replyPollMinutes]: "5",
  [SETTING_KEYS.testSending]: "false",
  // Manual by default. The first time live sending is armed you should be
  // watching it go out, not finding out afterwards.
  [SETTING_KEYS.dispatchMode]: "manual",
  [SETTING_KEYS.dispatchTime]: "09:00",
};

export async function getSetting(key: string): Promise<string> {
  const row = await get<SettingRow>(`SELECT * FROM settings WHERE key = ?`, [key]);
  return row?.value ?? DEFAULTS[key] ?? "";
}

export async function getBoolSetting(key: string): Promise<boolean> {
  return (await getSetting(key)).toLowerCase() === "true";
}

// ASYNC, like everything else that reads the database. It was left synchronous in the port, so
// `Number(getSetting(key))` was Number(Promise) - NaN - and the guard below quietly substituted
// the default on EVERY call. The visible effect: raising the minimum verification score on the
// Settings page changed nothing, and enrolment kept applying the built-in 60.
export async function getNumberSetting(key: string): Promise<number> {
  const parsed = Number(await getSetting(key));
  return Number.isFinite(parsed) ? parsed : Number(DEFAULTS[key] ?? 0);
}

export async function setSetting(key: string, value: string): Promise<void> {
  const previous = await getSetting(key);
  await run(
    `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
    [key, value, nowIso()],
  );
  if (previous !== value) {
    await recordEvent({
      type: EVENT_TYPES.settingChanged,
      entityType: "setting",
      // Secrets never belong in an audit payload that the dashboard renders.
      payload: { key, from: redact(key, previous), to: redact(key, value) },
    });
  }
}

function redact(key: string, value: string): string {
  return key.toLowerCase().includes("key") || key.toLowerCase().includes("token")
    ? "[redacted]"
    : value;
}

export async function allSettings(): Promise<Record<string, string>> {
  const rows = await all<SettingRow>(`SELECT * FROM settings`);
  const merged: Record<string, string> = { ...DEFAULTS };
  for (const row of rows) merged[row.key] = row.value;
  return merged;
}

export async function seedDefaultSettings(): Promise<void> {
  for (const [key, value] of Object.entries(DEFAULTS)) {
    await run(
      `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
       ON CONFLICT (key) DO NOTHING`,
      [key, value, nowIso()],
    );
  }
}

// ── The interlock ─────────────────────────────────────────────────────────

export interface SendMode {
  live: boolean;
  /** Short headline for the banner: what is happening right now. */
  headline: string;
  /** What a person would have to change to flip it. Written for someone who
   *  has never seen the code and does not know what DRY_RUN is. */
  detail: string;
  envAllows: boolean;
  dbAllows: boolean;
}

/** Switch 1. Read fresh from `.env.local`, falling back to the process
 *  environment only when the file has no opinion (a fresh clone, or a
 *  deployment that sets real environment variables instead of a file). */
export function envSendingAllowed(): boolean {
  const fromFile = readEnvFileValue("DRY_RUN");
  const raw = fromFile ?? process.env.DRY_RUN ?? "true";
  // Absent or malformed means dry-run. Failing closed is the only acceptable
  // default for something that contacts real people.
  return raw.trim().toLowerCase() === "false";
}

/** Is this running on Cloud Run, where the filesystem is ephemeral? */
export function isHosted(): boolean {
  // K_SERVICE is set by the Cloud Run runtime and by nothing else.
  return Boolean(process.env.K_SERVICE);
}

/**
 * Flip switch 1. Returns the value actually written.
 *
 * HOSTED, THIS REFUSES. Switch 1 is `DRY_RUN`, and on a laptop it lives in `.env.local` where
 * flipping it means editing a file. On Cloud Run the filesystem is ephemeral: the write would
 * succeed, the page would say sending was armed, and the next container would start dry again -
 * a toggle that lies about the one setting that decides whether strangers get email.
 *
 * So hosted it becomes a DEPLOYMENT setting, which is a stronger interlock rather than a weaker
 * one: the two switches now require genuinely different access. Arming the campaign is a database
 * write any admin can make from this page; allowing the machine to send at all takes a deploy.
 */
export async function setEnvSendingAllowed(allow: boolean): Promise<boolean> {
  if (isHosted()) {
    throw new Error(
      "DRY_RUN cannot be changed from this page in a hosted deployment - the filesystem is " +
        "ephemeral, so the change would be lost on the next container. Set DRY_RUN on the Cloud " +
        "Run service and redeploy.",
    );
  }
  const previous = await envSendingAllowed();
  setEnvFileValue("DRY_RUN", allow ? "false" : "true");
  if (previous !== allow) {
    await recordEvent({
      type: EVENT_TYPES.settingChanged,
      entityType: "setting",
      payload: { key: "DRY_RUN", from: previous ? "false" : "true", to: allow ? "false" : "true" },
    });
  }
  return allow;
}

export async function sendMode(): Promise<SendMode> {
  const envAllows = envSendingAllowed();
  const dbAllows = await getBoolSetting(SETTING_KEYS.liveSending);

  // Wording rule: lead with what is true right now, in plain words, and only
  // then name the switch. "DRY_RUN is not false" is a double negative about a
  // variable most readers have never seen — it tells you nothing useful.
  if (envAllows && dbAllows) {
    return {
      live: true,
      headline: "Live. Real email will be sent",
      detail: "Both safety switches are off. Messages in the queue will be delivered to real people.",
      envAllows,
      dbAllows,
    };
  }
  if (!envAllows && !dbAllows) {
    return {
      live: false,
      headline: "No email will be sent",
      detail:
        "Messages are written and recorded, but never delivered. Both safety switches are still on.",
      envAllows,
      dbAllows,
    };
  }
  if (!envAllows) {
    return {
      live: false,
      headline: "No email will be sent",
      detail:
        "This campaign is armed, but the machine is still blocked from sending. " +
        "Switch the machine on to finish.",
      envAllows,
      dbAllows,
    };
  }
  return {
    live: false,
    headline: "No email will be sent",
    detail:
      "The machine is allowed to send, but no campaign is armed. " +
      "Arm live sending on the Settings page to finish.",
    envAllows,
    dbAllows,
  };
}

export async function isDryRun(): Promise<boolean> {
  return !(await sendMode()).live;
}
