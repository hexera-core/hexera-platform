/**
 * Shared presentational pieces. Server components — no client JS unless a
 * component genuinely needs interactivity, which most of these don't.
 */
import type { ReactNode } from "react";

// ── Layout ────────────────────────────────────────────────────────────────

export function Panel({
  title,
  subtitle,
  actions,
  children,
  flush = false,
}: {
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className={flush ? "panel-flush" : "panel"}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 px-4 pt-3 pb-3 border-b border-[var(--color-line)]">
          <div>
            {title && <h2 className="t-label">{title}</h2>}
            {subtitle && <p className="text-[0.8125rem] text-[var(--color-muted)] mt-1">{subtitle}</p>}
          </div>
          {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
        </header>
      )}
      <div className={flush ? "" : "p-4"}>{children}</div>
    </section>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 mb-6">
      <div>
        <h1 className="t-display text-[2rem]">{title}</h1>
        {description && (
          <p className="text-[var(--color-muted)] mt-1 max-w-[68ch] text-[0.875rem]">{description}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}

// ── Status chips ──────────────────────────────────────────────────────────
// Every status in the app renders through this map, so a colour always means
// the same thing on every page. Each chip carries its label — colour is never
// the only signal.

const CHIP_TONE: Record<string, string> = {
  // verification
  valid: "chip-positive", risky: "chip-caution", invalid: "chip-danger", unknown: "chip-neutral",
  // enrollment
  pending: "chip-neutral", active: "chip-accent", completed: "chip-steel",
  replied: "chip-positive", bounced: "chip-danger", stopped: "chip-danger",
  review: "chip-caution", suppressed: "chip-danger",
  // reply classification
  positive: "chip-positive", negative: "chip-danger", neutral: "chip-caution",
  ooo: "chip-steel", auto: "chip-neutral", bounce: "chip-danger",
  // priority
  HOT: "chip-accent", WARM: "chip-caution",
  // campaign
  draft: "chip-neutral", paused: "chip-caution", archived: "chip-neutral",
  // message
  sent: "chip-positive", failed: "chip-danger",
  queued: "chip-neutral", skipped: "chip-neutral", received: "chip-steel",
};

export function Chip({ value, title }: { value: string | null | undefined; title?: string }) {
  if (!value) return <span className="text-[var(--color-faint)]">-</span>;
  return (
    <span className={`chip ${CHIP_TONE[value] ?? "chip-neutral"}`} title={title}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

// ── Numbers ───────────────────────────────────────────────────────────────

export function Stat({
  label,
  value,
  sub,
  tone = "default",
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "default" | "accent" | "positive" | "danger" | "caution";
}) {
  const toneColor = {
    default: "var(--color-ink)",
    accent: "var(--color-accent)",
    positive: "var(--color-positive)",
    danger: "var(--color-crimson-soft)",
    caution: "var(--color-caution)",
  }[tone];

  return (
    <div className="panel p-4 min-w-0">
      <div className="t-label truncate">{label}</div>
      <div
        className="t-num mt-2 text-[1.75rem] leading-none tabular-nums"
        style={{ color: toneColor }}
      >
        {value}
      </div>
      {sub && <div className="text-[0.75rem] text-[var(--color-faint)] mt-1.5 leading-snug">{sub}</div>}
    </div>
  );
}

export function pct(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return "-";
  return `${value.toFixed(digits)}%`;
}

// ── Charts ────────────────────────────────────────────────────────────────

/**
 * Daily activity chart.
 *
 * Bars for outbound volume (a count per discrete day) and a line for replies
 * (a trend across those days). One y-axis — both are counts, so there is no
 * second scale to mis-align.
 *
 */
export function ActivityChart({
  data,
  height = 180,
}: {
  data: { day: string; sent: number; replies: number }[];
  height?: number;
}) {
  if (!data.length) return <Empty>No activity yet.</Empty>;

  const width = 720;
  const padding = { top: 12, right: 12, bottom: 22, left: 32 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  const max = Math.max(1, ...data.map((d) => Math.max(d.sent, d.replies)));
  // Round the axis up to a clean number so the top gridline reads sensibly.
  const ceiling = max <= 5 ? 5 : Math.ceil(max / 5) * 5;

  const bandW = plotW / data.length;
  // 2px surface gap between adjacent bars.
  const barW = Math.max(2, Math.min(18, bandW - 2));
  const y = (value: number) => padding.top + plotH - (value / ceiling) * plotH;
  const cx = (i: number) => padding.left + i * bandW + bandW / 2;

  const ticks = [0, ceiling / 2, ceiling];
  const linePath = data
    .map((d, i) => `${i === 0 ? "M" : "L"} ${cx(i).toFixed(1)} ${y(d.replies).toFixed(1)}`)
    .join(" ");

  const labelEvery = Math.max(1, Math.ceil(data.length / 8));

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-auto block"
        role="img"
        aria-label={`Messages sent and replies received per day over the last ${data.length} days.`}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={padding.left} x2={width - padding.right}
              y1={y(t)} y2={y(t)}
              stroke="var(--color-grid)" strokeWidth="1"
            />
            <text
              x={padding.left - 6} y={y(t) + 3}
              textAnchor="end" fontSize="9"
              fill="var(--color-faint)" fontFamily="var(--font-mono)"
            >
              {t}
            </text>
          </g>
        ))}

        {data.map((d, i) => (
          <rect
            key={d.day}
            x={cx(i) - barW / 2}
            y={y(d.sent)}
            width={barW}
            height={Math.max(0, padding.top + plotH - y(d.sent))}
            rx="2"
            fill="var(--color-series-1)"
            opacity={d.sent ? 0.85 : 0}
          >
            <title>{`${d.day}: ${d.sent} sent, ${d.replies} replies`}</title>
          </rect>
        ))}

        {/* Halo first, then the line — keeps it readable over the bars. */}
        <path d={linePath} fill="none" stroke="var(--color-bg)" strokeWidth="4" strokeLinejoin="round" />
        <path d={linePath} fill="none" stroke="var(--color-series-2)" strokeWidth="2" strokeLinejoin="round" />

        {data.map((d, i) =>
          d.replies > 0 ? (
            <circle key={d.day} cx={cx(i)} cy={y(d.replies)} r="3.5"
              fill="var(--color-series-2)" stroke="var(--color-bg)" strokeWidth="2">
              <title>{`${d.day}: ${d.replies} replies`}</title>
            </circle>
          ) : null,
        )}

        {data.map((d, i) =>
          i % labelEvery === 0 ? (
            <text
              key={d.day} x={cx(i)} y={height - 6}
              textAnchor="middle" fontSize="9"
              fill="var(--color-faint)" fontFamily="var(--font-mono)"
            >
              {d.day.slice(5)}
            </text>
          ) : null,
        )}
      </svg>
      <figcaption className="flex flex-wrap gap-4 mt-2 t-label">
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: "var(--color-series-1)" }} />
          Sent
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-0.5" style={{ background: "var(--color-series-2)" }} />
          Replies
        </span>
      </figcaption>
    </figure>
  );
}

/**
 * Horizontal funnel. Ordered stages, so a single-hue ordinal treatment is
 * correct here — the bars descend in intensity because the stages descend in
 * sequence, not because bigger means darker.
 */
export function Funnel({
  stages,
}: {
  stages: { key: string; label: string; value: number; ofPrevious: number | null; hint: string }[];
}) {
  const max = Math.max(1, ...stages.map((s) => s.value));

  return (
    <ol className="list-none m-0 p-0 flex flex-col gap-2">
      {stages.map((stage, i) => {
        const width = (stage.value / max) * 100;
        const opacity = 1 - i * 0.09;
        return (
          <li key={stage.key} className="grid grid-cols-[7.5rem_1fr_auto] items-center gap-3">
            <span className="t-label truncate" title={stage.hint}>{stage.label}</span>
            <span className="relative h-6 bg-[var(--color-bg-2)] rounded-sm overflow-hidden">
              <span
                className="absolute inset-y-0 left-0 rounded-sm"
                style={{ width: `${width}%`, background: "var(--color-series-1)", opacity }}
              />
            </span>
            <span className="t-num text-[0.8125rem] text-right whitespace-nowrap">
              {stage.value}
              {stage.ofPrevious !== null && (
                <span className="text-[var(--color-faint)] ml-2 text-[0.6875rem]">
                  {stage.ofPrevious.toFixed(0)}%
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** Single-series ranked bars. One colour for every bar — length is the encoding. */
export function BarList({
  items,
  formatValue = (v: number) => String(v),
  emptyLabel = "Nothing to show yet.",
}: {
  items: { label: string; value: number; sub?: string }[];
  formatValue?: (value: number) => string;
  emptyLabel?: string;
}) {
  if (!items.length) return <Empty>{emptyLabel}</Empty>;
  const max = Math.max(1, ...items.map((i) => i.value));

  return (
    <ul className="list-none m-0 p-0 flex flex-col gap-1.5">
      {items.map((item) => (
        <li key={item.label} className="grid grid-cols-[1fr_auto] gap-2 items-center">
          <div className="min-w-0">
            <div className="flex justify-between gap-2 items-baseline">
              <span className="text-[0.8125rem] truncate" title={item.label}>{item.label}</span>
              <span className="t-num text-[0.75rem] text-[var(--color-muted)] whitespace-nowrap">
                {formatValue(item.value)}
                {item.sub && <span className="text-[var(--color-faint)] ml-1.5">{item.sub}</span>}
              </span>
            </div>
            <div className="h-1.5 bg-[var(--color-bg-2)] rounded-sm mt-1 overflow-hidden">
              <div
                className="h-full rounded-sm"
                style={{ width: `${(item.value / max) * 100}%`, background: "var(--color-series-1)" }}
              />
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * Coverage bars: how much of each segment is done versus outstanding.
 *
 * Two parts of one whole, so a stacked bar is right — the full width means
 * "the whole segment" and the split shows progress through it. The remainder
 * is a recessive surface fill rather than a second hue: it is the absence of
 * work, not a category of its own.
 */
export function CoverageBars({
  rows,
  emptyLabel = "Nothing to show yet.",
}: {
  rows: { segment: string; total: number; contacted: number; remaining: number }[];
  emptyLabel?: string;
}) {
  if (!rows.length) return <Empty>{emptyLabel}</Empty>;
  const max = Math.max(1, ...rows.map((r) => r.total));

  return (
    <ul className="list-none m-0 p-0 flex flex-col gap-2.5">
      {rows.map((row) => {
        const scale = (row.total / max) * 100;
        const donePct = row.total > 0 ? (row.contacted / row.total) * 100 : 0;
        return (
          <li key={row.segment}>
            <div className="flex justify-between gap-2 items-baseline mb-1">
              <span className="text-[0.8125rem] truncate" title={row.segment}>{row.segment}</span>
              <span className="t-num text-[0.6875rem] text-[var(--color-muted)] whitespace-nowrap">
                {row.contacted}/{row.total}
                <span className="text-[var(--color-faint)] ml-1.5">{donePct.toFixed(0)}%</span>
              </span>
            </div>
            <div className="h-2 flex" style={{ width: `${scale}%` }}>
              {row.contacted > 0 && (
                <span
                  className="h-full rounded-sm"
                  style={{
                    width: `${donePct}%`,
                    background: "var(--color-series-1)",
                    // 2px surface gap between the two segments.
                    marginRight: row.remaining > 0 ? 2 : 0,
                  }}
                  title={`${row.contacted} contacted`}
                />
              )}
              {row.remaining > 0 && (
                <span
                  className="h-full rounded-sm flex-1"
                  style={{ background: "var(--color-bg-2)", border: "1px solid var(--color-line)" }}
                  title={`${row.remaining} not yet contacted`}
                />
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Cumulative volume over time. A line, because a running total is a trend and
 * bars would imply each day's value is independent.
 */
export function CumulativeChart({
  data,
  height = 150,
}: {
  data: { day: string; sent: number }[];
  height?: number;
}) {
  if (!data.length) return <Empty>No activity yet.</Empty>;

  const width = 720;
  const padding = { top: 10, right: 12, bottom: 20, left: 34 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  const max = Math.max(1, ...data.map((d) => d.sent));
  const ceiling = max <= 5 ? 5 : Math.ceil(max / 5) * 5;
  const x = (i: number) => padding.left + (i / Math.max(1, data.length - 1)) * plotW;
  const y = (v: number) => padding.top + plotH - (v / ceiling) * plotH;

  const line = (pick: (d: (typeof data)[number]) => number) =>
    data.map((d, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(pick(d)).toFixed(1)}`).join(" ");

  const labelEvery = Math.max(1, Math.ceil(data.length / 8));

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-auto block"
        role="img"
        aria-label="Cumulative outbound volume over time."
      >
        {[0, ceiling / 2, ceiling].map((t) => (
          <g key={t}>
            <line x1={padding.left} x2={width - padding.right} y1={y(t)} y2={y(t)}
              stroke="var(--color-grid)" strokeWidth="1" />
            <text x={padding.left - 6} y={y(t) + 3} textAnchor="end" fontSize="9"
              fill="var(--color-faint)" fontFamily="var(--font-mono)">{t}</text>
          </g>
        ))}

        <path d={line((d) => d.sent)} fill="none"
          stroke="var(--color-series-1)" strokeWidth="2" strokeLinejoin="round" />

        {data.map((d, i) =>
          i % labelEvery === 0 ? (
            <text key={d.day} x={x(i)} y={height - 5} textAnchor="middle" fontSize="9"
              fill="var(--color-faint)" fontFamily="var(--font-mono)">{d.day.slice(5)}</text>
          ) : null,
        )}
      </svg>
      <figcaption className="mt-2 t-label">Running total of messages sent</figcaption>
    </figure>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="text-[0.8125rem] text-[var(--color-faint)] py-6 text-center m-0">{children}</p>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="t-label block mb-1.5">{label}</span>
      {children}
      {hint && <span className="block text-[0.6875rem] text-[var(--color-faint)] mt-1 leading-snug">{hint}</span>}
    </label>
  );
}
