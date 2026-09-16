/**
 * Spreadsheet → contacts table.
 *
 * Idempotent: re-running updates existing rows rather than duplicating them,
 * keyed on the normalized email (or on sheet + row id when there is no email).
 * That matters because the source spreadsheet is a living document — rows get
 * emails filled in over time, and re-importing must not orphan the outreach
 * history already attached to a contact.
 */
import { readXlsx, sheetToObjects, type Cell } from "./xlsx";
import { get, run, transaction } from "../db";
import { nowIso } from "../core/time";
import { recordEvent, EVENT_TYPES } from "../core/events";
import type { NameQuality, Priority } from "../core/types";
import { roleGroup } from "../core/roles";

/**
 * Words that indicate the "Name" cell holds a job title rather than a person.
 * The spreadsheet legitimately contains rows like "CEO", "Co-Founders" and
 * "Ross (Co-Founder)" where the real name was never sourced.
 */
const ROLE_WORDS = new Set([
  "ceo", "cto", "coo", "cfo", "cmo", "cro", "founder", "founders", "cofounder",
  "cofounders", "co-founder", "co-founders", "president", "vp", "svp", "evp",
  "head", "chief", "director", "lead", "manager", "owner", "principal",
  "engineer", "engineering", "team", "contact", "info", "unknown", "tbd", "n/a",
]);

const HONORIFICS = new Set(["dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "prof", "prof."]);

function text(value: Cell): string | null {
  if (value === null || value === undefined) return null;
  const s = String(value).trim();
  return s.length ? s : null;
}

/**
 * The source file was written with a mix of encodings, so a few em-dashes and
 * smart quotes arrive as mojibake. Repair the sequences we actually see rather
 * than attempting a general re-decode.
 */
function repairEncoding(value: string): string {
  return value
    .replace(/â€"/g, "·")
    .replace(/â€"/g, "·")
    .replace(/â€™/g, "’")
    .replace(/â€œ/g, "“")
    .replace(/â€/g, "”")
    .replace(/Â/g, "")
    .replace(/�/g, "·")
    // Any em or en dash already in the source becomes a middot too, so no
    // rendered string in the app contains one.
    .replace(/\s*[–—]\s*/g, " · ")
    .replace(/\s+/g, " ")
    .trim();
}

function clean(value: Cell): string | null {
  const raw = text(value);
  return raw === null ? null : repairEncoding(raw) || null;
}

export function normalizeEmail(raw: string | null): string | null {
  if (!raw) return null;
  const trimmed = raw.trim().toLowerCase();
  // A few cells carry a note alongside the address; take the address-looking part.
  const match = /[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}/.exec(trimmed);
  return match ? match[0] : null;
}

export function emailDomain(email: string | null): string | null {
  if (!email) return null;
  const at = email.lastIndexOf("@");
  return at === -1 ? null : email.slice(at + 1) || null;
}

export interface ParsedName {
  firstName: string | null;
  quality: NameQuality;
}

/**
 * Decide whether the Name cell can be used in a greeting.
 *
 * A false "person" here produces "Hi CEO," in a cold email to a company we
 * want as a design partner, so this leans hard toward flagging as placeholder
 * when uncertain — a flagged contact is simply held back for a human, which
 * costs nothing.
 */
export function parseName(fullName: string | null): ParsedName {
  if (!fullName) return { firstName: null, quality: "placeholder" };

  // "Ross (Co-Founder)" -> "Ross";  "CEO/CTO" -> "CEO CTO"
  const stripped = fullName
    .replace(/\([^)]*\)/g, " ")
    .replace(/[\/,]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  if (!stripped) return { firstName: null, quality: "placeholder" };

  const tokens = stripped.split(" ").filter((t) => !HONORIFICS.has(t.toLowerCase()));
  if (!tokens.length) return { firstName: null, quality: "placeholder" };

  const first = tokens[0];
  // Unicode-aware: this list is full of international founders, and an
  // ASCII-only strip turns "Frédéric" into "Frdric" — which is worse in a
  // greeting than not personalizing at all.
  const bare = first.replace(/[^\p{L}\-']/gu, "");

  if (!bare) return { firstName: null, quality: "placeholder" };
  if (ROLE_WORDS.has(bare.toLowerCase())) return { firstName: null, quality: "placeholder" };
  // A single character is an initial, not a name. Two-letter names ("AJ", "JB")
  // are real and common enough here to keep.
  if (bare.length < 2) return { firstName: null, quality: "placeholder" };

  // Preserve intentional casing like "AJ" or "McDonald"; only fix all-lowercase.
  const cased = bare === bare.toLowerCase() ? bare[0].toUpperCase() + bare.slice(1) : bare;
  return { firstName: cased, quality: "person" };
}

function normalizePriority(value: string | null): Priority {
  if (!value) return null;
  const upper = value.trim().toUpperCase();
  return upper === "HOT" || upper === "WARM" ? upper : null;
}

export interface IngestResult {
  inserted: number;
  updated: number;
  skipped: number;
  duplicateEmails: string[];
  bySheet: Record<string, number>;
  withoutEmail: number;
  placeholderNames: number;
}

interface ExistingRow {
  id: number;
  email_normalized: string | null;
}

export async function ingestWorkbook(filePath: string): Promise<IngestResult> {
  const sheets = readXlsx(filePath);
  const result: IngestResult = {
    inserted: 0,
    updated: 0,
    skipped: 0,
    duplicateEmails: [],
    bySheet: {},
    withoutEmail: 0,
    placeholderNames: 0,
  };

  // Tracks emails seen during THIS import so a contact duplicated across the
  // two sheets is reported rather than silently written twice.
  const seenEmails = new Map<string, string>();

  await transaction(async (client) => {
    for (const sheet of sheets) {
      const rows = sheetToObjects(sheet);
      result.bySheet[sheet.name] = rows.length;

      for (const row of rows) {
        const company = clean(row["Company"]);
        if (!company) {
          result.skipped++;
          continue;
        }

        const fullName = clean(row["Name"]);
        const email = normalizeEmail(text(row["Email"]));
        const domain = emailDomain(email);
        const { firstName, quality } = parseName(fullName);
        const sourceRowId = typeof row["ID"] === "number" ? row["ID"] : null;

        if (!email) result.withoutEmail++;
        if (quality === "placeholder") result.placeholderNames++;

        if (email) {
          const previousSheet = seenEmails.get(email);
          if (previousSheet) {
            result.duplicateEmails.push(`${email} (${previousSheet} + ${sheet.name})`);
          } else {
            seenEmails.set(email, sheet.name);
          }
        }

        // Match on email first; fall back to sheet+row identity for the
        // email-less rows so they stay stable across re-imports.
        const existing = email
          ? await get<ExistingRow>(`SELECT id, email_normalized FROM contacts WHERE email_normalized = ?`, [email], client)
          : await get<ExistingRow>(
              `SELECT id, email_normalized FROM contacts
               WHERE source_sheet = ? AND source_row_id IS ? AND email_normalized IS NULL`,
              [sheet.name, sourceRowId], client);

        const now = nowIso();
        const role = clean(row["Role"]);
        const values = [
          sheet.name,
          sourceRowId,
          company,
          fullName,
          firstName,
          quality,
          role,
          roleGroup(role),
          clean(row["Industry"]),
          clean(row["Tier"]),
          clean(row["Stage"]),
          text(row["Email"]),
          email,
          domain,
          clean(row["LinkedIn"]),
          normalizePriority(clean(row["Priority"])),
          clean(row["Outreach Channel"]),
        ];

        if (existing) {
          await run(
            `UPDATE contacts SET
               source_sheet = ?, source_row_id = ?, company = ?, full_name = ?, first_name = ?,
               name_quality = ?, role = ?, role_group = ?, industry = ?, tier = ?, stage = ?, email = ?,
               email_normalized = ?, domain = ?, linkedin = ?, priority = ?, outreach_channel = ?,
               updated_at = ?
             WHERE id = ?`,
            [...values, now, existing.id], client);
          result.updated++;
        } else {
          const inserted = await run(
            `INSERT INTO contacts (
               source_sheet, source_row_id, company, full_name, first_name, name_quality,
               role, role_group, industry, tier, stage, email, email_normalized, domain, linkedin,
               priority, outreach_channel, created_at, updated_at
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
            [...values, now, now], client);
          result.inserted++;
          await recordEvent({
            type: EVENT_TYPES.contactImported,
            entityType: "contact",
            entityId: inserted.lastInsertRowid,
            contactId: inserted.lastInsertRowid,
            payload: { company, email, sheet: sheet.name },
          });
        }
      }
    }
  });

  return result;
}
