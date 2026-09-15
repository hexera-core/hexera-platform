# Responsibility: Keep the read-only preflight asking about every permission the selected tiers need.
# Owns: the per-component permission assertions over preflight.sh.
# Boundaries: read-only inspection of the script; it calls no cloud and grants nothing.
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
PREFLIGHT = REPO / "deploy" / "gcp" / "scripts" / "preflight.sh"

#: component -> every resource type that tier creates or updates. NOT one representative permission
#: per tier: testIamPermissions answers only about the permission it is asked about, so a grant on
#: one resource proves nothing about its neighbours. An identity holding
#: compute.instanceTemplates.create but not compute.instanceGroupManagers.update would pass a
#: template-only check and then fail while rolling the fleet - mid-deploy, which is the failure this
#: preflight exists to prevent.
COMPONENT_PERMISSIONS = {
    "storage": [
        "storage.buckets.update",
        "storage.buckets.setIamPolicy",
        "storage.hmacKeys.list",       # cost v0.1.4 at stage 8/19
        "storage.hmacKeys.create",
    ],
    "data": [
        "cloudsql.instances.create",
        "cloudsql.databases.create",
        "cloudsql.users.create",
        "cloudsql.users.update",
        "redis.instances.create",
        "compute.addresses.create",
        "servicenetworking.services.addPeering",
    ],
    "queue": [
        "cloudscheduler.jobs.create",  # cost the v0.1.4 re-run at stage 13/19
        "cloudscheduler.jobs.update",
    ],
    "workers": [
        "compute.instanceTemplates.create",
    ],
    "edge": [
        "compute.urlMaps.create",
        "compute.urlMaps.update",
        "compute.backendServices.create",
        "compute.sslCertificates.create",
        "compute.targetHttpsProxies.create",
        "compute.globalForwardingRules.create",
        "compute.regionNetworkEndpointGroups.create",
    ],
}

#: Mutating the fleet is done by BOTH tiers - the queue tier resizes it, the workers tier replaces
#: its template - so either selection has to require these.
SHARED_FLEET_PERMISSIONS = ("compute.instanceGroupManagers.update", "compute.autoscalers.update")


def _script() -> str:
    return PREFLIGHT.read_text(encoding="utf-8")


def _perm_list() -> set[str]:
    match = re.search(r"^PERM_LIST='(.*)'$", _script(), re.M)
    assert match, "PERM_LIST is no longer a single-quoted assignment - the scan cannot read it"
    return set(re.findall(r'"([a-z]+\.[A-Za-z.]+)"', match.group(1)))


def _reported() -> set[str]:
    text = _script()
    loop = text[text.index("for perm in run.services.setIamPolicy"):]
    return set(re.findall(r"[a-z]+\.[A-Za-z.]+", loop[: loop.index("; do")]))


def _selected_helper() -> str:
    """The REAL `_selected` definition, lifted from preflight.sh so the tests exercise the shipped
    implementation rather than a copy that cannot regress with it."""
    match = re.search(r"^\s*_selected\(\) \{.*?^\s*\}$", _script(), re.M | re.S)
    assert match, "preflight.sh no longer defines _selected() - the component gate is gone"
    return match.group(0)


def test_every_component_permission_is_tested_against_iam():
    """testIamPermissions only answers about permissions it is asked about. One missing from
    PERM_LIST is not reported as missing - it is invisible, which is how the object store and the
    queue-depth publisher each took a release to discover."""
    queried = _perm_list()
    for component, perms in COMPONENT_PERMISSIONS.items():
        for perm in perms:
            assert perm in queried, (
                f"PERM_LIST does not ask about {perm}, so a run deploying '{component}' cannot "
                f"report it missing and discovers it mid-deploy instead")
    for perm in SHARED_FLEET_PERMISSIONS:
        assert perm in queried, f"PERM_LIST does not ask about {perm}"


def test_everything_queried_is_also_reported():
    """A permission queried but never reported is indistinguishable from one never queried: IAM
    answered and nothing read the answer. This held for five permissions before it was noticed."""
    assert _perm_list() - _reported() == set(), (
        f"queried but never reported: {sorted(_perm_list() - _reported())}")
    assert _reported() - _perm_list() == set(), (
        f"reported but never queried - always reads as missing: "
        f"{sorted(_reported() - _perm_list())}")


def test_component_permissions_are_gated_on_component_selection():
    """A merge to main deploys `images,migrate,console,admin` and must not be failed for lacking
    Cloud Scheduler it will never call."""
    text = _script()
    needed = text[text.index("_needed() {"):]
    needed = needed[: needed.index("\n  }")]
    for component, perms in COMPONENT_PERMISSIONS.items():
        for perm in perms:
            assert perm in needed, f"{perm} has no _needed arm, so it is required unconditionally"
        # The arm that mentions this component must be the one these permissions fall into.
        assert f"_selected {component}" in needed, f"no _needed arm selects on '{component}'"
    for perm in SHARED_FLEET_PERMISSIONS:
        assert perm in needed, f"{perm} is required unconditionally"
    assert "_selected queue || _selected workers" in needed, (
        "the fleet permissions are not required for BOTH the queue and workers tiers, though both "
        "mutate the managed instance group")


def test_the_real_selection_helper_implements_deploy_components_semantics():
    """Executes the helper LIFTED FROM preflight.sh, not a copy of it. A regression in the shipped
    `_selected` - mishandling `all`, an empty value, or a comma-delimited list - has to fail here.

    `all` means all, an unset variable never means less, and a named list means exactly itself.
    """
    helper = _selected_helper()
    cases = [
        ("all", "queue", 0),
        ("", "queue", 0),                              # empty -> all, never "none"
        ("images,migrate,console,admin", "queue", 1),  # the dev merge selection
        ("images,migrate,console,admin", "storage", 1),
        ("images,storage", "storage", 0),
        ("images,storage", "edge", 1),
        ("queue", "queue", 0),                         # single-element list
        ("workers,queue,edge", "edge", 0),             # last element of a list still matches
    ]
    for components, component, expected in cases:
        script = f'{helper}\nDEPLOY_COMPONENTS="{components}"\n_selected {component}\n'
        rc = subprocess.run(["bash", "-c", script]).returncode
        assert rc == expected, (
            f"DEPLOY_COMPONENTS={components!r} _selected {component} returned {rc}, expected "
            f"{expected}")

    # Unset, not merely empty - the case an operator running preflight.sh directly produces.
    script = f'{helper}\nunset DEPLOY_COMPONENTS\n_selected queue\n'
    assert subprocess.run(["bash", "-c", script]).returncode == 0, (
        "an UNSET DEPLOY_COMPONENTS must mean `all`; treating it as 'nothing selected' would make "
        "the preflight silently ask about nothing")


def test_the_hmac_failure_names_the_role_that_actually_grants_it():
    """roles/storage.admin carries no storage.hmacKeys.* permission at all - only
    roles/storage.hmacKeyAdmin does. Naming the wrong role is what makes this cost a second
    release: storage.admin is already granted, so the obvious fix looks already applied."""
    text = _script()
    assert "roles/storage.hmacKeyAdmin" in text, (
        "the hmac failure does not name roles/storage.hmacKeyAdmin")
    assert re.search(r"storage\.admin does NOT include it", text), (
        "the hmac failure does not warn that roles/storage.admin is insufficient")
