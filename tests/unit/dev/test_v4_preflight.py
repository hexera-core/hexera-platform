"""v4_eval preflight: location convention, digest pin and the permission checks, against a fake gcloud.

Every check has a test that makes it REJECT. Nothing here reaches the network: `Runner` is faked."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "v4_preflight", Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval/preflight.py"
)
assert _SPEC is not None and _SPEC.loader is not None
pf = importlib.util.module_from_spec(_SPEC)
sys.modules["v4_preflight"] = pf
_SPEC.loader.exec_module(pf)

DIGEST = "sha256:" + "c" * 64
IMAGE = f"us-central1-docker.pkg.dev/hexera-dev/mesh/mesh-fleet@{DIGEST}"
COMMIT = "d" * 40
SA = pf.SERVICE_ACCOUNT
MEMBER = f"serviceAccount:{SA}"


def _policy(*roles: str, member: str = MEMBER, condition: dict | None = None) -> dict:
    return {"bindings": [{"role": r, "members": [member], **({"condition": condition} if condition else {})}
                         for r in roles]}


class FakeGcloud:
    """Answers the exact gcloud argv the preflight issues. Anything unexpected fails the test, so a
    mutating or unplanned call cannot slip through."""

    def __init__(self, *, bucket_roles=("roles/storage.objectAdmin",), project_roles=(),
                 repo_roles=("roles/artifactregistry.reader",), image_digest=DIGEST, image_rc=0,
                 listing=None, listing_rc=0, sa_rc=0, sa_email=SA, sa_disabled=False,
                 bucket_policy_rc=0, repo_policy_rc=0, project_policy_rc=0, bucket_policies=None):
        self.bucket_roles, self.project_roles, self.repo_roles = bucket_roles, project_roles, repo_roles
        self.image_digest, self.image_rc = image_digest, image_rc
        self.listing = [f"{pf.INPUTS}/abc/a.step", f"{pf.INPUTS}/nist/b.step"] if listing is None else listing
        self.listing_rc, self.sa_rc, self.sa_email, self.sa_disabled = listing_rc, sa_rc, sa_email, sa_disabled
        self.bucket_policy_rc, self.repo_policy_rc, self.project_policy_rc = (
            bucket_policy_rc, repo_policy_rc, project_policy_rc)
        self.bucket_policies = bucket_policies or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        verb = " ".join(argv[1:4])
        if verb.startswith("storage buckets get-iam-policy"):
            bucket = argv[4]
            roles = self.bucket_policies.get(bucket, self.bucket_roles)
            return pf.Result(self.bucket_policy_rc, json.dumps(_policy(*roles)), "denied")
        if verb.startswith("projects get-iam-policy"):
            return pf.Result(self.project_policy_rc, json.dumps(_policy(*self.project_roles)), "denied")
        if verb.startswith("artifacts repositories get-iam-policy"):
            return pf.Result(self.repo_policy_rc, json.dumps(_policy(*self.repo_roles)), "denied")
        if verb.startswith("artifacts docker images"):
            body = {"image_summary": {"digest": self.image_digest}} if self.image_digest else {}
            return pf.Result(self.image_rc, json.dumps(body), "not found")
        if verb.startswith("storage ls"):
            return pf.Result(self.listing_rc, "\n".join(self.listing), "denied")
        if verb.startswith("iam service-accounts describe"):
            body = {"email": self.sa_email, **({"disabled": True} if self.sa_disabled else {})}
            return pf.Result(self.sa_rc, json.dumps(body), "denied")
        raise AssertionError(f"unexpected gcloud call: {argv}")


def _run(fake: FakeGcloud, **kw):
    args = {"image": IMAGE, "commit": COMMIT, "service_account": SA}
    args.update(kw)
    return pf.run_preflight(runner=fake, **args)


def _failed(report) -> set[str]:
    return {c.name for c in report.checks if not c.ok}


# ------------------------------------------------------------------ convention

def test_locations_follow_the_v3_bucket_and_are_parameterised():
    assert pf.BUCKET == "gs://dev-crsv3-transfer-224734058693"
    loc = pf.wave_locations()
    assert loc.inputs == "gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/inputs"
    assert loc.cache == "gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/cache"
    assert loc.results_root(COMMIT) == "gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/results/" + "d" * 12
    other = pf.wave_locations(bucket="gs://dev-crsv3-x", name="v4-trial")
    assert other.cache == "gs://dev-crsv3-x/eval/v4-trial/cache"


def test_cache_prefix_is_independent_of_the_commit():
    # verdict cache keys already carry the code version, so one prefix is shared across waves
    assert pf.wave_locations().cache == pf.wave_locations().cache and "d" * 12 not in pf.wave_locations().cache


@pytest.mark.parametrize("bucket", ["gs://some-other-bucket", "dev-crsv3-x", "gs://dev-crsv3-x/sub"])
def test_locations_refuse_a_bucket_outside_the_crsv3_convention(bucket):
    with pytest.raises(ValueError):
        pf.wave_locations(bucket=bucket)


@pytest.mark.parametrize("name", ["", "a/b", "..", "has space"])
def test_locations_refuse_a_bad_wave_name(name):
    with pytest.raises(ValueError):
        pf.wave_locations(name=name)


# ------------------------------------------------------------------ digest pin

@pytest.mark.parametrize("ref", [
    "us-central1-docker.pkg.dev/hexera-dev/mesh/x:latest",
    "us-central1-docker.pkg.dev/hexera-dev/mesh/x",
    "us-central1-docker.pkg.dev/hexera-dev/mesh/x@sha256:abc",
    "us-central1-docker.pkg.dev/hexera-dev/mesh/x@sha256:" + "C" * 64,
    "docker.io/library/x@sha256:" + "c" * 64,
    "",
])
def test_pin_refuses_anything_but_a_full_digest(ref):
    with pytest.raises(ValueError):
        pf.require_pinned_image(ref)


def test_pin_accepts_a_digest_even_alongside_a_tag():
    ref = "us-central1-docker.pkg.dev/hexera-dev/mesh/x:abc@" + DIGEST
    assert pf.require_pinned_image(ref) == ref


def test_resolve_tag_returns_the_registry_digest_not_the_tag():
    def runner(argv):
        assert argv[1:5] == ["artifacts", "docker", "images", "describe"]
        return pf.Result(0, json.dumps({"image_summary": {"digest": DIGEST}}), "")

    got = pf.resolve_to_digest("us-central1-docker.pkg.dev/hexera-dev/mesh/mesh-fleet:latest", runner)
    assert got == IMAGE


def test_resolve_tag_fails_closed_when_the_registry_gives_no_digest():
    with pytest.raises(ValueError):
        pf.resolve_to_digest("us-central1-docker.pkg.dev/hexera-dev/mesh/x:t",
                             lambda argv: pf.Result(0, "{}", ""))
    with pytest.raises(ValueError):
        pf.resolve_to_digest("us-central1-docker.pkg.dev/hexera-dev/mesh/x:t",
                             lambda argv: pf.Result(1, "", "NOT_FOUND"))


def test_provenance_stamp_carries_a_real_pin_and_satisfies_collect_style_refusal():
    stamp = pf.provenance_stamp(commit=COMMIT, image=IMAGE)
    assert stamp["commit"] == COMMIT and stamp["image"] == IMAGE and stamp["image_digest"] == DIGEST
    # the v3 comparability gate (the model for v4 collect) reads exactly these keys
    spec = importlib.util.spec_from_file_location(
        "v3_evalspec", Path(__file__).resolve().parents[3] / "devtools/validation/v3_eval/evalspec.py")
    assert spec is not None and spec.loader is not None
    evalspec = importlib.util.module_from_spec(spec)
    sys.modules["v3_evalspec"] = evalspec
    spec.loader.exec_module(evalspec)
    assert evalspec.check_comparable([{**stamp, "kind": "k", "sample_sha256": "s"}])["image"] == IMAGE


@pytest.mark.parametrize("commit,image", [
    ("", IMAGE), (" ", IMAGE), ("abc123", IMAGE), (COMMIT, ""),
    (COMMIT, "us-central1-docker.pkg.dev/hexera-dev/mesh/x:latest"),
])
def test_provenance_stamp_refuses_blank_short_or_floating(commit, image):
    with pytest.raises(ValueError):
        pf.provenance_stamp(commit=commit, image=image)


def test_vm_metadata_items_carry_the_digest_and_the_locations():
    items = pf.metadata_items(commit=COMMIT, image=IMAGE)
    assert f"v4-image={IMAGE}" in items and f"v4-commit={COMMIT}" in items
    assert f"v4-cache-uri={pf.wave_locations().cache}" in items


# ------------------------------------------------------------------ preflight: healthy

def test_healthy_environment_passes_every_check():
    report = _run(FakeGcloud())
    assert report.ok, [c for c in report.checks if not c.ok]
    assert {c.name for c in report.checks} == set(pf.CHECKS)


def test_preflight_issues_only_read_only_gcloud_verbs():
    fake = FakeGcloud()
    _run(fake)
    allowed = ("get-iam-policy", "describe", "ls")
    for argv in fake.calls:
        assert any(v in argv for v in allowed), argv
        assert not any(v in argv for v in ("cp", "rsync", "create", "delete", "add-iam-policy-binding", "rm"))


# ------------------------------------------------------------------ each check REJECTS

def test_inputs_read_rejects_a_service_account_without_a_read_role():
    report = _run(FakeGcloud(bucket_roles=("roles/storage.objectCreator",)))
    assert "inputs_read" in _failed(report) and not report.ok


def test_inputs_read_rejects_an_unreadable_prefix_and_an_empty_one():
    assert "inputs_read" in _failed(_run(FakeGcloud(listing_rc=1)))
    assert "inputs_read" in _failed(_run(FakeGcloud(listing=[])))


def test_cache_write_rejects_a_read_only_service_account():
    report = _run(FakeGcloud(bucket_roles=("roles/storage.objectViewer",)))
    assert "cache_write" in _failed(report) and "results_write" in _failed(report)


def test_cache_write_needs_read_too_because_a_cache_is_read_back():
    # legacyBucketWriter can create/delete but not get: a cache on it would never hit
    assert "cache_write" in _failed(_run(FakeGcloud(bucket_roles=("roles/storage.legacyBucketWriter",))))


def test_results_write_rejects_when_only_the_results_bucket_lacks_the_role():
    base = pf.wave_locations()
    loc = pf.WaveLocations(bucket=base.bucket, inputs=base.inputs, cache=base.cache,
                           results="gs://dev-crsv3-results/eval/v4-reprobe/results")
    fake = FakeGcloud(bucket_roles=("roles/storage.objectAdmin",),
                      bucket_policies={"gs://dev-crsv3-results": ("roles/storage.objectViewer",)})
    report = _run(fake, locations=loc)
    assert "results_write" in _failed(report) and "cache_write" not in _failed(report)


def test_a_conditional_grant_is_not_trusted():
    cond = {"title": "t", "expression": "resource.name.startsWith('x')"}
    fake = FakeGcloud()

    def conditional(argv):
        if "storage" in argv and "get-iam-policy" in argv:
            return pf.Result(0, json.dumps(_policy("roles/storage.objectAdmin", condition=cond)), "")
        return fake(argv)

    report = pf.run_preflight(runner=conditional, image=IMAGE, commit=COMMIT, service_account=SA)
    assert {"inputs_read", "cache_write", "results_write"} <= _failed(report)


def test_a_grant_to_somebody_else_does_not_count():
    fake = FakeGcloud()
    original = fake.__call__

    def other(argv):
        if "get-iam-policy" in argv:
            return pf.Result(0, json.dumps(_policy("roles/storage.objectAdmin",
                                                   "roles/artifactregistry.reader",
                                                   member="serviceAccount:someone-else@x.iam.gserviceaccount.com")), "")
        return original(argv)

    report = pf.run_preflight(runner=other, image=IMAGE, commit=COMMIT, service_account=SA)
    assert {"inputs_read", "cache_write", "results_write", "image_pullable"} <= _failed(report)


def test_an_unreadable_iam_policy_is_a_refusal_not_a_warning():
    report = _run(FakeGcloud(bucket_policy_rc=1, project_policy_rc=1, repo_policy_rc=1))
    assert {"inputs_read", "cache_write", "results_write", "image_pullable"} <= _failed(report)
    assert not report.ok


def test_a_project_level_role_satisfies_the_check_when_the_bucket_has_none():
    report = _run(FakeGcloud(bucket_roles=(), project_roles=("roles/storage.objectAdmin",)))
    assert report.ok


def test_image_digest_missing_in_the_registry_is_a_refusal():
    assert "image_exists" in _failed(_run(FakeGcloud(image_rc=1)))


def test_image_digest_mismatch_is_a_refusal():
    assert "image_exists" in _failed(_run(FakeGcloud(image_digest="sha256:" + "e" * 64)))


def test_a_floating_tag_is_refused_before_any_registry_call():
    fake = FakeGcloud()
    report = _run(fake, image="us-central1-docker.pkg.dev/hexera-dev/mesh/mesh-fleet:latest")
    assert "image_pinned" in _failed(report)
    assert not [c for c in fake.calls if "images" in c], "a floating tag must not reach describe"


def test_image_not_pullable_without_a_registry_reader_role():
    assert "image_pullable" in _failed(_run(FakeGcloud(repo_policy_rc=1)))
    assert "image_pullable" in _failed(_run(FakeGcloud(repo_roles=())))


def test_registry_writer_role_can_pull_but_a_loose_role_cannot():
    assert "image_pullable" not in _failed(_run(FakeGcloud(repo_roles=("roles/artifactregistry.writer",))))
    assert "image_pullable" in _failed(_run(FakeGcloud(repo_roles=("roles/artifactregistry.createOnPushWriter",))))


def test_wrong_service_account_is_refused_without_asking_gcloud():
    fake = FakeGcloud()
    report = _run(fake, service_account="someone-else@hexera-dev.iam.gserviceaccount.com")
    assert "identity" in _failed(report)


def test_missing_or_disabled_service_account_is_refused():
    assert "identity" in _failed(_run(FakeGcloud(sa_rc=1)))
    assert "identity" in _failed(_run(FakeGcloud(sa_disabled=True)))
    assert "identity" in _failed(_run(FakeGcloud(sa_email="other@x.iam.gserviceaccount.com")))


def test_a_timeout_or_crash_in_the_runner_is_a_refusal():
    def boom(argv):
        raise TimeoutError("gcloud hung")

    report = pf.run_preflight(runner=boom, image=IMAGE, commit=COMMIT, service_account=SA)
    assert not report.ok and _failed(report) == set(pf.CHECKS) - {"locations", "image_pinned"}


def test_fusion360_in_the_staged_inputs_is_refused():
    for bad in ("f360/part.step", "fusion360/part.step", "Fusion-360/x.step", "inputs/F360_gallery/s.step"):
        report = _run(FakeGcloud(listing=[f"{pf.INPUTS}/abc/a.step", f"{pf.INPUTS}/{bad}"]))
        assert "inputs_sources" in _failed(report), bad


def test_declared_sources_outside_the_allow_list_are_refused():
    assert "f360" not in pf.ALLOWED_SOURCES
    assert "inputs_sources" in _failed(_run(FakeGcloud(), staged_sources=["abc_fail", "f360"]))
    assert "inputs_sources" not in _failed(_run(FakeGcloud(), staged_sources=["abc_fail", "inject"]))


def test_cache_prefix_inside_inputs_or_results_is_refused():
    loc = pf.WaveLocations(bucket=pf.BUCKET, inputs=f"{pf.BUCKET}/eval/w/inputs",
                           cache=f"{pf.BUCKET}/eval/w/inputs/cache", results=f"{pf.BUCKET}/eval/w/results")
    assert "locations" in _failed(_run(FakeGcloud(), locations=loc))


def test_main_exits_nonzero_on_refusal_and_zero_when_healthy(monkeypatch, capsys):
    monkeypatch.setattr(pf, "gcloud_runner", lambda: FakeGcloud(bucket_roles=("roles/storage.objectViewer",)))
    assert pf.main(["--image", IMAGE, "--commit", COMMIT]) == 1
    assert "REFUSED" in capsys.readouterr().out
    monkeypatch.setattr(pf, "gcloud_runner", lambda: FakeGcloud())
    assert pf.main(["--image", IMAGE, "--commit", COMMIT]) == 0
