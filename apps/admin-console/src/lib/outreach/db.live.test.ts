import assert from "node:assert/strict";
import test from "node:test";

import { all, closeDb, get, run, scalar, transaction } from "./db";

// AGAINST A REAL POSTGRES, because the things most worth proving here cannot be proved with a fake.
// A fake can be told that `transaction` used a dedicated client; only a real server can show that
// a rollback actually rolled anything back, or that `search_path` resolved an unprefixed table.
//
// Skipped unless OUTREACH_DB_HOST is set, so the workspace lane - which has no database - stays
// hermetic. Run it with a throwaway server:
//
//   docker run -d --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=hexera -p 55433:5432 postgres:16-alpine
//   OUTREACH_DB_HOST=127.0.0.1 OUTREACH_DB_PORT=55433 OUTREACH_DB_USER=postgres \
//   OUTREACH_DB_PASSWORD=test OUTREACH_DB_NAME=hexera \
//     pnpm --filter @hexera/admin-console exec node --import tsx --test src/lib/outreach/db.live.test.ts
const live = Boolean(process.env.OUTREACH_DB_HOST);
const options = { skip: live ? false : "set OUTREACH_DB_HOST to run against a real database" };

const NOW = "2026-09-10T00:00:00.000Z";

test("resolves unprefixed table names through the search path", options, async () => {
  // The whole reason the ported SQL did not have to change.
  const count = await scalar<string>("SELECT count(*) FROM contacts");
  assert.equal(typeof count, "string", "count(*) comes back as a bigint string");
});

test("run() reports the id Postgres assigned", options, async () => {
  const result = await run(
    "INSERT INTO contacts (source_sheet, company, created_at, updated_at) VALUES (?, ?, ?, ?)",
    ["live", "RunIdCo", NOW, NOW],
  );
  assert.equal(result.changes, 1);
  assert.ok(result.lastInsertRowid > 0, "an insert must report the row it created");

  const row = await get<{ company: string }>("SELECT company FROM contacts WHERE id = ?", [
    result.lastInsertRowid,
  ]);
  assert.equal(row?.company, "RunIdCo");
});

test("a rollback actually undoes the work", options, async () => {
  const before = Number(await scalar<string>("SELECT count(*) FROM contacts"));

  await assert.rejects(
    transaction(async (client) => {
      await run(
        "INSERT INTO contacts (source_sheet, company, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ["live", "RolledBackCo", NOW, NOW],
        client,
      );
      // Proof the insert was real inside the transaction before it is undone.
      const seen = await scalar<string>(
        "SELECT count(*) FROM contacts WHERE company = ?",
        ["RolledBackCo"],
        client,
      );
      assert.equal(Number(seen), 1);
      throw new Error("deliberate");
    }),
    /deliberate/,
  );

  const after = Number(await scalar<string>("SELECT count(*) FROM contacts"));
  assert.equal(after, before, "the rolled-back insert must not survive");
  assert.equal(
    Number(await scalar<string>("SELECT count(*) FROM contacts WHERE company = ?", ["RolledBackCo"])),
    0,
  );
});

test("a committed transaction keeps its work", options, async () => {
  const id = await transaction(async (client) => {
    const result = await run(
      "INSERT INTO contacts (source_sheet, company, created_at, updated_at) VALUES (?, ?, ?, ?)",
      ["live", "CommittedCo", NOW, NOW],
      client,
    );
    return result.lastInsertRowid;
  });

  const row = await get<{ company: string }>("SELECT company FROM contacts WHERE id = ?", [id]);
  assert.equal(row?.company, "CommittedCo");
});

test("a question mark in data survives the round trip", options, async () => {
  // The placeholder rewriter treats it as data; this proves the database agrees.
  const result = await run(
    "INSERT INTO contacts (source_sheet, company, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
    ["live", "QuestionCo", "why? because.", NOW, NOW],
  );
  const row = await get<{ notes: string }>("SELECT notes FROM contacts WHERE id = ?", [
    result.lastInsertRowid,
  ]);
  assert.equal(row?.notes, "why? because.");
});

test("all() returns plain rows a server component can serialize", options, async () => {
  const rows = await all<{ company: string }>(
    "SELECT company FROM contacts WHERE source_sheet = ? ORDER BY id",
    ["live"],
  );
  assert.ok(rows.length > 0);
  // The SQLite module had to strip a null prototype here; pg does not, and this is the assertion
  // that says so rather than leaving it assumed.
  assert.equal(Object.getPrototypeOf(rows[0]), Object.prototype);
});

test.after(async () => {
  if (live) {
    await run("DELETE FROM contacts WHERE source_sheet = ?", ["live"]);
    await closeDb();
  }
});
