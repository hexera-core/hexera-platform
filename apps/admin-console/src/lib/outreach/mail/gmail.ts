/**
 * Gmail transport.
 *
 * Gmail was chosen over an ESP API because reply detection is the load-bearing
 * feature of this system: follow-ups must stop the moment someone answers.
 * Sending from the same mailbox that receives means replies arrive in the same
 * thread, and reading them needs no inbound MX routing or webhook endpoint.
 *
 * Scopes are minimal on purpose — send and read, never modify or delete.
 */
import { google } from "googleapis";

import { decryptSecret, encryptSecret, kmsKeyName } from "../crypto";
import type { OAuth2Client } from "google-auth-library";
import type { gmail_v1 } from "googleapis";
import { get, run } from "../db";
import { nowIso } from "../core/time";
import { buildMessage, type MessageParts } from "./mime";

export const GMAIL_SCOPES = [
  "https://www.googleapis.com/auth/gmail.send",
  // modify rather than readonly: it covers everything readonly did, plus
  // labels — the poller stars conversations where a human wrote back.
  "https://www.googleapis.com/auth/gmail.modify",
  "https://www.googleapis.com/auth/userinfo.email",
];

export interface StoredToken {
  id: number;
  account_email: string;
  access_token: string | null;
  refresh_token: string | null;
  scope: string | null;
  token_type: string | null;
  expiry_date: number | null;
  history_id: string | null;
  created_at: string;
  updated_at: string;
}

export class GmailNotConnectedError extends Error {
  constructor(message = "No Gmail account is connected. Connect one under Settings → Mailbox.") {
    super(message);
    this.name = "GmailNotConnectedError";
  }
}

export function oauthClient(): OAuth2Client {
  const clientId = process.env.GOOGLE_CLIENT_ID;
  const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
  const redirectUri = process.env.GOOGLE_REDIRECT_URI ?? "http://localhost:5789/oauth2callback";

  if (!clientId || !clientSecret) {
    throw new GmailNotConnectedError(
      "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set on this deployment, so no mailbox can " +
        "be connected. See docs/deployment/outreach.md.",
    );
  }
  return new google.auth.OAuth2(clientId, clientSecret, redirectUri);
}

/** Reads the stored token and opens its sealed fields. Nothing else should read the table. */
export async function storedToken(accountEmail?: string): Promise<StoredToken | undefined> {
  const row = accountEmail
    ? await get<StoredToken>(`SELECT * FROM oauth_tokens WHERE account_email = ?`, [accountEmail])
    : await get<StoredToken>(`SELECT * FROM oauth_tokens ORDER BY updated_at DESC LIMIT 1`);
  if (!row) return undefined;

  // Opened HERE and nowhere else, so there is exactly one place a sealed token becomes readable.
  const key = kmsKeyName(process.env);
  return {
    ...row,
    access_token: await decryptSecret(row.access_token, key),
    refresh_token: await decryptSecret(row.refresh_token, key),
  };
}

export async function saveToken(accountEmail: string, tokens: Record<string, unknown>): Promise<void> {
  const now = nowIso();
  await run(
    `INSERT INTO oauth_tokens (account_email, access_token, refresh_token, scope, token_type, expiry_date, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT (account_email) DO UPDATE SET
       access_token = excluded.access_token,
       -- Google only returns a refresh_token on the first consent. Later
       -- refreshes omit it, and overwriting with NULL would silently break
       -- unattended operation days later.
       refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
       scope = excluded.scope,
       token_type = excluded.token_type,
       expiry_date = excluded.expiry_date,
       updated_at = excluded.updated_at`,
    [
      accountEmail,
      await encryptSecret((tokens.access_token as string) ?? null, kmsKeyName(process.env)),
      await encryptSecret((tokens.refresh_token as string) ?? null, kmsKeyName(process.env)),
      (tokens.scope as string) ?? null,
      (tokens.token_type as string) ?? null,
      (tokens.expiry_date as number) ?? null,
      now,
      now,
    ],
  );
}

export async function saveHistoryId(accountEmail: string, historyId: string): Promise<void> {
  await run(`UPDATE oauth_tokens SET history_id = ?, updated_at = ? WHERE account_email = ?`, [
    historyId,
    nowIso(),
    accountEmail,
  ]);
}

/** An authorized client with the stored refresh token attached. */
export async function authorizedClient(accountEmail?: string): Promise<{ client: OAuth2Client; account: StoredToken }> {
  const token = await storedToken(accountEmail);
  if (!token?.refresh_token) throw new GmailNotConnectedError();

  const client = oauthClient();
  client.setCredentials({
    access_token: token.access_token ?? undefined,
    refresh_token: token.refresh_token,
    expiry_date: token.expiry_date ?? undefined,
    token_type: token.token_type ?? undefined,
    scope: token.scope ?? undefined,
  });

  // The library refreshes the access token transparently; persist the new one
  // so a fresh process does not have to refresh again immediately.
  client.on("tokens", (fresh) => {
    void saveToken(token.account_email, fresh as Record<string, unknown>).catch((error) => {
      console.error("could not persist a refreshed Gmail token", error);
    });
  });

  return { client, account: token };
}

export async function gmailApi(accountEmail?: string): Promise<{ api: gmail_v1.Gmail; account: StoredToken }> {
  const { client, account } = await authorizedClient(accountEmail);
  return { api: google.gmail({ version: "v1", auth: client }), account };
}

export interface SendResult {
  gmailMessageId: string;
  gmailThreadId: string;
  rfc822MessageId: string;
  raw: string;
}

/**
 * Sends one message.
 *
 * `threadId` is what puts a follow-up inside the existing conversation. Gmail
 * additionally requires the subject to match the thread, which is why
 * follow-up templates inherit the opener's subject rather than defining
 * their own.
 */
export async function sendEmail(
  parts: MessageParts,
  options: { threadId?: string | null; accountEmail?: string } = {},
): Promise<SendResult> {
  const { api } = await gmailApi(options.accountEmail);
  const built = buildMessage(parts);

  const response = await api.users.messages.send({
    userId: "me",
    requestBody: {
      raw: built.encoded,
      ...(options.threadId ? { threadId: options.threadId } : {}),
    },
  });

  const data = response.data;
  if (!data.id || !data.threadId) {
    throw new Error("Gmail accepted the message but returned no id — cannot track this send");
  }

  return {
    gmailMessageId: data.id,
    gmailThreadId: data.threadId,
    rfc822MessageId: built.messageId,
    raw: built.raw,
  };
}

export interface FetchedMessage {
  id: string;
  threadId: string;
  labelIds: string[];
  snippet: string;
  internalDate: string | null;
  headers: { name?: string | null; value?: string | null }[];
  body: string;
}

/** Walks the MIME tree for the best text representation of a message. */
function extractBody(payload: gmail_v1.Schema$MessagePart | undefined): string {
  if (!payload) return "";

  const decode = (data?: string | null) =>
    data ? Buffer.from(data.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8") : "";

  if (payload.mimeType === "text/plain" && payload.body?.data) return decode(payload.body.data);

  if (payload.parts?.length) {
    // Prefer text/plain; fall back to stripping tags out of text/html.
    const plain = payload.parts.find((p) => p.mimeType === "text/plain" && p.body?.data);
    if (plain) return decode(plain.body!.data);

    for (const part of payload.parts) {
      if (part.parts?.length) {
        const nested = extractBody(part);
        if (nested) return nested;
      }
    }

    const html = payload.parts.find((p) => p.mimeType === "text/html" && p.body?.data);
    if (html) return htmlToText(decode(html.body!.data));
  }

  if (payload.body?.data) {
    const decoded = decode(payload.body.data);
    return payload.mimeType === "text/html" ? htmlToText(decoded) : decoded;
  }
  return "";
}

function htmlToText(html: string): string {
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export async function fetchMessage(messageId: string, accountEmail?: string): Promise<FetchedMessage> {
  const { api } = await gmailApi(accountEmail);
  const response = await api.users.messages.get({ userId: "me", id: messageId, format: "full" });
  const data = response.data;
  return {
    id: data.id!,
    threadId: data.threadId!,
    labelIds: data.labelIds ?? [],
    snippet: data.snippet ?? "",
    internalDate: data.internalDate ?? null,
    headers: data.payload?.headers ?? [],
    body: extractBody(data.payload),
  };
}

/**
 * Lists inbound message ids matching a Gmail search query.
 *
 * A search query is used rather than the history API because history requires
 * an unbroken cursor — if the worker is off for a day, the history id can
 * expire and silently skip replies. A time-bounded search always returns the
 * full picture.
 */
export async function listInboundMessages(
  options: { query?: string; maxResults?: number; accountEmail?: string } = {},
): Promise<string[]> {
  const { api } = await gmailApi(options.accountEmail);
  const ids: string[] = [];
  let pageToken: string | undefined;

  do {
    const response = await api.users.messages.list({
      userId: "me",
      q: options.query ?? "in:inbox newer_than:30d",
      maxResults: Math.min(options.maxResults ?? 100, 500),
      pageToken,
    });
    for (const message of response.data.messages ?? []) {
      if (message.id) ids.push(message.id);
    }
    pageToken = response.data.nextPageToken ?? undefined;
  } while (pageToken && ids.length < (options.maxResults ?? 100));

  return ids;
}

/**
 * Stars one message, which stars its conversation in the Gmail UI. Used for
 * replies a human actually wrote, so the inbox itself shows which threads
 * are worth opening without the dashboard in between.
 */
export async function starMessage(messageId: string, accountEmail?: string): Promise<void> {
  const { api } = await gmailApi(accountEmail);
  await api.users.messages.modify({
    userId: "me",
    id: messageId,
    requestBody: { addLabelIds: ["STARRED"] },
  });
}

export async function getProfileEmail(client: OAuth2Client): Promise<string> {
  const api = google.gmail({ version: "v1", auth: client });
  const profile = await api.users.getProfile({ userId: "me" });
  const address = profile.data.emailAddress;
  if (!address) throw new Error("Gmail did not return the account's email address");
  return address.toLowerCase();
}

export async function connectionStatus(): Promise<{
  connected: boolean;
  account: string | null;
  error: string | null;
}> {
  try {
    const token = await storedToken();
    if (!token?.refresh_token) return { connected: false, account: null, error: "no account connected" };
    const { api } = await gmailApi();
    await api.users.getProfile({ userId: "me" });
    return { connected: true, account: token.account_email, error: null };
  } catch (error) {
    return {
      connected: false,
      account: (await storedToken())?.account_email ?? null,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
