# Responsibility: Keep the fleet roll using flags gcloud still accepts, without dropping min-ready.
# Owns: the rolling-action flag assertions and the minReadySec-via-API assertions.
# Boundaries: read-only inspection of the script; it calls no cloud and rolls nothing.
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
FLEET = REPO / "deploy" / "gcp" / "scripts" / "create-worker-fleet.sh"

#: Flags `gcloud compute instance-groups managed rolling-action start-update` accepts. `--min-ready`
#: is deliberately absent: it was removed from gcloud entirely - GA, beta, and the
#: `instance-groups managed update --update-policy-*` family all lack it - and passing it aborts the
#: command with `unrecognized arguments`, exit 2. That is what stopped the v0.1.5 release at 18/19.
ROLLING_ACTION_FLAGS = {
    "--zone", "--region", "--version", "--canary-version", "--type", "--replacement-method",
    "--max-surge", "--max-unavailable", "--minimal-action", "--most-disruptive-allowed-action",
    "--force",
}


def _script() -> str:
    return FLEET.read_text(encoding="utf-8")


def _rolling_action_invocation() -> str:
    """The `rolling-action start-update` command with its line continuations joined."""
    text = _script().replace("\\\n", " ")
    match = re.search(r"gc compute instance-groups managed rolling-action start-update[^\n]*", text)
    assert match, "the fleet roll no longer invokes rolling-action start-update"
    return match.group(0)


def test_the_roll_passes_only_flags_gcloud_accepts():
    """THE BUG THIS FILE EXISTS FOR. Only a RELEASE reaches this stage - a merge to main deploys
    `images,migrate,console,admin` and never rolls the fleet - so a flag that gcloud removed sat
    here unnoticed until a tag ran, and then aborted the deploy two stages from the end."""
    used = set(re.findall(r"--[a-z-]+", _rolling_action_invocation()))
    unknown = used - ROLLING_ACTION_FLAGS
    assert unknown == set(), (
        f"the fleet roll passes flags rolling-action start-update does not accept: "
        f"{sorted(unknown)} - gcloud aborts with 'unrecognized arguments' and exit 2")


def test_min_ready_is_not_passed_on_the_command_line():
    """Specifically the removed flag, named so a re-introduction fails with the reason rather than
    a generic 'unknown flag'."""
    assert "--min-ready" not in _rolling_action_invocation(), (
        "--min-ready is back on the rolling-action command; gcloud removed it from every track and "
        "the command aborts. Set updatePolicy.minReadySec through the Compute API instead")


def test_min_ready_is_still_applied_through_the_api():
    """Removing the flag must not silently drop the guard. Without minReadySec an instance counts as
    available the moment it reports RUNNING - for a worker, before it has pulled a job - so the roll
    would march through the fleet at boot speed and leave nothing draining the queue."""
    text = _script()
    assert "_mig_patch_min_ready" in text, (
        "min-ready is neither passed to gcloud nor applied through the API - the guard was dropped")
    assert "minReadySec" in text, "updatePolicy.minReadySec is never set"
    call = text.index("_mig_patch_min_ready \"${WORKER_MIG}\"")
    roll = text.index("gc compute instance-groups managed rolling-action start-update")
    assert call < roll, (
        "minReadySec is patched after the roll starts, so the roll runs under the old policy")


def test_the_patch_merges_rather_than_replacing_the_update_policy():
    """A PATCH on instanceGroupManagers replaces a nested object wholesale. Sending only
    minReadySec would discard maxSurge, maxUnavailable, replacementMethod and type - the settings
    applied immediately above - and leave the fleet rolling under defaults nobody chose."""
    text = _script()
    helper = text[text.index("_mig_patch_min_ready() {"):]
    helper = helper[: helper.index("\n}")]
    assert 'get("updatePolicy"' in helper, (
        "the patch does not read the existing updatePolicy, so it replaces it wholesale")
    assert re.search(r'current\["minReadySec"\]\s*=', helper), (
        "the patch does not merge minReadySec into the policy it read")


def test_the_failed_patch_stops_the_roll():
    """A guard that can fail silently is not a guard. If minReadySec cannot be applied the roll must
    not start under a weaker policy than the one this script states."""
    text = _script()
    window = text[text.index("_mig_patch_min_ready \"${WORKER_MIG}\""):]
    window = window[: window.index("gc compute instance-groups managed rolling-action start-update")]
    assert "die" in window, (
        "a failure to set minReadySec does not stop the deploy, so the roll can proceed under a "
        "policy this script did not choose")


@pytest.mark.skipif(shutil.which("gcloud") is None, reason="gcloud not installed")
def test_the_flag_allowlist_matches_the_installed_gcloud():
    """The allowlist above is a copy of gcloud's surface, so it can drift from it. Where gcloud is
    available, check the copy against the real thing rather than trusting it."""
    out = subprocess.run(
        ["gcloud", "compute", "instance-groups", "managed", "rolling-action", "start-update",
         "--help"],
        capture_output=True, text=True, timeout=120).stdout
    if not out:
        pytest.skip("gcloud help produced no output")
    assert "--min-ready" not in out, (
        "the installed gcloud DOES accept --min-ready - this fix and its reasoning need revisiting")
    for flag in ("--max-surge", "--max-unavailable", "--replacement-method", "--type"):
        assert flag in out, f"{flag} is no longer accepted by the installed gcloud"
