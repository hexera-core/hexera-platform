"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { CHROME, colorForSeries } from "./chart-palette";
import type { Series } from "@/lib/gcp/metrics";

// One chart for every graph on the Fleet page.
//
// The MARKERS are the reason this takes more than a series: a worker-count line means little on
// its own and a great deal against the floor and the ceiling that bound it. "We ran five workers"
// becomes "we sat at the ceiling for forty minutes", which is the reading that justifies raising
// it.
//
// A LEGEND APPEARS FOR TWO OR MORE SERIES AND NEVER FOR ONE - with one series the panel title
// names it, and a legend box saying the same thing is noise. Two or more series must never be
// distinguishable by colour alone, so the legend is not optional there; it is also the visible
// label that licenses the one series colour sitting below 3:1 against this surface.

export type Marker = { label: string; value: number };

// HOW A VALUE IS WRITTEN, named rather than supplied as a function.
//
// This component is a Client Component and the pages using it are Server Components. A function
// cannot cross that boundary - React refuses with "Functions cannot be passed directly to Client
// Components", which surfaces as a server error with a digest and no message, because Next strips
// the text before it reaches the browser. Passing the NAME of a format keeps the prop serializable
// and keeps the formatting where the rendering is.
export type ValueFormat = "count" | "milliseconds" | "perSecond";

// How much room the tick labels need. A millisecond value runs to four digits plus its unit, and
// at the default width the labels are CLIPPED - "1200ms" renders as "200ms", which is not a
// cosmetic problem: it is a wrong number shown confidently.
const AXIS_WIDTH: Record<ValueFormat, number> = {
  count: 40,
  milliseconds: 64,
  perSecond: 56,
};

const FORMATTERS: Record<ValueFormat, (value: number) => string> = {
  count: (value) => (Number.isInteger(value) ? String(value) : value.toFixed(1)),
  milliseconds: (value) => `${Math.round(value)}ms`,
  perSecond: (value) => `${value.toFixed(value < 1 ? 2 : 0)}/s`,
};

export function TimeSeriesChart({
  emptyNote,
  format = "count",
  kind = "line",
  markers = [],
  series,
}: {
  emptyNote: string;
  format?: ValueFormat;
  kind?: "area" | "line";
  markers?: readonly Marker[];
  series: readonly Series[];
}) {
  const write = FORMATTERS[format];
  const present = series.filter((one) => one.points.length > 0);
  if (present.length === 0) {
    return <p className="admin-empty">{emptyNote}</p>;
  }

  // Recharts wants one row per timestamp with a column per series. readSeries aligns every series
  // to the same alignmentPeriod, so their timestamps line up and a union of keys is safe.
  const byTime = new Map<string, Record<string, number | string>>();
  for (const one of present) {
    for (const point of one.points) {
      const row = byTime.get(point.at) ?? { at: point.at };
      row[one.label || "value"] = point.value;
      byTime.set(point.at, row);
    }
  }
  const rows = [...byTime.values()].sort((a, b) => String(a.at).localeCompare(String(b.at)));
  const keys = present.map((one) => one.label || "value");
  const Chart = kind === "area" ? AreaChart : LineChart;

  return (
    <div className="admin-chart">
      <ResponsiveContainer height={200} width="100%">
        <Chart data={rows} margin={{ bottom: 0, left: 0, right: 12, top: 8 }}>
          {/* Horizontal only: vertical rules add ink without adding a reading. */}
          <CartesianGrid stroke={CHROME.grid} strokeDasharray="0" vertical={false} />
          <XAxis
            axisLine={{ stroke: CHROME.axis }}
            dataKey="at"
            minTickGap={56}
            tick={{ fill: CHROME.muted, fontSize: 11 }}
            tickFormatter={(at: string) =>
              new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
            }
            tickLine={false}
          />
          <YAxis
            axisLine={false}
            tick={{ fill: CHROME.muted, fontSize: 11 }}
            tickFormatter={(value: number) => write(value)}
            tickLine={false}
            width={AXIS_WIDTH[format]}
          />
          <Tooltip
            // The crosshair is the point of hovering a time series: it reads every series at one
            // instant rather than one series at one point.
            cursor={{ stroke: CHROME.axis, strokeWidth: 1 }}
            // Recharts types these loosely (ValueType / ReactNode), so they are narrowed here
            // rather than asserted at the call site.
            formatter={(value, name) => [write(Number(value)), String(name)]}
            labelFormatter={(at) => new Date(String(at)).toLocaleString()}
            wrapperClassName="admin-tooltip"
          />
          {keys.length > 1 ? (
            <Legend iconType="plainline" wrapperStyle={{ color: "#52514e", fontSize: 12 }} />
          ) : null}
          {markers.map((marker) => (
            <ReferenceLine
              key={marker.label}
              label={{
                fill: CHROME.muted,
                fontSize: 11,
                position: "insideTopRight",
                value: marker.label,
              }}
              stroke={CHROME.axis}
              strokeDasharray="4 4"
              y={marker.value}
            />
          ))}
          {keys.map((key) =>
            kind === "area" ? (
              <Area
                activeDot={{ r: 4, strokeWidth: 2 }}
                dataKey={key}
                fill={colorForSeries(key)}
                fillOpacity={0.14}
                key={key}
                stroke={colorForSeries(key)}
                strokeWidth={2}
                // A worker count is a step function - it holds a value until an instance is added
                // or removed. Interpolating it draws counts the fleet never had.
                type="stepAfter"
              />
            ) : (
              <Line
                activeDot={{ r: 4, strokeWidth: 2 }}
                dataKey={key}
                dot={false}
                key={key}
                stroke={colorForSeries(key)}
                strokeWidth={2}
                type="monotone"
              />
            ),
          )}
        </Chart>
      </ResponsiveContainer>
    </div>
  );
}
