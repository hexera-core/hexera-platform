import assert from "node:assert/strict";
import test from "node:test";

import { insertNeedingReturningId, toPositionalPlaceholders, withReturningId } from "./sql";

test("numbers placeholders in order", () => {
  const { count, sql } = toPositionalPlaceholders(
    "SELECT * FROM contacts WHERE company = ? AND domain = ? LIMIT ?",
  );
  assert.equal(sql, "SELECT * FROM contacts WHERE company = $1 AND domain = $2 LIMIT $3");
  assert.equal(count, 3);
});

test("a question mark inside a string literal is DATA, not a placeholder", () => {
  // The failure this prevents is not a crash. Renumbering a literal's question mark shifts every
  // placeholder after it by one, and the query still runs - binding the wrong value to the wrong
  // column.
  const { count, sql } = toPositionalPlaceholders(
    "UPDATE enrollments SET stopped_reason = 'really?' WHERE id = ? AND status = ?",
  );
  assert.equal(sql, "UPDATE enrollments SET stopped_reason = 'really?' WHERE id = $1 AND status = $2");
  assert.equal(count, 2);
});

test("an escaped quote does not end the literal", () => {
  const { count } = toPositionalPlaceholders(
    "INSERT INTO templates (body) VALUES ('it''s a ? here') -- and ? there",
  );
  assert.equal(count, 0, "both question marks are inside a literal or a comment");
});

test("question marks in comments are left alone", () => {
  const line = toPositionalPlaceholders("SELECT 1 -- why ?\nWHERE id = ?");
  assert.equal(line.count, 1);
  assert.match(line.sql, /WHERE id = \$1/);

  const block = toPositionalPlaceholders("SELECT 1 /* ? ? */ WHERE id = ?");
  assert.equal(block.count, 1);
});

test("a quoted identifier is not a placeholder site either", () => {
  const { count } = toPositionalPlaceholders('SELECT "weird?column" FROM contacts WHERE id = ?');
  assert.equal(count, 1);
});

test("sql with no placeholders is returned unchanged", () => {
  const sql = "SELECT count(*) FROM contacts";
  assert.equal(toPositionalPlaceholders(sql).sql, sql);
});

test("an insert into a table with an id asks for it back", () => {
  // SQLite's run() answered lastInsertRowid. Postgres has no equivalent, and a wrapper returning 0
  // instead would attach every new enrollment to contact 0 without erroring.
  assert.equal(insertNeedingReturningId("INSERT INTO contacts (company) VALUES (?)"), true);
  assert.equal(withReturningId("INSERT INTO contacts (company) VALUES ($1)"),
               "INSERT INTO contacts (company) VALUES ($1) RETURNING id");
});

test("the two tables without an id column are left alone", () => {
  // `settings` is keyed by `key` and `domain_intel` by `domain`. RETURNING id against either is an
  // error, and it would be an error on a path that previously worked.
  assert.equal(insertNeedingReturningId("INSERT INTO settings (key, value) VALUES (?, ?)"), false);
  assert.equal(insertNeedingReturningId("INSERT INTO domain_intel (domain) VALUES (?)"), false);
});

test("a caller that already asked for something back is not overridden", () => {
  assert.equal(
    insertNeedingReturningId("INSERT INTO contacts (company) VALUES (?) RETURNING id, created_at"),
    false,
  );
});

test("only inserts are touched", () => {
  assert.equal(insertNeedingReturningId("UPDATE contacts SET company = ?"), false);
  assert.equal(insertNeedingReturningId("SELECT * FROM contacts"), false);
  assert.equal(insertNeedingReturningId("DELETE FROM contacts WHERE id = ?"), false);
});

test("a schema-qualified insert is recognised", () => {
  assert.equal(insertNeedingReturningId("INSERT INTO outreach.contacts (company) VALUES (?)"), true);
});

test("a trailing semicolon does not strand the RETURNING clause", () => {
  assert.equal(withReturningId("INSERT INTO contacts (company) VALUES ($1);"),
               "INSERT INTO contacts (company) VALUES ($1) RETURNING id");
});
