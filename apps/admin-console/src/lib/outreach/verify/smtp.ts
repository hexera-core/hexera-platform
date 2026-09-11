/**
 * SMTP RCPT probe — the only check that can actually confirm a mailbox exists.
 *
 * How it works: open a conversation with the domain's mail exchanger and get
 * as far as `RCPT TO:<them>` without ever sending `DATA`. The server's
 * response code tells us whether it would accept mail for that address, and
 * because no message body is transmitted, nothing is delivered.
 *
 * Two caveats that matter more than the code does:
 *
 *  1. Outbound port 25 is blocked by nearly every residential ISP and cloud
 *     host. When it is blocked every probe returns `unknown`, which is why
 *     this is opt-in via SMTP_PROBE_ENABLED and the pipeline is designed to
 *     produce useful verdicts without it.
 *  2. Large providers (Google, Microsoft) deliberately accept RCPT for
 *     unknown users to defeat exactly this technique. That is what the
 *     catch-all detection below is for — without it, a Google Workspace
 *     domain would report every address as valid.
 */
import net from "node:net";
import { randomBytes } from "node:crypto";

export interface SmtpProbeResult {
  /** true = accepted, false = rejected, null = inconclusive */
  accepted: boolean | null;
  code: number | null;
  message: string;
  /** Set when the failure is ours (blocked port, timeout) rather than a verdict. */
  inconclusiveReason?: string;
}

interface Session {
  socket: net.Socket;
  read: () => Promise<{ code: number; text: string }>;
  send: (line: string) => Promise<{ code: number; text: string }>;
  close: () => void;
}

function openSession(host: string, timeoutMs: number): Promise<Session> {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port: 25 });
    socket.setEncoding("utf8");
    socket.setTimeout(timeoutMs);

    let buffer = "";
    let pending: ((value: { code: number; text: string }) => void) | null = null;
    let failed: ((error: Error) => void) | null = null;

    const tryFlush = () => {
      if (!pending) return;
      // A complete SMTP reply ends with a line of the form "NNN <text>".
      // Continuation lines use "NNN-<text>", so wait for the space form.
      const match = /^(\d{3})(?: [^\n]*)?\r?\n$|(?:^|\n)(\d{3}) [^\n]*\r?\n$/.exec(buffer);
      if (!match) return;
      const code = Number(match[1] ?? match[2]);
      const text = buffer.trim();
      buffer = "";
      const settle = pending;
      pending = null;
      failed = null;
      settle({ code, text });
    };

    socket.on("data", (chunk: string) => {
      buffer += chunk;
      tryFlush();
    });

    const fail = (error: Error) => {
      if (failed) {
        const reject_ = failed;
        pending = null;
        failed = null;
        reject_(error);
      }
    };

    socket.on("error", (error) => {
      fail(error);
      reject(error);
    });
    socket.on("timeout", () => {
      const error = new Error("smtp timeout");
      fail(error);
      socket.destroy();
      reject(error);
    });
    socket.on("close", () => fail(new Error("connection closed by server")));

    const read = () =>
      new Promise<{ code: number; text: string }>((res, rej) => {
        pending = res;
        failed = rej;
        tryFlush();
      });

    const send = (line: string) => {
      socket.write(`${line}\r\n`);
      return read();
    };

    socket.on("connect", () => {
      resolve({
        socket,
        read,
        send,
        close: () => {
          try {
            socket.write("QUIT\r\n");
          } catch {
            // Already gone — nothing to do.
          }
          socket.destroy();
        },
      });
    });
  });
}

export interface ProbeOptions {
  mxHosts: string[];
  from: string;
  timeoutMs: number;
  /** Also probe a random address to see whether the domain accepts everything. */
  detectCatchAll?: boolean;
}

export interface FullProbeResult extends SmtpProbeResult {
  isCatchAll: boolean | null;
}

export async function probeMailbox(email: string, options: ProbeOptions): Promise<FullProbeResult> {
  const { mxHosts, from, timeoutMs, detectCatchAll = true } = options;
  if (!mxHosts.length) {
    return { accepted: null, code: null, message: "no MX host to probe", isCatchAll: null, inconclusiveReason: "no mx" };
  }

  const heloDomain = from.split("@")[1] ?? "localhost";
  let lastError = "";

  // Try mail exchangers in priority order — the primary is sometimes busy.
  for (const host of mxHosts.slice(0, 2)) {
    let session: Session | null = null;
    try {
      session = await openSession(host, timeoutMs);

      const greeting = await session.read();
      if (greeting.code !== 220) {
        lastError = `unexpected greeting ${greeting.code}`;
        session.close();
        continue;
      }

      const ehlo = await session.send(`EHLO ${heloDomain}`);
      if (ehlo.code !== 250) {
        const helo = await session.send(`HELO ${heloDomain}`);
        if (helo.code !== 250) {
          lastError = `EHLO/HELO refused (${helo.code})`;
          session.close();
          continue;
        }
      }

      const mailFrom = await session.send(`MAIL FROM:<${from}>`);
      if (mailFrom.code !== 250) {
        lastError = `MAIL FROM refused (${mailFrom.code})`;
        session.close();
        continue;
      }

      const rcpt = await session.send(`RCPT TO:<${email}>`);
      const accepted = rcpt.code >= 200 && rcpt.code < 300;

      // 4xx is a temporary refusal (greylisting, rate limit) — it says nothing
      // about whether the mailbox exists, so it must not be read as a verdict.
      if (!accepted && rcpt.code >= 400 && rcpt.code < 500) {
        session.close();
        return {
          accepted: null,
          code: rcpt.code,
          message: rcpt.text,
          isCatchAll: null,
          inconclusiveReason: "temporary refusal (greylisting or rate limit)",
        };
      }

      let isCatchAll: boolean | null = null;
      if (accepted && detectCatchAll) {
        const domain = email.slice(email.lastIndexOf("@") + 1);
        const nonce = `x-${randomBytes(8).toString("hex")}@${domain}`;
        try {
          const probe = await session.send(`RCPT TO:<${nonce}>`);
          // If a guaranteed-nonexistent address is also accepted, the domain
          // accepts everything and the positive result above is meaningless.
          isCatchAll = probe.code >= 200 && probe.code < 300;
        } catch {
          isCatchAll = null;
        }
      }

      session.close();
      return { accepted, code: rcpt.code, message: rcpt.text, isCatchAll };
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      session?.close();
    }
  }

  const blocked = /ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|timeout/i.test(lastError);
  return {
    accepted: null,
    code: null,
    message: lastError,
    isCatchAll: null,
    inconclusiveReason: blocked
      ? "port 25 appears blocked on this network — probe cannot run here"
      : lastError,
  };
}
