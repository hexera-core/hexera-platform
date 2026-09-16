"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { PartnerCompany, PartnerPerson } from "@/lib/outreach/analytics/queries";
import type { CompanyScore } from "@/lib/outreach/analytics/ranking";
import { Chip } from "./ui";

/**
 * The partner board.
 *
 * Every company is one small box. Detail lives in a dialog rather than
 * expanding in place, because 222 expandable cards turn the page into a wall
 * of text you have to scroll past to find anything. The box carries only what
 * you scan for: the company, how many people we have, and how they responded.
 */

/**
 * The light on each box answers one question: how did this company respond?
 *
 *   green   net positive. More people said yes than no.
 *   red     net negative, or someone asked us to stop.
 *   amber   we have written to them and nobody has answered yet.
 *   dark    we have not written to them.
 *
 * Deliberately unlabelled. Four states with an obvious traffic-light reading
 * do not need a legend taking up space above the board, and every box carries
 * the same wording as a tooltip so the colour is never the only signal.
 */
type Signal = "positive" | "negative" | "waiting" | "idle";

function signalOf(company: PartnerCompany): Signal {
  if (company.negative > 0 || company.suppressed > 0) {
    // A single "stop contacting us" outranks any number of warm replies.
    if (company.suppressed > 0 || company.negative >= company.positive) return "negative";
  }
  if (company.positive > 0) return "positive";
  if (company.liveSent > 0) return "waiting";
  return "idle";
}

const LED: Record<Signal, { color: string; hint: string }> = {
  positive: { color: "var(--color-positive)", hint: "Answered, net positive" },
  negative: { color: "var(--color-crimson)", hint: "Answered, net negative or asked us to stop" },
  waiting: { color: "var(--color-accent)", hint: "Contacted, no answer yet" },
  idle: { color: "var(--color-line-2)", hint: "Not contacted" },
};

/** The same reading, applied to one person rather than a whole company. */
function personSignal(p: PartnerPerson): Signal {
  if (p.last_reply === "negative" || p.last_reply === "bounce") return "negative";
  if (p.last_reply === "positive") return "positive";
  if (p.live_sent > 0) return "waiting";
  return "idle";
}

/** Longer wording, used by the filter dropdown and the dialog. */
type Status = {
  label: string;
  tone: "positive" | "danger" | "accent" | "steel" | "neutral";
};

function statusOf(company: PartnerCompany): Status {
  if (company.positive > 0) return { label: "interested", tone: "positive" };
  if (company.negative > 0 || company.suppressed > 0) return { label: "declined", tone: "danger" };
  if (company.replies > 0) return { label: "replied", tone: "steel" };
  if (company.liveSent > 0) return { label: "contacted", tone: "accent" };
  if (company.enrolled > 0) return { label: "queued", tone: "neutral" };
  // Companies without a verified email never reach the board; they live on
  // the Inventory view until an enrichment wave promotes them here.
  return { label: "ready", tone: "neutral" };
}

const CHIP: Record<Status["tone"], string> = {
  positive: "chip-positive",
  danger: "chip-danger",
  accent: "chip-accent",
  steel: "chip-steel",
  neutral: "chip-neutral",
};

export function PartnerGrid({
  companies,
  people,
  industries,
  scores,
  rankingReady,
  rankingNote,
}: {
  companies: PartnerCompany[];
  people: Record<string, PartnerPerson[]>;
  industries: string[];
  /** Empty until enough replies exist to rank from. */
  scores: Record<string, CompanyScore>;
  rankingReady: boolean;
  rankingNote: string;
}) {
  const [query, setQuery] = useState("");
  const [industry, setIndustry] = useState("");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState<"" | "yc" | "cold">("");
  const [order, setOrder] = useState<"name" | "fit">("name");
  const [open, setOpen] = useState<PartnerCompany | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  // A native dialog gives focus trapping, Esc to close, and an inert
  // background without any of it being written here.
  useEffect(() => {
    const node = dialogRef.current;
    if (!node) return;
    if (open && !node.open) node.showModal();
    if (!open && node.open) node.close();
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return companies.filter((c) => {
      if (industry && c.industry !== industry) return false;
      if (status && statusOf(c).label !== status) return false;
      if (kind === "yc" && !c.isYc) return false;
      if (kind === "cold" && c.isYc) return false;
      if (!q) return true;
      return (
        c.company.toLowerCase().includes(q) ||
        (c.tier ?? "").toLowerCase().includes(q) ||
        (c.names ?? "").toLowerCase().includes(q) ||
        (c.domain ?? "").toLowerCase().includes(q)
      );
    });
  }, [companies, query, industry, status, kind]);

  // Alphabetical by default, so a company is findable by scanning rather than
  // by remembering which bucket it landed in. "Best fit" only appears once the
  // ranking has real replies behind it.
  const ordered = useMemo(() => {
    const list = [...filtered];
    if (order === "fit" && rankingReady) {
      return list.sort(
        (a, b) =>
          (scores[b.company]?.score ?? 0) - (scores[a.company]?.score ?? 0) ||
          a.company.localeCompare(b.company),
      );
    }
    return list.sort((a, b) => a.company.localeCompare(b.company));
  }, [filtered, order, rankingReady, scores]);

  const statuses = useMemo(() => {
    const counts = new Map<string, number>();
    for (const c of companies) {
      const label = statusOf(c).label;
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [companies]);

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 mb-5">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search company, person, or domain"
          className="input"
          style={{ maxWidth: "20rem" }}
          aria-label="Search partners"
        />
        <select
          className="select"
          style={{ width: "auto" }}
          value={industry}
          onChange={(e) => setIndustry(e.target.value)}
          aria-label="Filter by industry"
        >
          <option value="">All industries</option>
          {industries.map((i) => (
            <option key={i} value={i}>
              {i}
            </option>
          ))}
        </select>
        <select
          className="select"
          style={{ width: "auto" }}
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Filter by status"
        >
          <option value="">Any status</option>
          {statuses.map(([label, n]) => (
            <option key={label} value={label}>
              {label} ({n})
            </option>
          ))}
        </select>
        <select
          className="select"
          style={{ width: "auto" }}
          value={kind}
          onChange={(e) => setKind(e.target.value as typeof kind)}
          aria-label="Filter by kind"
        >
          <option value="">Everything</option>
          <option value="cold">Cold-emailable only</option>
          <option value="yc">YC only</option>
        </select>
        {rankingReady ? (
          <select
            className="select"
            style={{ width: "auto" }}
            value={order}
            onChange={(e) => setOrder(e.target.value as "name" | "fit")}
            aria-label="Sort order"
          >
            <option value="name">A to Z</option>
            <option value="fit">Best fit first</option>
          </select>
        ) : null}
        <span className="t-label ml-auto" title={rankingNote}>
          {ordered.length} of {companies.length}
        </span>
      </div>

      <div className="partner-board">
        {ordered.map((company) => {
          const led = LED[signalOf(company)];
          return (
            <button
              key={company.company}
              type="button"
              className="partner-box"
              onClick={() => setOpen(company)}
              title={`${company.company}. ${led.hint}.`}
            >
              <span className="partner-box__dot" style={{ background: led.color }} aria-hidden="true" />
              <span className="partner-box__name">{company.company}</span>
              <span className="partner-box__meta">
                {company.people} {company.people === 1 ? "person" : "people"}
                {company.verifiedEmails > 0 ? ` · ${company.verifiedEmails} ✉` : " · no ✉"}
                {company.liveSent > 0 ? ` · ${company.liveSent} sent` : ""}
              </span>
              {company.isYc ? (
                <span
                  className="partner-box__yc"
                  title="YC company. Auto-enroll skips these; enroll by hand if you want them."
                >
                  YC
                </span>
              ) : null}
              {rankingReady && scores[company.company] ? (
                <span className="partner-box__fit" title={scores[company.company].basis.join(". ")}>
                  {scores[company.company].score}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      {!ordered.length && (
        <p className="text-[0.8125rem] text-[var(--color-faint)] py-10 text-center">
          Nothing matches that filter.
        </p>
      )}

      <dialog
        ref={dialogRef}
        className="partner-dialog"
        onClose={() => setOpen(null)}
        onClick={(e) => {
          // A click on ::backdrop is reported against the dialog element
          // itself, so target === the dialog means the click missed the panel.
          if (e.target === dialogRef.current) setOpen(null);
        }}
      >
        {open ? (
          <CompanyDetail
            company={open}
            people={people[open.company] ?? []}
            onClose={() => setOpen(null)}
          />
        ) : null}
      </dialog>
    </>
  );
}

function CompanyDetail({
  company,
  people,
  onClose,
}: {
  company: PartnerCompany;
  people: PartnerPerson[];
  onClose: () => void;
}) {
  const s = statusOf(company);

  return (
    <div className="partner-dialog__inner">
      <header className="partner-dialog__head">
        <div className="min-w-0">
          <span className="env-mode block truncate">{company.tier ?? company.industry ?? ""}</span>
          <h2 className="env-name">{company.company}</h2>
          {company.stage ? <p className="env-p">{company.stage}</p> : null}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className={`chip ${CHIP[s.tone]}`}>{s.label}</span>
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>
      </header>

      {company.isYc ? (
        <p
          className="m-0 px-4 py-2 text-[0.75rem] leading-snug"
          style={{
            background: "rgba(255,79,0,0.08)",
            borderTop: "1px solid rgba(255,79,0,0.35)",
            borderBottom: "1px solid rgba(255,79,0,0.35)",
            color: "var(--color-accent-soft)",
          }}
        >
          YC company. Auto-enroll never picks anyone here — if you want them in a
          sequence, tick them yourself on the People view.
        </p>
      ) : null}

      <div className="partner-dialog__stats">
        <span className="env-stat">
          people <b>{company.people}</b>
        </span>
        <span className="env-stat">
          verified ✉ <b>{company.verifiedEmails}</b>
        </span>
        <span className="env-stat">
          in flight <b>{company.enrolled}</b>
        </span>
        <span className="env-stat">
          delivered <b>{company.liveSent}</b>
        </span>
        <span className="env-stat">
          replies <b>{company.replies}</b>
        </span>
        {company.domain ? (
          <span className="env-stat">
            domain <b>{company.domain}</b>
          </span>
        ) : null}
      </div>

      <div className="partner-dialog__body">
        {people.map((p) => {
          const led = LED[personSignal(p)];
          return (
            <article key={p.id} className="person-row">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="person-row__name">{p.full_name ?? "Name not on file"}</h3>
                <span className="flex items-center gap-2">
                  <span className="t-label">{p.source_sheet}</span>
                  <span
                    className="partner-box__dot"
                    style={{ background: led.color }}
                    title={led.hint}
                    aria-label={led.hint}
                  />
                </span>
              </div>

              {p.role ? <p className="person-row__role">{p.role}</p> : null}

              <p className="person-row__email">{p.email ?? "No address on file"}</p>

              <div className="flex flex-wrap items-center gap-1.5 mt-2">
                {p.enrollment_status ? <Chip value={p.enrollment_status} /> : null}
                {p.current_step ? <span className="t-label">step {p.current_step}</span> : null}
                {p.live_sent > 0 ? (
                  <span className="chip chip-accent">{p.live_sent} delivered</span>
                ) : null}
                {p.last_reply ? <Chip value={p.last_reply} /> : null}
                {p.name_quality === "placeholder" ? (
                  <span
                    className="chip chip-caution"
                    title="The sheet lists a job title here instead of a name, so personalized sends are held back"
                  >
                    no name
                  </span>
                ) : null}
              </div>

              {p.outreach_channel ? <p className="person-row__note">{p.outreach_channel}</p> : null}
            </article>
          );
        })}

        {!people.length && (
          <p className="text-[0.8125rem] text-[var(--color-faint)]">No people on file.</p>
        )}
      </div>
    </div>
  );
}
