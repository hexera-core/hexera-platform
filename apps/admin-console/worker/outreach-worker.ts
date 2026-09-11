/**
 * The outreach sender, as one tick of a scheduled Cloud Run Job: verify -> send -> poll.
 *
 * WHY A SINGLE TICK AND NOT A LOOP. On a laptop this ran continuously and slept between ticks;
 * hosted, the schedule is the loop. A long-lived Cloud Run Job holding a database connection open
 * to sleep for a minute is a worse version of the same thing, and it hides failures - a crashed
 * loop looks identical to a quiet one, whereas a failed execution is visible.
 *
 * SENDING OBEYS THE TWO-SWITCH INTERLOCK. With either switch off this still verifies, still reads
 * replies, and still renders every due message so it can be read in the log - but it writes
 * nothing and advances nothing. A rehearsal leaves no trace.
 */
import { EVENT_TYPES, recordEvent } from "@/lib/outreach/core/events";
import { seedDefaultSettings, sendMode } from "@/lib/outreach/core/settings";
import { closeDb } from "@/lib/outreach/db";
import { dispatchMode, runDispatch } from "@/lib/outreach/engine/dispatch";
import { capacitySnapshot } from "@/lib/outreach/engine/scheduler";
import { pollReplies } from "@/lib/outreach/inbox/poller";
import { storedToken } from "@/lib/outreach/mail/gmail";
import { verifyAllContacts } from "@/lib/outreach/verify/index";

function log(message: string): void {
  console.log(`[${new Date().toISOString()}] ${message}`);
}

async function tick(): Promise<Record<string, number>> {
  const summary = {
    completed: 0, failed: 0, held: 0, previewed: 0,
    repliesFound: 0, sent: 0, skipped: 0, stopped: 0, verified: 0,
  };

  // 1. Verify anything new. Bounded per tick so one slow provider cannot starve the send step.
  try {
    const verification = await verifyAllContacts({ limit: 25 });
    summary.verified = verification.checked;
    if (verification.checked > 0) {
      log(`verified ${verification.checked} contact(s): ${JSON.stringify(verification.byStatus)}`);
    }
  } catch (error) {
    log(`verification error: ${error instanceof Error ? error.message : String(error)}`);
  }

  // 2. Send whatever is due, if dispatch says we may. The dashboard's button calls this same
  //    function; the only difference is the trigger, which is what lets the button ignore the
  //    release time.
  const dispatch = await runDispatch({ trigger: "automatic" });
  Object.assign(summary, {
    completed: dispatch.completed, failed: dispatch.failed, held: dispatch.held,
    previewed: dispatch.previewed, sent: dispatch.sent, skipped: dispatch.skipped,
    stopped: dispatch.stopped,
  });

  if (dispatch.attempted > 0) {
    log(
      `dispatched ${dispatch.attempted}: ${dispatch.sent} sent, ${dispatch.previewed} rehearsed, ` +
        `${dispatch.stopped} stopped, ${dispatch.failed} failed`,
    );
  }
  // Holding is the resting state in manual mode. Said once per tick and only when something is
  // actually waiting - silence here would read as "nothing was due".
  if (dispatch.held > 0) log(`${dispatch.held} due but holding: ${dispatch.heldReason}`);
  for (const error of dispatch.errors) log(`  FAILED ${error}`);

  // 3. Read replies. Skipped with no mailbox connected: there is nothing to read, and the failure
  //    would otherwise repeat every tick forever.
  if ((await storedToken())?.refresh_token) {
    try {
      const replies = await pollReplies();
      summary.repliesFound = replies.newReplies;
      if (replies.newReplies > 0) {
        log(
          `${replies.newReplies} new reply(ies): ${JSON.stringify(replies.byClassification)} - ` +
            `${replies.stopped} sequence(s) stopped, ${replies.suppressed} suppression(s) added`,
        );
      }
      for (const error of replies.errors) log(`  reply error: ${error}`);
    } catch (error) {
      log(`reply polling error: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  await recordEvent({ entityType: "worker", payload: summary, type: EVENT_TYPES.workerTick });
  return summary;
}

async function main(): Promise<void> {
  await seedDefaultSettings();

  const mode = await sendMode();
  const capacity = await capacitySnapshot();
  log(`outreach tick - ${mode.headline}`);
  log(`  dispatch: ${await dispatchMode()}; used today: ${capacity.globalUsed}`);

  // STATED EVERY TICK, not once at startup. A job execution is read in isolation in the log, so
  // "this one did not send" has to be answerable from that execution alone.
  if (!mode.live) log(`  REHEARSAL: ${mode.detail}`);

  try {
    const summary = await tick();
    log(`tick complete: ${JSON.stringify(summary)}`);
  } finally {
    // The pool outlives the tick otherwise, and the execution hangs until its task timeout.
    await closeDb();
  }
}

main().catch((error) => {
  log(`tick FAILED: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
  process.exitCode = 1;
});
