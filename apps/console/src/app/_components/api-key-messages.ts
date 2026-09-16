/**
 * Maps the outcome of POST /api-keys (mint) to the message to show for a failed request.
 * Call only when the response was not ok; a successful mint has its own body to render
 * (the once-only `presented` secret) and no message from here.
 *
 * 403 carries a specific meaning here (see meshpipeline/api/v1/api_keys.py,
 * `_require_console_session`): the caller authenticated with a valid API key, and key
 * management is reserved to a console session. Reporting that as a generic failure would tell
 * someone holding a live key that it looks broken or expired, when the truth is the opposite --
 * the key works, it simply cannot manage keys. Every other non-ok status is reported honestly,
 * without inventing a cause this function cannot know.
 */
export function mintFailureMessage(status: number): string {
  if (status === 403) {
    return "Only a console session can mint a key.";
  }
  return "Could not mint a key just now. Try again.";
}

/**
 * Maps the outcome of DELETE /api-keys/{id} (revoke) to the message to show, or null when the
 * key was actually revoked and nothing needs saying.
 *
 * The endpoint answers 200 with `{revoked: false}` for BOTH a key id that does not exist and one
 * already revoked (the repository's `revoked_at is null` predicate treats a repeat revocation as
 * "nothing to do" rather than an error). An ok response is therefore NOT on its own evidence of
 * a revocation -- only `revoked: true` is. Someone who clicks Revoke on a key they believe is
 * leaked and sees the page refresh must not be left believing the key is dead when the server
 * explicitly declined to revoke it; that would convert an actionable problem into a false sense
 * of safety. 403 carries the same console-session meaning as the mint path above.
 */
export function revokeOutcomeMessage(status: number, revoked: boolean): string | null {
  if (status === 403) {
    return "Only a console session can revoke a key.";
  }
  if (status < 200 || status >= 300) {
    return "Could not revoke that key. Try again.";
  }
  if (!revoked) {
    return "That key was not revoked. It may already be revoked, or no longer exist.";
  }
  return null;
}
