/**
 * The organisation's CAD repair setting as the product API returns it (GET/PUT /organization/repair-settings), and
 * every plain sentence the Meshing settings page says about it. Pure, so each sentence is tested where it is written.
 *
 * One switch, a PERMISSION: whether a run may choose to keep larger repairs (closing a gap, for example) without
 * showing them first. Off, every run shows them for review; on, each run's geometry check offers the choice, and the
 * run records what was picked. Only an organisation admin may change it (`can_change`); everyone else sees how it is.
 */
export type RepairSettings = {
  /** runs in this organisation may choose to skip the review of larger repairs */
  auto_accept_repairs: boolean;
  /** the signed-in user may change it (an organisation admin) */
  can_change?: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
};

export const REPAIR_TITLE = "CAD repair";

export const REPAIR_INTRO =
  "When we repair your CAD, small fixes are kept automatically. Larger changes, like closing a gap, are shown to you before they are kept.";

export const AUTO_LABEL = "Let runs accept repairs without asking (skip review)";

export const AUTO_HELP =
  "When this is on, each run's geometry check offers to keep larger repairs without showing them first. Each run records its choice.";

/** The one line under the switch: who may change it and what a run can do. */
export function scopeLine(settings: RepairSettings): string {
  if (settings.can_change === false) {
    return "Only an organisation admin can change this.";
  }
  return settings.auto_accept_repairs
    ? "Runs may choose to skip the review of larger repairs."
    : "Every run shows larger repairs for review before they are kept.";
}

/** A failed save, in plain words. 403: not an admin (or acting on an API key); 404: no organisation. */
export function repairSaveFailure(status: number): string {
  if (status === 403) {
    return "Only an organisation admin can change this.";
  }
  if (status === 404) {
    return "Your account has no organisation to hold this setting.";
  }
  return "Could not save just now. Try again.";
}
