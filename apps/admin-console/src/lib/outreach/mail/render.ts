/**
 * Merge-tag rendering.
 *
 * The governing rule: an unresolved tag is a hard error, never a silent
 * empty string. "Hi ," in a cold email to a VP of Aerodynamics is worse than
 * not sending at all, and a renderer that quietly swallows a missing value is
 * how that happens.
 *
 * Syntax:
 *   {{first_name}}          required — throws if missing
 *   {{first_name|there}}    optional — falls back to "there"
 */
import type { Contact } from "../core/types";

export class RenderError extends Error {
  constructor(
    message: string,
    public readonly missingTags: string[],
  ) {
    super(message);
    this.name = "RenderError";
  }
}

export type MergeValues = Record<string, string | null | undefined>;

const TAG_PATTERN = /\{\{\s*([a-z0-9_]+)\s*(?:\|([^}]*))?\}\}/gi;

export function buildMergeValues(
  contact: Contact,
  extra: MergeValues = {},
): MergeValues {
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
    ...extra,
  };
}

export interface RenderResult {
  text: string;
  usedTags: string[];
  usedDefaults: string[];
}

export function render(template: string, values: MergeValues): RenderResult {
  const missing: string[] = [];
  const usedTags: string[] = [];
  const usedDefaults: string[] = [];

  const text = template.replace(TAG_PATTERN, (_whole, rawName: string, fallback?: string) => {
    const name = rawName.toLowerCase();
    const value = values[name];
    const present = value !== null && value !== undefined && String(value).trim() !== "";

    if (present) {
      usedTags.push(name);
      return String(value).trim();
    }
    if (fallback !== undefined) {
      usedDefaults.push(name);
      return fallback.trim();
    }
    missing.push(name);
    return "";
  });

  if (missing.length) {
    const unique = [...new Set(missing)];
    throw new RenderError(
      `Template has unresolved merge tag(s): ${unique.map((t) => `{{${t}}}`).join(", ")}. ` +
        `Give the contact a value, or add a fallback like {{${unique[0]}|there}}.`,
      unique,
    );
  }

  return { text, usedTags: [...new Set(usedTags)], usedDefaults: [...new Set(usedDefaults)] };
}

/** Every tag a template references, for the editor's live validation. */
export function extractTags(template: string): { name: string; hasFallback: boolean }[] {
  const out = new Map<string, boolean>();
  for (const match of template.matchAll(TAG_PATTERN)) {
    const name = match[1].toLowerCase();
    out.set(name, out.get(name) || match[2] !== undefined);
  }
  return [...out.entries()].map(([name, hasFallback]) => ({ name, hasFallback }));
}

export const KNOWN_TAGS = [
  "first_name", "full_name", "company", "role", "industry", "tier",
  "stage", "email", "domain", "priority", "signature", "unsubscribe",
] as const;

export interface RenderedEmail {
  subject: string;
  body: string;
  usedDefaults: string[];
}

/**
 * Renders subject and body together so a failure in either aborts the whole
 * message rather than producing a half-personalized email.
 */
export function renderEmail(
  subject: string,
  body: string,
  values: MergeValues,
): RenderedEmail {
  const renderedSubject = render(subject, values);
  const renderedBody = render(body, values);
  return {
    subject: collapseWhitespace(renderedSubject.text),
    body: renderedBody.text,
    usedDefaults: [...new Set([...renderedSubject.usedDefaults, ...renderedBody.usedDefaults])],
  };
}

/** Subject lines must be a single line — a stray newline truncates the header. */
function collapseWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}
