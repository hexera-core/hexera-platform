import { all } from "../db";

// IMPORTED, NOT READ FROM DISK. These three sat beside the code as `lib/prospect/*.json` and were
// loaded with readFileSync against process.cwd(). That is a laptop assumption: the hosted console
// runs from Next's standalone output, where cwd is not the repository root and a relative data
// path resolves to nothing - the Partners page would have thrown on its first read in prod.
// Importing them makes the bundler responsible for shipping them, which it can verify at build
// time rather than at 3am.
import companiesYc from "./companies.yc.json";
import peopleEnriched from "./people.enriched.json";
import peopleFound from "./people.found.json";

/**
 * The enrichment backlog, derived live rather than stored.
 *
 * A company is "in inventory" when it has surveyed people in
 * people.found.json but no kept record in people.enriched.json — the exact
 * rule the workbook's "To enrich" tab is built from, so the website and the
 * sheet can never drift apart. Nothing maintains this list: the moment an
 * enrichment wave writes a kept record, the company vanishes from here and
 * its people appear on the board and in the People view, because both are
 * just projections of the same two files plus the DB.
 */

export interface InventoryPerson {
  name: string;
  title: string;
  /** Has an Apollo id, so a reveal costs one credit and no re-survey. */
  hasId: boolean;
}

export interface InventoryCompany {
  company: string;
  industry: string;
  segment: string;
  isYc: boolean;
  /** YC batch, e.g. "W21", when known. */
  batch: string | null;
  domain: string | null;
  people: InventoryPerson[];
  withIds: number;
  /**
   * A wave already ran on this company and kept nothing: every email came
   * back unverified or org-mismatched. Its Apollo ids are spent, so it must
   * not blend back into the backlog — planning the next wave from "awaiting
   * credits" rows would silently pay twice for a known-dead reveal.
   */
  attempted: boolean;
  note: string;
}

/** Segment key -> the label the workbook uses for the same section. */
const SEGMENT_LABEL: Record<string, string> = {
  yc: "YC companies",
  "aero-startups": "Aerospace & defence",
  "auto-motorsport": "Automotive, EV & motorsport",
  semiconductors: "Semiconductors",
  biomedical: "Medical devices & healthtech",
  "hvac-thermal": "HVAC, data-centre cooling & thermal",
  "turbomachinery-energy": "Energy, turbomachinery & cleantech",
  marine: "Marine, naval & offshore",
  "cfd-consultancies": "CFD & simulation consultancies",
};

interface FoundPerson {
  id?: string;
  name: string;
  title: string;
  domain?: string;
}

/** Company-name key: case-, accent- and punctuation-insensitive. */
function norm(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

export async function inventory(): Promise<{
  rows: InventoryCompany[];
  totals: {
    companies: number;
    people: number;
    withIds: number;
    yc: number;
    nonYc: number;
    /** Waves that ran and kept nothing; excluded from nextWave. */
    attempted: number;
    /** What the next paid wave can actually be planned from. */
    nextWave: number;
  };
}> {
  const found = peopleFound as Record<string, Record<string, FoundPerson[]>>;

  // The old code tolerated this file being absent, meaning "the first wave has not run yet". As an
  // import it is present or the build fails, so the tolerance has moved to where it now belongs:
  // an empty object still means everything surveyed is inventory, which is the correct reading.
  const enriched = peopleEnriched as Record<string, { kept?: boolean }[]>;

  const done = new Set<string>();
  const attempted = new Set<string>();
  for (const [company, records] of Object.entries(enriched)) {
    if (records.some((r) => r.kept)) done.add(norm(company));
    else attempted.add(norm(company));
  }

  // Belt over the file: any company the DB already holds a verified email
  // for is on the board, so it cannot also be inventory. A no-op when the
  // JSON is current; it heals the window where an enrichment wave has
  // written contacts but crashed before rewriting people.enriched.json.
  for (const row of await all<{ company: string }>(
    `SELECT DISTINCT company FROM contacts WHERE email_normalized IS NOT NULL`,
  )) {
    done.add(norm(row.company));
  }

  const yc = companiesYc as { batches: Record<string, string> };
  const batches = new Map(Object.entries(yc.batches).map(([k, v]) => [k.toLowerCase(), v]));

  const rows: InventoryCompany[] = [];
  for (const [segment, companies] of Object.entries(found)) {
    const industry = SEGMENT_LABEL[segment] ?? segment;
    const isYc = segment === "yc";
    for (const [company, people] of Object.entries(companies)) {
      if (!people.length || done.has(norm(company))) continue;
      const withIds = people.filter((p) => p.id).length;
      const batch = isYc ? (batches.get(company.toLowerCase()) ?? null) : null;
      const wasAttempted = attempted.has(norm(company));
      rows.push({
        company,
        industry,
        segment,
        isYc,
        batch,
        domain: people.find((p) => p.domain)?.domain ?? null,
        people: people.map((p) => ({ name: p.name, title: p.title, hasId: Boolean(p.id) })),
        withIds,
        attempted: wasAttempted,
        note: wasAttempted ? "wave spent, nothing kept" : "awaiting verification",
      });
    }
  }

  // Same order as the workbook tab: by section, most Apollo-ready first,
  // then name. "YC companies" sorts last alphabetically, which also matches
  // how the backlog is worked: cold segments first, YC stays parked.
  rows.sort(
    (a, b) =>
      a.industry.localeCompare(b.industry) ||
      b.withIds - a.withIds ||
      a.company.toLowerCase().localeCompare(b.company.toLowerCase()),
  );

  const ycCount = rows.filter((r) => r.isYc).length;
  const attemptedCount = rows.filter((r) => r.attempted).length;
  return {
    rows,
    totals: {
      companies: rows.length,
      people: rows.reduce((n, r) => n + r.people.length, 0),
      withIds: rows.reduce((n, r) => n + r.withIds, 0),
      yc: ycCount,
      nonYc: rows.length - ycCount,
      attempted: attemptedCount,
      nextWave: rows.filter((r) => !r.isYc && !r.attempted).length,
    },
  };
}
