# Responsibility: Keep the read-only preflight asking about every permission the selected tiers need.
# Owns: the per-component permission assertions over preflight.sh.
# Boundaries: read-only inspection of the script; it calls no cloud and grants nothing.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
PREFLIGHT = REPO / "deploy" / "gcp" / "scripts" / "preflight.sh"

#: component -> a permission that tier cannot run without. Each of these was discoverable from the
#: preflight, read-only, before any mutation; the two marked below each cost a release instead.
COMPONENT_PERMISSIONS = {
    "storage": "storage.hmacKeys.list",       # cost v0.1.4 at stage 8/19
    "queue": "cloudscheduler.jobs.create",    # cost the v0.1.4 re-run at stage 13/19
    "data": "cloudsql.instances.create",
    "workers": "compute.instanceTemplates.create",
    "edge": "compute.urlMaps.create",
}


def _script() -> str:
    return PREFLIGHT.read_text(encoding="utf-8")


def test_every_component_permission_is_tested_against_iam():
    """testIamPermissions only answers about permissions it is asked about. A permission missing
    from PERM_LIST is never reported as missing - it is simply invisible, which is how the object
    store and the queue-depth publisher each took a release to discover."""
    text = _script()
    perm_list = re.search(r"^PERM_LIST='(.*)'$", text, re.M)
    assert perm_list, "PERM_LIST is no longer a single-quoted assignment - the scan cannot read it"
    body = perm_list.group(1)
    for component, perm in COMPONENT_PERMISSIONS.items():
        assert f'"{perm}"' in body, (
            f"PERM_LIST does not ask about {perm}, so a run deploying '{component}' cannot report "
            f"it missing and discovers it mid-deploy instead")


def test_every_component_permission_is_reported_not_just_queried():
    """Asking IAM is half of it. A permission in PERM_LIST but absent from the reporting loop is
    answered and then thrown away."""
    text = _script()
    loop = text[text.index("for perm in run.services.setIamPolicy"):]
    loop = loop[: loop.index("do")]
    for perm in COMPONENT_PERMISSIONS.values():
        assert perm in loop, f"{perm} is queried but never reported - the loop does not iterate it"


def test_component_permissions_are_gated_on_component_selection():
    """A merge to main deploys `images,migrate,console,admin` and must not be failed for lacking
    Cloud Scheduler it will never call. Required-ness is derived from DEPLOY_COMPONENTS, the same
    way the mesh-era permissions derive theirs from discovery's dispositions."""
    text = _script()
    assert "_selected()" in text, "preflight has no component-selection helper"
    needed = text[text.index("_needed() {"):]
    needed = needed[: needed.index("\n  }")]
    for component, perm in COMPONENT_PERMISSIONS.items():
        arm = re.search(rf"{re.escape(perm)}[^\n]*\)\s*_selected (\w+)", needed)
        assert arm, f"{perm} has no _needed arm, so it is required unconditionally"
        assert arm.group(1) == component, (
            f"{perm} is gated on component '{arm.group(1)}', expected '{component}'")


def test_selection_matches_deploy_components_semantics():
    """`all` means all, an unset variable never means less, and a named list means exactly itself.
    Run the helper as the shell will run it rather than asserting its source text."""
    helper = (
        '_selected() { case ",${DEPLOY_COMPONENTS:-all}," in ,all,) return 0 ;; '
        '*",$1,"*) return 0 ;; *) return 1 ;; esac; }\n'
    )
    cases = [
        ("all", "queue", 0),
        ("", "queue", 0),                              # unset/empty -> all
        ("images,migrate,console,admin", "queue", 1),  # the dev merge selection
        ("images,migrate,console,admin", "storage", 1),
        ("images,storage", "storage", 0),
    ]
    for components, component, expected in cases:
        script = f'{helper}DEPLOY_COMPONENTS="{components}"\n_selected {component}\n'
        rc = subprocess.run(["bash", "-c", script]).returncode
        assert rc == expected, (
            f"DEPLOY_COMPONENTS={components!r} _selected {component} returned {rc}, expected "
            f"{expected}")


def test_the_hmac_failure_names_the_role_that_actually_grants_it():
    """roles/storage.admin carries no storage.hmacKeys.* permission at all - only
    roles/storage.hmacKeyAdmin does. Naming the wrong role is what makes this cost a second
    release: storage.admin is already granted, so the obvious fix looks already applied."""
    text = _script()
    assert "roles/storage.hmacKeyAdmin" in text, (
        "the hmac failure does not name roles/storage.hmacKeyAdmin")
    assert re.search(r"storage\.admin does NOT include it", text), (
        "the hmac failure does not warn that roles/storage.admin is insufficient")
