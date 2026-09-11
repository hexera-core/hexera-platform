/**
 * Verification orchestrator.
 *
 * Runs the checks cheapest-first and stops as soon as the verdict is settled,
 * so a malformed address never costs a DNS lookup and a dead domain never
 * costs an SMTP connection.
 *
 *   syntax → disposable → MX → (optional) SMTP RCPT → (optional) paid API
 *
 * The output is a status plus a 0-100 score plus a list of plain-English
 * reasons. The reasons are the point: "why won't this send" should be
 * answerable from the dashboard without reading code.
 */
import { all, get, run, transaction } from "../db";
import { nowIso } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import type { Contact, VerificationStatus } from "../core/types";
import { checkSyntax, domainPart, isDisposable, isFreeProvider, isRoleAccount } from "./heuristics";
import { getDomainIntel, setCatchAll } from "./dns";
import { probeMailbox } from "./smtp";
import { getVerifier } from "./providers";

export interface VerificationOutcome {
  status: VerificationStatus;
  score: number;
  syntaxOk: boolean;
  mxOk: boolean;
  mxHosts: string[];
  isRole: boolean;
  isDisposable: boolean;
  isFreeProvider: boolean;
  isCatchAll: boolean;
  smtpChecked: boolean;
  smtpCode: number | null;
  smtpMessage: string | null;
  provider: string;
  reasons: string[];
  durationMs: number;
}

/**
 * Score bands, and what they mean operationally:
 *
 *   90-100  mailbox confirmed by SMTP
 *   70-89   domain healthy, no flags — the normal "good" result without a probe
 *   50-69   deliverable but flagged (catch-all, role account, free provider)
 *   1-49    serious doubt
 *   0       do not send
 *
 * The default send threshold is 60, so catch-all and role accounts land just
 * under it and require a deliberate override.
 */
function scoreToStatus(score: number, hardInvalid: boolean): VerificationStatus {
  if (hardInvalid) return "invalid";
  if (score >= 70) return "valid";
  if (score >= 40) return "risky";
  if (score > 0) return "unknown";
  return "invalid";
}

export interface VerifyOptions {
  /** Bypass the 7-day domain_intel cache and re-query DNS. */
  forceDns?: boolean;
}

export async function verifyEmail(email: string, options: VerifyOptions = {}): Promise<VerificationOutcome> {
  const started = Date.now();
  const reasons: string[] = [];

  const base: VerificationOutcome = {
    status: "unknown",
    score: 0,
    syntaxOk: false,
    mxOk: false,
    mxHosts: [],
    isRole: false,
    isDisposable: false,
    isFreeProvider: false,
    isCatchAll: false,
    smtpChecked: false,
    smtpCode: null,
    smtpMessage: null,
    provider: "builtin",
    reasons,
    durationMs: 0,
  };

  const finish = (outcome: VerificationOutcome): VerificationOutcome => ({
    ...outcome,
    durationMs: Date.now() - started,
  });

  // ── 1. Syntax ───────────────────────────────────────────────────────────
  const syntax = checkSyntax(email);
  if (!syntax.ok) {
    reasons.push(`Invalid syntax: ${syntax.reason}`);
    return finish({ ...base, status: "invalid", score: 0 });
  }
  base.syntaxOk = true;

  const domain = domainPart(email);

  // ── 2. Disposable ───────────────────────────────────────────────────────
  if (isDisposable(domain)) {
    base.isDisposable = true;
    reasons.push("Disposable/throwaway email provider");
    return finish({ ...base, status: "invalid", score: 0 });
  }

  // ── 3. MX ───────────────────────────────────────────────────────────────
  const lookup = await getDomainIntel(domain, options.forceDns ?? false);
  const intel = lookup.intel;
  base.mxOk = intel.mx_ok === 1;
  base.mxHosts = intel.mx_hosts ? (JSON.parse(intel.mx_hosts) as string[]) : [];

  if (!lookup.definitive) {
    // The lookup failed rather than answering. Scored in the retry band, not
    // condemned — a SERVFAIL is our problem, not the prospect's.
    reasons.push(lookup.error ?? "DNS lookup was inconclusive");
    reasons.push("Re-run verification for this contact before deciding");
    return finish({ ...base, status: "unknown", score: 35 });
  }

  if (!base.mxOk) {
    reasons.push("Domain has no mail server (no MX or A record) — mail cannot be delivered");
    return finish({ ...base, status: "invalid", score: 0 });
  }
  reasons.push(`Domain accepts mail (${base.mxHosts.length} MX host${base.mxHosts.length === 1 ? "" : "s"})`);

  let score = 75;

  // ── 4. Heuristic adjustments ────────────────────────────────────────────
  if (isRoleAccount(email)) {
    base.isRole = true;
    score -= 20;
    reasons.push("Shared role mailbox rather than a named person — lower reply odds");
  }
  if (isFreeProvider(domain)) {
    base.isFreeProvider = true;
    score -= 10;
    reasons.push("Consumer mail provider rather than a company domain");
  }
  if (intel.is_catch_all === 1) {
    base.isCatchAll = true;
    score = Math.min(score, 55);
    reasons.push("Domain is catch-all — it accepts any address, so existence cannot be confirmed");
  }

  // ── 5. SMTP probe (opt-in) ──────────────────────────────────────────────
  const probeEnabled = (process.env.SMTP_PROBE_ENABLED ?? "false").toLowerCase() === "true";
  if (probeEnabled && base.mxHosts.length) {
    const probe = await probeMailbox(email, {
      mxHosts: base.mxHosts,
      from: process.env.SMTP_PROBE_FROM ?? "verify@hexera.so",
      timeoutMs: Number(process.env.SMTP_PROBE_TIMEOUT_MS ?? 8000),
      detectCatchAll: intel.is_catch_all === null,
    });

    base.smtpChecked = true;
    base.smtpCode = probe.code;
    base.smtpMessage = probe.message?.slice(0, 500) ?? null;

    if (probe.isCatchAll !== null) {
      setCatchAll(domain, probe.isCatchAll);
      if (probe.isCatchAll) {
        base.isCatchAll = true;
        score = Math.min(score, 55);
        reasons.push("Domain is catch-all — it accepts any address, so existence cannot be confirmed");
      }
    }

    if (probe.accepted === true && !base.isCatchAll) {
      score = 95;
      reasons.push(`Mailbox confirmed by the mail server (SMTP ${probe.code})`);
    } else if (probe.accepted === false) {
      reasons.push(`Mail server rejected this recipient (SMTP ${probe.code}) — the mailbox does not exist`);
      return finish({ ...base, status: "invalid", score: 0 });
    } else if (probe.inconclusiveReason) {
      reasons.push(`SMTP probe inconclusive: ${probe.inconclusiveReason}`);
    }
  } else if (!probeEnabled) {
    reasons.push("SMTP probe disabled — verdict is based on domain health, not mailbox existence");
  }

  // ── 6. Paid verifier (opt-in) ───────────────────────────────────────────
  const external = getVerifier();
  if (external) {
    try {
      const verdict = await external.verify(email);
      base.provider = external.name;
      if (verdict.isCatchAll !== null) {
        setCatchAll(domain, verdict.isCatchAll);
        base.isCatchAll = verdict.isCatchAll;
      }
      // The paid answer is authoritative when it is decisive; otherwise the
      // built-in signals stand.
      if (verdict.status === "valid") {
        score = Math.max(score, verdict.score ?? 90);
        reasons.push(`${external.name} confirmed this address as valid`);
      } else if (verdict.status === "invalid") {
        reasons.push(`${external.name} reported this address as invalid`);
        return finish({ ...base, status: "invalid", score: 0 });
      } else if (verdict.status === "risky") {
        score = Math.min(score, 55);
        reasons.push(`${external.name} flagged this address as risky`);
      }
    } catch (error) {
      reasons.push(`External verifier failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  score = Math.max(0, Math.min(100, score));
  return finish({ ...base, score, status: scoreToStatus(score, false) });
}

// ── Persistence ───────────────────────────────────────────────────────────

export async function saveVerification(contactId: number, outcome: VerificationOutcome): Promise<void> {
  await transaction(async (client) => {
    await run(
      `INSERT INTO verifications (
         contact_id, status, score, syntax_ok, mx_ok, mx_hosts, is_role, is_disposable,
         is_free_provider, is_catch_all, smtp_checked, smtp_code, smtp_message, provider,
         reasons, duration_ms, checked_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        contactId,
        outcome.status,
        outcome.score,
        outcome.syntaxOk ? 1 : 0,
        outcome.mxOk ? 1 : 0,
        JSON.stringify(outcome.mxHosts),
        outcome.isRole ? 1 : 0,
        outcome.isDisposable ? 1 : 0,
        outcome.isFreeProvider ? 1 : 0,
        outcome.isCatchAll ? 1 : 0,
        outcome.smtpChecked ? 1 : 0,
        outcome.smtpCode,
        outcome.smtpMessage,
        outcome.provider,
        JSON.stringify(outcome.reasons),
        outcome.durationMs,
        nowIso(),
      ], client);

    recordEvent({
      type: EVENT_TYPES.verificationChecked,
      entityType: "contact",
      entityId: contactId,
      contactId,
      payload: { status: outcome.status, score: outcome.score, provider: outcome.provider },
    });
  });
}

export interface VerifyBatchOptions {
  /** Re-check contacts that already have a result. */
  force?: boolean;
  limit?: number;
  /** Parallel workers. Kept low: DNS servers rate-limit, and MX results are cached per domain anyway. */
  concurrency?: number;
  onProgress?: (done: number, total: number, contact: Contact, outcome: VerificationOutcome) => void;
}

export async function verifyAllContacts(options: VerifyBatchOptions = {}): Promise<{
  checked: number;
  byStatus: Record<string, number>;
}> {
  const { force = false, limit, concurrency = 6, onProgress } = options;

  const sql = force
    ? `SELECT * FROM contacts WHERE email_normalized IS NOT NULL ORDER BY id`
    : `SELECT c.* FROM contacts c
       LEFT JOIN latest_verification v ON v.contact_id = c.id
       WHERE c.email_normalized IS NOT NULL AND v.id IS NULL
       ORDER BY c.id`;

  const contacts = await all<Contact>(limit ? `${sql} LIMIT ?` : sql, limit ? [limit] : []);
  const byStatus: Record<string, number> = {};
  let done = 0;

  // Simple worker pool: `concurrency` chasers pulling from one shared index.
  let cursor = 0;
  const workers = Array.from({ length: Math.min(concurrency, contacts.length || 1) }, async () => {
    for (;;) {
      const index = cursor++;
      if (index >= contacts.length) return;
      const contact = contacts[index];
      try {
        // A forced re-check should re-query DNS too, otherwise it just replays
        // whatever the cache already believed — including a stale mistake.
        const outcome = await verifyEmail(contact.email_normalized!, { forceDns: force });
        saveVerification(contact.id, outcome);
        byStatus[outcome.status] = (byStatus[outcome.status] ?? 0) + 1;
        done++;
        onProgress?.(done, contacts.length, contact, outcome);
      } catch (error) {
        byStatus.error = (byStatus.error ?? 0) + 1;
        done++;
        recordEvent({
          type: EVENT_TYPES.verificationFailed,
          entityType: "contact",
          entityId: contact.id,
          contactId: contact.id,
          payload: { error: error instanceof Error ? error.message : String(error) },
        });
      }
    }
  });

  await Promise.all(workers);
  return { checked: done, byStatus };
}

export async function latestVerification(contactId: number) {
  return await get(`SELECT * FROM latest_verification WHERE contact_id = ?`, [contactId]);
}
