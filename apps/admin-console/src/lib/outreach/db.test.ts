import assert from "node:assert/strict";
import test from "node:test";

import { all, get, poolConfigFrom, readOutreachDbConfig, run, scalar } from "./db";

type Seen = { text: string; values?: unknown[] };

function fake(rows: unknown[] = [], rowCount = rows.length) {
  const seen: Seen[] = [];
  return {
    on: {
      query: async (config: Seen) => {
        seen.push(config);
        return { rowCount, rows };
      },
    },
    seen,
  };
}

const ENV = {
  OUTREACH_DB_HOST: "/cloudsql/hexera-prod:us-central1:hexera",
  OUTREACH_DB_PASSWORD: "secret",
  OUTREACH_DB_USER: "outreach",
};

test("reads its connection from the environment", () => {
  const config = readOutreachDbConfig({ ...ENV, OUTREACH_DB_NAME: "hexera" });
  assert.equal(config.host, "/cloudsql/hexera-prod:us-central1:hexera");
  assert.equal(config.user, "outreach");
  assert.equal(config.port, 5432);
});

test("refuses to guess a missing credential, naming the one it wants", () => {
  // Named individually rather than "configuration is incomplete": the operator reading this is
  // looking at a Cloud Run environment and needs to know which key to add.
  assert.throws(() => readOutreachDbConfig({}), /OUTREACH_DB_HOST/);
  assert.throws(() => readOutreachDbConfig({ OUTREACH_DB_HOST: "h" }), /OUTREACH_DB_PASSWORD/);
  assert.throws(
    () => readOutreachDbConfig({ OUTREACH_DB_HOST: "h", OUTREACH_DB_PASSWORD: "p" }),
    /OUTREACH_DB_USER/,
  );
});

test("every connection resolves the outreach schema", () => {
  // Set as a CONNECTION option rather than a statement: the pool hands out a different connection
  // per query, so a `SET search_path` issued as a query would apply to one of them and not the next.
  const config = poolConfigFrom(readOutreachDbConfig(ENV));
  assert.equal(config.options, "-c search_path=outreach,public");
});

test("all() returns the rows and rewrites placeholders", async () => {
  const { on, seen } = fake([{ id: 1 }, { id: 2 }]);
  const rows = await all("SELECT * FROM contacts WHERE domain = ?", ["x.com"], on);

  assert.equal(rows.length, 2);
  assert.equal(seen[0].text, "SELECT * FROM contacts WHERE domain = $1");
  assert.deepEqual(seen[0].values, ["x.com"]);
});

test("get() returns undefined rather than null when nothing matched", async () => {
  // The SQLite module returned undefined, and call sites test for it.
  const { on } = fake([]);
  assert.equal(await get("SELECT 1 FROM contacts WHERE id = ?", [9], on), undefined);
});

test("run() reports the new row's id for an insert", async () => {
  const { on, seen } = fake([{ id: 4242 }], 1);
  const result = await run("INSERT INTO contacts (company) VALUES (?)", ["Acme"], on);

  assert.match(seen[0].text, /RETURNING id$/);
  assert.equal(result.lastInsertRowid, 4242);
  assert.equal(result.changes, 1);
});

test("run() leaves the two id-less tables alone", async () => {
  const { on, seen } = fake([], 1);
  const result = await run("INSERT INTO settings (key, value) VALUES (?, ?)", ["a", "b"], on);

  assert.doesNotMatch(seen[0].text, /RETURNING/);
  assert.equal(result.lastInsertRowid, 0);
  assert.equal(result.changes, 1);
});

test("run() reports rows changed for an update", async () => {
  const { on, seen } = fake([], 3);
  const result = await run("UPDATE enrollments SET status = ? WHERE campaign_id = ?", ["stopped", 1], on);

  assert.equal(result.changes, 3);
  assert.doesNotMatch(seen[0].text, /RETURNING/);
  assert.equal(seen[0].text, "UPDATE enrollments SET status = $1 WHERE campaign_id = $2");
});

test("a null rowCount counts as no rows changed", async () => {
  // pg reports null for statements that do not report a count. 0 is the honest reading; NaN in a
  // caller's arithmetic is not.
  const { on } = fake([], null as unknown as number);
  assert.equal((await run("UPDATE contacts SET notes = ?", ["x"], on)).changes, 0);
});

test("scalar() takes the first column of the first row", async () => {
  const { on } = fake([{ count: 17 }]);
  assert.equal(await scalar("SELECT count(*) FROM contacts", [], on), 17);
});

test("scalar() of nothing is undefined", async () => {
  const { on } = fake([]);
  assert.equal(await scalar("SELECT count(*) FROM contacts WHERE 1=0", [], on), undefined);
});
