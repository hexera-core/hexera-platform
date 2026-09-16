// Translating the outreach engine's SQL from SQLite to Postgres, at the wrapper rather than at the
// hundred-odd call sites that wrote it.
//
// The tables keep their names - they live in an `outreach` schema and `search_path` resolves them -
// so the only dialect difference the queries themselves carry is the placeholder.

// SQLite numbers nothing; Postgres numbers everything. `?` becomes `$1..$n` IN ORDER.
//
// A NAIVE REPLACE IS WRONG, and wrong in a way that produces a working query with different
// meaning. `WHERE stopped_reason = 'why?'` contains a question mark that is DATA; renumbering it
// shifts every placeholder after it by one and silently binds the wrong parameter to the wrong
// column. So the scan tracks string literals, dollar-quoted bodies and both comment forms, and only
// rewrites a `?` that is none of those.
export function toPositionalPlaceholders(sql: string): { sql: string; count: number } {
  let out = "";
  let index = 0;
  let i = 0;

  while (i < sql.length) {
    const ch = sql[i];
    const next = sql[i + 1];

    if (ch === "'" || ch === '"') {
      // A quoted literal or identifier. '' and "" are escapes for the quote itself, not the end.
      const quote = ch;
      let j = i + 1;
      while (j < sql.length) {
        if (sql[j] === quote && sql[j + 1] === quote) {
          j += 2;
          continue;
        }
        if (sql[j] === quote) break;
        j += 1;
      }
      out += sql.slice(i, j + 1);
      i = j + 1;
      continue;
    }

    if (ch === "-" && next === "-") {
      const end = sql.indexOf("\n", i);
      const stop = end === -1 ? sql.length : end;
      out += sql.slice(i, stop);
      i = stop;
      continue;
    }

    if (ch === "/" && next === "*") {
      const end = sql.indexOf("*/", i + 2);
      const stop = end === -1 ? sql.length : end + 2;
      out += sql.slice(i, stop);
      i = stop;
      continue;
    }

    if (ch === "?") {
      index += 1;
      out += `$${index}`;
      i += 1;
      continue;
    }

    out += ch;
    i += 1;
  }

  return { count: index, sql: out };
}

// Tables whose primary key is NOT `id`. Everything else in the outreach schema has a BIGSERIAL
// `id`, and an INSERT into one of those gets `RETURNING id` appended so the wrapper can answer what
// SQLite's `lastInsertRowid` answered.
//
// WHY A LIST AND NOT A TRY/CATCH. Appending RETURNING to these two fails, and a wrapper that
// swallowed that error and retried would turn a genuine SQL mistake into a silent second attempt.
// Two names are cheap to state; guessing is not.
const TABLES_WITHOUT_ID = new Set(["settings", "domain_intel"]);

const INSERT_TARGET = /^\s*INSERT\s+INTO\s+"?(?:outreach\.)?"?([a-z_][a-z0-9_]*)"?/i;

// Does this statement need `RETURNING id` added so the caller can learn the new row's id?
export function insertNeedingReturningId(sql: string): boolean {
  const match = INSERT_TARGET.exec(sql);
  if (!match) return false;
  if (TABLES_WITHOUT_ID.has(match[1].toLowerCase())) return false;
  // A caller that already asked for something back is answered with what they asked for.
  return !/\bRETURNING\b/i.test(sql);
}

export function withReturningId(sql: string): string {
  return `${sql.trimEnd().replace(/;\s*$/, "")} RETURNING id`;
}
