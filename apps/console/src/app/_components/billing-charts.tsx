import { type DayActivity } from "@/app/_components/ledger-activity";
import { formatCredits } from "@/app/_components/ledger";

// SERVER-RENDERED SVG, no client code: the charts are pictures of a number series, not widgets,
// so they arrive with the page and stay right under every rerender. <title> inside each shape is
// the tooltip; role="img" + desc makes them readable without sight.

const WIDTH = 660;
const HEIGHT = 176;
const PAD_TOP = 10;
const PAD_BOTTOM = 24;
const PAD_SIDE = 4;

function slotSpan(count: number): number {
  return (WIDTH - PAD_SIDE * 2) / Math.max(count, 1);
}

/** Daily spend up, grants down - one zero line, both magnitudes on the same scale. */
export function SpendChart({ days }: { days: DayActivity[] }) {
  const slot = slotSpan(days.length);
  const barWidth = Math.max(slot - 6, 2);
  const mid = HEIGHT - PAD_BOTTOM;
  const usable = mid - PAD_TOP;
  const max = Math.max(1, ...days.map((d) => Math.max(d.spent, d.granted)));
  const ticks = days.filter((_, i) => i % Math.ceil(days.length / 6) === 0);
  return (
    <svg
      aria-label="Credits spent and granted per day over the last 30 days"
      className="chart"
      role="img"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
    >
      <line className="chart__base" x1={PAD_SIDE} x2={WIDTH - PAD_SIDE} y1={mid} y2={mid} />
      {days.map((d, i) => {
        const x = PAD_SIDE + i * slot + (slot - barWidth) / 2;
        const up = (d.spent / max) * usable;
        const down = (d.granted / max) * usable;
        return (
          <g key={d.day}>
            <title>{`${d.label}: ${formatCredits(d.spent)} spent, ${formatCredits(d.granted)} granted`}</title>
            {d.spent > 0 ? (
              <rect className="chart__bar-spend" height={up} rx={1.5} width={barWidth} x={x} y={mid - up} />
            ) : null}
            {d.granted > 0 ? (
              <rect className="chart__bar-grant" height={down} rx={1.5} width={barWidth} x={x} y={mid} />
            ) : null}
          </g>
        );
      })}
      {ticks.map((d) => (
        <text
          className="chart__label"
          key={d.day}
          textAnchor="middle"
          x={PAD_SIDE + days.indexOf(d) * slot + slot / 2}
          y={HEIGHT - 6}
        >
          {d.label}
        </text>
      ))}
      {max > 1 ? (
        <text className="chart__label" textAnchor="start" x={PAD_SIDE} y={PAD_TOP + 2}>
          {formatCredits(max)}
        </text>
      ) : null}
    </svg>
  );
}

/** End-of-day balance, drawn only where the ledger window can actually compute it. */
export function BalanceChart({
  days,
  series,
}: {
  days: DayActivity[];
  series: (number | null)[];
}) {
  const slot = slotSpan(days.length);
  const top = PAD_TOP;
  const bottom = HEIGHT - PAD_BOTTOM;
  const values = series.filter((v): v is number => v !== null);
  if (!values.length) {
    return (
      <p className="page__sub" style={{ padding: "1rem" }}>
        Not enough ledger history in this window to draw a balance yet.
      </p>
    );
  }
  const hi = Math.max(...values);
  const lo = Math.min(...values);
  const span = Math.max(hi - lo, 1);
  const x = (i: number) => PAD_SIDE + i * slot + slot / 2;
  const y = (v: number) => bottom - ((v - lo) / span) * (bottom - top);
  const points = series
    .map((v, i) => (v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`))
    .filter(Boolean)
    .join(" ");
  const firstIdx = series.findIndex((v) => v !== null);
  const area = `M ${x(firstIdx).toFixed(1)},${bottom} ` +
    series
      .map((v, i) => (v === null ? "" : `L ${x(i).toFixed(1)},${y(v).toFixed(1)}`))
      .join(" ") +
    ` L ${x(series.length - 1).toFixed(1)},${bottom} Z`;
  return (
    <svg
      aria-label="Credit balance at the end of each day over the last 30 days"
      className="chart"
      role="img"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
    >
      <line className="chart__base" x1={PAD_SIDE} x2={WIDTH - PAD_SIDE} y1={bottom} y2={bottom} />
      <path className="chart__area" d={area} />
      <polyline className="chart__line" points={points} />
      {series.map((v, i) =>
        v === null ? null : (
          <circle className="chart__dot" cx={x(i)} cy={y(v)} key={days[i].day} r={i === series.length - 1 ? 3 : 0}>
            <title>{`${days[i].label}: ${formatCredits(v)} credits`}</title>
          </circle>
        ),
      )}
      {days.filter((_, i) => i % Math.ceil(days.length / 6) === 0).map((d) => (
        <text
          className="chart__label"
          key={d.day}
          textAnchor="middle"
          x={x(days.indexOf(d))}
          y={HEIGHT - 6}
        >
          {d.label}
        </text>
      ))}
      <text className="chart__label" textAnchor="start" x={PAD_SIDE} y={top + 2}>
        {formatCredits(hi)}
      </text>
      <text
        className="chart__value"
        textAnchor="end"
        x={WIDTH - PAD_SIDE}
        y={y(values[values.length - 1]) - 6}
      >
        {formatCredits(values[values.length - 1])}
      </text>
    </svg>
  );
}
