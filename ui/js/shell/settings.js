// Responsibility: Paint the readiness chip and name what is not ready when the API is not.
// Boundaries: a readout, not a control - it opens nothing, and no credential is typed into the page.

/* The API readiness chip.
 *
 * A readout, not a control: it reports whether the API can actually serve a job and names what
 * is not ready when it cannot. It opens nothing - there is no connection dialog. Credentials are
 * supplied by the deployment, not typed into the page.
 */
import { health } from "../api/client.js";

const HEALTH_RETRY_MS = 6000;
let retryTimer = null;

const $ = (id) => document.getElementById(id);

/** Paint the readiness chip. Retries while it is not ready. */
export async function refreshHealth() {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  const { ready, status, down } = await health();
  const dot = $("dot"), label = $("api-lbl"), chip = $("api-status");
  // Three distinct states, because "we cannot tell" must not look like "everything is fine".
  if (dot) dot.className = "dot" + (ready ? " online" : status === "unknown" ? "" : " error");
  // QUIET WHEN CONNECTED: the chip hides itself on "ok" (css/theme.css) and shows only when the
  // connection is actually down. It retries every few seconds, so "Reconnecting" is the truth.
  if (chip) chip.dataset.state = ready ? "ok" : status === "unknown" ? "unknown" : "down";
  if (label) {
    label.textContent = ready ? "Connected"
      : down.length ? "Some services are down"
        : status === "unknown" ? "Status unknown"
          : "Reconnecting…";
  }
  if (chip) {
    chip.title = ready
      ? "The API and every dependency it needs are ready."
      : down.length ? `Not ready: ${down.join(", ")}`
        : status === "unknown" ? "The API is serving but reports no readiness probe."
          : "The API did not respond.";
  }
  if (!ready) retryTimer = setTimeout(refreshHealth, HEALTH_RETRY_MS);
}
