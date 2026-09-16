/**
 * Reading and writing single values in `.env.local`.
 *
 * This exists for exactly one variable: DRY_RUN. Everything else in that file
 * is a credential or a path, set once at setup, and has no business being
 * editable from a web page.
 *
 * Why read from disk rather than `process.env`: both the dashboard and the
 * worker load the file once at startup, so a change made in one process is
 * invisible to the other until both restart. Reading the file on each check
 * costs a few hundred microseconds and means the switch takes effect
 * everywhere immediately, which is the behaviour anyone would expect from a
 * button labelled "stop sending".
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const ENV_PATH = () => resolve(process.cwd(), ".env.local");

/** The value as it is on disk right now, or null if the file or key is absent. */
export function readEnvFileValue(key: string): string | null {
  const path = ENV_PATH();
  if (!existsSync(path)) return null;

  let found: string | null = null;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    if (trimmed.slice(0, eq).trim() !== key) continue;

    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    // Keep going rather than returning: if the key appears twice, the last one
    // is what a dotenv loader would leave in place, so it is what we report.
    found = value;
  }
  return found;
}

/**
 * Rewrite one key in place, leaving every other line, comment and blank
 * exactly as it was. Appends the key if it is not there yet.
 *
 * Writes the whole file in one call so a crash mid-write cannot leave a
 * half-written `.env.local` with the credentials truncated.
 */
export function setEnvFileValue(key: string, value: string): void {
  const path = ENV_PATH();
  const existing = existsSync(path) ? readFileSync(path, "utf8") : "";
  const eol = existing.includes("\r\n") ? "\r\n" : "\n";
  const lines = existing.split(/\r?\n/);

  let replaced = false;
  const next = lines.map((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) return line;
    const eq = trimmed.indexOf("=");
    if (eq === -1 || trimmed.slice(0, eq).trim() !== key) return line;
    replaced = true;
    return `${key}=${value}`;
  });

  if (!replaced) {
    if (next.length && next[next.length - 1] !== "") next.push("");
    next.push(`${key}=${value}`, "");
  }

  writeFileSync(path, next.join(eol), "utf8");
  // Keep this process consistent with what it just wrote, so anything still
  // reading process.env directly does not disagree with the file.
  process.env[key] = value;
}
