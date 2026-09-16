/**
 * Reply classification — rules first, always.
 *
 * Design principles, in priority order:
 *
 *  1. **Auto-replies are not replies.** An out-of-office must never cancel a
 *     follow-up sequence, and a bounce must never be read as a human "no".
 *     These are separated before any sentiment scoring happens.
 *
 *  2. **Opt-outs are absolute.** "Take us off your list" short-circuits
 *     everything with maximum confidence. There is no scoring path that can
 *     outvote it, because getting this wrong is both a relationship-ender and
 *     a legal problem.
 *
 *  3. **Negative beats positive on a tie.** The cost of wrongly stopping a
 *     warm lead is one missed follow-up that a human will see in the triage
 *     queue anyway. The cost of wrongly continuing after a "no" is a
 *     complaint. Those are not symmetric.
 *
 *  4. **Low confidence routes to a human.** `requires_review` is not a
 *     failure state — it is the intended destination for anything genuinely
 *     ambiguous, and the sequence stops while it waits.
 */
import type { ReplyClassification } from "../core/types";

export interface ClassificationResult {
  classification: ReplyClassification;
  confidence: number;
  matchedRules: string[];
  requiresReview: boolean;
  /** Set when the reply asks to stop contacting the whole company, not just this person. */
  optOutScope: "email" | "domain" | null;
  classifier: "rules" | "llm";
}

interface Rule {
  id: string;
  pattern: RegExp;
  weight: number;
}

/**
 * Explicit opt-out. These end the conversation with the organization, so they
 * suppress at domain scope.
 */
const OPT_OUT_DOMAIN: Rule[] = [
  { id: "unsubscribe", pattern: /\bunsubscribe\b/i, weight: 1 },
  { id: "remove_from_list", pattern: /\b(remove|take)\s+(me|us|this|our)?\s*(from|off)\s+(your|the|this)?\s*(mailing\s+)?list\b/i, weight: 1 },
  { id: "take_us_off", pattern: /\btake\s+(me|us)\s+off\b/i, weight: 1 },
  { id: "stop_emailing", pattern: /\b(stop|cease)\s+(emailing|contacting|messaging|sending)\b/i, weight: 1 },
  { id: "do_not_contact", pattern: /\b(do\s*not|don'?t)\s+(contact|email|message)\s+(me|us|this)\b/i, weight: 1 },
  { id: "no_solicitation", pattern: /\b(no|unsolicited)\s+(solicitation|vendors?|sales\s+(emails?|pitches?))\b/i, weight: 1 },
  { id: "gdpr", pattern: /\b(gdpr|ccpa|data\s+protection)\b.*\b(request|delete|erase)\b/i, weight: 1 },
];

/** Rejection of this offer, but not necessarily a request to never be contacted again. */
const NEGATIVE: Rule[] = [
  { id: "not_interested", pattern: /\bnot\s+interested\b/i, weight: 3 },
  { id: "no_thanks", pattern: /\bno,?\s+thank(s|\s+you)\b/i, weight: 3 },
  { id: "not_a_fit", pattern: /\b(not|isn'?t)\s+(a\s+)?(good\s+)?fit\b/i, weight: 3 },
  { id: "we_pass", pattern: /\b(we'?ll|i'?ll|we|i)\s+pass\b/i, weight: 3 },
  { id: "no_budget", pattern: /\bno\s+(budget|funding|money)\b/i, weight: 2 },
  { id: "already_have", pattern: /\b(we\s+)?already\s+(have|use|using|work\s+with)\b/i, weight: 2 },
  { id: "built_in_house", pattern: /\b(in[-\s]house|internally|ourselves)\b.*\b(built|develop|handle|solve)/i, weight: 2 },
  { id: "wrong_person", pattern: /\b(wrong|not\s+the\s+right)\s+(person|contact|team)\b/i, weight: 1 },
  { id: "no_need", pattern: /\b(don'?t|do\s+not)\s+(have\s+a\s+)?need\b/i, weight: 2 },
  { id: "not_looking", pattern: /\b(not|aren'?t)\s+(currently\s+)?looking\b/i, weight: 2 },
  { id: "please_stop", pattern: /\bplease\s+stop\b/i, weight: 3 },
  { id: "spam_accusation", pattern: /\b(this\s+is\s+)?spam\b/i, weight: 3 },
];

const POSITIVE: Rule[] = [
  { id: "interested", pattern: /\b(i'?m|we'?re|am|are)\s+(very\s+|quite\s+|definitely\s+)?interested\b/i, weight: 3 },
  { id: "interested_bare", pattern: /\binterested\b/i, weight: 2 },
  { id: "lets_talk", pattern: /\b(let'?s|happy\s+to|glad\s+to|would\s+like\s+to)\s+(chat|talk|connect|discuss|meet|jump\s+on)\b/i, weight: 3 },
  { id: "book_call", pattern: /\b(book|schedule|set\s+up|arrange)\s+(a\s+)?(call|meeting|time|demo|chat)\b/i, weight: 3 },
  { id: "calendar_link", pattern: /\b(calendly\.com|cal\.com|savvycal|hubspot\.com\/meetings)\b/i, weight: 3 },
  { id: "send_more", pattern: /\b(send|share)\s+(me|us|over)?\s*(more|some)?\s*(info|information|details|deck|docs)\b/i, weight: 3 },
  { id: "tell_me_more", pattern: /\btell\s+me\s+more\b/i, weight: 3 },
  { id: "sounds_good", pattern: /\b(sounds|looks)\s+(good|interesting|great)\b/i, weight: 2 },
  { id: "when_free", pattern: /\b(when|what\s+time)\s+(are|would)\s+you\s+(free|available)\b/i, weight: 3 },
  { id: "pricing", pattern: /\b(pricing|price|cost|how\s+much|quote)\b/i, weight: 2 },
  { id: "demo", pattern: /\b(see\s+a\s+)?demo\b/i, weight: 2 },
  { id: "worth_a_look", pattern: /\bworth\s+(a\s+)?(look|exploring|discussing)\b/i, weight: 2 },
  { id: "technical_question", pattern: /\b(does|can|what)\s+(it|this|hexera)\s+(support|handle|work\s+with)\b/i, weight: 2 },
];

/** Interest that exists but is deferred. Real, but not now. */
const DEFER: Rule[] = [
  { id: "circle_back", pattern: /\b(circle\s+back|touch\s+base|follow\s+up|reach\s+out)\s+(in|next|later|after|around|q[1-4])\b/i, weight: 2 },
  { id: "not_right_now", pattern: /\b(not\s+(right\s+now|at\s+the\s+moment|currently)|bad\s+timing|too\s+early)\b/i, weight: 2 },
  { id: "keep_in_touch", pattern: /\bkeep\s+(me|us)\s+(posted|in\s+the\s+loop|informed)\b/i, weight: 2 },
  { id: "check_back", pattern: /\b(check|ping)\s+(back|in)\s+(with\s+(me|us)\s+)?(in|next|later)\b/i, weight: 2 },
];

/** Hand-off to someone else. Engagement, and needs a human to act on it. */
const REFERRAL: Rule[] = [
  { id: "loop_in", pattern: /\b(cc'?ing|copying|looping\s+in|introduc(e|ing)\s+you)\b/i, weight: 3 },
  { id: "talk_to", pattern: /\b(you\s+should|better\s+to|please)\s+(talk|speak|reach\s+out)\s+to\b/i, weight: 3 },
  { id: "forwarded", pattern: /\bi'?(ve|ll)\s+forward(ed)?\b/i, weight: 2 },
  { id: "right_person", pattern: /\b(is|would\s+be)\s+the\s+right\s+person\b/i, weight: 2 },
];

const OUT_OF_OFFICE: Rule[] = [
  { id: "ooo_phrase", pattern: /\bout\s+of\s+(the\s+)?office\b/i, weight: 3 },
  { id: "ooo_auto", pattern: /\bauto(matic)?[-\s]?repl(y|ies)\b/i, weight: 3 },
  { id: "on_leave", pattern: /\b(on|taking)\s+(annual\s+|parental\s+|maternity\s+|paternity\s+|sick\s+|medical\s+)?leave\b/i, weight: 3 },
  { id: "vacation", pattern: /\b(on\s+)?(vacation|holiday|pto)\b/i, weight: 3 },
  { id: "away_until", pattern: /\b(away|out|back|returning)\s+(from\s+the\s+office\s+)?(until|on|till)\b/i, weight: 3 },
  { id: "limited_access", pattern: /\blimited\s+access\s+to\s+(my\s+)?e?-?mail\b/i, weight: 3 },
  { id: "no_longer_here", pattern: /\bno\s+longer\s+(with|at|works?\s+(at|for))\b/i, weight: 3 },
];

const BOUNCE: Rule[] = [
  { id: "dsn_subject", pattern: /\b(delivery\s+status\s+notification|undeliverable|delivery\s+has\s+failed|returned\s+mail|mail\s+delivery\s+failed)\b/i, weight: 3 },
  { id: "address_not_found", pattern: /\b(address\s+not\s+found|recipient\s+(address\s+)?rejected|user\s+unknown|no\s+such\s+user|mailbox\s+(is\s+)?unavailable)\b/i, weight: 3 },
  { id: "smtp_550", pattern: /\b5\.[157]\.[0-9]\b|\b550[\s-]/i, weight: 3 },
  { id: "does_not_exist", pattern: /\b(account|address|mailbox)\s+(does\s+not|doesn'?t)\s+exist\b/i, weight: 3 },
];

const BOUNCE_SENDERS = /^(mailer-daemon|postmaster|no-?reply|nobody)@/i;

function applyRules(text: string, rules: Rule[]): { score: number; matched: string[] } {
  let score = 0;
  const matched: string[] = [];
  for (const rule of rules) {
    if (rule.pattern.test(text)) {
      score += rule.weight;
      matched.push(rule.id);
    }
  }
  return { score, matched };
}

export interface ClassifyInput {
  /** Body with quoted history already stripped — see stripQuotedText. */
  body: string;
  subject: string;
  fromEmail: string;
  /** From Auto-Submitted / Precedence headers. */
  isAutoSubmitted: boolean;
}

export function classifyReply(input: ClassifyInput): ClassificationResult {
  const haystack = `${input.subject}\n${input.body}`;

  // ── 1. Bounces ──────────────────────────────────────────────────────────
  const bounce = applyRules(haystack, BOUNCE);
  if (BOUNCE_SENDERS.test(input.fromEmail) && bounce.score > 0) {
    return {
      classification: "bounce",
      confidence: 0.97,
      matchedRules: ["sender:mailer-daemon", ...bounce.matched],
      requiresReview: false,
      optOutScope: null,
      classifier: "rules",
    };
  }
  if (bounce.score >= 6) {
    return {
      classification: "bounce",
      confidence: 0.85,
      matchedRules: bounce.matched,
      requiresReview: false,
      optOutScope: null,
      classifier: "rules",
    };
  }

  // ── 2. Opt-out — absolute, checked before anything can outweigh it ──────
  const optOut = applyRules(haystack, OPT_OUT_DOMAIN);
  if (optOut.score > 0) {
    return {
      classification: "negative",
      confidence: 0.98,
      matchedRules: optOut.matched,
      requiresReview: false,
      // An unsubscribe request covers the organization, not just the mailbox
      // that happened to answer.
      optOutScope: "domain",
      classifier: "rules",
    };
  }

  // ── 3. Out of office ────────────────────────────────────────────────────
  const ooo = applyRules(haystack, OUT_OF_OFFICE);
  if (ooo.score >= 3) {
    return {
      classification: "ooo",
      confidence: input.isAutoSubmitted ? 0.95 : 0.8,
      matchedRules: ooo.matched,
      // "No longer with the company" looks like an OOO but means the contact
      // is dead and someone needs to re-target the account.
      requiresReview: ooo.matched.includes("no_longer_here"),
      optOutScope: null,
      classifier: "rules",
    };
  }

  // A machine sent it and no rule identified it — do not treat it as a human reply.
  if (input.isAutoSubmitted) {
    return {
      classification: "auto",
      confidence: 0.85,
      matchedRules: ["header:auto-submitted"],
      requiresReview: false,
      optOutScope: null,
      classifier: "rules",
    };
  }

  // ── 4. Sentiment ────────────────────────────────────────────────────────
  const negative = applyRules(haystack, NEGATIVE);
  const positive = applyRules(haystack, POSITIVE);
  const defer = applyRules(haystack, DEFER);
  const referral = applyRules(haystack, REFERRAL);

  const matched = [...negative.matched, ...positive.matched, ...defer.matched, ...referral.matched];

  // A referral is engagement even when the words around it are a soft no.
  if (referral.score >= 3 && negative.score < 3) {
    return {
      classification: "positive",
      confidence: 0.75,
      matchedRules: matched,
      requiresReview: true,
      optOutScope: null,
      classifier: "rules",
    };
  }

  if (negative.score > 0 && negative.score >= positive.score) {
    const decisive = negative.score >= 3;
    return {
      classification: "negative",
      confidence: decisive ? 0.88 : 0.6,
      matchedRules: matched,
      requiresReview: !decisive,
      // A plain "not interested" is not an opt-out request; suppress only this
      // address so a different contact at the company stays reachable.
      optOutScope: decisive ? "email" : null,
      classifier: "rules",
    };
  }

  if (positive.score >= 3) {
    return {
      classification: "positive",
      confidence: positive.score >= 5 ? 0.9 : 0.75,
      matchedRules: matched,
      requiresReview: false,
      optOutScope: null,
      classifier: "rules",
    };
  }

  if (defer.score >= 2) {
    return {
      classification: "neutral",
      confidence: 0.7,
      matchedRules: matched,
      requiresReview: true,
      optOutScope: null,
      classifier: "rules",
    };
  }

  if (positive.score > 0) {
    return {
      classification: "positive",
      confidence: 0.55,
      matchedRules: matched,
      requiresReview: true,
      optOutScope: null,
      classifier: "rules",
    };
  }

  // A human wrote something the rules do not recognize. Stop the sequence and
  // let a person read it — this is the designed destination for ambiguity,
  // not a fallback.
  return {
    classification: "neutral",
    confidence: 0.3,
    matchedRules: matched,
    requiresReview: true,
    optOutScope: null,
    classifier: "rules",
  };
}
