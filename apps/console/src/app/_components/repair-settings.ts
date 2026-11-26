/**
 * The organisation's CAD repair setting as the product API returns it (GET/PUT /organization/repair-settings), and
 * every plain sentence the Meshing settings page says about it. Pure, so each sentence is tested where it is written.
 *
 * One switch: whether larger repairs (closing a gap, for example) are accepted automatically for every run, or shown
 * to the person who started the run first. Only an organisation admin may change it (`can_change`); everyone else
 * sees how it is set. A run can still choose for itself in its geometry check, unless the switch says otherwise.
 */
export type RepairSettings = {
  /** larger repairs are kept without a review, for every run in the organisation */
  auto_accept_repairs: boolean;
  /** the signed-in user may change it (an organisation admin) */
  can_change?: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
};

export const REPAIR_TITLE = "CAD repair";

export const REPAIR_INTRO =
  "When we repair your CAD, small fixes are kept automatically. Larger changes, like closing a gap, are shown to you before they are kept.";

export const AUTO_LABEL = "Accept repairs automatically (skip review)";

export const AUTO_HELP =
  "Larger repairs are kept without asking, for every run in this organisation. Each run records that it was accepted this way.";

/** The one line under the switch: who may change it and what a run can still do. */
export function scopeLine(settings: RepairSettings): string {
  if (settings.can_change === false) {
    return "Only an organisation admin can change this.";
  }
  return settings.auto_accept_repairs
    ? "Every run accepts larger repairs automatically."
    : "Runs ask before keeping larger repairs. A run can still choose to skip the review in its geometry check.";
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
