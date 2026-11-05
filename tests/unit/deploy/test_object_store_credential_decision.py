# Responsibility: Keep the reuse-or-mint decision from treating an unreadable HMAC list as an empty one.
# Owns: the assertions over create-object-storage.sh's credential-decision block.
# Boundaries: read-only inspection of the script; it calls no cloud and mints nothing.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
OBJECT_STORAGE = REPO / "deploy" / "gcp" / "scripts" / "create-object-storage.sh"


def _script() -> str:
    return OBJECT_STORAGE.read_text(encoding="utf-8")


def _hmac_list_command(text: str) -> str:
    """The `gcloud storage hmac list` invocation, with its line continuations joined."""
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        if "storage hmac list" in line and "--filter" in line:
            return line
    raise AssertionError("the HMAC list command was not found - the scan subject is empty")


def test_a_failed_hmac_list_is_not_collapsed_into_an_empty_one():
    """THE BUG THIS FILE EXISTS FOR.

    `roles/storage.admin` carries no `storage.hmacKeys.*` permission at all - only
    `roles/storage.hmacKeyAdmin` does - so the deploy identity's list returned PERMISSION_DENIED.
    With the failure swallowed, the empty result read as "no ACTIVE key matches the recorded id",
    which is the exact condition that sends this to the mint branch. It announced a repair and tried
    to mint, and the same missing permission refused that too - so the prod release stopped here
    reporting a minting failure while the real fault was that the key it already had was unreadable.
    That key was ACTIVE and correctly paired the whole time.

    Keeping the exit status is what separates "this account has no keys" from "I cannot see them".
    """
    command = _hmac_list_command(_script())
    assert "|| true" not in command, (
        "the HMAC list still ends in `|| true`, so a PERMISSION_DENIED read is indistinguishable "
        "from an account with no keys - and an empty list is what selects the mint branch")


def test_an_undecidable_list_refuses_before_the_mint_branch():
    """Order is the contract, not tidiness.

    Minting is the one action that must never follow an inconclusive read: it either adds a second
    credential beside one that may still be in use, or fails with a message about creating a key
    when the fault is reading one. The refusal therefore has to come BEFORE the branch that mints.
    """
    text = _script()
    read_flag = text.index("HMAC_LIST_READ_OK")
    refusal = text.index('if [ "${HMAC_LIST_READ_OK}" = "0" ]')
    mint = text.index("storage hmac create")
    assert read_flag < refusal < mint, (
        "the unreadable-list refusal does not sit between the list read and the mint call, so an "
        "inconclusive read can still reach `gcloud storage hmac create`")


def test_the_refusal_names_the_role_that_actually_grants_the_read():
    """A refusal that does not say which role to grant sends the reader to roles/storage.admin,
    which is precisely the role that does NOT carry storage.hmacKeys.list. Naming hmacKeyAdmin is
    the difference between a one-line fix and re-deriving tonight's investigation."""
    text = _script()
    refusal = text[text.index('if [ "${HMAC_LIST_READ_OK}" = "0" ]'):]
    refusal = refusal[: refusal.index("\nfi\n")]
    assert "storage.hmacKeys.list" in refusal, "the refusal does not name the missing permission"
    assert "roles/storage.hmacKeyAdmin" in refusal, (
        "the refusal does not name roles/storage.hmacKeyAdmin, the only predefined role that grants "
        "storage.hmacKeys.list")


def test_reuse_still_requires_both_a_live_key_and_a_stored_secret():
    """The guard above must not have widened the reuse condition. GCS never reveals which secret
    belongs to which key, so reuse is only safe when the recorded id is ACTIVE *and* the secret
    container holds a version - either alone is a runtime that authenticates against nothing."""
    text = _script()
    assert re.search(
        r'if \[ "\$\{RECORDED_IS_ACTIVE\}" = "1" \] && \[ "\$\{SECRET_HAS_VERSION\}" = "1" \]', text), (
        "the reuse condition no longer requires both an ACTIVE recorded key and a stored secret "
        "version")
