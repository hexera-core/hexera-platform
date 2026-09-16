/**
 * RFC 5322 message construction for the Gmail API, which takes a complete
 * raw message rather than structured fields.
 *
 * Sent as multipart/alternative: a plain-text part, and an HTML part that is
 * nothing but escaped paragraphs.
 *
 * Plain text alone would be the purer choice, and it is still what any client
 * that prefers it will show. The problem is how Gmail draws it: it wraps
 * text/plain at a fixed character count, and in a proportional font that
 * leaves a ragged right edge with orphaned words on their own lines. The
 * message is fine, the rendering is not, and the reader cannot tell the
 * difference.
 *
 * The HTML part carries no styling, no images, no tracking pixel, no fonts
 * and no links we did not write. That matters: those are the things that mark
 * a message as machine-sent, not the existence of an HTML part. Every real
 * mail client sends one.
 */
import { randomBytes } from "node:crypto";

export interface MessageParts {
  from: string;
  fromName?: string;
  to: string;
  toName?: string;
  subject: string;
  body: string;
  /** RFC822 Message-ID of the message being replied to; makes clients thread it. */
  inReplyTo?: string | null;
  /** Full ancestry chain, oldest first. */
  references?: string[];
  replyTo?: string;
}

/**
 * RFC 2047 encoded-word. Headers are ASCII-only, so any non-ASCII in a name
 * or subject has to be encoded — otherwise "Loïc" arrives as mojibake, which
 * is exactly the kind of detail that makes outreach look automated.
 */
function encodeHeaderValue(value: string): string {
  if (/^[\x20-\x7E]*$/.test(value)) return value;
  return `=?UTF-8?B?${Buffer.from(value, "utf8").toString("base64")}?=`;
}

function formatAddress(email: string, name?: string): string {
  if (!name) return email;
  const encoded = encodeHeaderValue(name);
  // A quoted display name must not contain bare quotes or backslashes.
  const safe = encoded === name ? `"${name.replace(/["\\]/g, "")}"` : encoded;
  return `${safe} <${email}>`;
}

/**
 * Plain text to the smallest HTML that renders it faithfully.
 *
 * Escaping happens first and covers everything, so a contact whose company is
 * written "Foo & Bar <Ltd>" cannot break the message or inject markup. Blank
 * lines become paragraphs; single newlines inside a paragraph, as in a
 * signature block, become line breaks.
 */
export function textToHtml(text: string): string {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  const paragraphs = escaped
    .split(/\r?\n\s*\r?\n/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => `<p>${block.replace(/\r?\n/g, "<br>")}</p>`);

  return `<html><body>\r\n${paragraphs.join("\r\n")}\r\n</body></html>`;
}

function base64Body(value: string): string {
  return Buffer.from(value.replace(/\r?\n/g, "\r\n"), "utf8")
    .toString("base64")
    .replace(/(.{76})/g, "$1\r\n");
}


export function generateMessageId(domain: string): string {
  return `<${Date.now().toString(36)}.${randomBytes(12).toString("hex")}@${domain}>`;
}

/**
 * Folds a header that exceeds the 78-character soft limit onto continuation
 * lines. Mainly matters for References, which grows with every follow-up.
 */
function foldHeader(name: string, value: string): string {
  const full = `${name}: ${value}`;
  if (full.length <= 78) return full;

  const parts = value.split(" ").filter(Boolean);
  const lines: string[] = [];
  let current = `${name}:`;
  for (const part of parts) {
    if (current.length + part.length + 1 > 78 && current !== `${name}:`) {
      lines.push(current);
      current = ` ${part}`;
    } else {
      current += ` ${part}`;
    }
  }
  lines.push(current);
  return lines.join("\r\n");
}

export interface BuiltMessage {
  raw: string;
  /** base64url, the encoding the Gmail API expects. */
  encoded: string;
  messageId: string;
}

export function buildMessage(parts: MessageParts): BuiltMessage {
  // Random, so no message body can accidentally contain the delimiter and
  // split itself in half on the way through.
  const boundary = `--=_hexera_${randomBytes(16).toString("hex")}`;
  const fromDomain = parts.from.split("@")[1] ?? "localhost";
  const messageId = generateMessageId(fromDomain);

  const headers: string[] = [
    foldHeader("From", formatAddress(parts.from, parts.fromName)),
    foldHeader("To", formatAddress(parts.to, parts.toName)),
    foldHeader("Subject", encodeHeaderValue(parts.subject)),
    `Message-ID: ${messageId}`,
    `Date: ${new Date().toUTCString()}`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`,
  ];

  if (parts.replyTo) headers.push(foldHeader("Reply-To", formatAddress(parts.replyTo)));

  // In-Reply-To and References are what make a follow-up land inside the
  // original conversation instead of starting a second thread in the
  // recipient's inbox.
  if (parts.inReplyTo) headers.push(`In-Reply-To: ${parts.inReplyTo}`);
  if (parts.references?.length) {
    headers.push(foldHeader("References", parts.references.join(" ")));
  }

  // Plain text first. In multipart/alternative the parts run least rich to
  // most, and a client that prefers plain text takes the first one it
  // understands, so the order is the preference.
  const body = [
    `--${boundary}`,
    'Content-Type: text/plain; charset="UTF-8"',
    "Content-Transfer-Encoding: base64",
    "",
    base64Body(parts.body),
    `--${boundary}`,
    'Content-Type: text/html; charset="UTF-8"',
    "Content-Transfer-Encoding: base64",
    "",
    base64Body(textToHtml(parts.body)),
    `--${boundary}--`,
    "",
  ].join("\r\n");

  const raw = `${headers.join("\r\n")}\r\n\r\n${body}`;


  return {
    raw,
    encoded: Buffer.from(raw, "utf8")
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, ""),
    messageId,
  };
}

// ── Inbound parsing ───────────────────────────────────────────────────────

export interface ParsedInbound {
  from: string;
  fromName: string | null;
  to: string;
  subject: string;
  date: string | null;
  messageId: string | null;
  inReplyTo: string | null;
  references: string[];
  /** True when headers mark this as an auto-responder rather than a person. */
  isAutoSubmitted: boolean;
  autoSubmittedReason: string | null;
}

export interface HeaderPair {
  name?: string | null;
  value?: string | null;
}

export function headerValue(headers: HeaderPair[], name: string): string | null {
  const found = headers.find((h) => h.name?.toLowerCase() === name.toLowerCase());
  return found?.value ?? null;
}

export function parseAddress(value: string | null): { email: string; name: string | null } {
  if (!value) return { email: "", name: null };
  const angle = /<([^>]+)>/.exec(value);
  if (angle) {
    const name = value.slice(0, angle.index).trim().replace(/^"|"$/g, "");
    return { email: angle[1].trim().toLowerCase(), name: name || null };
  }
  return { email: value.trim().toLowerCase(), name: null };
}

/**
 * Headers that mark a message as machine-generated.
 *
 * Detecting these correctly is load-bearing: an out-of-office reply must
 * never cancel a follow-up sequence, and a bounce notification must never be
 * scored as a human "no".
 */
export function detectAutoSubmitted(headers: HeaderPair[]): { isAuto: boolean; reason: string | null } {
  const autoSubmitted = headerValue(headers, "Auto-Submitted");
  if (autoSubmitted && autoSubmitted.toLowerCase() !== "no") {
    return { isAuto: true, reason: `Auto-Submitted: ${autoSubmitted}` };
  }
  // Microsoft/Exchange
  const msAuto = headerValue(headers, "X-Auto-Response-Suppress");
  if (msAuto) return { isAuto: true, reason: "X-Auto-Response-Suppress present" };

  const precedence = headerValue(headers, "Precedence");
  if (precedence && ["bulk", "auto_reply", "junk", "list"].includes(precedence.toLowerCase())) {
    return { isAuto: true, reason: `Precedence: ${precedence}` };
  }
  if (headerValue(headers, "X-Autoreply") || headerValue(headers, "X-Autorespond")) {
    return { isAuto: true, reason: "X-Autoreply header present" };
  }
  if (headerValue(headers, "List-Unsubscribe") && headerValue(headers, "List-Id")) {
    return { isAuto: true, reason: "mailing list message" };
  }
  return { isAuto: false, reason: null };
}

export function parseInboundHeaders(headers: HeaderPair[]): ParsedInbound {
  const from = parseAddress(headerValue(headers, "From"));
  const to = parseAddress(headerValue(headers, "To"));
  const auto = detectAutoSubmitted(headers);
  const references = (headerValue(headers, "References") ?? "")
    .split(/\s+/)
    .filter((r) => r.startsWith("<"));

  return {
    from: from.email,
    fromName: from.name,
    to: to.email,
    subject: headerValue(headers, "Subject") ?? "",
    date: headerValue(headers, "Date"),
    messageId: headerValue(headers, "Message-ID"),
    inReplyTo: headerValue(headers, "In-Reply-To"),
    references,
    isAutoSubmitted: auto.isAuto,
    autoSubmittedReason: auto.reason,
  };
}

/**
 * Strips quoted history and signatures so the classifier scores what the
 * person actually wrote.
 *
 * Without this, our own message body is quoted underneath their two-word
 * reply and dominates the keyword matching — a "no thanks" gets buried under
 * the enthusiastic pitch it is rejecting.
 */
export function stripQuotedText(body: string): string {
  const lines = body.split(/\r?\n/);
  const kept: string[] = [];

  const boundaries = [
    /^\s*On .+ wrote:\s*$/i,
    /^\s*On .+,.+ at .+ wrote:/i,
    /^-{2,}\s*Original Message\s*-{2,}/i,
    /^_{5,}\s*$/,
    /^-{5,}\s*$/,
    /^\s*From:\s.+/i,
    /^\s*Sent from my /i,
    /^\s*Get Outlook for /i,
  ];

  for (const line of lines) {
    if (boundaries.some((pattern) => pattern.test(line))) break;
    if (/^\s*>/.test(line)) continue;
    kept.push(line);
  }

  return kept.join("\n").trim();
}
