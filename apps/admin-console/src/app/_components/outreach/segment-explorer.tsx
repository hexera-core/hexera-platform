"use client";

import { useMemo, useState } from "react";

/**
 * Interactive segment chart.
 *
 * The static tables answer "what are the numbers". This answers "which cut of
 * the list should I work next", which needs you to switch dimension and
 * measure freely rather than read four fixed tables.
 *
 * Two things it refuses to do, because both are how segment charts mislead:
 *
 *  1. It will not plot a rate computed from almost nothing. A segment with two
 *     people contacted and one reply is not a 50% reply rate. Anything under
 *     the sample threshold is drawn hollow and sorted out of the lead, and the
 *     threshold is a control you can see rather than a hidden constant.
 *
 *  2. Every bar carries its raw counts next to the percentage. A rate with no
 *     denominator beside it is not a finding.
 */

export interface SegmentDatum {
  segment: string;
  contacts: number;
  contacted: number;
  replies: number;
  positive: number;
  negative: number;
  replyRate: number;
  positiveRate: number;
}

const DIMENSIONS = [
  { key: "role_group", label: "Role" },
  { key: "industry", label: "Industry" },
  { key: "tier", label: "Tier" },
  { key: "source_sheet", label: "Source sheet" },
] as const;

type DimensionKey = (typeof DIMENSIONS)[number]["key"];

const MEASURES = [
  {
    key: "replyRate",
    label: "Reply rate",
    unit: "%",
    needsContact: true,
    describe: "Share of the people contacted in this segment who wrote back.",
    value: (d: SegmentDatum) => d.replyRate,
    denominator: (d: SegmentDatum) => d.contacted,
    detail: (d: SegmentDatum) => `${d.replies} of ${d.contacted} contacted`,
  },
  {
    key: "positiveRate",
    label: "Positive share",
    unit: "%",
    needsContact: true,
    describe: "Of the people in this segment who replied, how many were interested.",
    value: (d: SegmentDatum) => d.positiveRate,
    denominator: (d: SegmentDatum) => d.replies,
    detail: (d: SegmentDatum) => `${d.positive} of ${d.replies} replies`,
  },
  {
    key: "positive",
    label: "Interested replies",
    unit: "",
    needsContact: false,
    describe: "Raw count of positive replies. No denominator, so no small-sample problem.",
    value: (d: SegmentDatum) => d.positive,
    denominator: () => Infinity,
    detail: (d: SegmentDatum) => `${d.positive} interested`,
  },
  {
    key: "coverage",
    label: "Coverage",
    unit: "%",
    needsContact: false,
    describe: "How much of the segment has been contacted at all.",
    value: (d: SegmentDatum) => (d.contacts > 0 ? (d.contacted / d.contacts) * 100 : 0),
    denominator: (d: SegmentDatum) => d.contacts,
    detail: (d: SegmentDatum) => `${d.contacted} of ${d.contacts} people`,
  },
  {
    key: "contacts",
    label: "People on file",
    unit: "",
    needsContact: false,
    describe: "How big each segment is before any outreach.",
    value: (d: SegmentDatum) => d.contacts,
    denominator: () => Infinity,
    detail: (d: SegmentDatum) => `${d.contacts} people`,
  },
] as const;

type MeasureKey = (typeof MEASURES)[number]["key"];

export function SegmentExplorer({ data }: { data: Record<DimensionKey, SegmentDatum[]> }) {
  const [dimension, setDimension] = useState<DimensionKey>("role_group");
  // Second breakdown, rendered beside the first. Role and industry answer
  // different questions (who to write to, and what to say), so the common case
  // is wanting both on screen rather than toggling between them.
  const [compare, setCompare] = useState<DimensionKey | "">("industry");
  const [measure, setMeasure] = useState<MeasureKey>("replyRate");
  const [minSample, setMinSample] = useState(5);

  const spec = MEASURES.find((m) => m.key === measure)!;
  const shown: DimensionKey[] = compare && compare !== dimension ? [dimension, compare] : [dimension];

  return (
    <div>
      <div className="flex flex-wrap items-end gap-2 mb-4">
        <label className="block">
          <span className="t-label block mb-1.5">Break down by</span>
          <select
            className="select"
            style={{ width: "auto" }}
            value={dimension}
            onChange={(e) => {
              const next = e.target.value as DimensionKey;
              setDimension(next);
              if (compare === next) setCompare("");
            }}
          >
            {DIMENSIONS.map((d) => (
              <option key={d.key} value={d.key}>{d.label}</option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="t-label block mb-1.5">Compare with</span>
          <select
            className="select"
            style={{ width: "auto" }}
            value={compare}
            onChange={(e) => setCompare(e.target.value as DimensionKey | "")}
          >
            <option value="">nothing</option>
            {DIMENSIONS.filter((d) => d.key !== dimension).map((d) => (
              <option key={d.key} value={d.key}>{d.label}</option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="t-label block mb-1.5">Measure</span>
          <select
            className="select"
            style={{ width: "auto" }}
            value={measure}
            onChange={(e) => setMeasure(e.target.value as MeasureKey)}
          >
            {MEASURES.map((m) => (
              <option key={m.key} value={m.key}>{m.label}</option>
            ))}
          </select>
        </label>

        {spec.unit === "%" ? (
          <label className="block">
            <span className="t-label block mb-1.5">Ignore under</span>
            <select
              className="select"
              style={{ width: "auto" }}
              value={minSample}
              onChange={(e) => setMinSample(Number(e.target.value))}
            >
              {[1, 3, 5, 10, 20].map((n) => (
                <option key={n} value={n}>
                  {n === 1 ? "no minimum" : `${n} in sample`}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      <p className="text-[0.75rem] text-[var(--color-muted)] mb-4 leading-snug">{spec.describe}</p>

      <div
        className="grid gap-6"
        style={{ gridTemplateColumns: `repeat(${shown.length}, minmax(min(280px, 100%), 1fr))` }}
      >
        {shown.map((key) => (
          <SegmentChart
            key={key}
            title={DIMENSIONS.find((d) => d.key === key)!.label}
            rows={data[key] ?? []}
            spec={spec}
            minSample={minSample}
          />
        ))}
      </div>
    </div>
  );
}

function SegmentChart({
  title,
  rows,
  spec,
  minSample,
}: {
  title: string;
  rows: SegmentDatum[];
  spec: (typeof MEASURES)[number];
  minSample: number;
}) {
  const prepared = useMemo(() => {
    return rows
      .map((row) => ({
        row,
        value: spec.value(row),
        sample: spec.denominator(row),
        detail: spec.detail(row),
      }))
      .map((entry) => ({ ...entry, thin: entry.sample < minSample }))
      .sort((a, b) => {
        // Under-sampled segments never take the lead, however flattering their
        // percentage looks.
        if (a.thin !== b.thin) return a.thin ? 1 : -1;
        return b.value - a.value || a.row.segment.localeCompare(b.row.segment);
      });
  }, [rows, spec, minSample]);

  // Each chart scales to its own maximum. The two panels answer separate
  // questions and are never summed, so a shared axis would only shrink one of
  // them for no gain.
  const max = Math.max(1, ...prepared.map((p) => p.value));
  const anyData = prepared.some((p) => p.value > 0);
  const thinCount = prepared.filter((p) => p.thin).length;

  return (
    <section>
      <header className="flex items-baseline justify-between gap-2 mb-3 pb-2 border-b border-[var(--color-line)]">
        <h3 className="t-label m-0">{title}</h3>
        {thinCount > 0 && spec.unit === "%" ? (
          <span className="t-label" title="Drawn hollow and sorted last">
            {thinCount} thin
          </span>
        ) : null}
      </header>

      {!anyData ? (
        <p className="text-[0.8125rem] text-[var(--color-faint)] py-6 text-center m-0">
          Nothing to plot yet.
        </p>
      ) : (
        <ul className="list-none m-0 p-0 flex flex-col gap-2">
          {prepared.map(({ row, value, detail, thin }) => (
            <li key={row.segment}>
              <div className="flex justify-between gap-3 items-baseline mb-1">
                <span className="text-[0.8125rem] truncate" title={row.segment}>
                  {row.segment}
                </span>
                <span className="t-num text-[0.75rem] whitespace-nowrap">
                  <span style={{ color: thin ? "var(--color-faint)" : "var(--color-ink)" }}>
                    {spec.unit === "%" ? `${value.toFixed(0)}%` : value}
                  </span>
                  <span className="text-[var(--color-faint)] text-[0.6875rem] ml-2">{detail}</span>
                </span>
              </div>
              <div className="h-2 bg-[var(--color-bg-2)] rounded-sm overflow-hidden">
                <div
                  className="h-full rounded-sm"
                  style={{
                    width: `${(value / max) * 100}%`,
                    // Hollow for under-sampled segments: still drawn to scale,
                    // but it reads as provisional rather than as a result, and
                    // stays legible without relying on colour alone.
                    background: thin ? "transparent" : "var(--color-series-1)",
                    border: thin ? "1px solid var(--color-series-1)" : "none",
                    opacity: thin ? 0.5 : 1,
                  }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
