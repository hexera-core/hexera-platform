/**
 * Cheap, offline signals about an address. These run before any network call
 * and can decide the verdict on their own for the obvious cases.
 */

/**
 * Mailboxes staffed by a queue rather than a person. Deliverable, but a cold
 * note to sales@ is a different (worse) play than one to the Head of
 * Aerodynamics, so these are flagged rather than blocked.
 */
const ROLE_LOCAL_PARTS = new Set([
  "info", "sales", "support", "contact", "contacts", "admin", "administrator",
  "hello", "hi", "team", "careers", "jobs", "recruiting", "hr", "press",
  "media", "marketing", "billing", "accounts", "accounting", "finance",
  "help", "helpdesk", "office", "enquiries", "enquiry", "inquiries", "inquiry",
  "general", "mail", "email", "webmaster", "postmaster", "hostmaster", "abuse",
  "legal", "privacy", "security", "noreply", "no-reply", "donotreply",
  "do-not-reply", "newsletter", "notifications", "service", "customerservice",
  "partners", "partnerships", "bd", "investors", "ir", "pr",
]);

/**
 * Free consumer mail. Not invalid — plenty of early-stage founders genuinely
 * use Gmail — but a corporate contact list address here is more often a stale
 * personal address than a working work address, so it costs a little score.
 */
const FREE_PROVIDERS = new Set([
  "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
  "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
  "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
  "pm.me", "gmx.com", "gmx.net", "mail.com", "yandex.com", "yandex.ru",
  "zoho.com", "fastmail.com", "hey.com", "tutanota.com", "tuta.io",
  "comcast.net", "verizon.net", "sbcglobal.net", "att.net", "qq.com", "163.com",
]);

/** Throwaway inbox providers. An address here is never worth sending to. */
const DISPOSABLE_DOMAINS = new Set([
  "mailinator.com", "guerrillamail.com", "guerrillamail.net", "10minutemail.com",
  "tempmail.com", "temp-mail.org", "throwawaymail.com", "yopmail.com",
  "trashmail.com", "sharklasers.com", "getnada.com", "maildrop.cc",
  "dispostable.com", "fakeinbox.com", "mailnesia.com", "mintemail.com",
  "spamgourmet.com", "mytemp.email", "moakt.com", "emailondeck.com",
  "burnermail.io", "spam4.me", "grr.la", "einrot.com", "tempr.email",
]);

/**
 * Practical syntax check.
 *
 * Deliberately stricter than RFC 5322 — that grammar permits quoted local
 * parts and comments that no real corporate mailbox uses, and accepting them
 * would only let typos through.
 */
export function checkSyntax(email: string): { ok: boolean; reason?: string } {
  if (!email) return { ok: false, reason: "empty address" };
  if (email.length > 254) return { ok: false, reason: "address exceeds 254 characters" };

  const at = email.lastIndexOf("@");
  if (at <= 0 || at === email.length - 1) return { ok: false, reason: "missing local part or domain" };

  const local = email.slice(0, at);
  const domain = email.slice(at + 1);

  if (local.length > 64) return { ok: false, reason: "local part exceeds 64 characters" };
  if (/^\.|\.$|\.\./.test(local)) return { ok: false, reason: "local part has a leading, trailing or doubled dot" };
  if (!/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$/.test(local)) {
    return { ok: false, reason: "local part contains an illegal character" };
  }
  if (domain.length > 253) return { ok: false, reason: "domain exceeds 253 characters" };
  if (!/^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$/.test(domain)) {
    return { ok: false, reason: "domain is not a valid hostname" };
  }
  if (!/\.[A-Za-z]{2,}$/.test(domain)) return { ok: false, reason: "domain has no valid TLD" };

  return { ok: true };
}

export function localPart(email: string): string {
  const at = email.lastIndexOf("@");
  return at === -1 ? email : email.slice(0, at);
}

export function domainPart(email: string): string {
  const at = email.lastIndexOf("@");
  return at === -1 ? "" : email.slice(at + 1);
}

export function isRoleAccount(email: string): boolean {
  // Strip plus-addressing and separators: "sales+eu@" and "sales-team@" are both role accounts.
  const local = localPart(email).toLowerCase().split("+")[0];
  if (ROLE_LOCAL_PARTS.has(local)) return true;
  const head = local.split(/[.\-_]/)[0];
  return ROLE_LOCAL_PARTS.has(head);
}

export function isFreeProvider(domain: string): boolean {
  return FREE_PROVIDERS.has(domain.toLowerCase());
}

export function isDisposable(domain: string): boolean {
  const lower = domain.toLowerCase();
  if (DISPOSABLE_DOMAINS.has(lower)) return true;
  // Catch subdomains of known throwaway hosts (e.g. "foo.mailinator.com").
  return [...DISPOSABLE_DOMAINS].some((d) => lower.endsWith(`.${d}`));
}
