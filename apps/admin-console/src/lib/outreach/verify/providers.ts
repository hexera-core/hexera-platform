/**
 * Optional paid-verifier adapters.
 *
 * The built-in checks (syntax + MX + heuristics, optionally SMTP) are free and
 * get most of the way there. A paid API is the reliable way to confirm
 * mailboxes when port 25 is blocked, which it will be on most networks — so
 * the seam exists, unused by default, rather than being retrofitted later.
 *
 * Adding one: implement ExternalVerifier and register it in `getVerifier`.
 */

export interface ExternalVerdict {
  status: "valid" | "risky" | "invalid" | "unknown";
  score: number | null;
  isCatchAll: boolean | null;
  raw: unknown;
}

export interface ExternalVerifier {
  name: string;
  verify(email: string): Promise<ExternalVerdict>;
}

class HunterVerifier implements ExternalVerifier {
  name = "hunter";
  constructor(private apiKey: string) {}

  async verify(email: string): Promise<ExternalVerdict> {
    const url = `https://api.hunter.io/v2/email-verifier?email=${encodeURIComponent(email)}&api_key=${encodeURIComponent(this.apiKey)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`hunter responded ${response.status}`);
    const body = (await response.json()) as { data?: { status?: string; score?: number; accept_all?: boolean } };
    const data = body.data ?? {};

    const status =
      data.status === "valid" ? "valid"
      : data.status === "invalid" ? "invalid"
      : data.status === "accept_all" || data.status === "webmail" || data.status === "disposable" ? "risky"
      : "unknown";

    return { status, score: data.score ?? null, isCatchAll: data.accept_all ?? null, raw: body };
  }
}

class ZeroBounceVerifier implements ExternalVerifier {
  name = "zerobounce";
  constructor(private apiKey: string) {}

  async verify(email: string): Promise<ExternalVerdict> {
    const url = `https://api.zerobounce.net/v2/validate?api_key=${encodeURIComponent(this.apiKey)}&email=${encodeURIComponent(email)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`zerobounce responded ${response.status}`);
    const body = (await response.json()) as { status?: string; sub_status?: string };

    const status =
      body.status === "valid" ? "valid"
      : body.status === "invalid" ? "invalid"
      : body.status === "catch-all" || body.status === "unknown" || body.status === "do_not_mail" ? "risky"
      : "unknown";

    return {
      status,
      score: null,
      isCatchAll: body.status === "catch-all" ? true : null,
      raw: body,
    };
  }
}

export function getVerifier(): ExternalVerifier | null {
  const provider = (process.env.VERIFIER_PROVIDER ?? "none").toLowerCase();
  const apiKey = process.env.VERIFIER_API_KEY ?? "";
  if (provider === "none" || !provider) return null;
  if (!apiKey) {
    console.warn(`VERIFIER_PROVIDER=${provider} but VERIFIER_API_KEY is empty — falling back to built-in checks.`);
    return null;
  }
  if (provider === "hunter") return new HunterVerifier(apiKey);
  if (provider === "zerobounce") return new ZeroBounceVerifier(apiKey);
  console.warn(`Unknown VERIFIER_PROVIDER "${provider}" — falling back to built-in checks.`);
  return null;
}
