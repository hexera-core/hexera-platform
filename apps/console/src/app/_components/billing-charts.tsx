import { type SpendSeries } from "@/app/_components/ledger-activity";
import { formatCredits } from "@/app/_components/ledger";

// SERVER-RENDERED SVG, no client code: the chart is a picture of a number series, not a widget,
// so it arrives with the page and stays right under every rerender. <title> inside each bar is
// the tooltip; role="img" + aria-label makes it readable without sight.

const WIDTH = 660;
const HEIGHT = 200;
const PAD_TOP = 14;
const PAD_BOTTOM = 24;
const PAD_SIDE = 4;

/** Daily credits used, stacked by source. One series, one question: where did the credits go. */
export function CreditsChart({ days, sources }: SpendSeries) {
  const slot = (WIDTH - PAD_SIDE * 2) / Math.max(days.length, 1);
  const barWidth = Math.max(slot - 6, 2);
  const bottom = HEIGHT - PAD_BOTTOM;
  const usable = bottom - PAD_TOP;
  const max = Math.max(1, ...days.map((d) => d.total));
  const ticks = days.filter((_, i) => i % Math.ceil(days.length / 6) === 0);
  const indexOf = new Map(sources.map((s, i) => [s.key, i]));
  return (
    <svg
      aria-label="Daily credits used by source"
      className="chart"
      role="img"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
    >
      <line className="chart__base" x1={PAD_SIDE} x2={WIDTH - PAD_SIDE} y1={bottom} y2={bottom} />
      {days.map((d, i) => {
        const x = PAD_SIDE + i * slot + (slot - barWidth) / 2;
        let y = bottom;
        return (
          <g key={d.day}>
            <title>
              {`${d.label}: ${formatCredits(d.total)} credits` +
                (d.parts.length > 1
                  ? ` - ${d.parts.map((p) => `${p.label} ${formatCredits(p.amount)}`).join(", ")}`
                  : "")}
            </title>
            {d.parts.map((part) => {
              const h = (part.amount / max) * usable;
              y -= h;
              return (
                <rect
                  className={`chart__seg chart__seg--s${indexOf.get(part.key) ?? 0}`}
                  height={h}
                  key={part.key}
                  rx={part.amount === d.total ? 1.5 : 0}
                  width={barWidth}
                  x={x}
                  y={y}
                />
              );
            })}
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
        <text className="chart__label" textAnchor="start" x={PAD_SIDE} y={PAD_TOP}>
          {formatCredits(max)}
        </text>
      ) : null}
    </svg>
  );
}
