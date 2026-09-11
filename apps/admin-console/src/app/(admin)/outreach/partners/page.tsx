import Link from "next/link";
import {
  partnersByIndustry,
  allPeopleByCompany,
  contactRows,
  filterOptions,
  campaignSummaries,
} from "@/lib/outreach/analytics/queries";
import { companyScores, readiness } from "@/lib/outreach/analytics/ranking";
import { PartnerGrid } from "@/app/_components/outreach/partner-grid";
import { EnrollPicker, type EnrollRow } from "@/app/_components/outreach/enroll-picker";
import { Panel, PageHeader, Field } from "@/app/_components/outreach/ui";
import { enrollSelected } from "@/app/(admin)/outreach/actions";
import { getNumberSetting, getBoolSetting, SETTING_KEYS } from "@/lib/outreach/core/settings";
import { inventory, type InventoryCompany } from "@/lib/outreach/prospect/inventory";

export const dynamic = "force-dynamic";

/**
 * Companies and people, one page, two lenses.
 *
 * The company view answers "which companies do we have, and where do we stand
 * with each one". The people view answers "what is the state of this person,
 * and who do I enroll next". They used to be separate pages, which meant the
 * question you were actually asking always lived one navigation away from the
 * answer you were looking at. Now it is one toggle, and both lenses share the
 * page without either UI giving anything up.
 */
export default async function PartnersPage({
  searchParams,
}: {
  // Next 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const one = (key: string) => {
    const value = params[key];
    return typeof value === "string" && value.length ? value : undefined;
  };
  const raw = one("view");
  const view = raw === "people" ? "people" : raw === "inventory" ? "inventory" : "companies";

  const DESCRIPTIONS = {
    companies: "Every company with a verified email on the board. Click any box for the full record; switch to People to enroll.",
    people: "Every person with a verified email, and the exact reason anything is held back. Tick and enroll from here.",
    inventory:
      "Surveyed but not yet enriched. These stay off the board and out of the enroll pool until a wave verifies their emails, then they move over on their own.",
  } as const;

  return (
    <>
      <PageHeader title="Partners & contacts" description={DESCRIPTIONS[view]} />

      {/* The lens toggle. Links rather than state, so each view is a URL you
          can bookmark, share, or land on from anywhere else in the app. */}
      <div className="flex gap-1.5 mb-5">
        {(
          [
            ["companies", "Companies"],
            ["people", "People"],
            ["inventory", "Inventory"],
          ] as const
        ).map(([key, label]) => (
          <Link
            key={key}
            href={key === "companies" ? "/partners" : `/partners?view=${key}`}
            className="t-label px-3 py-1.5 rounded-sm transition-colors"
            style={{
              background: view === key ? "rgba(255,79,0,0.12)" : "rgba(238,235,225,0.04)",
              color: view === key ? "var(--color-accent-soft)" : "var(--color-muted)",
            }}
          >
            {label}
          </Link>
        ))}
      </div>

      {view === "companies" ? (
        <CompaniesView />
      ) : view === "people" ? (
        <PeopleView params={params} one={one} />
      ) : (
        <InventoryView one={one} />
      )}
    </>
  );
}

// ── Lens 1: the board ─────────────────────────────────────────────────────

async function CompaniesView() {
  const industries = partnersByIndustry();
  const people = await allPeopleByCompany();
  const companies = (await industries).flatMap((i) => i.companies);

  const ranking = readiness();
  const scores = Object.fromEntries(await companyScores());

  const totals = companies.reduce(
    (acc, c) => ({
      people: acc.people + c.people,
      verified: acc.verified + c.verifiedEmails,
      liveSent: acc.liveSent + c.liveSent,
      positive: acc.positive + c.positive,
      yc: acc.yc + (c.isYc ? 1 : 0),
    }),
    { people: 0, verified: 0, liveSent: 0, positive: 0, yc: 0 },
  );
  const backlog = (await inventory()).totals.companies;

  return (
    <>
      <div className="flex flex-wrap gap-x-8 gap-y-2 mb-7 pb-5 border-b border-[var(--color-line)]">
        <span className="env-stat">
          companies <b>{companies.length}</b>
        </span>
        <span className="env-stat">
          verified ✉ <b>{totals.verified}</b>
        </span>
        <span className="env-stat">
          YC <b>{totals.yc}</b>
        </span>
        <Link href="/partners?view=inventory" className="env-stat hover:text-[var(--color-ink)] transition-colors">
          in inventory <b>{backlog}</b> →
        </Link>
        <span className="env-stat">
          delivered <b>{totals.liveSent}</b>
        </span>
        <span className="env-stat">
          interested <b>{totals.positive}</b>
        </span>
      </div>

      <PartnerGrid
        companies={companies}
        people={people}
        industries={(await industries).map((i) => i.industry)}
        scores={scores}
        rankingReady={(await ranking).ready}
        rankingNote={(await ranking).message}
      />
    </>
  );
}

// ── Lens 3: the enrichment inventory ──────────────────────────────────────
//
// Companies we have surveyed (people found, titles known, Apollo ids in
// hand) but not yet paid to reveal. Derived live from the same files the
// workbook's "To enrich" tab is built from, so this view and the sheet
// always agree, and a company disappears from here the moment a wave
// enriches it — no bookkeeping, anywhere.

async function InventoryView({ one }: { one: (key: string) => string | undefined }) {
  const inv = await inventory();
  const q = one("q")?.toLowerCase();
  const industryFilter = one("industry");
  const kind = one("kind");

  const rows = (await inv).rows.filter((r) => {
    if (q && !(r.company.toLowerCase().includes(q) || r.people.some((p) => p.name.toLowerCase().includes(q)))) return false;
    if (industryFilter && r.industry !== industryFilter) return false;
    if (kind === "yc" && !r.isYc) return false;
    if (kind === "cold" && r.isYc) return false;
    return true;
  });

  const industries = [...new Set(inv.rows.map((r) => r.industry))];

  return (
    <>
      <div className="flex flex-wrap gap-x-8 gap-y-2 mb-7 pb-5 border-b border-[var(--color-line)]">
        <span className="env-stat">
          companies awaiting <b>{(await inv).totals.companies}</b>
        </span>
        <span className="env-stat">
          people already found <b>{(await inv).totals.people}</b>
        </span>
        <span className="env-stat">
          apollo-ready <b>{(await inv).totals.withIds}</b>
        </span>
        <span className="env-stat">
          awaiting verification <b>{(await inv).totals.nextWave}</b>
        </span>
        <span className="env-stat">
          YC <b>{(await inv).totals.yc}</b>
        </span>
        {(await inv).totals.attempted > 0 && (
          <span className="env-stat" style={{ color: "var(--color-crimson-soft)" }}>
            wave spent, nothing kept <b>{(await inv).totals.attempted}</b>
          </span>
        )}
      </div>

      <form method="get" className="panel p-4 mb-4 grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(180px, 100%), 1fr))" }}>
        <input type="hidden" name="view" value="inventory" />
        <Field label="Search">
          <input name="q" defaultValue={one("q") ?? ""} placeholder="company or person" className="input" />
        </Field>
        <Field label="Industry">
          <select name="industry" defaultValue={industryFilter ?? ""} className="select">
            <option value="">All</option>
            {industries.map((i) => (
              <option key={i} value={i}>{i}</option>
            ))}
          </select>
        </Field>
        <Field label="Kind">
          <select name="kind" defaultValue={kind ?? ""} className="select">
            <option value="">Everything</option>
            <option value="cold">Non-YC</option>
            <option value="yc">YC</option>
          </select>
        </Field>
        <div className="flex items-end gap-2">
          <button type="submit" className="btn btn-primary">Apply</button>
          <Link href="/partners?view=inventory" className="btn">Reset</Link>
        </div>
      </form>

      {rows.length === 0 ? (
        <p className="t-label py-8 text-center">
          {(await inv).rows.length === 0
            ? "Inventory is empty: everything surveyed has been enriched."
            : "Nothing matches these filters."}
        </p>
      ) : (
        <div className="scroll-x">
          <table className="tbl">
            <thead>
              <tr>
                <th>Company</th>
                <th>Industry</th>
                <th style={{ textAlign: "right" }}>People</th>
                <th>Who we found</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <InventoryRow key={`${r.segment}:${r.company}`} row={r} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function InventoryRow({ row }: { row: InventoryCompany }) {
  const shown = row.people.slice(0, 3);
  const more = row.people.length - shown.length;
  return (
    <tr>
      <td>
        <div>
          {row.company}
          {row.isYc && <span className="chip chip-accent ml-1.5 align-middle">YC</span>}
        </div>
        {row.domain && (
          <div className="text-[0.6875rem] text-[var(--color-faint)]" style={{ fontFamily: "var(--font-mono)" }}>
            {row.domain}
          </div>
        )}
      </td>
      <td className="text-[var(--color-muted)]">{row.industry}</td>
      <td style={{ textAlign: "right" }}>
        {row.people.length}
        {row.withIds < row.people.length && (
          <span className="text-[var(--color-faint)]"> ({row.withIds} ready)</span>
        )}
      </td>
      <td>
        {shown.map((p, i) => (
          <div key={`${i}:${p.name}`} className="text-[0.75rem] leading-snug">
            {p.name} <span className="text-[var(--color-faint)]">— {p.title}</span>
          </div>
        ))}
        {more > 0 && <div className="text-[0.6875rem] text-[var(--color-faint)]">+{more} more</div>}
      </td>
      <td>
        {row.attempted ? (
          <span className="chip chip-danger">wave spent · needs re-survey</span>
        ) : (
          <span className="chip">awaiting verification</span>
        )}
      </td>
    </tr>
  );
}

// ── Lens 2: the people table with the enroll picker ───────────────────────

async function PeopleView({
  params,
  one,
}: {
  params: Record<string, string | string[] | undefined>;
  one: (key: string) => string | undefined;
}) {
  const page = Math.max(1, Number(one("page") ?? 1));
  const perPage = 100;

  const filter = {
    search: one("q"),
    sheet: one("sheet"),
    industry: one("industry"),
    roleGroup: one("role"),
    verification: one("verification"),
    enrollment: one("enrollment"),
    limit: perPage,
    offset: (page - 1) * perPage,
  };

  const { rows, total } = await contactRows(filter);

  // Why a contact cannot be enrolled, stated per row. The same rules the
  // enrollment gate applies, surfaced before you click rather than after.
  const minScore = await getNumberSetting(SETTING_KEYS.minVerificationScore);
  const allowPlaceholder = await getBoolSetting(SETTING_KEYS.allowPlaceholderNames);
  const allowRisky = await getBoolSetting(SETTING_KEYS.allowRiskySends);

  function blockedReason(r: Record<string, unknown>): string | null {
    if (Number(r.suppressed) > 0) return "suppressed";
    if (!r.email) return "no verified email";
    if (r.verification_status === "invalid") return "address is dead";
    if (r.verification_status == null) return "not verified yet";
    if (!allowRisky && r.verification_status === "risky") return "risky address";
    if (Number(r.verification_score ?? 0) < minScore) return `score below ${minScore}`;
    if (!allowPlaceholder && r.name_quality === "placeholder") return "no real first name";
    return null;
  }

  const enrollRows: EnrollRow[] = rows.map((row) => {
    const r = row as Record<string, unknown>;
    return {
      id: r.id as number,
      company: (r.company as string) ?? "-",
      industry: (r.industry as string) ?? "",
      tier: (r.source_sheet as string) ?? "",
      person: (r.full_name as string) ?? "-",
      role: (r.role as string) ?? "",
      roleGroup: (r.role_group as string) ?? "-",
      email: (r.email as string) ?? "-",
      suppressed: Number(r.suppressed) > 0,
      placeholderName: r.name_quality === "placeholder",
      verification: (r.verification_status as string) ?? null,
      verificationScore: r.verification_score === null ? null : Number(r.verification_score),
      verificationReasons: r.verification_reasons
        ? (JSON.parse(String(r.verification_reasons)) as string[]).join(" · ")
        : null,
      sent: Number(r.messages_sent ?? 0),
      lastReply: (r.last_reply as string) ?? null,
      step: r.current_step ? Number(r.current_step) : null,
      blocked: blockedReason(r),
      enrolled: r.enrollment_status != null,
      enrollmentStatus: (r.enrollment_status as string) ?? null,
      isYc: Number(r.is_yc) > 0,
    };
  });
  const options = filterOptions();
  const campaigns = campaignSummaries();
  const totalPages = Math.max(1, Math.ceil(total / perPage));

  const queryWithout = (key: string) => {
    const next = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (k !== key && k !== "page" && typeof v === "string") next.set(k, v);
    }
    return next.toString();
  };

  return (
    <>
      {/* Filters. One row above the table, as a plain GET form so every
          filtered view is a shareable URL. The hidden view field keeps the
          form from bouncing you back to the company board on submit. */}
      <form method="get" className="panel p-4 mb-4 grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(150px, 100%), 1fr))" }}>
        <input type="hidden" name="view" value="people" />
        <Field label="Search">
          <input name="q" defaultValue={one("q") ?? ""} placeholder="company, name, email" className="input" />
        </Field>
        <Field label="Sheet">
          <select name="sheet" defaultValue={one("sheet") ?? ""} className="select">
            <option value="">All</option>
            {(await options).sheets.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Industry">
          <select name="industry" defaultValue={one("industry") ?? ""} className="select">
            <option value="">All</option>
            {(await options).industries.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Role">
          <select name="role" defaultValue={one("role") ?? ""} className="select">
            <option value="">All roles</option>
            {(await options).roleGroups.map((r) => (
              <option key={r.value} value={r.value}>
                {r.value} ({r.n})
              </option>
            ))}
          </select>
        </Field>
        <Field label="Verification">
          <select name="verification" defaultValue={one("verification") ?? ""} className="select">
            <option value="">All</option>
            <option value="valid">Valid</option>
            <option value="risky">Risky</option>
            <option value="invalid">Invalid</option>
            <option value="unknown">Unknown</option>
            <option value="unverified">Not checked</option>
          </select>
        </Field>
        <Field label="Sequence">
          <select name="enrollment" defaultValue={one("enrollment") ?? ""} className="select">
            <option value="">All</option>
            <option value="none">Not enrolled</option>
            <option value="pending">Pending</option>
            <option value="active">Active</option>
            <option value="replied">Replied</option>
            <option value="stopped">Stopped</option>
            <option value="review">Needs review</option>
            <option value="completed">Completed</option>
          </select>
        </Field>
        <div className="flex items-end gap-2">
          <button type="submit" className="btn btn-primary">Apply</button>
          <Link href="/partners?view=people" className="btn">Reset</Link>
        </div>
      </form>

      <Panel
        title="Enroll"
        subtitle="Tick the ones you want. Set a total and the system fills the rest from whatever your filters above are showing, without displacing anything you picked. YC companies enroll only when you tick them yourself — auto-fill never touches them. Nobody without a verified email can be enrolled at all."
      >
        <EnrollPicker
          rows={enrollRows}
          campaigns={(await campaigns).map((c) => ({ id: c.id, name: c.name }))}
          action={enrollSelected}
          roleGroup={one("role")}
          industry={one("industry")}
        />
      </Panel>

      {totalPages > 1 && (
        <nav className="flex items-center justify-between mt-4 t-label">
          <span>Page {page} of {totalPages}</span>
          <span className="flex gap-2">
            {page > 1 && (
              <Link href={`/partners?${queryWithout("page")}&page=${page - 1}`} className="btn">Previous</Link>
            )}
            {page < totalPages && (
              <Link href={`/partners?${queryWithout("page")}&page=${page + 1}`} className="btn">Next</Link>
            )}
          </span>
        </nav>
      )}
    </>
  );
}
