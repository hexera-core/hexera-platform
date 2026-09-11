/**
 * The one-time import: the laptop's SQLite file into the hosted `outreach` schema.
 *
 *   node apps/admin-console/import-outreach.js --from ~/hexera-ops/data/outreach.db
 *   node apps/admin-console/import-outreach.js --from … --dry-run
 *
 * A SCRIPT AND NOT A MIGRATION, deliberately. A migration runs everywhere and describes a change
 * every environment needs; this moves data that exists on exactly one machine, once. Putting it in
 * the alembic chain would mean every future environment replays an import of a file it does not
 * have.
 *
 * IT REFUSES TO RUN TWICE. Every table is checked for existing rows first, because the failure
 * mode of a re-run is not an error - it is a duplicate contact list, and a contact enrolled twice
 * is a person emailed twice. `--force` exists for the case where a half-finished import has to be
 * cleaned up and redone, and it truncates rather than merging, because merging two partial imports
 * is guesswork about which side is right.
 *
 * ORDER MATTERS: parents before children, or a foreign key rejects the row. Sequences are reset at
 * the end, or the first insert after the import collides with an id the import already used.
 */
import { DatabaseSync } from "node:sqlite";

import { all, closeDb, getPool, run, transaction } from "@/lib/outreach/db";

// Dependency order. Reversed for the truncate, so nothing is orphaned on the way out.
const TABLES = [
  "contacts",
  "domain_intel",
  "templates",
  "campaigns",
  "sequence_steps",
  "enrollments",
  "verifications",
  "messages",
  "replies",
  "suppressions",
  "events",
  "settings",
  "oauth_tokens",
  "send_ledger",
] as const;

// `oauth_tokens` carries the mailbox credential. It is NOT imported: the token in the laptop's
// file is plaintext, importing it would write plaintext into the shared database, and the sealing
// this port added only happens on save. Reconnecting the mailbox from the Settings page takes one
// click and produces a sealed token, which is the state we actually want.
const SKIP = new Set(["oauth_tokens"]);

type Args = { dryRun: boolean; force: boolean; from: string };

function parseArgs(argv: readonly string[]): Args {
  const from = argv[argv.indexOf("--from") + 1];
  if (!argv.includes("--from") || !from || from.startsWith("--")) {
    throw new Error("usage: import-outreach --from <path to outreach.db> [--dry-run] [--force]");
  }
  return { dryRun: argv.includes("--dry-run"), force: argv.includes("--force"), from };
}

function log(message: string): void {
  console.log(`[import] ${message}`);
}

export async function importFrom(args: Args): Promise<Record<string, number>> {
  const source = new DatabaseSync(args.from, { readOnly: true });
  const moved: Record<string, number> = {};

  try {
    // REFUSE BEFORE MUTATING. A re-run duplicates the contact list, and a duplicated contact is a
    // person emailed twice.
    const occupied: string[] = [];
    for (const table of TABLES) {
      if (SKIP.has(table)) continue;
      const [row] = await all<{ n: string }>(`SELECT count(*) n FROM ${table}`);
      if (Number(row?.n ?? 0) > 0) occupied.push(`${table} (${row.n})`);
    }
    if (occupied.length > 0 && !args.force) {
      throw new Error(
        `these tables already hold rows: ${occupied.join(", ")}. Importing again would duplicate ` +
          `them, and a contact enrolled twice is a person emailed twice. Pass --force to empty ` +
          `them first if a previous import has to be redone.`,
      );
    }

    await transaction(async (client) => {
      if (args.force && occupied.length > 0) {
        for (const table of [...TABLES].reverse()) {
          if (SKIP.has(table)) continue;
          await run(`DELETE FROM ${table}`, [], client);
        }
        log(`--force: emptied ${TABLES.length - SKIP.size} table(s) first`);
      }

      for (const table of TABLES) {
        if (SKIP.has(table)) {
          log(`${table}: skipped - reconnect the mailbox instead, so the token is sealed`);
          continue;
        }
        const rows = source.prepare(`SELECT * FROM ${table}`).all() as Record<string, unknown>[];
        if (rows.length === 0) {
          moved[table] = 0;
          continue;
        }

        const columns = Object.keys({ ...rows[0] });
        const placeholders = columns.map(() => "?").join(", ");
        for (const row of rows) {
          await run(
            `INSERT INTO ${table} (${columns.join(", ")}) VALUES (${placeholders})`,
            columns.map((c) => (row as Record<string, unknown>)[c] ?? null),
            client,
          );
        }
        moved[table] = rows.length;
        log(`${table}: ${rows.length}`);
      }

      // The identity sequences still start at 1. Without this the first row written after the
      // import collides with an id the import already used.
      for (const table of TABLES) {
        if (SKIP.has(table) || !moved[table]) continue;
        const [has] = await all<{ n: string }>(
          `SELECT count(*) n FROM information_schema.columns
           WHERE table_schema = 'outreach' AND table_name = $1 AND column_name = 'id'`,
          [table],
          client,
        );
        if (Number(has?.n ?? 0) === 0) continue;
        await run(
          `SELECT setval(pg_get_serial_sequence('outreach.${table}', 'id'),
                         COALESCE((SELECT MAX(id) FROM ${table}), 1))`,
          [],
          client,
        );
      }

      if (args.dryRun) {
        log("--dry-run: rolling back, nothing was kept");
        throw new DryRun();
      }
    });
  } finally {
    source.close();
  }

  return moved;
}

class DryRun extends Error {}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  log(`reading ${args.from}`);
  try {
    const moved = await importFrom(args);
    const total = Object.values(moved).reduce((a, b) => a + b, 0);
    log(`imported ${total} row(s) across ${Object.keys(moved).length} table(s)`);
  } catch (error) {
    if (error instanceof DryRun) {
      log("dry run complete - the counts above are what a real run would write");
    } else {
      throw error;
    }
  } finally {
    await getPool() && (await closeDb());
  }
}

if (process.argv[1]?.includes("import-outreach")) {
  main().catch((error) => {
    console.error(`[import] FAILED: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  });
}
