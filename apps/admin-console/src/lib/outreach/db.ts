import { Pool, type PoolClient, type PoolConfig } from "pg";

import { insertNeedingReturningId, toPositionalPlaceholders, withReturningId } from "./sql";

// The outreach engine's database access, moved from a local SQLite file to Cloud SQL.
//
// WHAT CHANGED AND WHAT DID NOT. The original module's five functions - all, get, run, scalar,
// transaction - keep their names and their argument shapes, so the ported call sites read as they
// did. Every one of them is now ASYNC, because pg is, and that is the single largest source of
// change in this port: 28 files gain an `await` even where their SQL is untouched. A missed one is
// a type error rather than a runtime surprise, which is why the types are not loosened.
//
// THE SEARCH PATH IS WHY THE SQL DID NOT HAVE TO CHANGE. The tables live in an `outreach` schema
// and keep the names the application already writes, so `SELECT ... FROM contacts` resolves
// without editing a hundred query strings. It is set on the CONNECTION, not per statement: pg
// hands out pooled connections, so a `SET search_path` issued as a query would apply to whichever
// connection happened to serve it and not to the next one.

export type Row = Record<string, unknown>;

export type OutreachDbConfig = {
  database: string;
  host: string;
  password: string;
  port: number;
  user: string;
};

export function readOutreachDbConfig(env: Record<string, string | undefined>): OutreachDbConfig {
  const required = (name: string): string => {
    const value = env[name]?.trim();
    if (!value) {
      throw new Error(
        `${name} is not set on the admin console, so the outreach pages have no database to read.`,
      );
    }
    return value;
  };

  // A Cloud SQL unix socket is given as the HOST - `/cloudsql/<connection-name>` - which is how the
  // Cloud Run integration exposes it. pg treats a host beginning with `/` as a socket directory, so
  // the same two fields cover both the socket and a plain TCP host.
  return {
    database: env.OUTREACH_DB_NAME?.trim() || "hexera",
    host: required("OUTREACH_DB_HOST"),
    password: required("OUTREACH_DB_PASSWORD"),
    port: Number(env.OUTREACH_DB_PORT?.trim() || 5432),
    user: required("OUTREACH_DB_USER"),
  };
}

export function poolConfigFrom(config: OutreachDbConfig): PoolConfig {
  return {
    ...config,
    // EVERY connection resolves the outreach tables by their unprefixed names. Issued as a
    // connection option rather than a statement so it cannot be missed on a connection the pool
    // opens later.
    options: "-c search_path=outreach,public",
    // A Cloud Run container is small and the database is shared; a large pool here is a way to
    // exhaust Postgres's connection limit from a page nobody is looking at.
    max: Number(process.env.OUTREACH_DB_POOL_MAX ?? 5),
  };
}

let pool: Pool | null = null;

export function getPool(): Pool {
  if (pool) return pool;
  // Next's dev server re-evaluates modules on edit, which would open a new pool per change and
  // eventually exhaust the server's connections. The handle is parked on globalThis for the same
  // reason the SQLite module parked its file handle there.
  const key = Symbol.for("hexera.outreach.pool");
  const cached = (globalThis as Record<symbol, unknown>)[key];
  if (cached) {
    pool = cached as Pool;
    return pool;
  }
  pool = new Pool(poolConfigFrom(readOutreachDbConfig(process.env)));
  (globalThis as Record<symbol, unknown>)[key] = pool;
  return pool;
}

// The minimum a caller needs: the pool, or a client taken from it inside a transaction.
export type Queryable = {
  query(config: { text: string; values?: unknown[] }): Promise<{ rowCount: number | null; rows: unknown[] }>;
};

function prepare(sql: string, params: readonly unknown[]): { text: string; values: unknown[] } {
  return { text: toPositionalPlaceholders(sql).sql, values: [...params] };
}

export async function all<T = Row>(
  sql: string,
  params: readonly unknown[] = [],
  on: Queryable = getPool(),
): Promise<T[]> {
  const result = await on.query(prepare(sql, params));
  return result.rows as T[];
}

export async function get<T = Row>(
  sql: string,
  params: readonly unknown[] = [],
  on: Queryable = getPool(),
): Promise<T | undefined> {
  const result = await on.query(prepare(sql, params));
  return (result.rows[0] as T | undefined) ?? undefined;
}

export async function run(
  sql: string,
  params: readonly unknown[] = [],
  on: Queryable = getPool(),
): Promise<{ changes: number; lastInsertRowid: number }> {
  // SQLite answered `lastInsertRowid` for every write. Postgres has no equivalent, so an INSERT
  // into a table with an `id` is asked to return it. A wrapper that returned 0 instead would not
  // error - it would attach every new enrollment to contact 0.
  const needsId = insertNeedingReturningId(sql);
  const text = toPositionalPlaceholders(needsId ? withReturningId(sql) : sql).sql;
  const result = await on.query({ text, values: [...params] });

  const first = result.rows[0] as { id?: number | string } | undefined;
  return {
    changes: result.rowCount ?? 0,
    lastInsertRowid: needsId && first?.id !== undefined ? Number(first.id) : 0,
  };
}

/** Single-column convenience for COUNT/SUM queries. */
export async function scalar<T = number>(
  sql: string,
  params: readonly unknown[] = [],
  on: Queryable = getPool(),
): Promise<T | undefined> {
  const row = await get<Row>(sql, params, on);
  if (!row) return undefined;
  const values = Object.values(row);
  return values.length ? (values[0] as T) : undefined;
}

/**
 * Runs `fn` inside a transaction, rolling back if it throws.
 *
 * THE CLIENT IS DEDICATED, AND THAT IS THE WHOLE POINT. `BEGIN` applies to a CONNECTION, and a
 * pool hands a different connection to each query. Running a transaction against the pool sends
 * BEGIN down one connection and the statements down whichever others are free - so the work is not
 * in the transaction, the rollback rolls back nothing, and another request's statements can land
 * between them. It fails intermittently and only under load, which is the worst way to find out.
 *
 * Every query inside `fn` must therefore be given the client it is handed.
 */
export async function transaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await getPool().connect();
  try {
    await client.query("BEGIN");
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    try {
      await client.query("ROLLBACK");
    } catch {
      // A rollback failure means the transaction was already resolved; the original error is the
      // one worth surfacing.
    }
    throw error;
  } finally {
    // Always, or the pool leaks a connection per failed transaction and the page stops responding
    // once `max` of them have failed.
    client.release();
  }
}

export async function closeDb(): Promise<void> {
  if (!pool) return;
  await pool.end();
  pool = null;
  delete (globalThis as Record<symbol, unknown>)[Symbol.for("hexera.outreach.pool")];
}
