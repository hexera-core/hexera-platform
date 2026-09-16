// THE CHART PALETTE, and why these hexes and not the page's own warm neutrals.
//
// Every value here is a documented slot from the data-visualization standard's reference palette,
// validated with its checker against THIS app's surface (#f8f7f3) rather than the reference
// surface. Eyeballed colours are not permitted, and the warm slate the rest of the console uses
// fails the chroma floor - it reads as grey and stops doing identity work.
//
// COLOUR FOLLOWS THE ENTITY, NEVER ITS POSITION. SERIES_COLOR maps a series NAME to a hue, so a
// window in which the 4xx series happens to be empty does not repaint 5xx with 4xx's colour. An
// index-based palette - `palette[i % palette.length]` - is the specific mistake this shape exists
// to prevent.
//
// Validator results against #f8f7f3, light mode:
//   #2a78d6, #eb6834   - all checks pass (adjacent CVD dE 24.7, normal dE 33.6)
//   #ec835a, #d03b3b   - all checks pass (adjacent CVD dE 13.9, normal dE 15.7)
// Both sets carry one colour below 3:1 contrast, which obliges visible labels: every chart using
// two or more series renders a legend, which is that relief.

// Categorical slots 1 and 2, in the standard's fixed order. Never cycled: a chart needing a third
// series needs a different form, not a third improvised hue.
export const CATEGORICAL = ["#2a78d6", "#eb6834"] as const;

// Reserved status roles. These never stand in for "series 3" - they mean what they are named.
export const STATUS = {
  critical: "#d03b3b",
  good: "#0ca30c",
  serious: "#ec835a",
} as const;

export const SERIES_COLOR: Record<string, string> = {
  "2xx": STATUS.good,
  "4xx": STATUS.serious,
  "5xx": STATUS.critical,
  active: CATEGORICAL[0],
  idle: CATEGORICAL[1],
  p50: CATEGORICAL[0],
  p95: CATEGORICAL[1],
};

export function colorForSeries(label: string): string {
  return SERIES_COLOR[label] ?? CATEGORICAL[0];
}

// Chart chrome, from the same reference. Recessive by design: the grid and axes are there to be
// read past, not read.
export const CHROME = {
  axis: "#c3c2b7",
  grid: "#e1e0d9",
  muted: "#898781",
  surface: "#f8f7f3",
} as const;
