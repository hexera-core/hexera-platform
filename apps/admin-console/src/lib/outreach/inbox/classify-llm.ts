/**
 * Optional second-opinion classifier backed by Claude.
 *
 * This is deliberately a *supplement*, never a replacement. The rules in
 * classify.ts run first and always, and this only gets consulted when the
 * rules were not confident. Two consequences follow, and both are enforced
 * in `classifyWithLlm` below:
 *
 *  - A rules-based hard stop (explicit opt-out, bounce) is never re-litigated.
 *    Those outcomes are legally and relationally significant, and a
 *    probabilistic model should not be able to overturn them.
 *  - If no API key is configured, or the call fails for any reason, the
 *    rules verdict stands and the reply is routed to human review. The
 *    pipeline never blocks on this being available.
 */
import { z } from "zod";
import type { ClassificationResult } from "./classify";
import type { ReplyClassification } from "../core/types";

const ReplyAnalysis = z.object({
  classification: z.enum(["positive", "negative", "neutral", "ooo", "auto", "bounce"]),
  confidence: z.number().min(0).max(1),
  opt_out_requested: z.boolean(),
  reasoning: z.string(),
});

const SYSTEM_PROMPT = `You classify replies to cold outreach emails sent by Hexera, a company that automates CAD-to-mesh generation for aerospace CFD simulation.

Classify the reply into exactly one category:

- positive: interest, a question about the product, a request for a call/demo/pricing, or a referral to a colleague.
- negative: a rejection, a statement that this is not relevant, or a request to stop contacting them.
- neutral: a human wrote back but their intent is genuinely unclear, or they are deferring to a later date.
- ooo: an out-of-office or leave auto-responder.
- auto: any other automated message (ticket acknowledgement, no-reply notification, mailing list).
- bounce: a delivery failure notice.

Set opt_out_requested to true only when they ask not to be contacted again, as opposed to simply declining this offer.

Two judgement calls that matter:
- A deferral ("circle back next quarter") is neutral, not negative. They have not said no.
- A referral to someone else is positive, even when the person writing is declining for themselves.

Be conservative: when a reply could plausibly be read as negative, classify it as negative. A missed follow-up costs one email; contacting someone who asked you to stop costs the relationship.`;

export interface LlmClassifierOptions {
  apiKey?: string;
  model?: string;
}

export function isLlmClassifierAvailable(): boolean {
  return Boolean(process.env.ANTHROPIC_API_KEY);
}

export async function classifyWithLlm(
  input: { subject: string; body: string; fromEmail: string },
  rulesVerdict: ClassificationResult,
  options: LlmClassifierOptions = {},
): Promise<ClassificationResult> {
  const apiKey = options.apiKey ?? process.env.ANTHROPIC_API_KEY;
  if (!apiKey) return rulesVerdict;

  // Hard stops are not open to reinterpretation.
  if (rulesVerdict.optOutScope !== null || rulesVerdict.classification === "bounce") {
    return rulesVerdict;
  }

  try {
    const { default: Anthropic } = await import("@anthropic-ai/sdk");
    const { zodOutputFormat } = await import("@anthropic-ai/sdk/helpers/zod");
    const client = new Anthropic({ apiKey });

    const response = await client.messages.parse({
      model: options.model ?? process.env.CLASSIFIER_MODEL ?? "claude-opus-5",
      max_tokens: 16000,
      system: SYSTEM_PROMPT,
      // Classification is a short, scoped task; low effort keeps latency and
      // cost down without hurting accuracy here.
      output_config: {
        effort: "low",
        format: zodOutputFormat(ReplyAnalysis),
      },
      messages: [
        {
          role: "user",
          content:
            `From: ${input.fromEmail}\n` +
            `Subject: ${input.subject}\n\n` +
            `${input.body.slice(0, 6000)}`,
        },
      ],
    });

    // A safety refusal is not a classification. Keep the rules verdict and
    // send it to a human rather than inventing an outcome.
    if (response.stop_reason === "refusal") {
      return { ...rulesVerdict, requiresReview: true };
    }

    const parsed = response.parsed_output;
    if (!parsed) return { ...rulesVerdict, requiresReview: true };

    const classification = parsed.classification as ReplyClassification;

    return {
      classification,
      confidence: parsed.confidence,
      matchedRules: [...rulesVerdict.matchedRules, `llm:${parsed.reasoning.slice(0, 160)}`],
      // Disagreement between the two classifiers is exactly the case a human
      // should look at, even when the model is confident.
      requiresReview:
        parsed.confidence < 0.75 || classification !== rulesVerdict.classification,
      optOutScope: parsed.opt_out_requested ? "domain" : null,
      classifier: "llm",
    };
  } catch (error) {
    console.warn(
      `LLM classifier unavailable (${error instanceof Error ? error.message : String(error)}); ` +
        `keeping the rules verdict and flagging for review.`,
    );
    return { ...rulesVerdict, requiresReview: true };
  }
}
