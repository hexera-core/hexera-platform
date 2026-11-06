/**
 * Maps the outcome of POST /account/erasure to the message to show, or null when the request was
 * accepted. A 409 carries a sentence the API wrote for the person (an active subscription, a
 * member who is not the owner) and is shown as given; every other failure is reported without a
 * cause this function cannot know.
 */
export function erasureOutcomeMessage(status: number, detail?: string): string | null {
  if (status === 202) {
    return null;
  }
  if (status === 400) {
    return "Type your account's email address exactly to confirm.";
  }
  if (status === 409 && detail) {
    return detail;
  }
  return "Could not delete the account just now. Try again, or contact support.";
}

/** Whether the typed confirmation matches the account address, case-insensitively. */
export function confirmationMatches(typed: string, email: string): boolean {
  return typed.trim().toLowerCase() === email.trim().toLowerCase() && email.trim() !== "";
}
