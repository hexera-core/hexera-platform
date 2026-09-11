// src/lib/outreach/db.ts
import { Pool } from "pg";

// src/lib/outreach/sql.ts
function toPositionalPlaceholders(sql) {
  let out = "";
  let index = 0;
  let i = 0;
  while (i < sql.length) {
    const ch = sql[i];
    const next = sql[i + 1];
    if (ch === "'" || ch === '"') {
      const quote = ch;
      let j = i + 1;
      while (j < sql.length) {
        if (sql[j] === quote && sql[j + 1] === quote) {
          j += 2;
          continue;
        }
        if (sql[j] === quote) break;
        j += 1;
      }
      out += sql.slice(i, j + 1);
      i = j + 1;
      continue;
    }
    if (ch === "-" && next === "-") {
      const end = sql.indexOf("\n", i);
      const stop = end === -1 ? sql.length : end;
      out += sql.slice(i, stop);
      i = stop;
      continue;
    }
    if (ch === "/" && next === "*") {
      const end = sql.indexOf("*/", i + 2);
      const stop = end === -1 ? sql.length : end + 2;
      out += sql.slice(i, stop);
      i = stop;
      continue;
    }
    if (ch === "?") {
      index += 1;
      out += `$${index}`;
      i += 1;
      continue;
    }
    out += ch;
    i += 1;
  }
  return { count: index, sql: out };
}
var TABLES_WITHOUT_ID = /* @__PURE__ */ new Set(["settings", "domain_intel"]);
var INSERT_TARGET = /^\s*INSERT\s+INTO\s+"?(?:outreach\.)?"?([a-z_][a-z0-9_]*)"?/i;
function insertNeedingReturningId(sql) {
  const match = INSERT_TARGET.exec(sql);
  if (!match) return false;
  if (TABLES_WITHOUT_ID.has(match[1].toLowerCase())) return false;
  return !/\bRETURNING\b/i.test(sql);
}
function withReturningId(sql) {
  return `${sql.trimEnd().replace(/;\s*$/, "")} RETURNING id`;
}

// src/lib/outreach/db.ts
function readOutreachDbConfig(env) {
  const required = (name) => {
    const value = env[name]?.trim();
    if (!value) {
      throw new Error(
        `${name} is not set on the admin console, so the outreach pages have no database to read.`
      );
    }
    return value;
  };
  return {
    database: env.OUTREACH_DB_NAME?.trim() || "hexera",
    host: required("OUTREACH_DB_HOST"),
    password: required("OUTREACH_DB_PASSWORD"),
    port: Number(env.OUTREACH_DB_PORT?.trim() || 5432),
    user: required("OUTREACH_DB_USER")
  };
}
function poolConfigFrom(config) {
  return {
    ...config,
    // EVERY connection resolves the outreach tables by their unprefixed names. Issued as a
    // connection option rather than a statement so it cannot be missed on a connection the pool
    // opens later.
    options: "-c search_path=outreach,public",
    // A Cloud Run container is small and the database is shared; a large pool here is a way to
    // exhaust Postgres's connection limit from a page nobody is looking at.
    max: Number(process.env.OUTREACH_DB_POOL_MAX ?? 5)
  };
}
var pool = null;
function getPool() {
  if (pool) return pool;
  const key = Symbol.for("hexera.outreach.pool");
  const cached = globalThis[key];
  if (cached) {
    pool = cached;
    return pool;
  }
  pool = new Pool(poolConfigFrom(readOutreachDbConfig(process.env)));
  globalThis[key] = pool;
  return pool;
}
function prepare(sql, params) {
  return { text: toPositionalPlaceholders(sql).sql, values: [...params] };
}
async function all(sql, params = [], on = getPool()) {
  const result = await on.query(prepare(sql, params));
  return result.rows;
}
async function get(sql, params = [], on = getPool()) {
  const result = await on.query(prepare(sql, params));
  return result.rows[0] ?? void 0;
}
async function run(sql, params = [], on = getPool()) {
  const needsId = insertNeedingReturningId(sql);
  const text = toPositionalPlaceholders(needsId ? withReturningId(sql) : sql).sql;
  const result = await on.query({ text, values: [...params] });
  const first = result.rows[0];
  return {
    changes: result.rowCount ?? 0,
    lastInsertRowid: needsId && first?.id !== void 0 ? Number(first.id) : 0
  };
}
async function transaction(fn) {
  const client2 = await getPool().connect();
  try {
    await client2.query("BEGIN");
    const result = await fn(client2);
    await client2.query("COMMIT");
    return result;
  } catch (error) {
    try {
      await client2.query("ROLLBACK");
    } catch {
    }
    throw error;
  } finally {
    client2.release();
  }
}
async function closeDb() {
  if (!pool) return;
  await pool.end();
  pool = null;
  delete globalThis[Symbol.for("hexera.outreach.pool")];
}

// src/lib/outreach/core/time.ts
function nowIso() {
  return (/* @__PURE__ */ new Date()).toISOString();
}
var WEEKDAY_TO_ISO = {
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
  Sun: 7
};
var formatterCache = /* @__PURE__ */ new Map();
function formatter(timeZone) {
  let cached = formatterCache.get(timeZone);
  if (!cached) {
    cached = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      weekday: "short"
    });
    formatterCache.set(timeZone, cached);
  }
  return cached;
}
function zonedParts(date, timeZone) {
  const parts = formatter(timeZone).formatToParts(date);
  const pick = (type) => parts.find((p) => p.type === type)?.value ?? "0";
  const hour = Number(pick("hour")) % 24;
  return {
    year: Number(pick("year")),
    month: Number(pick("month")),
    day: Number(pick("day")),
    hour,
    minute: Number(pick("minute")),
    second: Number(pick("second")),
    weekday: WEEKDAY_TO_ISO[pick("weekday")] ?? 1
  };
}
function dayKey(date, timeZone) {
  const p = zonedParts(date, timeZone);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}
function zonedTimeToUtc(year, month, day, hour, minute, timeZone) {
  let guess = Date.UTC(year, month - 1, day, hour, minute, 0, 0);
  for (let pass = 0; pass < 2; pass++) {
    const seen = zonedParts(new Date(guess), timeZone);
    const seenAsUtc = Date.UTC(seen.year, seen.month - 1, seen.day, seen.hour, seen.minute, 0, 0);
    const drift = seenAsUtc - guess;
    if (drift === 0) break;
    guess -= drift;
  }
  return new Date(guess);
}
function parseSendDays(spec) {
  const days = spec.split(",").map((s) => Number(s.trim())).filter((n) => Number.isInteger(n) && n >= 1 && n <= 7);
  return days.length ? new Set(days) : /* @__PURE__ */ new Set([1, 2, 3, 4, 5]);
}
function isWithinSendWindow(date, window) {
  const p = zonedParts(date, window.timezone);
  if (!parseSendDays(window.send_days).has(p.weekday)) return false;
  return p.hour >= window.send_window_start && p.hour < window.send_window_end;
}
function nextWindowSlot(from, window, jitterMinutes = 0) {
  const days = parseSendDays(window.send_days);
  let cursor = new Date(from.getTime());
  for (let attempt = 0; attempt < 400; attempt++) {
    const p = zonedParts(cursor, window.timezone);
    if (!days.has(p.weekday)) {
      cursor = startOfNextZonedDay(cursor, window.timezone);
      continue;
    }
    if (p.hour < window.send_window_start) {
      cursor = zonedTimeToUtc(p.year, p.month, p.day, window.send_window_start, 0, window.timezone);
    } else if (p.hour >= window.send_window_end) {
      cursor = startOfNextZonedDay(cursor, window.timezone);
      continue;
    }
    if (jitterMinutes > 0) {
      const jittered = new Date(cursor.getTime() + Math.floor(Math.random() * jitterMinutes) * 6e4);
      if (isWithinSendWindow(jittered, window)) return jittered;
    }
    return cursor;
  }
  throw new Error(
    `Could not find a send slot within 400 days for timezone=${window.timezone} days=${window.send_days} window=${window.send_window_start}-${window.send_window_end}`
  );
}
function startOfNextZonedDay(date, timeZone) {
  const p = zonedParts(date, timeZone);
  const localMidnight = zonedTimeToUtc(p.year, p.month, p.day, 0, 0, timeZone);
  const nextish = new Date(localMidnight.getTime() + 25 * 60 * 60 * 1e3);
  const q = zonedParts(nextish, timeZone);
  return zonedTimeToUtc(q.year, q.month, q.day, 0, 0, timeZone);
}
function addSendingDays(from, count, window, jitterMinutes = 0) {
  const days = parseSendDays(window.send_days);
  let cursor = from;
  let remaining = count;
  while (remaining > 0) {
    cursor = startOfNextZonedDay(cursor, window.timezone);
    if (days.has(zonedParts(cursor, window.timezone).weekday)) remaining--;
  }
  return nextWindowSlot(cursor, window, jitterMinutes);
}

// src/lib/outreach/core/events.ts
var EVENT_TYPES = {
  contactImported: "contact.imported",
  contactUpdated: "contact.updated",
  verificationChecked: "verification.checked",
  verificationFailed: "verification.failed",
  enrollmentCreated: "enrollment.created",
  enrollmentStatusChanged: "enrollment.status_changed",
  enrollmentStepAdvanced: "enrollment.step_advanced",
  enrollmentCompleted: "enrollment.completed",
  messageQueued: "message.queued",
  messageSent: "message.sent",
  messageDryRun: "message.dry_run",
  messageFailed: "message.failed",
  messageSkipped: "message.skipped",
  replyReceived: "reply.received",
  replyClassified: "reply.classified",
  replyReviewed: "reply.reviewed",
  suppressionAdded: "suppression.added",
  suppressionRemoved: "suppression.removed",
  campaignStatusChanged: "campaign.status_changed",
  settingChanged: "setting.changed",
  dispatchRun: "dispatch.run",
  workerTick: "worker.tick",
  workerError: "worker.error"
};
async function recordEvent(input) {
  const result = await run(
    `INSERT INTO events (type, entity_type, entity_id, campaign_id, contact_id, payload, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [
      input.type,
      input.entityType ?? null,
      input.entityId ?? null,
      input.campaignId ?? null,
      input.contactId ?? null,
      input.payload === void 0 ? null : JSON.stringify(input.payload),
      nowIso()
    ]
  );
  return result.lastInsertRowid;
}

// src/lib/outreach/core/env-file.ts
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
var ENV_PATH = () => resolve(process.cwd(), ".env.local");
function readEnvFileValue(key) {
  const path = ENV_PATH();
  if (!existsSync(path)) return null;
  let found = null;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    if (trimmed.slice(0, eq).trim() !== key) continue;
    let value = trimmed.slice(eq + 1).trim();
    if (value.startsWith('"') && value.endsWith('"') || value.startsWith("'") && value.endsWith("'")) {
      value = value.slice(1, -1);
    }
    found = value;
  }
  return found;
}

// src/lib/outreach/core/settings.ts
var SETTING_KEYS = {
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
  dispatchTime: "dispatch_time"
};
var DEFAULTS = {
  [SETTING_KEYS.liveSending]: "false",
  [SETTING_KEYS.sendingAddress]: "",
  [SETTING_KEYS.sendingName]: "Rehaan Kadhar",
  [SETTING_KEYS.signature]: "Rehaan\nHexera \xB7 hexera.so",
  [SETTING_KEYS.unsubscribeLine]: `If this isn't relevant, just reply "no thanks" and I won't follow up.`,
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
  [SETTING_KEYS.dispatchTime]: "09:00"
};
async function getSetting(key) {
  const row = await get(`SELECT * FROM settings WHERE key = ?`, [key]);
  return row?.value ?? DEFAULTS[key] ?? "";
}
async function getBoolSetting(key) {
  return (await getSetting(key)).toLowerCase() === "true";
}
async function seedDefaultSettings() {
  for (const [key, value] of Object.entries(DEFAULTS)) {
    await run(
      `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
       ON CONFLICT (key) DO NOTHING`,
      [key, value, nowIso()]
    );
  }
}
function envSendingAllowed() {
  const fromFile = readEnvFileValue("DRY_RUN");
  const raw = fromFile ?? process.env.DRY_RUN ?? "true";
  return raw.trim().toLowerCase() === "false";
}
async function sendMode() {
  const envAllows = envSendingAllowed();
  const dbAllows = await getBoolSetting(SETTING_KEYS.liveSending);
  if (envAllows && dbAllows) {
    return {
      live: true,
      headline: "Live. Real email will be sent",
      detail: "Both safety switches are off. Messages in the queue will be delivered to real people.",
      envAllows,
      dbAllows
    };
  }
  if (!envAllows && !dbAllows) {
    return {
      live: false,
      headline: "No email will be sent",
      detail: "Messages are written and recorded, but never delivered. Both safety switches are still on.",
      envAllows,
      dbAllows
    };
  }
  if (!envAllows) {
    return {
      live: false,
      headline: "No email will be sent",
      detail: "This campaign is armed, but the machine is still blocked from sending. Switch the machine on to finish.",
      envAllows,
      dbAllows
    };
  }
  return {
    live: false,
    headline: "No email will be sent",
    detail: "The machine is allowed to send, but no campaign is armed. Arm live sending on the Settings page to finish.",
    envAllows,
    dbAllows
  };
}
async function isDryRun() {
  return !(await sendMode()).live;
}

// src/lib/outreach/engine/scheduler.ts
async function enrollmentsByIds(ids) {
  if (!ids.length) return [];
  const placeholders = ids.map(() => "?").join(", ");
  return hydrate(
    await all(
      `SELECT ${ENROLLMENT_COLUMNS}
       FROM enrollments e
       JOIN campaigns c ON c.id = e.campaign_id
       WHERE e.id IN (${placeholders})
         AND e.status IN ('pending', 'active')
         AND c.status <> 'archived'`,
      ids
    )
  );
}
var ENROLLMENT_COLUMNS = `
  e.id AS e_id, e.campaign_id, e.contact_id, e.status AS e_status, e.current_step,
  e.next_send_at, e.thread_id, e.last_message_id, e.stopped_reason, e.enrolled_at,
  e.updated_at AS e_updated_at`;
async function hydrate(rows) {
  const items = [];
  for (const row of rows) {
    const campaign = await get(`SELECT * FROM campaigns WHERE id = ?`, [row.campaign_id]);
    const contact = await get(`SELECT * FROM contacts WHERE id = ?`, [row.contact_id]);
    if (!campaign || !contact) continue;
    items.push({
      campaign,
      contact,
      enrollment: {
        id: row.e_id,
        campaign_id: row.campaign_id,
        contact_id: row.contact_id,
        status: row.e_status,
        current_step: row.current_step,
        next_send_at: row.next_send_at,
        thread_id: row.thread_id,
        last_message_id: row.last_message_id,
        stopped_reason: row.stopped_reason,
        enrolled_at: row.enrolled_at,
        updated_at: row.e_updated_at
      }
    });
  }
  return items;
}
async function dueEnrollments(now = /* @__PURE__ */ new Date(), limit = 200) {
  const rows = all(
    `SELECT
       e.id AS e_id, e.campaign_id, e.contact_id, e.status AS e_status, e.current_step,
       e.next_send_at, e.thread_id, e.last_message_id, e.stopped_reason, e.enrolled_at,
       e.updated_at AS e_updated_at
     FROM enrollments e
     JOIN campaigns c ON c.id = e.campaign_id
     WHERE e.status IN ('pending', 'active')
       AND e.next_send_at IS NOT NULL
       AND e.next_send_at <= ?
       AND c.status <> 'archived'
     ORDER BY e.next_send_at ASC
     LIMIT ?`,
    [now.toISOString(), limit]
  );
  return hydrate(await rows);
}
async function countsFor(day, campaignId, domain) {
  const campaign = (await get(
    `SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ? AND campaign_id = ?`,
    [day, campaignId]
  ))?.n ?? 0;
  const domainCount = (await get(
    `SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ? AND domain = ?`,
    [day, domain]
  ))?.n ?? 0;
  return { campaign, domain: domainCount };
}
async function checkCapacity(campaign, domain, now = /* @__PURE__ */ new Date(), options = {}) {
  const day = dayKey(now, campaign.timezone);
  const counts = await countsFor(day, campaign.id, domain);
  const limits = {
    campaign: campaign.daily_cap,
    domain: campaign.per_domain_daily_cap
  };
  if (!options.ignoreWindow && !isWithinSendWindow(now, campaign)) {
    return { allowed: false, reason: "outside the campaign's send window", counts, limits };
  }
  if (counts.campaign >= limits.campaign) {
    return {
      allowed: false,
      reason: `campaign daily cap reached (${counts.campaign}/${limits.campaign})`,
      counts,
      limits
    };
  }
  if (counts.domain >= limits.domain) {
    return {
      allowed: false,
      reason: `already contacted ${counts.domain} person(s) at ${domain} today (limit ${limits.domain})`,
      counts,
      limits
    };
  }
  return { allowed: true, reason: null, counts, limits };
}
async function recordSend(campaign, domain, now = /* @__PURE__ */ new Date()) {
  const day = dayKey(now, campaign.timezone);
  await run(
    `INSERT INTO send_ledger (day, campaign_id, domain, count) VALUES (?, ?, ?, 1)
     ON CONFLICT (day, campaign_id, domain) DO UPDATE SET count = count + 1`,
    [day, campaign.id, domain]
  );
}
async function capacitySnapshot(now = /* @__PURE__ */ new Date()) {
  const campaigns = await all(`SELECT * FROM campaigns WHERE status <> 'archived'`);
  const day = dayKey(now, campaigns[0]?.timezone ?? "UTC");
  return {
    day,
    globalUsed: (await get(`SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ?`, [day]))?.n ?? 0,
    // Promise.all, because the per-campaign counter is a query: a plain .map would build an array
    // of promises and the caller would render "[object Promise] of 40".
    perCampaign: await Promise.all(campaigns.map(async (campaign) => ({
      campaignId: campaign.id,
      name: campaign.name,
      used: (await get(
        `SELECT COALESCE(SUM(count), 0) n FROM send_ledger WHERE day = ? AND campaign_id = ?`,
        [dayKey(now, campaign.timezone), campaign.id]
      ))?.n ?? 0,
      limit: campaign.daily_cap
    })))
  };
}

// src/lib/outreach/core/suppression.ts
async function checkSuppressed(email) {
  if (!email) return { suppressed: false, scope: null, reason: null, matchedValue: null };
  const normalized = email.trim().toLowerCase();
  const domain = normalized.slice(normalized.lastIndexOf("@") + 1);
  const match = await get(
    `SELECT * FROM suppressions
     WHERE (scope = 'email' AND value = ?) OR (scope = 'domain' AND value = ?)
     ORDER BY CASE scope WHEN 'email' THEN 0 ELSE 1 END
     LIMIT 1`,
    [normalized, domain]
  );
  if (!match) return { suppressed: false, scope: null, reason: null, matchedValue: null };
  return {
    suppressed: true,
    scope: match.scope,
    reason: match.reason,
    matchedValue: match.value
  };
}
async function suppress(options) {
  const value = options.value.trim().toLowerCase();
  if (!value) return false;
  const result = await run(
    `INSERT INTO suppressions (scope, value, reason, source, contact_id, created_at)
     VALUES (?, ?, ?, ?, ?, ?)
     ON CONFLICT (scope, value) DO NOTHING`,
    [options.scope, value, options.reason, options.source, options.contactId ?? null, nowIso()]
  );
  if (result.changes > 0) {
    recordEvent({
      type: EVENT_TYPES.suppressionAdded,
      entityType: "suppression",
      contactId: options.contactId ?? null,
      payload: { scope: options.scope, value, reason: options.reason, source: options.source }
    });
    return true;
  }
  return false;
}
function domainOf(email) {
  return email.slice(email.lastIndexOf("@") + 1).toLowerCase();
}

// src/lib/outreach/mail/render.ts
var RenderError = class extends Error {
  constructor(message, missingTags) {
    super(message);
    this.missingTags = missingTags;
    this.name = "RenderError";
  }
};
var TAG_PATTERN = /\{\{\s*([a-z0-9_]+)\s*(?:\|([^}]*))?\}\}/gi;
function buildMergeValues(contact, extra = {}) {
  return {
    first_name: contact.first_name,
    full_name: contact.full_name,
    company: contact.company,
    role: contact.role,
    industry: contact.industry,
    tier: contact.tier,
    stage: contact.stage,
    email: contact.email_normalized,
    domain: contact.domain,
    priority: contact.priority,
    ...extra
  };
}
function render(template, values) {
  const missing = [];
  const usedTags = [];
  const usedDefaults = [];
  const text = template.replace(TAG_PATTERN, (_whole, rawName, fallback) => {
    const name = rawName.toLowerCase();
    const value = values[name];
    const present = value !== null && value !== void 0 && String(value).trim() !== "";
    if (present) {
      usedTags.push(name);
      return String(value).trim();
    }
    if (fallback !== void 0) {
      usedDefaults.push(name);
      return fallback.trim();
    }
    missing.push(name);
    return "";
  });
  if (missing.length) {
    const unique = [...new Set(missing)];
    throw new RenderError(
      `Template has unresolved merge tag(s): ${unique.map((t) => `{{${t}}}`).join(", ")}. Give the contact a value, or add a fallback like {{${unique[0]}|there}}.`,
      unique
    );
  }
  return { text, usedTags: [...new Set(usedTags)], usedDefaults: [...new Set(usedDefaults)] };
}
function renderEmail(subject, body, values) {
  const renderedSubject = render(subject, values);
  const renderedBody = render(body, values);
  return {
    subject: collapseWhitespace(renderedSubject.text),
    body: renderedBody.text,
    usedDefaults: [.../* @__PURE__ */ new Set([...renderedSubject.usedDefaults, ...renderedBody.usedDefaults])]
  };
}
function collapseWhitespace(value) {
  return value.replace(/\s+/g, " ").trim();
}

// src/lib/outreach/mail/gmail.ts
import { google } from "googleapis";

// src/lib/outreach/crypto.ts
import { KeyManagementServiceClient } from "@google-cloud/kms";
var PREFIX = "kms:v1:";
var client = null;
function getKmsClient() {
  client ??= new KeyManagementServiceClient();
  return client;
}
function kmsKeyName(env) {
  return env.OUTREACH_KMS_KEY?.trim() || null;
}
async function encryptSecret(plaintext, keyName, cipher = getKmsClient()) {
  if (plaintext === null || plaintext === void 0 || plaintext === "") return null;
  if (!keyName) {
    throw new Error(
      "OUTREACH_KMS_KEY is not set, so a mailbox refresh token cannot be encrypted. Refusing to write it in the clear."
    );
  }
  if (plaintext.startsWith(PREFIX)) return plaintext;
  const [result] = await cipher.encrypt({ name: keyName, plaintext: Buffer.from(plaintext, "utf8") });
  if (!result.ciphertext) throw new Error("KMS returned no ciphertext for a token it was asked to encrypt.");
  return PREFIX + Buffer.from(result.ciphertext).toString("base64");
}
async function decryptSecret(stored, keyName, cipher = getKmsClient()) {
  if (stored === null || stored === void 0 || stored === "") return null;
  if (!stored.startsWith(PREFIX)) return stored;
  if (!keyName) {
    throw new Error("OUTREACH_KMS_KEY is not set, so an encrypted token cannot be read back.");
  }
  const [result] = await cipher.decrypt({
    ciphertext: Buffer.from(stored.slice(PREFIX.length), "base64"),
    name: keyName
  });
  if (!result.plaintext) throw new Error("KMS returned no plaintext for a token it was asked to decrypt.");
  return Buffer.from(result.plaintext).toString("utf8");
}

// src/lib/outreach/mail/mime.ts
import { randomBytes } from "node:crypto";
function encodeHeaderValue(value) {
  if (/^[\x20-\x7E]*$/.test(value)) return value;
  return `=?UTF-8?B?${Buffer.from(value, "utf8").toString("base64")}?=`;
}
function formatAddress(email, name) {
  if (!name) return email;
  const encoded = encodeHeaderValue(name);
  const safe = encoded === name ? `"${name.replace(/["\\]/g, "")}"` : encoded;
  return `${safe} <${email}>`;
}
function textToHtml(text) {
  const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const paragraphs = escaped.split(/\r?\n\s*\r?\n/).map((block) => block.trim()).filter(Boolean).map((block) => `<p>${block.replace(/\r?\n/g, "<br>")}</p>`);
  return `<html><body>\r
${paragraphs.join("\r\n")}\r
</body></html>`;
}
function base64Body(value) {
  return Buffer.from(value.replace(/\r?\n/g, "\r\n"), "utf8").toString("base64").replace(/(.{76})/g, "$1\r\n");
}
function generateMessageId(domain) {
  return `<${Date.now().toString(36)}.${randomBytes(12).toString("hex")}@${domain}>`;
}
function foldHeader(name, value) {
  const full = `${name}: ${value}`;
  if (full.length <= 78) return full;
  const parts = value.split(" ").filter(Boolean);
  const lines = [];
  let current = `${name}:`;
  for (const part of parts) {
    if (current.length + part.length + 1 > 78 && current !== `${name}:`) {
      lines.push(current);
      current = ` ${part}`;
    } else {
      current += ` ${part}`;
    }
  }
  lines.push(current);
  return lines.join("\r\n");
}
function buildMessage(parts) {
  const boundary = `--=_hexera_${randomBytes(16).toString("hex")}`;
  const fromDomain = parts.from.split("@")[1] ?? "localhost";
  const messageId = generateMessageId(fromDomain);
  const headers = [
    foldHeader("From", formatAddress(parts.from, parts.fromName)),
    foldHeader("To", formatAddress(parts.to, parts.toName)),
    foldHeader("Subject", encodeHeaderValue(parts.subject)),
    `Message-ID: ${messageId}`,
    `Date: ${(/* @__PURE__ */ new Date()).toUTCString()}`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`
  ];
  if (parts.replyTo) headers.push(foldHeader("Reply-To", formatAddress(parts.replyTo)));
  if (parts.inReplyTo) headers.push(`In-Reply-To: ${parts.inReplyTo}`);
  if (parts.references?.length) {
    headers.push(foldHeader("References", parts.references.join(" ")));
  }
  const body = [
    `--${boundary}`,
    'Content-Type: text/plain; charset="UTF-8"',
    "Content-Transfer-Encoding: base64",
    "",
    base64Body(parts.body),
    `--${boundary}`,
    'Content-Type: text/html; charset="UTF-8"',
    "Content-Transfer-Encoding: base64",
    "",
    base64Body(textToHtml(parts.body)),
    `--${boundary}--`,
    ""
  ].join("\r\n");
  const raw = `${headers.join("\r\n")}\r
\r
${body}`;
  return {
    raw,
    encoded: Buffer.from(raw, "utf8").toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
    messageId
  };
}
function headerValue(headers, name) {
  const found = headers.find((h) => h.name?.toLowerCase() === name.toLowerCase());
  return found?.value ?? null;
}
function parseAddress(value) {
  if (!value) return { email: "", name: null };
  const angle = /<([^>]+)>/.exec(value);
  if (angle) {
    const name = value.slice(0, angle.index).trim().replace(/^"|"$/g, "");
    return { email: angle[1].trim().toLowerCase(), name: name || null };
  }
  return { email: value.trim().toLowerCase(), name: null };
}
function detectAutoSubmitted(headers) {
  const autoSubmitted = headerValue(headers, "Auto-Submitted");
  if (autoSubmitted && autoSubmitted.toLowerCase() !== "no") {
    return { isAuto: true, reason: `Auto-Submitted: ${autoSubmitted}` };
  }
  const msAuto = headerValue(headers, "X-Auto-Response-Suppress");
  if (msAuto) return { isAuto: true, reason: "X-Auto-Response-Suppress present" };
  const precedence = headerValue(headers, "Precedence");
  if (precedence && ["bulk", "auto_reply", "junk", "list"].includes(precedence.toLowerCase())) {
    return { isAuto: true, reason: `Precedence: ${precedence}` };
  }
  if (headerValue(headers, "X-Autoreply") || headerValue(headers, "X-Autorespond")) {
    return { isAuto: true, reason: "X-Autoreply header present" };
  }
  if (headerValue(headers, "List-Unsubscribe") && headerValue(headers, "List-Id")) {
    return { isAuto: true, reason: "mailing list message" };
  }
  return { isAuto: false, reason: null };
}
function parseInboundHeaders(headers) {
  const from = parseAddress(headerValue(headers, "From"));
  const to = parseAddress(headerValue(headers, "To"));
  const auto = detectAutoSubmitted(headers);
  const references = (headerValue(headers, "References") ?? "").split(/\s+/).filter((r) => r.startsWith("<"));
  return {
    from: from.email,
    fromName: from.name,
    to: to.email,
    subject: headerValue(headers, "Subject") ?? "",
    date: headerValue(headers, "Date"),
    messageId: headerValue(headers, "Message-ID"),
    inReplyTo: headerValue(headers, "In-Reply-To"),
    references,
    isAutoSubmitted: auto.isAuto,
    autoSubmittedReason: auto.reason
  };
}
function stripQuotedText(body) {
  const lines = body.split(/\r?\n/);
  const kept = [];
  const boundaries = [
    /^\s*On .+ wrote:\s*$/i,
    /^\s*On .+,.+ at .+ wrote:/i,
    /^-{2,}\s*Original Message\s*-{2,}/i,
    /^_{5,}\s*$/,
    /^-{5,}\s*$/,
    /^\s*From:\s.+/i,
    /^\s*Sent from my /i,
    /^\s*Get Outlook for /i
  ];
  for (const line of lines) {
    if (boundaries.some((pattern) => pattern.test(line))) break;
    if (/^\s*>/.test(line)) continue;
    kept.push(line);
  }
  return kept.join("\n").trim();
}

// src/lib/outreach/mail/gmail.ts
var GmailNotConnectedError = class extends Error {
  constructor(message = "No Gmail account is connected. Run: npm run gmail:auth") {
    super(message);
    this.name = "GmailNotConnectedError";
  }
};
function oauthClient() {
  const clientId = process.env.GOOGLE_CLIENT_ID;
  const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
  const redirectUri = process.env.GOOGLE_REDIRECT_URI ?? "http://localhost:5789/oauth2callback";
  if (!clientId || !clientSecret) {
    throw new GmailNotConnectedError(
      "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set. See README \u2192 Connecting Gmail."
    );
  }
  return new google.auth.OAuth2(clientId, clientSecret, redirectUri);
}
async function storedToken(accountEmail) {
  const row = accountEmail ? await get(`SELECT * FROM oauth_tokens WHERE account_email = ?`, [accountEmail]) : await get(`SELECT * FROM oauth_tokens ORDER BY updated_at DESC LIMIT 1`);
  if (!row) return void 0;
  const key = kmsKeyName(process.env);
  return {
    ...row,
    access_token: await decryptSecret(row.access_token, key),
    refresh_token: await decryptSecret(row.refresh_token, key)
  };
}
async function saveToken(accountEmail, tokens) {
  const now = nowIso();
  await run(
    `INSERT INTO oauth_tokens (account_email, access_token, refresh_token, scope, token_type, expiry_date, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (account_email) DO UPDATE SET
       access_token = excluded.access_token,
       -- Google only returns a refresh_token on the first consent. Later
       -- refreshes omit it, and overwriting with NULL would silently break
       -- unattended operation days later.
       refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
       scope = excluded.scope,
       token_type = excluded.token_type,
       expiry_date = excluded.expiry_date,
       updated_at = excluded.updated_at`,
    [
      accountEmail,
      await encryptSecret(tokens.access_token ?? null, kmsKeyName(process.env)),
      await encryptSecret(tokens.refresh_token ?? null, kmsKeyName(process.env)),
      tokens.scope ?? null,
      tokens.token_type ?? null,
      tokens.expiry_date ?? null,
      now,
      now
    ]
  );
}
async function authorizedClient(accountEmail) {
  const token = await storedToken(accountEmail);
  if (!token?.refresh_token) throw new GmailNotConnectedError();
  const client2 = oauthClient();
  client2.setCredentials({
    access_token: token.access_token ?? void 0,
    refresh_token: token.refresh_token,
    expiry_date: token.expiry_date ?? void 0,
    token_type: token.token_type ?? void 0,
    scope: token.scope ?? void 0
  });
  client2.on("tokens", (fresh) => {
    saveToken(token.account_email, fresh);
  });
  return { client: client2, account: token };
}
async function gmailApi(accountEmail) {
  const { client: client2, account } = await authorizedClient(accountEmail);
  return { api: google.gmail({ version: "v1", auth: client2 }), account };
}
async function sendEmail(parts, options = {}) {
  const { api } = await gmailApi(options.accountEmail);
  const built = buildMessage(parts);
  const response = await api.users.messages.send({
    userId: "me",
    requestBody: {
      raw: built.encoded,
      ...options.threadId ? { threadId: options.threadId } : {}
    }
  });
  const data = response.data;
  if (!data.id || !data.threadId) {
    throw new Error("Gmail accepted the message but returned no id \u2014 cannot track this send");
  }
  return {
    gmailMessageId: data.id,
    gmailThreadId: data.threadId,
    rfc822MessageId: built.messageId,
    raw: built.raw
  };
}
function extractBody(payload) {
  if (!payload) return "";
  const decode = (data) => data ? Buffer.from(data.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8") : "";
  if (payload.mimeType === "text/plain" && payload.body?.data) return decode(payload.body.data);
  if (payload.parts?.length) {
    const plain = payload.parts.find((p) => p.mimeType === "text/plain" && p.body?.data);
    if (plain) return decode(plain.body.data);
    for (const part of payload.parts) {
      if (part.parts?.length) {
        const nested = extractBody(part);
        if (nested) return nested;
      }
    }
    const html = payload.parts.find((p) => p.mimeType === "text/html" && p.body?.data);
    if (html) return htmlToText(decode(html.body.data));
  }
  if (payload.body?.data) {
    const decoded = decode(payload.body.data);
    return payload.mimeType === "text/html" ? htmlToText(decoded) : decoded;
  }
  return "";
}
function htmlToText(html) {
  return html.replace(/<style[\s\S]*?<\/style>/gi, "").replace(/<script[\s\S]*?<\/script>/gi, "").replace(/<br\s*\/?>/gi, "\n").replace(/<\/p>/gi, "\n\n").replace(/<[^>]+>/g, "").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\n{3,}/g, "\n\n").trim();
}
async function fetchMessage(messageId, accountEmail) {
  const { api } = await gmailApi(accountEmail);
  const response = await api.users.messages.get({ userId: "me", id: messageId, format: "full" });
  const data = response.data;
  return {
    id: data.id,
    threadId: data.threadId,
    labelIds: data.labelIds ?? [],
    snippet: data.snippet ?? "",
    internalDate: data.internalDate ?? null,
    headers: data.payload?.headers ?? [],
    body: extractBody(data.payload)
  };
}
async function listInboundMessages(options = {}) {
  const { api } = await gmailApi(options.accountEmail);
  const ids = [];
  let pageToken;
  do {
    const response = await api.users.messages.list({
      userId: "me",
      q: options.query ?? "in:inbox newer_than:30d",
      maxResults: Math.min(options.maxResults ?? 100, 500),
      pageToken
    });
    for (const message of response.data.messages ?? []) {
      if (message.id) ids.push(message.id);
    }
    pageToken = response.data.nextPageToken ?? void 0;
  } while (pageToken && ids.length < (options.maxResults ?? 100));
  return ids;
}
async function starMessage(messageId, accountEmail) {
  const { api } = await gmailApi(accountEmail);
  await api.users.messages.modify({
    userId: "me",
    id: messageId,
    requestBody: { addLabelIds: ["STARRED"] }
  });
}

// src/lib/outreach/engine/state.ts
var ALLOWED = {
  pending: ["active", "suppressed", "stopped", "review", "bounced", "replied"],
  active: ["active", "completed", "replied", "bounced", "stopped", "review", "suppressed"],
  // Terminal states. `review` is the one that can be re-opened, and only by a
  // human acting through the dashboard.
  completed: ["replied", "review"],
  replied: ["review", "stopped"],
  bounced: [],
  stopped: ["review"],
  review: ["active", "stopped", "replied", "completed", "suppressed"],
  suppressed: ["review"]
};
var IllegalTransitionError = class extends Error {
  constructor(from, to) {
    super(`Illegal enrollment transition: ${from} \u2192 ${to}`);
    this.name = "IllegalTransitionError";
  }
};
function canTransition(from, to) {
  return ALLOWED[from]?.includes(to) ?? false;
}
async function transition(enrollmentId, to, options = {}) {
  return await transaction(async (client2) => {
    const current = await get(`SELECT * FROM enrollments WHERE id = ?`, [enrollmentId], client2);
    if (!current) throw new Error(`Enrollment ${enrollmentId} not found`);
    if (current.status !== to && !canTransition(current.status, to)) {
      throw new IllegalTransitionError(current.status, to);
    }
    const terminal = to !== "pending" && to !== "active";
    const nextSendAt = terminal ? null : options.nextSendAt ?? current.next_send_at;
    await run(
      `UPDATE enrollments SET
         status = ?, next_send_at = ?, current_step = ?, thread_id = ?,
         last_message_id = ?, stopped_reason = ?, updated_at = ?
       WHERE id = ?`,
      [
        to,
        nextSendAt,
        options.currentStep ?? current.current_step,
        options.threadId !== void 0 ? options.threadId : current.thread_id,
        options.lastMessageId !== void 0 ? options.lastMessageId : current.last_message_id,
        options.reason ?? current.stopped_reason,
        nowIso(),
        enrollmentId
      ],
      client2
    );
    if (current.status !== to) {
      recordEvent({
        type: EVENT_TYPES.enrollmentStatusChanged,
        entityType: "enrollment",
        entityId: enrollmentId,
        campaignId: current.campaign_id,
        contactId: current.contact_id,
        payload: {
          from: current.status,
          to,
          reason: options.reason ?? null,
          actor: options.actor ?? "worker"
        }
      });
    }
    return await get(`SELECT * FROM enrollments WHERE id = ?`, [enrollmentId], client2);
  });
}
async function stopSequence(enrollmentId, status, reason, actor = "worker") {
  return await transition(enrollmentId, status, { reason, nextSendAt: null, actor });
}

// src/lib/outreach/engine/sender.ts
async function loadStep(campaignId, stepNumber) {
  const step = await get(
    `SELECT * FROM sequence_steps WHERE campaign_id = ? AND step_number = ?`,
    [campaignId, stepNumber]
  );
  if (!step) return null;
  const template = await get(`SELECT * FROM templates WHERE id = ?`, [step.template_id]);
  if (!template) return null;
  return { ...step, template };
}
async function hasInboundReply(enrollmentId) {
  const row = await get(
    `SELECT COUNT(*) n FROM replies r
     WHERE r.enrollment_id = ?
       AND r.classification NOT IN ('ooo', 'auto', 'bounce')`,
    [enrollmentId]
  );
  return (row?.n ?? 0) > 0;
}
async function threadSubject(enrollmentId) {
  const first = await get(
    `SELECT subject FROM messages
     WHERE enrollment_id = ? AND direction = 'outbound' AND subject IS NOT NULL
     ORDER BY step_number ASC LIMIT 1`,
    [enrollmentId]
  );
  return first?.subject ?? null;
}
async function sendStep(item, options = {}) {
  const now = options.now ?? /* @__PURE__ */ new Date();
  const { enrollment, contact, campaign } = item;
  const nextStepNumber = enrollment.current_step + 1;
  const priorSend = await get(
    `SELECT id FROM messages
     WHERE enrollment_id = ? AND step_number = ? AND direction = 'outbound'
       AND status IN ('sent', 'bounced')`,
    [enrollment.id, nextStepNumber]
  );
  if (priorSend) {
    return { kind: "skipped", reason: `step ${nextStepNumber} was already sent (message ${priorSend.id})` };
  }
  const step = await loadStep(campaign.id, nextStepNumber);
  if (!step) {
    transition(enrollment.id, "completed", {
      reason: "all sequence steps sent, no reply",
      nextSendAt: null
    });
    recordEvent({
      type: EVENT_TYPES.enrollmentCompleted,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { steps_sent: enrollment.current_step }
    });
    return { kind: "completed" };
  }
  if (step.only_if_no_reply && nextStepNumber > 1 && await hasInboundReply(enrollment.id)) {
    stopSequence(enrollment.id, "replied", "contact replied \u2014 follow-ups cancelled");
    return { kind: "stopped", reason: "contact already replied" };
  }
  const suppression = await checkSuppressed(contact.email_normalized);
  if (suppression.suppressed) {
    stopSequence(
      enrollment.id,
      "suppressed",
      `suppressed (${suppression.scope}: ${suppression.matchedValue}) \u2014 ${suppression.reason}`
    );
    return { kind: "stopped", reason: `suppressed: ${suppression.reason}` };
  }
  if (!contact.email_normalized) {
    stopSequence(enrollment.id, "review", "contact has no email address");
    return { kind: "stopped", reason: "no email address" };
  }
  const domain = contact.domain ?? contact.email_normalized.split("@")[1];
  const capacity = checkCapacity(campaign, domain, now, { ignoreWindow: options.ignoreWindow });
  if (!(await capacity).allowed && !options.previewOnly) {
    return { kind: "skipped", reason: (await capacity).reason ?? "no capacity" };
  }
  const isFollowUp = step.template.kind === "follow_up" || nextStepNumber > 1;
  const inheritedSubject = isFollowUp ? await threadSubject(enrollment.id) : null;
  let subject;
  let body;
  try {
    const values = buildMergeValues(contact, {
      signature: await getSetting(SETTING_KEYS.signature),
      unsubscribe: await getSetting(SETTING_KEYS.unsubscribeLine)
    });
    const rendered = renderEmail(step.template.subject, step.template.body, values);
    body = rendered.body;
    subject = inheritedSubject ? (await inheritedSubject).startsWith("Re: ") ? inheritedSubject : `Re: ${inheritedSubject}` : rendered.subject;
  } catch (error) {
    const message = error instanceof RenderError ? error.message : String(error);
    stopSequence(enrollment.id, "review", `template render failed: ${message}`);
    recordEvent({
      type: EVENT_TYPES.messageFailed,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { error: message, step: nextStepNumber }
    });
    return { kind: "failed", error: message };
  }
  const fromEmail = await getSetting(SETTING_KEYS.sendingAddress);
  if (!fromEmail && !options.previewOnly) {
    return { kind: "skipped", reason: "no sending address configured \u2014 run npm run gmail:auth" };
  }
  const parts = {
    from: fromEmail || "unconfigured@localhost",
    fromName: await getSetting(SETTING_KEYS.sendingName),
    to: contact.email_normalized,
    toName: contact.full_name ?? void 0,
    subject,
    body,
    inReplyTo: enrollment.last_message_id,
    references: enrollment.last_message_id ? [enrollment.last_message_id] : void 0
  };
  if (options.previewOnly) {
    return { kind: "preview", messageId: 0, subject, body, to: contact.email_normalized };
  }
  const dryRun = await isDryRun();
  if (dryRun) {
    recordEvent({
      type: EVENT_TYPES.messageDryRun,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { step: nextStepNumber, subject, to: contact.email_normalized }
    });
    return { kind: "preview", messageId: 0, subject, body, to: contact.email_normalized };
  }
  try {
    const result = await sendEmail(parts, { threadId: enrollment.thread_id });
    const messageId = await transaction(async (client2) => {
      const inserted = await run(
        `INSERT INTO messages (
           enrollment_id, contact_id, direction, step_number, template_id, status,
           subject, body, snippet, to_email, from_email, gmail_message_id,
           gmail_thread_id, rfc822_message_id, in_reply_to, scheduled_for, sent_at, created_at
         ) VALUES (?, ?, 'outbound', ?, ?, 'sent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          enrollment.id,
          contact.id,
          nextStepNumber,
          step.template_id,
          subject,
          body,
          body.slice(0, 200),
          contact.email_normalized,
          fromEmail,
          result.gmailMessageId,
          result.gmailThreadId,
          result.rfc822MessageId,
          enrollment.last_message_id,
          enrollment.next_send_at,
          nowIso(),
          nowIso()
        ],
        client2
      );
      recordEvent({
        type: EVENT_TYPES.messageSent,
        entityType: "message",
        entityId: inserted.lastInsertRowid,
        campaignId: campaign.id,
        contactId: contact.id,
        payload: { step: nextStepNumber, subject, to: contact.email_normalized, thread: result.gmailThreadId }
      });
      return inserted.lastInsertRowid;
    });
    advanceEnrollment(enrollment, campaign, nextStepNumber, result.gmailThreadId, result.rfc822MessageId, now);
    recordSend(campaign, domain, now);
    return { kind: "sent", messageId, gmailMessageId: result.gmailMessageId, subject };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await run(
      `INSERT INTO messages (
         enrollment_id, contact_id, direction, step_number, template_id, status,
         subject, body, to_email, from_email, error, created_at
       ) VALUES (?, ?, 'outbound', ?, ?, 'failed', ?, ?, ?, ?, ?, ?)`,
      [
        enrollment.id,
        contact.id,
        nextStepNumber,
        step.template_id,
        subject,
        body,
        contact.email_normalized,
        fromEmail,
        message,
        nowIso()
      ]
    );
    recordEvent({
      type: EVENT_TYPES.messageFailed,
      entityType: "enrollment",
      entityId: enrollment.id,
      campaignId: campaign.id,
      contactId: contact.id,
      payload: { error: message, step: nextStepNumber }
    });
    const alreadyFailed = await get(
      `SELECT COUNT(*) n FROM messages WHERE enrollment_id = ? AND status = 'failed' AND step_number = ?`,
      [enrollment.id, nextStepNumber]
    );
    if ((alreadyFailed?.n ?? 0) >= 2) {
      stopSequence(enrollment.id, "review", `send failed twice: ${message}`);
    } else {
      transition(enrollment.id, "active", {
        nextSendAt: addSendingDays(now, 1, campaign, 60).toISOString()
      });
    }
    return { kind: "failed", error: message };
  }
}
async function advanceEnrollment(enrollment, campaign, sentStep, threadId, rfc822MessageId, now) {
  const nextStep = await get(
    `SELECT * FROM sequence_steps WHERE campaign_id = ? AND step_number = ?`,
    [campaign.id, sentStep + 1]
  );
  const nextSendAt = nextStep ? addSendingDays(now, Math.max(1, nextStep.delay_days), campaign, 90).toISOString() : null;
  transition(enrollment.id, "active", {
    currentStep: sentStep,
    nextSendAt,
    threadId: threadId ?? enrollment.thread_id,
    lastMessageId: rfc822MessageId ?? enrollment.last_message_id
  });
  recordEvent({
    type: EVENT_TYPES.enrollmentStepAdvanced,
    entityType: "enrollment",
    entityId: enrollment.id,
    campaignId: campaign.id,
    contactId: enrollment.contact_id,
    payload: { step: sentStep, next_step_at: nextSendAt }
  });
  if (!nextStep) {
    await run(`UPDATE enrollments SET next_send_at = ? WHERE id = ?`, [
      addSendingDays(now, 5, campaign, 0).toISOString(),
      enrollment.id
    ]);
  }
}

// src/lib/outreach/engine/dispatch.ts
async function dispatchMode() {
  return await getSetting(SETTING_KEYS.dispatchMode) === "automatic" ? "automatic" : "manual";
}
async function isManual() {
  return await dispatchMode() === "manual";
}
async function dispatchTime() {
  const raw = await getSetting(SETTING_KEYS.dispatchTime) || "09:00";
  const match = /^(\d{1,2}):(\d{2})$/.exec(raw.trim());
  const hour = match ? Number(match[1]) : 9;
  const minute = match ? Number(match[2]) : 0;
  const valid = hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59;
  const h = valid ? hour : 9;
  const m = valid ? minute : 0;
  return { hour: h, minute: m, text: `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}` };
}
async function autoDispatchGate(campaign, now = /* @__PURE__ */ new Date()) {
  if (await isManual()) {
    return { allowed: false, reason: "manual mode, nothing sends on its own" };
  }
  const { hour, minute, text } = await dispatchTime();
  const local = zonedParts(now, campaign.timezone);
  const released = local.hour > hour || local.hour === hour && local.minute >= minute;
  return released ? { allowed: true, reason: `automatic, released at ${text}` } : { allowed: false, reason: `automatic, but today's run does not start until ${text}` };
}
var dispatchInFlight = false;
async function runDispatch(options) {
  const now = options.now ?? /* @__PURE__ */ new Date();
  const manual = options.trigger === "manual";
  const summary = {
    attempted: 0,
    sent: 0,
    previewed: 0,
    skipped: 0,
    stopped: 0,
    completed: 0,
    failed: 0,
    held: 0,
    heldReason: null,
    errors: [],
    skippedReasons: []
  };
  if (dispatchInFlight) {
    summary.heldReason = "another dispatch is already running \u2014 this click did nothing";
    return summary;
  }
  dispatchInFlight = true;
  try {
    return await runDispatchInner(options, now, manual, summary);
  } finally {
    dispatchInFlight = false;
  }
}
async function runDispatchInner(options, now, manual, summary) {
  const items = manual ? await enrollmentsByIds(options.enrollmentIds ?? []) : await dueEnrollments(now, options.limit ?? 200);
  for (const item of items) {
    if (!manual) {
      const gate = autoDispatchGate(item.campaign, now);
      if (!(await gate).allowed) {
        summary.held++;
        summary.heldReason = (await gate).reason;
        continue;
      }
    }
    summary.attempted++;
    try {
      const outcome = await sendStep(item, { now, ignoreWindow: manual });
      switch (outcome.kind) {
        case "sent":
          summary.sent++;
          break;
        case "preview":
          summary.previewed++;
          break;
        case "skipped":
          summary.skipped++;
          summary.skippedReasons.push(`${item.contact.company}: ${outcome.reason}`);
          break;
        case "stopped":
          summary.stopped++;
          break;
        case "completed":
          summary.completed++;
          break;
        case "failed":
          summary.failed++;
          summary.errors.push(`${item.contact.email_normalized}: ${outcome.error}`);
          break;
      }
    } catch (error) {
      summary.failed++;
      summary.errors.push(
        `${item.contact.email_normalized}: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
  if (summary.attempted > 0) {
    recordEvent({
      type: EVENT_TYPES.dispatchRun,
      entityType: "worker",
      payload: { trigger: options.trigger, ...summary }
    });
  }
  return summary;
}

// src/lib/outreach/inbox/classify.ts
var OPT_OUT_DOMAIN = [
  { id: "unsubscribe", pattern: /\bunsubscribe\b/i, weight: 1 },
  { id: "remove_from_list", pattern: /\b(remove|take)\s+(me|us|this|our)?\s*(from|off)\s+(your|the|this)?\s*(mailing\s+)?list\b/i, weight: 1 },
  { id: "take_us_off", pattern: /\btake\s+(me|us)\s+off\b/i, weight: 1 },
  { id: "stop_emailing", pattern: /\b(stop|cease)\s+(emailing|contacting|messaging|sending)\b/i, weight: 1 },
  { id: "do_not_contact", pattern: /\b(do\s*not|don'?t)\s+(contact|email|message)\s+(me|us|this)\b/i, weight: 1 },
  { id: "no_solicitation", pattern: /\b(no|unsolicited)\s+(solicitation|vendors?|sales\s+(emails?|pitches?))\b/i, weight: 1 },
  { id: "gdpr", pattern: /\b(gdpr|ccpa|data\s+protection)\b.*\b(request|delete|erase)\b/i, weight: 1 }
];
var NEGATIVE = [
  { id: "not_interested", pattern: /\bnot\s+interested\b/i, weight: 3 },
  { id: "no_thanks", pattern: /\bno,?\s+thank(s|\s+you)\b/i, weight: 3 },
  { id: "not_a_fit", pattern: /\b(not|isn'?t)\s+(a\s+)?(good\s+)?fit\b/i, weight: 3 },
  { id: "we_pass", pattern: /\b(we'?ll|i'?ll|we|i)\s+pass\b/i, weight: 3 },
  { id: "no_budget", pattern: /\bno\s+(budget|funding|money)\b/i, weight: 2 },
  { id: "already_have", pattern: /\b(we\s+)?already\s+(have|use|using|work\s+with)\b/i, weight: 2 },
  { id: "built_in_house", pattern: /\b(in[-\s]house|internally|ourselves)\b.*\b(built|develop|handle|solve)/i, weight: 2 },
  { id: "wrong_person", pattern: /\b(wrong|not\s+the\s+right)\s+(person|contact|team)\b/i, weight: 1 },
  { id: "no_need", pattern: /\b(don'?t|do\s+not)\s+(have\s+a\s+)?need\b/i, weight: 2 },
  { id: "not_looking", pattern: /\b(not|aren'?t)\s+(currently\s+)?looking\b/i, weight: 2 },
  { id: "please_stop", pattern: /\bplease\s+stop\b/i, weight: 3 },
  { id: "spam_accusation", pattern: /\b(this\s+is\s+)?spam\b/i, weight: 3 }
];
var POSITIVE = [
  { id: "interested", pattern: /\b(i'?m|we'?re|am|are)\s+(very\s+|quite\s+|definitely\s+)?interested\b/i, weight: 3 },
  { id: "interested_bare", pattern: /\binterested\b/i, weight: 2 },
  { id: "lets_talk", pattern: /\b(let'?s|happy\s+to|glad\s+to|would\s+like\s+to)\s+(chat|talk|connect|discuss|meet|jump\s+on)\b/i, weight: 3 },
  { id: "book_call", pattern: /\b(book|schedule|set\s+up|arrange)\s+(a\s+)?(call|meeting|time|demo|chat)\b/i, weight: 3 },
  { id: "calendar_link", pattern: /\b(calendly\.com|cal\.com|savvycal|hubspot\.com\/meetings)\b/i, weight: 3 },
  { id: "send_more", pattern: /\b(send|share)\s+(me|us|over)?\s*(more|some)?\s*(info|information|details|deck|docs)\b/i, weight: 3 },
  { id: "tell_me_more", pattern: /\btell\s+me\s+more\b/i, weight: 3 },
  { id: "sounds_good", pattern: /\b(sounds|looks)\s+(good|interesting|great)\b/i, weight: 2 },
  { id: "when_free", pattern: /\b(when|what\s+time)\s+(are|would)\s+you\s+(free|available)\b/i, weight: 3 },
  { id: "pricing", pattern: /\b(pricing|price|cost|how\s+much|quote)\b/i, weight: 2 },
  { id: "demo", pattern: /\b(see\s+a\s+)?demo\b/i, weight: 2 },
  { id: "worth_a_look", pattern: /\bworth\s+(a\s+)?(look|exploring|discussing)\b/i, weight: 2 },
  { id: "technical_question", pattern: /\b(does|can|what)\s+(it|this|hexera)\s+(support|handle|work\s+with)\b/i, weight: 2 }
];
var DEFER = [
  { id: "circle_back", pattern: /\b(circle\s+back|touch\s+base|follow\s+up|reach\s+out)\s+(in|next|later|after|around|q[1-4])\b/i, weight: 2 },
  { id: "not_right_now", pattern: /\b(not\s+(right\s+now|at\s+the\s+moment|currently)|bad\s+timing|too\s+early)\b/i, weight: 2 },
  { id: "keep_in_touch", pattern: /\bkeep\s+(me|us)\s+(posted|in\s+the\s+loop|informed)\b/i, weight: 2 },
  { id: "check_back", pattern: /\b(check|ping)\s+(back|in)\s+(with\s+(me|us)\s+)?(in|next|later)\b/i, weight: 2 }
];
var REFERRAL = [
  { id: "loop_in", pattern: /\b(cc'?ing|copying|looping\s+in|introduc(e|ing)\s+you)\b/i, weight: 3 },
  { id: "talk_to", pattern: /\b(you\s+should|better\s+to|please)\s+(talk|speak|reach\s+out)\s+to\b/i, weight: 3 },
  { id: "forwarded", pattern: /\bi'?(ve|ll)\s+forward(ed)?\b/i, weight: 2 },
  { id: "right_person", pattern: /\b(is|would\s+be)\s+the\s+right\s+person\b/i, weight: 2 }
];
var OUT_OF_OFFICE = [
  { id: "ooo_phrase", pattern: /\bout\s+of\s+(the\s+)?office\b/i, weight: 3 },
  { id: "ooo_auto", pattern: /\bauto(matic)?[-\s]?repl(y|ies)\b/i, weight: 3 },
  { id: "on_leave", pattern: /\b(on|taking)\s+(annual\s+|parental\s+|maternity\s+|paternity\s+|sick\s+|medical\s+)?leave\b/i, weight: 3 },
  { id: "vacation", pattern: /\b(on\s+)?(vacation|holiday|pto)\b/i, weight: 3 },
  { id: "away_until", pattern: /\b(away|out|back|returning)\s+(from\s+the\s+office\s+)?(until|on|till)\b/i, weight: 3 },
  { id: "limited_access", pattern: /\blimited\s+access\s+to\s+(my\s+)?e?-?mail\b/i, weight: 3 },
  { id: "no_longer_here", pattern: /\bno\s+longer\s+(with|at|works?\s+(at|for))\b/i, weight: 3 }
];
var BOUNCE = [
  { id: "dsn_subject", pattern: /\b(delivery\s+status\s+notification|undeliverable|delivery\s+has\s+failed|returned\s+mail|mail\s+delivery\s+failed)\b/i, weight: 3 },
  { id: "address_not_found", pattern: /\b(address\s+not\s+found|recipient\s+(address\s+)?rejected|user\s+unknown|no\s+such\s+user|mailbox\s+(is\s+)?unavailable)\b/i, weight: 3 },
  { id: "smtp_550", pattern: /\b5\.[157]\.[0-9]\b|\b550[\s-]/i, weight: 3 },
  { id: "does_not_exist", pattern: /\b(account|address|mailbox)\s+(does\s+not|doesn'?t)\s+exist\b/i, weight: 3 }
];
var BOUNCE_SENDERS = /^(mailer-daemon|postmaster|no-?reply|nobody)@/i;
function applyRules(text, rules) {
  let score = 0;
  const matched = [];
  for (const rule of rules) {
    if (rule.pattern.test(text)) {
      score += rule.weight;
      matched.push(rule.id);
    }
  }
  return { score, matched };
}
function classifyReply(input) {
  const haystack = `${input.subject}
${input.body}`;
  const bounce = applyRules(haystack, BOUNCE);
  if (BOUNCE_SENDERS.test(input.fromEmail) && bounce.score > 0) {
    return {
      classification: "bounce",
      confidence: 0.97,
      matchedRules: ["sender:mailer-daemon", ...bounce.matched],
      requiresReview: false,
      optOutScope: null,
      classifier: "rules"
    };
  }
  if (bounce.score >= 6) {
    return {
      classification: "bounce",
      confidence: 0.85,
      matchedRules: bounce.matched,
      requiresReview: false,
      optOutScope: null,
      classifier: "rules"
    };
  }
  const optOut = applyRules(haystack, OPT_OUT_DOMAIN);
  if (optOut.score > 0) {
    return {
      classification: "negative",
      confidence: 0.98,
      matchedRules: optOut.matched,
      requiresReview: false,
      // An unsubscribe request covers the organization, not just the mailbox
      // that happened to answer.
      optOutScope: "domain",
      classifier: "rules"
    };
  }
  const ooo = applyRules(haystack, OUT_OF_OFFICE);
  if (ooo.score >= 3) {
    return {
      classification: "ooo",
      confidence: input.isAutoSubmitted ? 0.95 : 0.8,
      matchedRules: ooo.matched,
      // "No longer with the company" looks like an OOO but means the contact
      // is dead and someone needs to re-target the account.
      requiresReview: ooo.matched.includes("no_longer_here"),
      optOutScope: null,
      classifier: "rules"
    };
  }
  if (input.isAutoSubmitted) {
    return {
      classification: "auto",
      confidence: 0.85,
      matchedRules: ["header:auto-submitted"],
      requiresReview: false,
      optOutScope: null,
      classifier: "rules"
    };
  }
  const negative = applyRules(haystack, NEGATIVE);
  const positive = applyRules(haystack, POSITIVE);
  const defer = applyRules(haystack, DEFER);
  const referral = applyRules(haystack, REFERRAL);
  const matched = [...negative.matched, ...positive.matched, ...defer.matched, ...referral.matched];
  if (referral.score >= 3 && negative.score < 3) {
    return {
      classification: "positive",
      confidence: 0.75,
      matchedRules: matched,
      requiresReview: true,
      optOutScope: null,
      classifier: "rules"
    };
  }
  if (negative.score > 0 && negative.score >= positive.score) {
    const decisive = negative.score >= 3;
    return {
      classification: "negative",
      confidence: decisive ? 0.88 : 0.6,
      matchedRules: matched,
      requiresReview: !decisive,
      // A plain "not interested" is not an opt-out request; suppress only this
      // address so a different contact at the company stays reachable.
      optOutScope: decisive ? "email" : null,
      classifier: "rules"
    };
  }
  if (positive.score >= 3) {
    return {
      classification: "positive",
      confidence: positive.score >= 5 ? 0.9 : 0.75,
      matchedRules: matched,
      requiresReview: false,
      optOutScope: null,
      classifier: "rules"
    };
  }
  if (defer.score >= 2) {
    return {
      classification: "neutral",
      confidence: 0.7,
      matchedRules: matched,
      requiresReview: true,
      optOutScope: null,
      classifier: "rules"
    };
  }
  if (positive.score > 0) {
    return {
      classification: "positive",
      confidence: 0.55,
      matchedRules: matched,
      requiresReview: true,
      optOutScope: null,
      classifier: "rules"
    };
  }
  return {
    classification: "neutral",
    confidence: 0.3,
    matchedRules: matched,
    requiresReview: true,
    optOutScope: null,
    classifier: "rules"
  };
}

// src/lib/outreach/inbox/classify-llm.ts
import { z } from "zod";
var ReplyAnalysis = z.object({
  classification: z.enum(["positive", "negative", "neutral", "ooo", "auto", "bounce"]),
  confidence: z.number().min(0).max(1),
  opt_out_requested: z.boolean(),
  reasoning: z.string()
});
var SYSTEM_PROMPT = `You classify replies to cold outreach emails sent by Hexera, a company that automates CAD-to-mesh generation for aerospace CFD simulation.

Classify the reply into exactly one category:

- positive: interest, a question about the product, a request for a call/demo/pricing, or a referral to a colleague.
- negative: a rejection, a statement that this is not relevant, or a request to stop contacting them.
- neutral: a human wrote back but their intent is genuinely unclear, or they are deferring to a later date.
- ooo: an out-of-office or leave auto-responder.
- auto: any other automated message (ticket acknowledgement, no-reply notification, mailing list).
- bounce: a delivery failure notice.

Set opt_out_requested to true only when they ask not to be contacted again, as opposed to simply declining this offer.

Two judgement calls that matter:
- A deferral ("circle back next quarter") is neutral, not negative. They have not said no.
- A referral to someone else is positive, even when the person writing is declining for themselves.

Be conservative: when a reply could plausibly be read as negative, classify it as negative. A missed follow-up costs one email; contacting someone who asked you to stop costs the relationship.`;
function isLlmClassifierAvailable() {
  return Boolean(process.env.ANTHROPIC_API_KEY);
}
async function classifyWithLlm(input, rulesVerdict, options = {}) {
  const apiKey = options.apiKey ?? process.env.ANTHROPIC_API_KEY;
  if (!apiKey) return rulesVerdict;
  if (rulesVerdict.optOutScope !== null || rulesVerdict.classification === "bounce") {
    return rulesVerdict;
  }
  try {
    const { default: Anthropic } = await import("@anthropic-ai/sdk");
    const { zodOutputFormat } = await import("@anthropic-ai/sdk/helpers/zod");
    const client2 = new Anthropic({ apiKey });
    const response = await client2.messages.parse({
      model: options.model ?? process.env.CLASSIFIER_MODEL ?? "claude-opus-5",
      max_tokens: 16e3,
      system: SYSTEM_PROMPT,
      // Classification is a short, scoped task; low effort keeps latency and
      // cost down without hurting accuracy here.
      output_config: {
        effort: "low",
        format: zodOutputFormat(ReplyAnalysis)
      },
      messages: [
        {
          role: "user",
          content: `From: ${input.fromEmail}
Subject: ${input.subject}

${input.body.slice(0, 6e3)}`
        }
      ]
    });
    if (response.stop_reason === "refusal") {
      return { ...rulesVerdict, requiresReview: true };
    }
    const parsed = response.parsed_output;
    if (!parsed) return { ...rulesVerdict, requiresReview: true };
    const classification = parsed.classification;
    return {
      classification,
      confidence: parsed.confidence,
      matchedRules: [...rulesVerdict.matchedRules, `llm:${parsed.reasoning.slice(0, 160)}`],
      // Disagreement between the two classifiers is exactly the case a human
      // should look at, even when the model is confident.
      requiresReview: parsed.confidence < 0.75 || classification !== rulesVerdict.classification,
      optOutScope: parsed.opt_out_requested ? "domain" : null,
      classifier: "llm"
    };
  } catch (error) {
    console.warn(
      `LLM classifier unavailable (${error instanceof Error ? error.message : String(error)}); keeping the rules verdict and flagging for review.`
    );
    return { ...rulesVerdict, requiresReview: true };
  }
}

// src/lib/outreach/inbox/poller.ts
async function alreadyProcessed(gmailMessageId) {
  return Boolean(
    await get(`SELECT id FROM messages WHERE gmail_message_id = ?`, [gmailMessageId])
  );
}
var BOUNCE_SENDER = /^(mailer-daemon|postmaster)@/i;
async function failedRecipient(message) {
  const header = message.headers.find((h) => h.name?.toLowerCase() === "x-failed-recipients")?.value;
  if (header?.trim()) return header.trim().toLowerCase();
  const text = `${message.snippet}
${message.body}`;
  const stated = text.match(
    /(?:wasn't delivered to|could not be delivered to|delivery to|failed for|final-recipient:[^;]*;)\s*<?([^\s<>;,"']+@[^\s<>;,"']+)>?/i
  );
  return stated ? stated[1].toLowerCase().replace(/\.+$/, "") : null;
}
async function matchEnrollment(message, fromEmail) {
  let enrollment = await get(
    `SELECT * FROM enrollments WHERE thread_id = ? ORDER BY updated_at DESC LIMIT 1`,
    [message.threadId]
  );
  if (!enrollment) {
    const address = BOUNCE_SENDER.test(fromEmail) ? failedRecipient(message) ?? fromEmail : fromEmail;
    enrollment = await get(
      `SELECT e.* FROM enrollments e
       JOIN contacts c ON c.id = e.contact_id
       WHERE c.email_normalized = ?
       ORDER BY CASE WHEN e.status IN ('pending','active') THEN 0 ELSE 1 END, e.updated_at DESC
       LIMIT 1`,
      [address]
    );
  }
  if (!enrollment) return null;
  const contact = await get(`SELECT * FROM contacts WHERE id = ?`, [enrollment.contact_id]);
  const campaign = await get(`SELECT * FROM campaigns WHERE id = ?`, [enrollment.campaign_id]);
  if (!contact || !campaign) return null;
  return { enrollment, contact, campaign };
}
async function applyOutcome(match, verdict, result) {
  const { enrollment, contact, campaign } = match;
  switch (verdict.classification) {
    case "negative": {
      if (await getBoolSetting(SETTING_KEYS.autoStopOnNegative)) {
        stopSequence(enrollment.id, "stopped", `negative reply (${verdict.matchedRules.join(", ")})`, "reply-classifier");
        result.stopped++;
      }
      if (contact.email_normalized) {
        const scope = verdict.optOutScope === "domain" && await getBoolSetting(SETTING_KEYS.autoSuppressDomainOnOptOut) ? "domain" : "email";
        const value = scope === "domain" ? domainOf(contact.email_normalized) : contact.email_normalized;
        if (await suppress({
          scope,
          value,
          reason: verdict.optOutScope === "domain" ? "explicit opt-out request" : "declined outreach",
          source: "negative_reply",
          contactId: contact.id
        })) {
          result.suppressed++;
        }
      }
      break;
    }
    case "positive": {
      stopSequence(enrollment.id, "replied", "positive reply \u2014 handed to a human", "reply-classifier");
      result.stopped++;
      break;
    }
    case "neutral": {
      stopSequence(enrollment.id, "review", "reply needs a human read before continuing", "reply-classifier");
      result.stopped++;
      break;
    }
    case "ooo": {
      if (enrollment.status === "active" || enrollment.status === "pending") {
        const resumeAt = addSendingDays(/* @__PURE__ */ new Date(), 7, campaign, 60);
        transition(enrollment.id, "active", {
          nextSendAt: resumeAt.toISOString(),
          reason: "out-of-office \u2014 follow-up deferred",
          actor: "reply-classifier"
        });
      }
      break;
    }
    case "auto":
      break;
    case "bounce": {
      stopSequence(enrollment.id, "bounced", "hard bounce \u2014 address is undeliverable", "reply-classifier");
      result.stopped++;
      await run(
        `UPDATE messages SET status = 'bounced', error = ?
         WHERE enrollment_id = ? AND direction = 'outbound' AND status = 'sent'`,
        ["hard bounce, address is undeliverable", enrollment.id]
      );
      if (contact.email_normalized) {
        if (await suppress({
          scope: "email",
          value: contact.email_normalized,
          reason: "hard bounce",
          source: "bounce",
          contactId: contact.id
        })) {
          result.suppressed++;
        }
      }
      break;
    }
  }
}
async function pollReplies(options = {}) {
  const result = {
    scanned: 0,
    matched: 0,
    newReplies: 0,
    byClassification: {},
    stopped: 0,
    suppressed: 0,
    errors: []
  };
  const useLlm = options.useLlm ?? isLlmClassifierAvailable();
  const ids = await listInboundMessages({
    query: options.query ?? "in:inbox newer_than:30d",
    maxResults: options.maxResults ?? 100
  });
  for (const id of ids) {
    result.scanned++;
    if (await alreadyProcessed(id)) continue;
    try {
      const message = await fetchMessage(id);
      const headers = parseInboundHeaders(message.headers);
      const match = await matchEnrollment(message, headers.from);
      if (!match) continue;
      result.matched++;
      const cleanBody = stripQuotedText(message.body || message.snippet);
      let verdict = classifyReply({
        body: cleanBody,
        subject: headers.subject,
        fromEmail: headers.from,
        isAutoSubmitted: headers.isAutoSubmitted
      });
      if (useLlm && (verdict.requiresReview || verdict.confidence < 0.7)) {
        verdict = await classifyWithLlm(
          { subject: headers.subject, body: cleanBody, fromEmail: headers.from },
          verdict
        );
      }
      await transaction(async (client2) => {
        const inserted = await run(
          `INSERT INTO messages (
             enrollment_id, contact_id, direction, status, subject, body, snippet,
             to_email, from_email, gmail_message_id, gmail_thread_id,
             rfc822_message_id, in_reply_to, received_at, created_at
           ) VALUES (?, ?, 'inbound', 'received', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            match.enrollment.id,
            match.contact.id,
            headers.subject,
            cleanBody,
            message.snippet.slice(0, 200),
            headers.to,
            headers.from,
            message.id,
            message.threadId,
            headers.messageId,
            headers.inReplyTo,
            headers.date ?? nowIso(),
            nowIso()
          ],
          client2
        );
        await run(
          `INSERT INTO replies (
             message_id, enrollment_id, contact_id, classification, confidence,
             classifier, matched_rules, requires_review, received_at, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          [
            inserted.lastInsertRowid,
            match.enrollment.id,
            match.contact.id,
            verdict.classification,
            verdict.confidence,
            verdict.classifier,
            JSON.stringify(verdict.matchedRules),
            verdict.requiresReview ? 1 : 0,
            headers.date ?? nowIso(),
            nowIso()
          ],
          client2
        );
        recordEvent({
          type: EVENT_TYPES.replyClassified,
          entityType: "reply",
          entityId: inserted.lastInsertRowid,
          campaignId: match.campaign.id,
          contactId: match.contact.id,
          payload: {
            classification: verdict.classification,
            confidence: verdict.confidence,
            classifier: verdict.classifier,
            requires_review: verdict.requiresReview,
            snippet: cleanBody.slice(0, 200)
          }
        });
      });
      if (["positive", "negative", "neutral"].includes(verdict.classification)) {
        try {
          await starMessage(message.id);
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          result.errors.push(`star failed for ${headers.from}: ${detail}`);
        }
      }
      result.newReplies++;
      result.byClassification[verdict.classification] = (result.byClassification[verdict.classification] ?? 0) + 1;
      applyOutcome(match, verdict, result);
    } catch (error) {
      result.errors.push(`${id}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return result;
}

// src/lib/outreach/verify/heuristics.ts
var ROLE_LOCAL_PARTS = /* @__PURE__ */ new Set([
  "info",
  "sales",
  "support",
  "contact",
  "contacts",
  "admin",
  "administrator",
  "hello",
  "hi",
  "team",
  "careers",
  "jobs",
  "recruiting",
  "hr",
  "press",
  "media",
  "marketing",
  "billing",
  "accounts",
  "accounting",
  "finance",
  "help",
  "helpdesk",
  "office",
  "enquiries",
  "enquiry",
  "inquiries",
  "inquiry",
  "general",
  "mail",
  "email",
  "webmaster",
  "postmaster",
  "hostmaster",
  "abuse",
  "legal",
  "privacy",
  "security",
  "noreply",
  "no-reply",
  "donotreply",
  "do-not-reply",
  "newsletter",
  "notifications",
  "service",
  "customerservice",
  "partners",
  "partnerships",
  "bd",
  "investors",
  "ir",
  "pr"
]);
var FREE_PROVIDERS = /* @__PURE__ */ new Set([
  "gmail.com",
  "googlemail.com",
  "yahoo.com",
  "yahoo.co.uk",
  "ymail.com",
  "hotmail.com",
  "hotmail.co.uk",
  "outlook.com",
  "live.com",
  "msn.com",
  "aol.com",
  "icloud.com",
  "me.com",
  "mac.com",
  "protonmail.com",
  "proton.me",
  "pm.me",
  "gmx.com",
  "gmx.net",
  "mail.com",
  "yandex.com",
  "yandex.ru",
  "zoho.com",
  "fastmail.com",
  "hey.com",
  "tutanota.com",
  "tuta.io",
  "comcast.net",
  "verizon.net",
  "sbcglobal.net",
  "att.net",
  "qq.com",
  "163.com"
]);
var DISPOSABLE_DOMAINS = /* @__PURE__ */ new Set([
  "mailinator.com",
  "guerrillamail.com",
  "guerrillamail.net",
  "10minutemail.com",
  "tempmail.com",
  "temp-mail.org",
  "throwawaymail.com",
  "yopmail.com",
  "trashmail.com",
  "sharklasers.com",
  "getnada.com",
  "maildrop.cc",
  "dispostable.com",
  "fakeinbox.com",
  "mailnesia.com",
  "mintemail.com",
  "spamgourmet.com",
  "mytemp.email",
  "moakt.com",
  "emailondeck.com",
  "burnermail.io",
  "spam4.me",
  "grr.la",
  "einrot.com",
  "tempr.email"
]);
function checkSyntax(email) {
  if (!email) return { ok: false, reason: "empty address" };
  if (email.length > 254) return { ok: false, reason: "address exceeds 254 characters" };
  const at = email.lastIndexOf("@");
  if (at <= 0 || at === email.length - 1) return { ok: false, reason: "missing local part or domain" };
  const local = email.slice(0, at);
  const domain = email.slice(at + 1);
  if (local.length > 64) return { ok: false, reason: "local part exceeds 64 characters" };
  if (/^\.|\.$|\.\./.test(local)) return { ok: false, reason: "local part has a leading, trailing or doubled dot" };
  if (!/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$/.test(local)) {
    return { ok: false, reason: "local part contains an illegal character" };
  }
  if (domain.length > 253) return { ok: false, reason: "domain exceeds 253 characters" };
  if (!/^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$/.test(domain)) {
    return { ok: false, reason: "domain is not a valid hostname" };
  }
  if (!/\.[A-Za-z]{2,}$/.test(domain)) return { ok: false, reason: "domain has no valid TLD" };
  return { ok: true };
}
function localPart(email) {
  const at = email.lastIndexOf("@");
  return at === -1 ? email : email.slice(0, at);
}
function domainPart(email) {
  const at = email.lastIndexOf("@");
  return at === -1 ? "" : email.slice(at + 1);
}
function isRoleAccount(email) {
  const local = localPart(email).toLowerCase().split("+")[0];
  if (ROLE_LOCAL_PARTS.has(local)) return true;
  const head = local.split(/[.\-_]/)[0];
  return ROLE_LOCAL_PARTS.has(head);
}
function isFreeProvider(domain) {
  return FREE_PROVIDERS.has(domain.toLowerCase());
}
function isDisposable(domain) {
  const lower = domain.toLowerCase();
  if (DISPOSABLE_DOMAINS.has(lower)) return true;
  return [...DISPOSABLE_DOMAINS].some((d) => lower.endsWith(`.${d}`));
}

// src/lib/outreach/verify/dns.ts
import { Resolver } from "node:dns/promises";
var CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1e3;
function resolver() {
  const instance = new Resolver({ timeout: 5e3, tries: 2 });
  instance.setServers(["1.1.1.1", "8.8.8.8", "9.9.9.9"]);
  return instance;
}
var AUTHORITATIVE_NEGATIVES = /* @__PURE__ */ new Set(["ENOTFOUND", "NXDOMAIN", "ENODATA"]);
async function resolveWithRetry(fn, attempts = 3) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      const code = error.code ?? "";
      if (AUTHORITATIVE_NEGATIVES.has(code)) throw error;
      if (attempt < attempts - 1) {
        await new Promise((r) => setTimeout(r, 250 * 2 ** attempt));
      }
    }
  }
  throw lastError;
}
async function lookupMx(domain) {
  let mxCode = "";
  try {
    const records = await resolveWithRetry(() => resolver().resolveMx(domain));
    const hosts = records.filter((r) => r.exchange && r.exchange !== ".").sort((a, b) => a.priority - b.priority).map((r) => r.exchange.toLowerCase());
    if (hosts.length) return { ok: true, hosts, definitive: true };
    mxCode = "ENODATA";
  } catch (error) {
    mxCode = error.code ?? "UNKNOWN";
    if (mxCode === "ENOTFOUND" || mxCode === "NXDOMAIN") {
      return { ok: false, hosts: [], error: "domain does not exist", definitive: true };
    }
    if (!AUTHORITATIVE_NEGATIVES.has(mxCode)) {
      return {
        ok: false,
        hosts: [],
        error: `DNS lookup failed (${mxCode}) \u2014 could not determine whether this domain accepts mail`,
        definitive: false
      };
    }
  }
  try {
    const addresses = await resolveWithRetry(() => resolver().resolve4(domain));
    if (addresses.length) return { ok: true, hosts: [domain.toLowerCase()], definitive: true };
  } catch (error) {
    const aCode = error.code ?? "UNKNOWN";
    if (!AUTHORITATIVE_NEGATIVES.has(aCode)) {
      return {
        ok: false,
        hosts: [],
        error: `DNS lookup failed (${aCode}) \u2014 could not determine whether this domain accepts mail`,
        definitive: false
      };
    }
  }
  return { ok: false, hosts: [], error: "no MX or A record", definitive: true };
}
async function getDomainIntel(domain, force = false) {
  const cached = await get(`SELECT * FROM domain_intel WHERE domain = ?`, [domain]);
  if (cached && !force) {
    const age = Date.now() - new Date(cached.checked_at).getTime();
    if (age < CACHE_TTL_MS) return { intel: cached, definitive: true };
  }
  const mx = await lookupMx(domain);
  const now = nowIso();
  if (!mx.definitive) {
    return {
      intel: cached ?? {
        domain,
        mx_ok: 0,
        mx_hosts: null,
        is_catch_all: null,
        is_disposable: isDisposable(domain) ? 1 : 0,
        is_free: isFreeProvider(domain) ? 1 : 0,
        checked_at: now
      },
      definitive: false,
      error: mx.error
    };
  }
  await run(
    `INSERT INTO domain_intel (domain, mx_ok, mx_hosts, is_catch_all, is_disposable, is_free, checked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (domain) DO UPDATE SET
       mx_ok = excluded.mx_ok,
       mx_hosts = excluded.mx_hosts,
       is_disposable = excluded.is_disposable,
       is_free = excluded.is_free,
       checked_at = excluded.checked_at`,
    [
      domain,
      mx.ok ? 1 : 0,
      JSON.stringify(mx.hosts),
      // Preserve any catch-all determination from a previous SMTP probe;
      // a plain MX refresh has nothing to say about it.
      cached?.is_catch_all ?? null,
      isDisposable(domain) ? 1 : 0,
      isFreeProvider(domain) ? 1 : 0,
      now
    ]
  );
  return {
    intel: await get(`SELECT * FROM domain_intel WHERE domain = ?`, [domain]),
    definitive: true
  };
}
async function setCatchAll(domain, isCatchAll) {
  await run(`UPDATE domain_intel SET is_catch_all = ? WHERE domain = ?`, [isCatchAll ? 1 : 0, domain]);
}

// src/lib/outreach/verify/smtp.ts
import net from "node:net";
import { randomBytes as randomBytes2 } from "node:crypto";
function openSession(host, timeoutMs) {
  return new Promise((resolve2, reject) => {
    const socket = net.createConnection({ host, port: 25 });
    socket.setEncoding("utf8");
    socket.setTimeout(timeoutMs);
    let buffer = "";
    let pending = null;
    let failed = null;
    const tryFlush = () => {
      if (!pending) return;
      const match = /^(\d{3})(?: [^\n]*)?\r?\n$|(?:^|\n)(\d{3}) [^\n]*\r?\n$/.exec(buffer);
      if (!match) return;
      const code = Number(match[1] ?? match[2]);
      const text = buffer.trim();
      buffer = "";
      const settle = pending;
      pending = null;
      failed = null;
      settle({ code, text });
    };
    socket.on("data", (chunk) => {
      buffer += chunk;
      tryFlush();
    });
    const fail = (error) => {
      if (failed) {
        const reject_ = failed;
        pending = null;
        failed = null;
        reject_(error);
      }
    };
    socket.on("error", (error) => {
      fail(error);
      reject(error);
    });
    socket.on("timeout", () => {
      const error = new Error("smtp timeout");
      fail(error);
      socket.destroy();
      reject(error);
    });
    socket.on("close", () => fail(new Error("connection closed by server")));
    const read = () => new Promise((res, rej) => {
      pending = res;
      failed = rej;
      tryFlush();
    });
    const send = (line) => {
      socket.write(`${line}\r
`);
      return read();
    };
    socket.on("connect", () => {
      resolve2({
        socket,
        read,
        send,
        close: () => {
          try {
            socket.write("QUIT\r\n");
          } catch {
          }
          socket.destroy();
        }
      });
    });
  });
}
async function probeMailbox(email, options) {
  const { mxHosts, from, timeoutMs, detectCatchAll = true } = options;
  if (!mxHosts.length) {
    return { accepted: null, code: null, message: "no MX host to probe", isCatchAll: null, inconclusiveReason: "no mx" };
  }
  const heloDomain = from.split("@")[1] ?? "localhost";
  let lastError = "";
  for (const host of mxHosts.slice(0, 2)) {
    let session = null;
    try {
      session = await openSession(host, timeoutMs);
      const greeting = await session.read();
      if (greeting.code !== 220) {
        lastError = `unexpected greeting ${greeting.code}`;
        session.close();
        continue;
      }
      const ehlo = await session.send(`EHLO ${heloDomain}`);
      if (ehlo.code !== 250) {
        const helo = await session.send(`HELO ${heloDomain}`);
        if (helo.code !== 250) {
          lastError = `EHLO/HELO refused (${helo.code})`;
          session.close();
          continue;
        }
      }
      const mailFrom = await session.send(`MAIL FROM:<${from}>`);
      if (mailFrom.code !== 250) {
        lastError = `MAIL FROM refused (${mailFrom.code})`;
        session.close();
        continue;
      }
      const rcpt = await session.send(`RCPT TO:<${email}>`);
      const accepted = rcpt.code >= 200 && rcpt.code < 300;
      if (!accepted && rcpt.code >= 400 && rcpt.code < 500) {
        session.close();
        return {
          accepted: null,
          code: rcpt.code,
          message: rcpt.text,
          isCatchAll: null,
          inconclusiveReason: "temporary refusal (greylisting or rate limit)"
        };
      }
      let isCatchAll = null;
      if (accepted && detectCatchAll) {
        const domain = email.slice(email.lastIndexOf("@") + 1);
        const nonce = `x-${randomBytes2(8).toString("hex")}@${domain}`;
        try {
          const probe = await session.send(`RCPT TO:<${nonce}>`);
          isCatchAll = probe.code >= 200 && probe.code < 300;
        } catch {
          isCatchAll = null;
        }
      }
      session.close();
      return { accepted, code: rcpt.code, message: rcpt.text, isCatchAll };
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      session?.close();
    }
  }
  const blocked = /ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|timeout/i.test(lastError);
  return {
    accepted: null,
    code: null,
    message: lastError,
    isCatchAll: null,
    inconclusiveReason: blocked ? "port 25 appears blocked on this network \u2014 probe cannot run here" : lastError
  };
}

// src/lib/outreach/verify/providers.ts
var HunterVerifier = class {
  constructor(apiKey) {
    this.apiKey = apiKey;
  }
  name = "hunter";
  async verify(email) {
    const url = `https://api.hunter.io/v2/email-verifier?email=${encodeURIComponent(email)}&api_key=${encodeURIComponent(this.apiKey)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`hunter responded ${response.status}`);
    const body = await response.json();
    const data = body.data ?? {};
    const status = data.status === "valid" ? "valid" : data.status === "invalid" ? "invalid" : data.status === "accept_all" || data.status === "webmail" || data.status === "disposable" ? "risky" : "unknown";
    return { status, score: data.score ?? null, isCatchAll: data.accept_all ?? null, raw: body };
  }
};
var ZeroBounceVerifier = class {
  constructor(apiKey) {
    this.apiKey = apiKey;
  }
  name = "zerobounce";
  async verify(email) {
    const url = `https://api.zerobounce.net/v2/validate?api_key=${encodeURIComponent(this.apiKey)}&email=${encodeURIComponent(email)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`zerobounce responded ${response.status}`);
    const body = await response.json();
    const status = body.status === "valid" ? "valid" : body.status === "invalid" ? "invalid" : body.status === "catch-all" || body.status === "unknown" || body.status === "do_not_mail" ? "risky" : "unknown";
    return {
      status,
      score: null,
      isCatchAll: body.status === "catch-all" ? true : null,
      raw: body
    };
  }
};
function getVerifier() {
  const provider = (process.env.VERIFIER_PROVIDER ?? "none").toLowerCase();
  const apiKey = process.env.VERIFIER_API_KEY ?? "";
  if (provider === "none" || !provider) return null;
  if (!apiKey) {
    console.warn(`VERIFIER_PROVIDER=${provider} but VERIFIER_API_KEY is empty \u2014 falling back to built-in checks.`);
    return null;
  }
  if (provider === "hunter") return new HunterVerifier(apiKey);
  if (provider === "zerobounce") return new ZeroBounceVerifier(apiKey);
  console.warn(`Unknown VERIFIER_PROVIDER "${provider}" \u2014 falling back to built-in checks.`);
  return null;
}

// src/lib/outreach/verify/index.ts
function scoreToStatus(score, hardInvalid) {
  if (hardInvalid) return "invalid";
  if (score >= 70) return "valid";
  if (score >= 40) return "risky";
  if (score > 0) return "unknown";
  return "invalid";
}
async function verifyEmail(email, options = {}) {
  const started = Date.now();
  const reasons = [];
  const base = {
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
    durationMs: 0
  };
  const finish = (outcome) => ({
    ...outcome,
    durationMs: Date.now() - started
  });
  const syntax = checkSyntax(email);
  if (!syntax.ok) {
    reasons.push(`Invalid syntax: ${syntax.reason}`);
    return finish({ ...base, status: "invalid", score: 0 });
  }
  base.syntaxOk = true;
  const domain = domainPart(email);
  if (isDisposable(domain)) {
    base.isDisposable = true;
    reasons.push("Disposable/throwaway email provider");
    return finish({ ...base, status: "invalid", score: 0 });
  }
  const lookup = await getDomainIntel(domain, options.forceDns ?? false);
  const intel = lookup.intel;
  base.mxOk = intel.mx_ok === 1;
  base.mxHosts = intel.mx_hosts ? JSON.parse(intel.mx_hosts) : [];
  if (!lookup.definitive) {
    reasons.push(lookup.error ?? "DNS lookup was inconclusive");
    reasons.push("Re-run verification for this contact before deciding");
    return finish({ ...base, status: "unknown", score: 35 });
  }
  if (!base.mxOk) {
    reasons.push("Domain has no mail server (no MX or A record) \u2014 mail cannot be delivered");
    return finish({ ...base, status: "invalid", score: 0 });
  }
  reasons.push(`Domain accepts mail (${base.mxHosts.length} MX host${base.mxHosts.length === 1 ? "" : "s"})`);
  let score = 75;
  if (isRoleAccount(email)) {
    base.isRole = true;
    score -= 20;
    reasons.push("Shared role mailbox rather than a named person \u2014 lower reply odds");
  }
  if (isFreeProvider(domain)) {
    base.isFreeProvider = true;
    score -= 10;
    reasons.push("Consumer mail provider rather than a company domain");
  }
  if (intel.is_catch_all === 1) {
    base.isCatchAll = true;
    score = Math.min(score, 55);
    reasons.push("Domain is catch-all \u2014 it accepts any address, so existence cannot be confirmed");
  }
  const probeEnabled = (process.env.SMTP_PROBE_ENABLED ?? "false").toLowerCase() === "true";
  if (probeEnabled && base.mxHosts.length) {
    const probe = await probeMailbox(email, {
      mxHosts: base.mxHosts,
      from: process.env.SMTP_PROBE_FROM ?? "verify@hexera.so",
      timeoutMs: Number(process.env.SMTP_PROBE_TIMEOUT_MS ?? 8e3),
      detectCatchAll: intel.is_catch_all === null
    });
    base.smtpChecked = true;
    base.smtpCode = probe.code;
    base.smtpMessage = probe.message?.slice(0, 500) ?? null;
    if (probe.isCatchAll !== null) {
      setCatchAll(domain, probe.isCatchAll);
      if (probe.isCatchAll) {
        base.isCatchAll = true;
        score = Math.min(score, 55);
        reasons.push("Domain is catch-all \u2014 it accepts any address, so existence cannot be confirmed");
      }
    }
    if (probe.accepted === true && !base.isCatchAll) {
      score = 95;
      reasons.push(`Mailbox confirmed by the mail server (SMTP ${probe.code})`);
    } else if (probe.accepted === false) {
      reasons.push(`Mail server rejected this recipient (SMTP ${probe.code}) \u2014 the mailbox does not exist`);
      return finish({ ...base, status: "invalid", score: 0 });
    } else if (probe.inconclusiveReason) {
      reasons.push(`SMTP probe inconclusive: ${probe.inconclusiveReason}`);
    }
  } else if (!probeEnabled) {
    reasons.push("SMTP probe disabled \u2014 verdict is based on domain health, not mailbox existence");
  }
  const external = getVerifier();
  if (external) {
    try {
      const verdict = await external.verify(email);
      base.provider = external.name;
      if (verdict.isCatchAll !== null) {
        setCatchAll(domain, verdict.isCatchAll);
        base.isCatchAll = verdict.isCatchAll;
      }
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
async function saveVerification(contactId, outcome) {
  await transaction(async (client2) => {
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
        nowIso()
      ],
      client2
    );
    recordEvent({
      type: EVENT_TYPES.verificationChecked,
      entityType: "contact",
      entityId: contactId,
      contactId,
      payload: { status: outcome.status, score: outcome.score, provider: outcome.provider }
    });
  });
}
async function verifyAllContacts(options = {}) {
  const { force = false, limit, concurrency = 6, onProgress } = options;
  const sql = force ? `SELECT * FROM contacts WHERE email_normalized IS NOT NULL ORDER BY id` : `SELECT c.* FROM contacts c
       LEFT JOIN latest_verification v ON v.contact_id = c.id
       WHERE c.email_normalized IS NOT NULL AND v.id IS NULL
       ORDER BY c.id`;
  const contacts = await all(limit ? `${sql} LIMIT ?` : sql, limit ? [limit] : []);
  const byStatus = {};
  let done = 0;
  let cursor = 0;
  const workers = Array.from({ length: Math.min(concurrency, contacts.length || 1) }, async () => {
    for (; ; ) {
      const index = cursor++;
      if (index >= contacts.length) return;
      const contact = contacts[index];
      try {
        const outcome = await verifyEmail(contact.email_normalized, { forceDns: force });
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
          payload: { error: error instanceof Error ? error.message : String(error) }
        });
      }
    }
  });
  await Promise.all(workers);
  return { checked: done, byStatus };
}

// worker/outreach-worker.ts
function log(message) {
  console.log(`[${(/* @__PURE__ */ new Date()).toISOString()}] ${message}`);
}
async function tick() {
  const summary = {
    completed: 0,
    failed: 0,
    held: 0,
    previewed: 0,
    repliesFound: 0,
    sent: 0,
    skipped: 0,
    stopped: 0,
    verified: 0
  };
  try {
    const verification = await verifyAllContacts({ limit: 25 });
    summary.verified = verification.checked;
    if (verification.checked > 0) {
      log(`verified ${verification.checked} contact(s): ${JSON.stringify(verification.byStatus)}`);
    }
  } catch (error) {
    log(`verification error: ${error instanceof Error ? error.message : String(error)}`);
  }
  const dispatch = await runDispatch({ trigger: "automatic" });
  Object.assign(summary, {
    completed: dispatch.completed,
    failed: dispatch.failed,
    held: dispatch.held,
    previewed: dispatch.previewed,
    sent: dispatch.sent,
    skipped: dispatch.skipped,
    stopped: dispatch.stopped
  });
  if (dispatch.attempted > 0) {
    log(
      `dispatched ${dispatch.attempted}: ${dispatch.sent} sent, ${dispatch.previewed} rehearsed, ${dispatch.stopped} stopped, ${dispatch.failed} failed`
    );
  }
  if (dispatch.held > 0) log(`${dispatch.held} due but holding: ${dispatch.heldReason}`);
  for (const error of dispatch.errors) log(`  FAILED ${error}`);
  if ((await storedToken())?.refresh_token) {
    try {
      const replies = await pollReplies();
      summary.repliesFound = replies.newReplies;
      if (replies.newReplies > 0) {
        log(
          `${replies.newReplies} new reply(ies): ${JSON.stringify(replies.byClassification)} - ${replies.stopped} sequence(s) stopped, ${replies.suppressed} suppression(s) added`
        );
      }
      for (const error of replies.errors) log(`  reply error: ${error}`);
    } catch (error) {
      log(`reply polling error: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  await recordEvent({ entityType: "worker", payload: summary, type: EVENT_TYPES.workerTick });
  return summary;
}
async function main() {
  await seedDefaultSettings();
  const mode = await sendMode();
  const capacity = await capacitySnapshot();
  log(`outreach tick - ${mode.headline}`);
  log(`  dispatch: ${await dispatchMode()}; used today: ${capacity.globalUsed}`);
  if (!mode.live) log(`  REHEARSAL: ${mode.detail}`);
  try {
    const summary = await tick();
    log(`tick complete: ${JSON.stringify(summary)}`);
  } finally {
    await closeDb();
  }
}
main().catch((error) => {
  log(`tick FAILED: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
  process.exitCode = 1;
});
