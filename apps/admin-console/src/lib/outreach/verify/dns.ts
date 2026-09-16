/**
 * MX lookup with a domain-level cache.
 *
 * The list has 239 contacts across 203 domains, but the big targets have
 * several people each — caching per domain means one DNS round trip per
 * company instead of one per person.
 */
import { Resolver } from "node:dns/promises";
import { get, run } from "../db";
import { nowIso } from "../core/time";
import { isDisposable, isFreeProvider } from "./heuristics";

export interface MxResult {
  ok: boolean;
  hosts: string[];
  error?: string;
  /**
   * Whether a negative answer is *evidence* or merely *absence of evidence*.
   *
   * This distinction is the difference between correctly dropping a dead
   * domain and silently deleting a live prospect. NXDOMAIN and ENODATA are
   * authoritative answers — the domain really cannot receive mail. SERVFAIL,
   * timeouts and refusals are failures of our own lookup and say nothing
   * about the domain, so they must never be cached or scored as invalid.
   */
  definitive: boolean;
}

export interface DomainIntel {
  domain: string;
  mx_ok: number;
  mx_hosts: string | null;
  is_catch_all: number | null;
  is_disposable: number;
  is_free: number;
  checked_at: string;
}

/** Re-check a domain's MX after this long. Records change, but not hourly. */
const CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function resolver(): Resolver {
  const instance = new Resolver({ timeout: 5000, tries: 2 });
  // Corporate/ISP resolvers sometimes return NXDOMAIN for everything or
  // hijack failures to an ad page. Public resolvers keep results honest and
  // consistent across whatever network this runs on.
  instance.setServers(["1.1.1.1", "8.8.8.8", "9.9.9.9"]);
  return instance;
}

/** Codes that represent a real answer from DNS rather than a failed lookup. */
const AUTHORITATIVE_NEGATIVES = new Set(["ENOTFOUND", "NXDOMAIN", "ENODATA"]);

async function resolveWithRetry<T>(fn: () => Promise<T>, attempts = 3): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error;
      const code = (error as NodeJS.ErrnoException).code ?? "";
      // An authoritative "no" will not change on retry; only retry the
      // failures that plausibly will.
      if (AUTHORITATIVE_NEGATIVES.has(code)) throw error;
      if (attempt < attempts - 1) {
        await new Promise((r) => setTimeout(r, 250 * 2 ** attempt));
      }
    }
  }
  throw lastError;
}

export async function lookupMx(domain: string): Promise<MxResult> {
  let mxCode = "";
  try {
    const records = await resolveWithRetry(() => resolver().resolveMx(domain));
    const hosts = records
      .filter((r) => r.exchange && r.exchange !== ".")
      .sort((a, b) => a.priority - b.priority)
      .map((r) => r.exchange.toLowerCase());
    if (hosts.length) return { ok: true, hosts, definitive: true };
    mxCode = "ENODATA";
  } catch (error) {
    mxCode = (error as NodeJS.ErrnoException).code ?? "UNKNOWN";
    if (mxCode === "ENOTFOUND" || mxCode === "NXDOMAIN") {
      return { ok: false, hosts: [], error: "domain does not exist", definitive: true };
    }
    if (!AUTHORITATIVE_NEGATIVES.has(mxCode)) {
      // SERVFAIL / timeout / refused — our lookup failed, the domain is not
      // implicated. Reported as non-definitive so it is retried, not written off.
      return {
        ok: false,
        hosts: [],
        error: `DNS lookup failed (${mxCode}) — could not determine whether this domain accepts mail`,
        definitive: false,
      };
    }
  }

  // No MX record is not automatically fatal: RFC 5321 says a host with an A
  // record accepts mail on its own address as an implicit MX.
  try {
    const addresses = await resolveWithRetry(() => resolver().resolve4(domain));
    if (addresses.length) return { ok: true, hosts: [domain.toLowerCase()], definitive: true };
  } catch (error) {
    const aCode = (error as NodeJS.ErrnoException).code ?? "UNKNOWN";
    if (!AUTHORITATIVE_NEGATIVES.has(aCode)) {
      return {
        ok: false,
        hosts: [],
        error: `DNS lookup failed (${aCode}) — could not determine whether this domain accepts mail`,
        definitive: false,
      };
    }
  }

  return { ok: false, hosts: [], error: "no MX or A record", definitive: true };
}

export interface DomainIntelResult {
  intel: DomainIntel;
  /** False when the lookup itself failed; the caller must not treat mx_ok=0 as a verdict. */
  definitive: boolean;
  error?: string;
}

export async function getDomainIntel(domain: string, force = false): Promise<DomainIntelResult> {
  const cached = await get<DomainIntel>(`SELECT * FROM domain_intel WHERE domain = ?`, [domain]);
  if (cached && !force) {
    const age = Date.now() - new Date(cached.checked_at).getTime();
    if (age < CACHE_TTL_MS) return { intel: cached, definitive: true };
  }

  const mx = await lookupMx(domain);
  const now = nowIso();

  // Never cache an inconclusive lookup. Writing mx_ok=0 here would turn a
  // momentary network blip into a permanent "this company is unreachable",
  // and the 7-day TTL means nobody would notice for a week.
  if (!mx.definitive) {
    return {
      intel: cached ?? {
        domain,
        mx_ok: 0,
        mx_hosts: null,
        is_catch_all: null,
        is_disposable: isDisposable(domain) ? 1 : 0,
        is_free: isFreeProvider(domain) ? 1 : 0,
        checked_at: now,
      },
      definitive: false,
      error: mx.error,
    };
  }

  await run(
    `INSERT INTO domain_intel (domain, mx_ok, mx_hosts, is_catch_all, is_disposable, is_free, checked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (domain) DO UPDATE SET
       mx_ok = excluded.mx_ok,
       mx_hosts = excluded.mx_hosts,
       is_disposable = excluded.is_disposable,
       is_free = excluded.is_free,
       checked_at = excluded.checked_at`,
    [
      domain,
      mx.ok ? 1 : 0,
      JSON.stringify(mx.hosts),
      // Preserve any catch-all determination from a previous SMTP probe;
      // a plain MX refresh has nothing to say about it.
      cached?.is_catch_all ?? null,
      isDisposable(domain) ? 1 : 0,
      isFreeProvider(domain) ? 1 : 0,
      now,
    ],
  );

  return {
    intel: (await get<DomainIntel>(`SELECT * FROM domain_intel WHERE domain = ?`, [domain]))!,
    definitive: true,
  };
}

export async function setCatchAll(domain: string, isCatchAll: boolean): Promise<void> {
  await run(`UPDATE domain_intel SET is_catch_all = ? WHERE domain = ?`, [isCatchAll ? 1 : 0, domain]);
}
