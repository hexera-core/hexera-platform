/**
 * Apollo, for finding the right people and their real addresses.
 *
 * Two calls, and the split matters:
 *
 *   1. Search    POST /api/v1/mixed_people/api_search
 *                Free. Filters by job title and company domain. Returns who
 *                exists and their Apollo id, and deliberately returns NO
 *                email address.
 *
 *   2. Enrich    POST /api/v1/people/bulk_match
 *                Costs credits, ten people per call. Returns the address and
 *                an `email_status` saying whether Apollo verified it.
 *
 * So searching widely is free and only revealing addresses costs anything,
 * which is the right shape: cast a wide net on titles, pay only for the
 * people you actually want to write to.
 *
 * The rule this module enforces, and the reason it exists: nothing is
 * accepted unless `email_status` is "verified". Apollo will happily return
 * addresses it has guessed from a name pattern, which is exactly what the
 * first list was full of and exactly what hard-bounced twice. A guessed
 * address is not a cheaper verified address, it is a liability that costs
 * sending reputation to discover.
 */

const BASE = "https://api.apollo.io/api/v1";

export interface ApolloPerson {
  id: string;
  name: string;
  firstName: string | null;
  lastName: string | null;
  title: string | null;
  organization: string | null;
  domain: string | null;
  linkedinUrl: string | null;
}

export interface EnrichedPerson extends ApolloPerson {
  email: string;
  /** Apollo's own word for it. Only "verified" is ever accepted downstream. */
  emailStatus: string;
}

export function apolloKey(): string | null {
  const key = process.env.APOLLO_API_KEY?.trim();
  return key ? key : null;
}

async function post(path: string, body: unknown): Promise<Record<string, unknown>> {
  const key = apolloKey();
  if (!key) throw new Error("APOLLO_API_KEY is not set in .env.local");

  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      accept: "application/json",
      // Apollo takes the key as a header, never a query parameter, which
      // keeps it out of any request log along the way.
      "x-api-key": key,
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");

    // Apollo gates the API behind a paid plan, separately from credits. The
    // docs say search costs zero credits, which is true and misleading: on
    // the free tier it returns 403 before any credit question arises. Worth
    // saying plainly rather than as a stack trace, because the fix is a
    // billing decision rather than anything in this code.
    if (response.status === 403 && detail.includes("API_INACCESSIBLE")) {
      throw new Error(
        `Apollo refused the request: the API is not part of your current plan.\n` +
          `  Zero credits and free are different things here. Every endpoint,\n` +
          `  search included, needs a paid plan. See https://www.apollo.io/pricing\n` +
          `  Nothing was spent and nothing was written.`,
      );
    }

    if (response.status === 401) {
      throw new Error("Apollo rejected the key. Check APOLLO_API_KEY in .env.local.");
    }
    if (response.status === 429) {
      throw new Error("Apollo rate limit hit. Wait a minute and run it again.");
    }

    throw new Error(`Apollo ${path} responded ${response.status}: ${detail.slice(0, 300)}`);
  }
  return (await response.json()) as Record<string, unknown>;
}

export interface SearchOptions {
  /** Omit entirely to match any title: the last resort for companies so
   *  small that no title filter survives contact with their org chart. */
  titles?: string[];
  /** Company domains, without "www." or "@". Apollo caps this at 1000. */
  domains?: string[];
  locations?: string[];
  page?: number;
  perPage?: number;
  /** Loose title matching. Off by default: "Engineer" should not return sales. */
  includeSimilarTitles?: boolean;
}

export interface SearchResult {
  people: ApolloPerson[];
  page: number;
  totalPages: number;
  totalEntries: number;
}

/** Free. Returns who exists, with no addresses. */
export async function searchPeople(options: SearchOptions): Promise<SearchResult> {
  const body: Record<string, unknown> = {
    page: options.page ?? 1,
    per_page: Math.min(options.perPage ?? 100, 100),
    // Ask Apollo up front for people it believes it has a verified address
    // for. It is a hint rather than a guarantee, so the enrichment step
    // checks again rather than trusting this.
    contact_email_status: ["verified"],
  };
  if (options.titles?.length) {
    body.person_titles = options.titles;
    body.include_similar_titles = options.includeSimilarTitles ?? false;
  }
  if (options.domains?.length) body.q_organization_domains_list = options.domains.slice(0, 1000);
  if (options.locations?.length) body.organization_locations = options.locations;

  const json = await post("/mixed_people/api_search", body);
  const raw = (json.people ?? json.contacts ?? []) as Record<string, unknown>[];
  const pagination = (json.pagination ?? {}) as Record<string, number>;

  return {
    people: raw.map(toPerson),
    page: pagination.page ?? 1,
    totalPages: pagination.total_pages ?? 1,
    totalEntries: pagination.total_entries ?? raw.length,
  };
}

/**
 * Costs credits. Ten per call, so callers batch.
 *
 * `reveal_personal_emails` is false on purpose. Personal addresses cost more,
 * and a cold pitch sent to somebody's private Gmail about their day job is
 * the kind of thing that gets a company blocked rather than a reply.
 */
export async function enrichPeople(ids: string[]): Promise<EnrichedPerson[]> {
  const out: EnrichedPerson[] = [];

  for (let i = 0; i < ids.length; i += 10) {
    const json = await post("/people/bulk_match", {
      reveal_personal_emails: false,
      reveal_phone_number: false,
      details: ids.slice(i, i + 10).map((id) => ({ id })),
    });

    for (const row of ((json.matches ?? []) as (Record<string, unknown> | null)[])) {
      if (!row) continue;
      const email = typeof row.email === "string" ? row.email : null;
      if (!email) continue;
      out.push({
        ...toPerson(row),
        email: email.toLowerCase(),
        emailStatus: String(row.email_status ?? "unknown"),
      });
    }
  }

  return out;
}

/** The gate. Everything else in this file exists to feed it. */
export function isUsable(person: EnrichedPerson): boolean {
  return person.emailStatus === "verified";
}

function toPerson(row: Record<string, unknown>): ApolloPerson {
  const org = (row.organization ?? {}) as Record<string, unknown>;
  return {
    id: String(row.id ?? ""),
    name: String(row.name ?? `${row.first_name ?? ""} ${row.last_name ?? ""}`.trim()),
    firstName: (row.first_name as string) ?? null,
    lastName: (row.last_name as string) ?? null,
    title: (row.title as string) ?? null,
    organization: (org.name as string) ?? (row.organization_name as string) ?? null,
    domain:
      (org.primary_domain as string) ??
      (org.website_url as string)?.replace(/^https?:\/\/(www\.)?/, "").replace(/\/.*$/, "") ??
      null,
    linkedinUrl: (row.linkedin_url as string) ?? null,
  };
}
