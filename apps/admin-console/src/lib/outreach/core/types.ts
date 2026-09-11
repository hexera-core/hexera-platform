/**
 * Shared domain types. The string-literal unions here mirror the CHECK-free
 * TEXT columns in schema.sql — SQLite will not enforce them, so these types
 * plus the guards below are the enforcement layer.
 */

export type SourceSheet = "Ambitious" | "Realistic";
export type Priority = "HOT" | "WARM" | null;
export type NameQuality = "person" | "placeholder";

export interface Contact {
  /** 1 = YC company: warm channel, never cold-enrolled. */
  is_yc?: number;
  id: number;
  source_sheet: SourceSheet;
  source_row_id: number | null;
  company: string;
  full_name: string | null;
  first_name: string | null;
  name_quality: NameQuality;
  role: string | null;
  role_group: string | null;
  industry: string | null;
  tier: string | null;
  stage: string | null;
  email: string | null;
  email_normalized: string | null;
  domain: string | null;
  linkedin: string | null;
  priority: Priority;
  outreach_channel: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export type VerificationStatus = "valid" | "risky" | "invalid" | "unknown";

export interface Verification {
  id: number;
  contact_id: number;
  status: VerificationStatus;
  score: number;
  syntax_ok: number;
  mx_ok: number;
  mx_hosts: string | null;
  is_role: number;
  is_disposable: number;
  is_free_provider: number;
  is_catch_all: number;
  smtp_checked: number;
  smtp_code: number | null;
  smtp_message: string | null;
  provider: string;
  reasons: string | null;
  duration_ms: number | null;
  checked_at: string;
}

export type TemplateKind = "opener" | "follow_up";

export interface Template {
  id: number;
  name: string;
  subject: string;
  body: string;
  kind: TemplateKind;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export type CampaignStatus = "draft" | "active" | "paused" | "archived";

export interface Campaign {
  id: number;
  name: string;
  description: string | null;
  status: CampaignStatus;
  timezone: string;
  daily_cap: number;
  send_window_start: number;
  send_window_end: number;
  send_days: string;
  per_domain_daily_cap: number;
  created_at: string;
  updated_at: string;
}

export interface SequenceStep {
  id: number;
  campaign_id: number;
  step_number: number;
  template_id: number;
  delay_days: number;
  only_if_no_reply: number;
  created_at: string;
}

/**
 * Enrollment lifecycle. Only `pending` and `active` are live states — every
 * other value is terminal and means the scheduler will never touch the row
 * again without human action.
 */
export type EnrollmentStatus =
  | "pending"
  | "active"
  | "completed"
  | "replied"
  | "bounced"
  | "stopped"
  | "review"
  | "suppressed";

export const LIVE_ENROLLMENT_STATUSES: readonly EnrollmentStatus[] = ["pending", "active"];

export const TERMINAL_ENROLLMENT_STATUSES: readonly EnrollmentStatus[] = [
  "completed",
  "replied",
  "bounced",
  "stopped",
  "review",
  "suppressed",
];

export function isTerminal(status: EnrollmentStatus): boolean {
  return TERMINAL_ENROLLMENT_STATUSES.includes(status);
}

export interface Enrollment {
  id: number;
  campaign_id: number;
  contact_id: number;
  status: EnrollmentStatus;
  current_step: number;
  next_send_at: string | null;
  thread_id: string | null;
  last_message_id: string | null;
  stopped_reason: string | null;
  enrolled_at: string;
  updated_at: string;
}

export type MessageDirection = "outbound" | "inbound";
export type MessageStatus = "queued" | "dry_run" | "sent" | "failed" | "skipped" | "received";

export interface Message {
  id: number;
  enrollment_id: number | null;
  contact_id: number | null;
  direction: MessageDirection;
  step_number: number | null;
  template_id: number | null;
  status: MessageStatus;
  subject: string | null;
  body: string | null;
  snippet: string | null;
  to_email: string | null;
  from_email: string | null;
  gmail_message_id: string | null;
  gmail_thread_id: string | null;
  rfc822_message_id: string | null;
  in_reply_to: string | null;
  error: string | null;
  scheduled_for: string | null;
  sent_at: string | null;
  received_at: string | null;
  created_at: string;
}

/**
 * Reply intent.
 *
 * `ooo` and `auto` are explicitly NOT replies for sequencing purposes — an
 * auto-responder must never be mistaken for engagement, or every follow-up
 * gets cancelled by a vacation message.
 */
export type ReplyClassification = "positive" | "negative" | "neutral" | "ooo" | "auto" | "bounce";

export interface Reply {
  id: number;
  message_id: number;
  enrollment_id: number | null;
  contact_id: number | null;
  classification: ReplyClassification;
  confidence: number;
  classifier: "rules" | "llm" | "human";
  matched_rules: string | null;
  requires_review: number;
  reviewed_at: string | null;
  reviewed_by: string | null;
  human_override: string | null;
  received_at: string;
  created_at: string;
}

export type SuppressionScope = "email" | "domain";

export interface Suppression {
  id: number;
  scope: SuppressionScope;
  value: string;
  reason: string;
  source: string;
  contact_id: number | null;
  created_at: string;
}

export interface OutreachEvent {
  id: number;
  type: string;
  entity_type: string | null;
  entity_id: number | null;
  campaign_id: number | null;
  contact_id: number | null;
  payload: string | null;
  created_at: string;
}

/** A contact joined to its latest verification and enrollment — the shape the UI renders. */
export interface ContactView extends Contact {
  verification_status: VerificationStatus | null;
  verification_score: number | null;
  verification_checked_at: string | null;
  is_catch_all: number | null;
  is_role: number | null;
  enrollment_id: number | null;
  enrollment_status: EnrollmentStatus | null;
  current_step: number | null;
  next_send_at: string | null;
  campaign_id: number | null;
  campaign_name: string | null;
  last_reply_classification: ReplyClassification | null;
  suppressed: number;
  messages_sent: number;
}
