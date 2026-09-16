"use server";

/**
 * Server actions for the dashboard.
 *
 * These are reachable by direct POST, not only through the UI, so each one
 * re-validates its own inputs rather than trusting the form that called it.
 * The dangerous ones (arming live sending, bulk enrollment) additionally
 * require an explicit typed confirmation.
 */
import { revalidatePath } from "next/cache";
import { z } from "zod";
import { get, run } from "@/lib/outreach/db";
import { nowIso } from "@/lib/outreach/core/time";
import { recordEvent, EVENT_TYPES } from "@/lib/outreach/core/events";
import { runDispatch, dispatchTime } from "@/lib/outreach/engine/dispatch";
import { scheduleUnscheduledEnrollments } from "@/lib/outreach/engine/enroll";
import {
  setSetting,
  getSetting,
  getBoolSetting,
  envSendingAllowed,
  setEnvSendingAllowed,
  sendMode,
  SETTING_KEYS,
} from "@/lib/outreach/core/settings";
import { suppress, unsuppress, checkSuppressed } from "@/lib/outreach/core/suppression";
import { enrollContacts, findCandidates, checkEligibility } from "@/lib/outreach/engine/enroll";
import { stopSequence, transition } from "@/lib/outreach/engine/state";
import type { Campaign, Contact, ReplyClassification, Template } from "@/lib/outreach/core/types";
import { renderEmail, buildMergeValues } from "@/lib/outreach/mail/render";
import { sendEmail } from "@/lib/outreach/mail/gmail";

// Every outreach page, by the path it is actually served at. These moved under /outreach when
// the dashboard became a section of the admin console; revalidating the standalone app's old
// root paths revalidated nothing, which showed up as a mutation that "did not take" until reload.
const OUTREACH_PATHS = [
  "/outreach",
  "/outreach/analytics",
  "/outreach/contacts",
  "/outreach/inbox",
  "/outreach/partners",
  "/outreach/queue",
  "/outreach/settings",
  "/outreach/templates",
];

async function revalidateAll(): Promise<void> {
  for (const path of OUTREACH_PATHS) {
    revalidatePath(path);
  }
}

// ── Live sending interlock ────────────────────────────────────────────────

export async function setLiveSending(formData: FormData) {
  const enable = formData.get("enable") === "true";

  if (enable) {
    // Arming live sending is the one action in the app that can contact real
    // people. A checkbox is too easy to hit by accident.
    if (String(formData.get("confirm") ?? "").trim().toUpperCase() !== "SEND LIVE") {
      return { ok: false, message: 'Type SEND LIVE exactly to arm live sending.' };
    }
  }

  await setSetting(SETTING_KEYS.liveSending, enable ? "true" : "false");
  await revalidateAll();

  if (!enable) return { ok: true, message: "Live sending switched off. The system is back in dry run." };

  return {
    ok: true,
    message: envSendingAllowed()
      ? "Live sending armed. Messages in the queue will now be delivered to real people."
      : "Campaign armed. The machine is still blocked from sending, so switch that on too.",
  };
}

// ── Dispatch ───────────────────────────────────────────────

/**
 * Send exactly the enrollments that were ticked.
 *
 * Manual mode only, and there is deliberately no "send everything" variant.
 * A single button that mails the entire queue is the one shape of mistake this
 * whole app exists to prevent.
 */
export async function sendSelected(formData: FormData) {
  const ids = formData
    .getAll("enrollment_id")
    .map((v) => Number(v))
    .filter((n) => Number.isInteger(n) && n > 0);

  // One form, two buttons. The button that was pressed contributes its own
  // name/value to the FormData, which is how the intent gets here.
  if (formData.get("intent") === "unenroll") return unenroll(ids);

  if (!ids.length) return { ok: false, message: "Tick at least one to send." };

  const summary = await runDispatch({ trigger: "manual", enrollmentIds: ids });
  await revalidateAll();

  const parts: string[] = [];
  if (summary.sent) parts.push(`${summary.sent} sent`);
  if (summary.previewed) parts.push(`${summary.previewed} rehearsed, not delivered`);
  if (summary.stopped) parts.push(`${summary.stopped} stopped`);
  if (summary.completed) parts.push(`${summary.completed} finished their sequence`);
  if (summary.skipped) parts.push(`${summary.skipped} held back`);
  if (summary.failed) parts.push(`${summary.failed} failed`);

  const detail = parts.join(", ") || "nothing happened";
  const why = summary.skippedReasons.length ? ` ${summary.skippedReasons[0]}.` : "";
  const err = summary.failed ? ` First error: ${summary.errors[0]}` : "";
  const rehearsal = (await sendMode()).live
    ? ""
    : " Sending is switched off, so nothing actually left the machine.";

  return { ok: summary.failed === 0, message: `${detail}.${rehearsal}${why}${err}` };
}

/**
 * Take people back out of a campaign.
 *
 * Two behaviours, and the difference matters. If nothing has ever been sent to
 * that enrollment, the row is deleted outright: you added someone by mistake,
 * there is no history to preserve, and leaving a "stopped" tombstone behind
 * would mean the dashboard keeps counting a decision you reversed.
 *
 * If a message HAS gone out, the enrollment is stopped rather than deleted.
 * A cold-outreach system that forgets it already emailed someone will
 * eventually email them again, and that is worse than an untidy list.
 */
async function unenroll(ids: number[]) {
  if (!ids.length) return { ok: false, message: "Tick at least one to remove." };

  let removed = 0;
  let stopped = 0;

  for (const id of ids) {
    const sent =
      (
        await get<{ n: number }>(
          `SELECT COUNT(*) n FROM messages WHERE enrollment_id = ? AND direction = 'outbound'`,
          [id],
        )
      )?.n ?? 0;

    if (sent > 0) {
      await stopSequence(id, "stopped", "removed from the campaign by hand", "human");
      stopped++;
      continue;
    }

    const enrollment = await get<{ campaign_id: number; contact_id: number }>(
      `SELECT campaign_id, contact_id FROM enrollments WHERE id = ?`,
      [id],
    );
    if (!enrollment) continue;

    await run(`DELETE FROM enrollments WHERE id = ?`, [id]);
    await recordEvent({
      type: EVENT_TYPES.enrollmentStatusChanged,
      entityType: "enrollment",
      entityId: id,
      campaignId: enrollment.campaign_id,
      contactId: enrollment.contact_id,
      payload: { to: "removed", reason: "unenrolled before anything was sent", actor: "human" },
    });
    removed++;
  }

  await revalidateAll();

  const parts: string[] = [];
  if (removed) parts.push(`${removed} removed from the campaign`);
  if (stopped) parts.push(`${stopped} stopped, kept on record because mail had already gone out`);

  return { ok: true, message: `${parts.join(". ")}.` };
}

const DISPATCH_SCHEMA = z.object({
  mode: z.enum(["manual", "automatic"]),
  time: z.string().regex(/^([01]?\d|2[0-3]):[0-5]\d$/).optional().or(z.literal("")),
});

export async function updateDispatch(formData: FormData) {
  const parsed = DISPATCH_SCHEMA.safeParse({
    mode: formData.get("mode"),
    time: String(formData.get("time") ?? ""),
  });
  if (!parsed.success) {
    return { ok: false, message: "Give the time as HH:MM on a 24-hour clock, for example 09:00." };
  }

  await setSetting(SETTING_KEYS.dispatchMode, parsed.data.mode);
  if (parsed.data.time) await setSetting(SETTING_KEYS.dispatchTime, parsed.data.time);

  if (parsed.data.mode === "manual") {
    await revalidateAll();
    return { ok: true, message: "Manual. Nothing sends unless you tick it and press send." };
  }

  // Automatic mode runs on timers, so anything without one would sit there
  // forever looking enrolled. Give those a slot before handing over.
  const scheduled = scheduleUnscheduledEnrollments();
  await revalidateAll();

  return {
    ok: true,
    message:
      `Automatic. Sending starts at ${(await dispatchTime()).text} each day, campaign time.` +
      (scheduled ? ` Gave ${scheduled} enrollment(s) without a time a slot.` : ""),
  };
}

/**
 * Switch 1: whether this machine may put mail on the wire at all.
 *
 * Writes DRY_RUN to `.env.local`. No typed confirmation here, deliberately:
 * this switch alone cannot send anything, and making both switches ceremonial
 * trains people to type past the one that matters.
 */
export async function setEnvSending(formData: FormData) {
  const enable = formData.get("enable") === "true";

  await setEnvSendingAllowed(enable);
  await revalidateAll();

  if (!enable) {
    return {
      ok: true,
      message: "Sending blocked at the machine level. Nothing goes out until this is switched back on.",
    };
  }

  return {
    ok: true,
    message: await getBoolSetting(SETTING_KEYS.liveSending)
      ? "Machine unblocked, and a campaign is already armed. Live email will now go out."
      : "Machine unblocked. Nothing sends until you arm live sending as well.",
  };
}

const SETTING_SCHEMA = z.object({
  key: z.string().min(1),
  value: z.string(),
});

export async function updateSetting(formData: FormData) {
  const parsed = SETTING_SCHEMA.safeParse({
    key: formData.get("key"),
    value: formData.get("value"),
  });
  if (!parsed.success) return { ok: false, message: "Invalid setting." };

  // live_sending has its own confirmation path; it must not be settable here.
  if (parsed.data.key === SETTING_KEYS.liveSending) {
    return { ok: false, message: "Use the live-sending control for that setting." };
  }

  await setSetting(parsed.data.key, parsed.data.value);
  await revalidateAll();
  return { ok: true, message: "Saved." };
}

// ── Campaigns ─────────────────────────────────────────────────────────────

export async function setCampaignStatus(formData: FormData) {
  const id = Number(formData.get("campaign_id"));
  const status = String(formData.get("status") ?? "");

  if (!["draft", "active", "paused", "archived"].includes(status)) {
    return { ok: false, message: "Unknown campaign status." };
  }
  const campaign = await get<Campaign>(`SELECT * FROM campaigns WHERE id = ?`, [id]);
  if (!campaign) return { ok: false, message: "Campaign not found." };

  await run(`UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?`, [status, nowIso(), id]);
  await recordEvent({
    type: EVENT_TYPES.campaignStatusChanged,
    entityType: "campaign",
    entityId: id,
    campaignId: id,
    payload: { from: campaign.status, to: status },
  });

  await revalidateAll();
  return { ok: true, message: `Campaign is now ${status}.` };
}

const CAMPAIGN_SETTINGS = z.object({
  campaign_id: z.coerce.number().int().positive(),
  daily_cap: z.coerce.number().int().min(1).max(500),
  per_domain_daily_cap: z.coerce.number().int().min(1).max(20),
  send_window_start: z.coerce.number().int().min(0).max(23),
  send_window_end: z.coerce.number().int().min(1).max(24),
  send_days: z.string().regex(/^[1-7](,[1-7])*$/, "Send days must be ISO weekday numbers, e.g. 1,2,3,4,5"),
  timezone: z.string().min(1),
});

export async function updateCampaign(formData: FormData) {
  const parsed = CAMPAIGN_SETTINGS.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    return { ok: false, message: parsed.error.issues[0]?.message ?? "Invalid campaign settings." };
  }
  const data = parsed.data;
  if (data.send_window_end <= data.send_window_start) {
    return { ok: false, message: "The send window must end after it starts." };
  }

  await run(
    `UPDATE campaigns SET daily_cap = ?, per_domain_daily_cap = ?, send_window_start = ?,
       send_window_end = ?, send_days = ?, timezone = ?, updated_at = ? WHERE id = ?`,
    [
      data.daily_cap, data.per_domain_daily_cap, data.send_window_start,
      data.send_window_end, data.send_days, data.timezone, nowIso(), data.campaign_id,
    ],
  );
  await revalidateAll();
  return { ok: true, message: "Campaign settings saved." };
}

// ── Enrollment ────────────────────────────────────────────────────────────

/**
 * Enroll the ticked contacts, and top up from the list if you ticked fewer
 * than you asked for.
 *
 * The two halves matter in that order. Anyone you picked by hand goes in
 * first, so a top-up can never displace a deliberate choice; it only fills
 * whatever is left. Setting the target to the number you ticked turns the
 * top-up off entirely.
 */
export async function enrollSelected(formData: FormData) {
  const campaignId = Number(formData.get("campaign_id"));
  if (!Number.isInteger(campaignId) || campaignId <= 0) {
    return { ok: false, message: "Pick a campaign first." };
  }

  const picked = formData
    .getAll("contact_id")
    .map((v) => Number(v))
    .filter((n) => Number.isInteger(n) && n > 0);

  const target = Math.min(Math.max(Number(formData.get("target") ?? picked.length), 0), 250);
  const roleGroup = String(formData.get("role_group") ?? "") || undefined;
  const industry = String(formData.get("industry") ?? "") || undefined;

  if (!picked.length && target === 0) {
    return { ok: false, message: "Tick some contacts, or set a number for the system to choose." };
  }

  // The gap the system fills. Never negative: ticking more than the target
  // means you meant to enroll all of them, not that some should be dropped.
  const shortfall = Math.max(target - picked.length, 0);
  let autoIds: number[] = [];

  if (shortfall > 0) {
    autoIds = (
      await findCandidates({
        roleGroup,
        industry,
        excludeEnrolled: true,
        // Over-fetch, because the picked ones will be in here too and have to
        // come out before we count.
        limit: shortfall + picked.length + 50,
      })
    )
      .map((c) => c.id)
    .filter((id) => !picked.includes(id))
    .slice(0, shortfall);
  }

  const result = await enrollContacts({ campaignId, contactIds: [...picked, ...autoIds] });
  await revalidateAll();

  const parts: string[] = [];
  if (picked.length) parts.push(`${picked.length} you chose`);
  if (autoIds.length) parts.push(`${autoIds.length} chosen for you`);
  if (shortfall > autoIds.length) {
    parts.push(`${shortfall - autoIds.length} short, the list ran out of eligible contacts`);
  }

  return {
    ok: (await result).enrolled > 0,
    message:
      `Enrolled ${(await result).enrolled}` +
      (parts.length ? ` (${parts.join(", ")})` : "") +
      (result.alreadyEnrolled ? `. ${(await result).alreadyEnrolled} were already in a sequence` : "") +
      ".",
    skipped: (await result).skipped,
  };
}

export async function enrollFiltered(formData: FormData) {
  const campaignId = Number(formData.get("campaign_id"));
  const limit = Math.min(Number(formData.get("limit") ?? 25), 250);
  const roleGroup = String(formData.get("role_group") ?? "") || undefined;
  const industry = String(formData.get("industry") ?? "") || undefined;
  const sheet = String(formData.get("sheet") ?? "") || undefined;

  if (!Number.isInteger(campaignId) || campaignId <= 0) {
    return { ok: false, message: "Pick a campaign first." };
  }

  const candidates = await findCandidates({
    roleGroup, industry, sheet,
    excludeEnrolled: true,
    limit,
  });

  if (!(await candidates).length) {
    return { ok: false, message: "No unenrolled contacts match that filter." };
  }

  const result = await enrollContacts({
    campaignId,
    contactIds: (await candidates).map((c) => c.id),
  });

  await revalidateAll();

  const skippedNote = (await result).skipped.length
    ? ` ${(await result).skipped.length} held back (not verified, no address, or a name the greeting can't use).`
    : "";

  return {
    ok: true,
    message: `Enrolled ${(await result).enrolled} contact(s).${skippedNote}`,
    skipped: (await result).skipped.slice(0, 25),
  };
}

export async function enrollOne(formData: FormData) {
  const campaignId = Number(formData.get("campaign_id"));
  const contactId = Number(formData.get("contact_id"));
  const force = formData.get("force") === "true";

  if (!campaignId || !contactId) return { ok: false, message: "Missing campaign or contact." };

  const result = await enrollContacts({ campaignId, contactIds: [contactId], force });
  await revalidateAll();

  if (result.enrolled) {
    return {
      ok: true,
      message: force ? "Enrolled and parked for review. It will not send until you release it." : "Enrolled.",
    };
  }
  if (result.alreadyEnrolled) return { ok: false, message: "Already enrolled in that campaign." };
  return { ok: false, message: (await result).skipped[0]?.reasons.join("; ") ?? "Not eligible." };
}

export async function stopEnrollment(formData: FormData) {
  const enrollmentId = Number(formData.get("enrollment_id"));
  const reason = String(formData.get("reason") ?? "stopped by hand");
  if (!enrollmentId) return { ok: false, message: "Missing enrollment." };

  await stopSequence(enrollmentId, "stopped", reason, "human");
  await revalidateAll();
  return { ok: true, message: "Sequence stopped." };
}

export async function resumeEnrollment(formData: FormData) {
  const enrollmentId = Number(formData.get("enrollment_id"));
  if (!enrollmentId) return { ok: false, message: "Missing enrollment." };

  // A release from review is still an entry into the cold sequence, so it
  // faces the same gate as enrollment. Without this, force-enroll followed
  // by a release would activate a contact the gate had just refused.
  const contact = await get<import("@/lib/outreach/core/types").Contact>(
    `SELECT c.* FROM contacts c JOIN enrollments e ON e.contact_id = c.id WHERE e.id = ?`,
    [enrollmentId],
  );
  if (!contact) return { ok: false, message: "Enrollment has no contact." };
  const eligibility = await checkEligibility(contact);
  if (!(await eligibility).eligible) {
    return { ok: false, message: `Refused: ${(await eligibility).reasons.join("; ")}` };
  }

  await transition(enrollmentId, "active", {
    nextSendAt: nowIso(),
    reason: "released by a human from review",
    actor: "human",
  });
  await revalidateAll();
  return { ok: true, message: "Sequence resumed. It sends on the next worker tick inside the send window." };
}

// ── Reply triage ──────────────────────────────────────────────────────────

const CLASSIFICATIONS: ReplyClassification[] = ["positive", "negative", "neutral", "ooo", "auto", "bounce"];

export async function reviewReply(formData: FormData) {
  const replyId = Number(formData.get("reply_id"));
  const override = String(formData.get("classification") ?? "") as ReplyClassification;

  if (!replyId) return { ok: false, message: "Missing reply." };
  if (!CLASSIFICATIONS.includes(override)) return { ok: false, message: "Unknown classification." };

  const reply = await get<{
    id: number; enrollment_id: number | null; contact_id: number | null; classification: string;
  }>(`SELECT * FROM replies WHERE id = ?`, [replyId]);
  if (!reply) return { ok: false, message: "Reply not found." };

  await run(
    `UPDATE replies SET human_override = ?, classification = ?, classifier = 'human',
       reviewed_at = ?, reviewed_by = 'dashboard', requires_review = 0 WHERE id = ?`,
    [override, override, nowIso(), replyId],
  );

  await recordEvent({
    type: EVENT_TYPES.replyReviewed,
    entityType: "reply",
    entityId: replyId,
    contactId: reply.contact_id,
    payload: { from: reply.classification, to: override },
  });

  // A human decision moves the sequence the same way the classifier would
  // have, so a corrected label actually changes what happens next.
  if (reply.enrollment_id) {
    if (override === "negative") {
      await stopSequence(reply.enrollment_id, "stopped", "negative reply (confirmed by a human)", "human");
    } else if (override === "positive") {
      await stopSequence(reply.enrollment_id, "replied", "positive reply (confirmed by a human)", "human");
    } else if (override === "bounce") {
      await stopSequence(reply.enrollment_id, "bounced", "bounce (confirmed by a human)", "human");
    }
  }

  await revalidateAll();
  return { ok: true, message: `Marked as ${override}.` };
}

export async function dismissReview(formData: FormData) {
  const replyId = Number(formData.get("reply_id"));
  if (!replyId) return { ok: false, message: "Missing reply." };

  await run(`UPDATE replies SET requires_review = 0, reviewed_at = ?, reviewed_by = 'dashboard' WHERE id = ?`, [
    nowIso(),
    replyId,
  ]);
  await revalidateAll();
  return { ok: true, message: "Cleared from the review queue." };
}

// ── Suppression ───────────────────────────────────────────────────────────

export async function addSuppression(formData: FormData) {
  const rawValue = String(formData.get("value") ?? "").trim().toLowerCase();
  const scope = String(formData.get("scope") ?? "email");
  const reason = String(formData.get("reason") ?? "added by hand");

  if (!rawValue) return { ok: false, message: "Enter an address or domain." };
  if (scope !== "email" && scope !== "domain") return { ok: false, message: "Scope must be email or domain." };
  if (scope === "email" && !rawValue.includes("@")) {
    return { ok: false, message: "That does not look like an email address." };
  }
  if (scope === "domain" && rawValue.includes("@")) {
    return { ok: false, message: "Enter a bare domain, with no @." };
  }

  const added = await suppress({ scope, value: rawValue, reason, source: "manual" });
  await revalidateAll();
  return added
    ? { ok: true, message: `Suppressed ${rawValue}. Nothing will be sent there again.` }
    : { ok: false, message: "Already on the suppression list." };
}

export async function removeSuppression(formData: FormData) {
  const value = String(formData.get("value") ?? "");
  const scope = String(formData.get("scope") ?? "email");
  if (scope !== "email" && scope !== "domain") return { ok: false, message: "Bad scope." };

  const removed = await unsuppress(scope, value);
  await revalidateAll();
  return removed
    ? { ok: true, message: `Removed ${value} from the suppression list.` }
    : { ok: false, message: "Not found." };
}

// ── Templates ─────────────────────────────────────────────────────────────

const TEMPLATE_SCHEMA = z.object({
  template_id: z.coerce.number().int().positive(),
  subject: z.string().max(300),
  body: z.string().min(1, "The body cannot be empty.").max(20000),
});

export async function updateTemplate(formData: FormData) {
  const parsed = TEMPLATE_SCHEMA.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    return { ok: false, message: parsed.error.issues[0]?.message ?? "Invalid template." };
  }

  await run(`UPDATE templates SET subject = ?, body = ?, updated_at = ? WHERE id = ?`, [
    parsed.data.subject,
    parsed.data.body,
    nowIso(),
    parsed.data.template_id,
  ]);
  await revalidateAll();
  return { ok: true, message: "Template saved." };
}

// ── One-off test sends ────────────────────────────────────────────────────
//
// DELIBERATE BYPASS, and worth understanding before changing it.
//
// Campaign sending is gated behind two switches because the risk it guards is
// a 220-message blast at a list of real prospects. This tool is a different
// shape of risk: one message, to one address you typed by hand, triggered by
// one click, with no automation behind it. Holding it behind the campaign
// interlock would mean arming the whole outreach engine just to check that
// Gmail is wired up, which is a worse trade.
//
// So it has its own switch (`test_sending`), off by default, and ignores
// DRY_RUN and live_sending. What it does NOT skip: it still refuses to send
// to a suppressed address, and it still writes an audit event. Nothing this
// system sends should ever be invisible afterwards.
//
// It writes no contact, no enrollment and no message row, so it cannot
// pollute analytics or make a company look contacted.

export async function setTestSending(formData: FormData) {
  const enable = formData.get("enable") === "true";
  await setSetting(SETTING_KEYS.testSending, enable ? "true" : "false");
  await revalidateAll();
  return {
    ok: true,
    message: enable
      ? "Test sending is on. One-off messages will be delivered for real."
      : "Test sending is off.",
  };
}

const TEST_SEND = z.object({
  to: z.string().trim().email("That does not look like an email address."),
  template_id: z.coerce.number().int().positive(),
  first_name: z.string().trim().max(80).optional(),
  company: z.string().trim().max(120).optional(),
});

export async function sendTestEmail(formData: FormData) {
  const parsed = TEST_SEND.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    return { ok: false, message: parsed.error.issues[0]?.message ?? "Invalid input." };
  }
  const { to, template_id, first_name, company } = parsed.data;

  if (!await getBoolSetting(SETTING_KEYS.testSending)) {
    return { ok: false, message: "Turn on test sending first." };
  }

  const suppression = await checkSuppressed(to);
  if (suppression.suppressed) {
    return {
      ok: false,
      message: `${to} is on the suppression list (${(await suppression).reason}). Not sending, even as a test.`,
    };
  }

  const template = await get<Template>(`SELECT * FROM templates WHERE id = ?`, [template_id]);
  if (!template) return { ok: false, message: "Template not found." };

  const from = await getSetting(SETTING_KEYS.sendingAddress);
  if (!from) return { ok: false, message: "No mailbox connected. Run: npm run gmail:auth" };

  // A synthetic contact, never persisted. Fallbacks mean the render cannot
  // fail on a missing merge tag the way a real contact's would.
  const stand_in = {
    first_name: first_name || "there",
    full_name: first_name || "there",
    company: company || "your team",
    role: "engineer",
    industry: "Aerospace",
    tier: "Test",
    stage: "Test",
    email_normalized: to,
    domain: to.split("@")[1],
    priority: null,
  } as unknown as Contact;

  try {
    const rendered = renderEmail(
      template.subject || "Hexera test",
      template.body,
      buildMergeValues(stand_in, {
        signature: await getSetting(SETTING_KEYS.signature),
        unsubscribe: await getSetting(SETTING_KEYS.unsubscribeLine),
      }),
    );

    const result = await sendEmail({
      from,
      fromName: await getSetting(SETTING_KEYS.sendingName),
      to,
      subject: rendered.subject,
      body: rendered.body,
    });

    await recordEvent({
      type: "message.test_sent",
      entityType: "test",
      payload: { to, template: template.name, gmail_message_id: result.gmailMessageId },
    });

    await revalidateAll();
    return {
      ok: true,
      message: `Sent to ${to}. Gmail id ${result.gmailMessageId}. Check the inbox.`,
    };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    await recordEvent({ type: "message.test_failed", entityType: "test", payload: { to, error: detail } });
    return { ok: false, message: `Gmail refused it: ${detail}` };
  }
}
