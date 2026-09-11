import assert from "node:assert/strict";
import test from "node:test";

import { types } from "pg";

// Importing db.ts for its side effect: registering the parsers. The pool is lazy, so nothing
// connects.
import "./db";

// pg's DEFAULT behaviour returns these as strings, which is what made the analytics page report
// 221 replies out of 5 - "2" + "2" + "1". The port inherited `n: number` types from the SQLite
// original, and TypeScript cannot check a claim about what a driver hands back at runtime, so the
// guarantee has to be asserted here.
test("COUNT(*) and other int8 columns arrive as numbers, not strings", () => {
  const parse = types.getTypeParser(20);
  const parsed = parse("5");
  assert.equal(typeof parsed, "number");
  assert.equal(parsed, 5);
});

test("numeric columns - ROUND(AVG(...)) - arrive as numbers too", () => {
  const parse = types.getTypeParser(1700);
  const parsed = parse("72.5");
  assert.equal(typeof parsed, "number");
  assert.equal(parsed, 72.5);
});

test("summing parsed counts adds rather than concatenating", () => {
  const parse = types.getTypeParser(20) as (value: string) => number;
  const rows = [parse("2"), parse("2"), parse("1")];
  assert.equal(rows.reduce((a, b) => a + b, 0), 5);
});
