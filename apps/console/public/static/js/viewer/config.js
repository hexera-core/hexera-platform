// Responsibility: Hold the viewer's degraded-mode values, used only when the served configuration cannot be read.
// Boundaries: they must equal the served defaults; the backend stays the source of truth.

/* The viewer's DEGRADED-MODE policy values.
 *
 * The backend is the single source of truth (settings.policy, served by
 * api/v1/client_config.py) and the viewer fetches them at open time. These are what it uses
 * when that request fails, so they must equal the served defaults - a test asserts it, and it
 * can only do so because they are data in their own module rather than an object literal
 * buried in a DOM-heavy file.
 */
export const VIEWER_FALLBACK = Object.freeze({
  grid_px: 2.5,
  fine_fill: 8,
  frame_frac: 0.85,
  flag_span_factor: 2.5,
  max_flags: 20,
});
