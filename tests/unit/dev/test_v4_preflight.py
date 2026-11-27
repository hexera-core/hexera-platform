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
IMAGE_COMMIT = "e" * 40  # the commit the toolchain image was built from (not the code commit)
SHA_A, SHA_B, SHA_F360 = "a" * 64, "b" * 64, "f" * 64
#: (wanted sha256s, barred sha256s): two cleared entries and one Fusion 360 entry
EXPECTED = ({SHA_A, SHA_B}, {SHA_F360})
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
                 bucket_policy_rc=0, repo_policy_rc=0, project_policy_rc=0, bucket_policies=None,
                 image_revision=IMAGE_COMMIT, inspect_rc=0):
        self.image_revision, self.inspect_rc = image_revision, inspect_rc
        self.bucket_roles, self.project_roles, self.repo_roles = bucket_roles, project_roles, repo_roles
        self.image_digest, self.image_rc = image_digest, image_rc
        self.listing = ([f"{pf.INPUTS}/{SHA_A}/a.step", f"{pf.INPUTS}/{SHA_B}/b.step"]
                        if listing is None else listing)
        self.listing_rc, self.sa_rc, self.sa_email, self.sa_disabled = listing_rc, sa_rc, sa_email, sa_disabled
        self.bucket_policy_rc, self.repo_policy_rc, self.project_policy_rc = (
            bucket_policy_rc, repo_policy_rc, project_policy_rc)
        self.bucket_policies = bucket_policies or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        verb = " ".join(argv[1:4])
        if argv[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            labels = {"org.opencontainers.image.title": "Hexera Platform"}
            if self.image_revision is not None:
                labels[pf.REVISION_LABEL] = self.image_revision
            return pf.Result(self.inspect_rc, json.dumps({"config": {"Labels": labels}}), "unauthorized")
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
    args = {"image": IMAGE, "commit": COMMIT, "image_commit": IMAGE_COMMIT, "service_account": SA, "expected": EXPECTED}
    args.update(kw)
    return pf.run_preflight(runner=fake, **args)


def _failed(report) -> set[str]:
    return {c.name for c in report.checks if not c.ok}


# ------------------------------------------------------------------ convention

def test_locations_follow_the_launcher_and_are_parameterised():
    assert pf.BUCKET == "gs://dev-crsv3-transfer-224734058693"
    loc = pf.wave_locations(commit=COMMIT)
    assert loc.inputs == "gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/inputs"
    assert loc.cache == "gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/verdict-cache"
    assert loc.results == "gs://dev-crsv3-transfer-224734058693/eval/reprobe-" + "d" * 12
    other = pf.wave_locations(bucket="gs://dev-crsv3-x", name="v4-trial", commit=COMMIT)
    assert other.cache == "gs://dev-crsv3-x/eval/v4-trial/verdict-cache"


def test_cache_prefix_is_independent_of_the_commit():
    # verdict cache keys already carry the code version, so one prefix is shared across waves
    assert pf.wave_locations(commit="1" * 40).cache == pf.wave_locations(commit="2" * 40).cache
    assert pf.wave_locations(commit="1" * 40).results != pf.wave_locations(commit="2" * 40).results


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
    allowed = ("get-iam-policy", "describe", "ls", "inspect")
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
    assert not report.ok and _failed(report) == set(pf.CHECKS) - {"locations", "image_pinned", "cache_shared"}  # cache_shared is local


def test_fusion360_in_the_staged_inputs_is_refused():
    for bad in ("f360/part.step", "fusion360/part.step", "Fusion-360/x.step", "inputs/F360_gallery/s.step"):
        report = _run(FakeGcloud(listing=[f"{pf.INPUTS}/{SHA_A}/a.step", f"{pf.INPUTS}/{bad}"]))
        assert "inputs_sources" in _failed(report), bad


def test_declared_sources_outside_the_allow_list_are_refused():
    assert "f360" not in pf.ALLOWED_SOURCES
    assert "inputs_sources" in _failed(_run(FakeGcloud(), staged_sources=["abc_fail", "f360"]))
    assert pf.ALLOWED_SOURCES == frozenset({"abc_fail", "abc_pass", "occt", "nist", "inject"})
    assert "inputs_sources" not in _failed(_run(FakeGcloud(), staged_sources=["abc_fail", "inject"]))


def test_cache_prefix_inside_inputs_or_results_is_refused():
    loc = pf.WaveLocations(bucket=pf.BUCKET, inputs=f"{pf.BUCKET}/eval/w/inputs",
                           cache=f"{pf.BUCKET}/eval/w/inputs/cache", results=f"{pf.BUCKET}/eval/w/results")
    assert "locations" in _failed(_run(FakeGcloud(), locations=loc))


def test_main_exits_nonzero_on_refusal_and_zero_when_healthy(monkeypatch, capsys):
    monkeypatch.setattr(pf, "gcloud_runner", lambda: FakeGcloud(bucket_roles=("roles/storage.objectViewer",)))
    assert pf.main(["--image", IMAGE, "--commit", COMMIT]) == 1
    assert "REFUSED" in capsys.readouterr().out
    # the default path reads the committed pool: a listing of exactly its cleared entries passes
    shas = sorted(pf.expected_inputs(pf.DEFAULT_POOL, None)[0])
    monkeypatch.setattr(pf, "gcloud_runner", lambda: FakeGcloud(
        listing=[f"{pf.INPUTS}/{h}/x.step" for h in shas]))
    assert pf.main(["--image", IMAGE, "--commit", COMMIT]) == 1  # no --image-commit: nothing to verify the label against
    capsys.readouterr()
    assert pf.main(["--image", IMAGE, "--commit", COMMIT, "--image-commit", IMAGE_COMMIT]) == 0
    out = capsys.readouterr().out
    assert out.count("note:") == 6 and "LAUNCHER" in out and "deny policies" in out


# ------------------------------------------------------------------ permissions, not role names

def _roles(*roles: str) -> FakeGcloud:
    return FakeGcloud(bucket_roles=roles)


@pytest.mark.parametrize("role", ["roles/storage.legacyBucketOwner",     # list, no get
                                  "roles/storage.legacyObjectReader",    # get, no list
                                  "roles/storage.legacyObjectOwner",     # get, no list
                                  "roles/storage.legacyBucketReader"])   # list, no get
def test_a_role_with_only_half_of_get_and_list_cannot_read(role):
    report = _run(_roles(role))
    assert "inputs_read" in _failed(report) and not report.ok
    detail = next(c.detail for c in report.checks if c.name == "inputs_read")
    assert "storage.objects" in detail          # names the missing permission, not just "no role"


@pytest.mark.parametrize("role", ["roles/storage.legacyBucketWriter",    # list+create+delete, no get
                                  "roles/storage.legacyBucketOwner",     # list+create+delete, no get
                                  "roles/storage.objectCreator",         # create only
                                  "roles/storage.legacyObjectOwner"])    # get, no create/delete
def test_a_role_that_does_not_supply_all_write_permissions_cannot_write(role):
    report = _run(_roles(role))
    assert {"cache_write", "results_write"} <= _failed(report)


def test_the_union_of_roles_can_satisfy_what_no_single_role_does():
    # list (legacyBucketOwner) + get (legacyObjectReader) together can read; neither alone can
    assert "inputs_read" not in _failed(_run(_roles("roles/storage.legacyBucketOwner",
                                                    "roles/storage.legacyObjectReader")))
    full = _run(_roles("roles/storage.legacyBucketOwner", "roles/storage.legacyObjectReader"))
    assert not _failed(full)                    # get+list+create+delete all supplied by the union


def test_an_unknown_or_custom_role_supplies_nothing():
    assert {"inputs_read", "cache_write", "results_write"} <= _failed(
        _run(_roles("projects/hexera-dev/roles/customCorpusReader")))


def test_the_decision_really_comes_from_the_permission_table(monkeypatch):
    """Disable-the-check proof: give legacyBucketOwner the missing `get` and the same grant passes."""
    assert "inputs_read" in _failed(_run(_roles("roles/storage.legacyBucketOwner")))
    table = dict(pf.ROLE_PERMISSIONS)
    table["roles/storage.legacyBucketOwner"] = table["roles/storage.legacyBucketOwner"] | {pf.GET}
    monkeypatch.setattr(pf, "ROLE_PERMISSIONS", table)
    assert not _failed(_run(_roles("roles/storage.legacyBucketOwner")))


# ------------------------------------------------------------------ drift pins

def _launcher():
    spec = importlib.util.spec_from_file_location(
        "fleet_launcher_for_preflight", Path(__file__).resolve().parents[3] / "devtools/validation/v3_eval/launch_fleet.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_launcher_for_preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


def _v4_args(lf):
    """The namespace `main` would hand the launcher for a v4 wave: parsed by the launcher's OWN parser, so a flag
    added to launch_fleet is here by construction (a hand-built namespace drifted three times in one sprint).
    Only what `main` derives after parsing is set by hand: the defaults it fills in and the resolved sources."""
    args = lf.build_parser().parse_args(
        ["--profile", "v4", "--image", IMAGE, "--commit", COMMIT, "--image-commit", COMMIT[:12],
         "--wave-id", "w-test", "--wave-type", "normal", "--pool", str(pf.DEFAULT_POOL)])
    args.prefix = lf.V4_PREFIX
    args.sources = sorted(pf.ALLOWED_SOURCES)
    return args


def test_the_launcher_parser_defines_every_arg_the_launcher_reads():
    import re
    lf = _launcher()
    read = set(re.findall(r"\bargs\.([a-z_]+)", Path(lf.__file__).read_text()))
    have = set(vars(lf.build_parser().parse_args(["--image", "i", "--commit", "c", "--image-commit", "c"])))
    derived = {"files_counted", "image_commit_link"}  # set after parsing (main; the v4 preflight), never a flag
    assert read - derived <= have, f"launch_fleet reads args the parser does not define: {sorted(read - derived - have)}"


def test_the_preflight_checks_exactly_the_locations_the_launcher_writes():
    lf = _launcher()
    args = _v4_args(lf)
    meta = dict(m.split("=", 1) for m in lf.V4.extra_metadata(args))
    loc = pf.locations_for_prefix(args.prefix, COMMIT)
    assert meta["v4-cache-uri"] == loc.cache == pf.wave_locations(commit=COMMIT).cache
    assert lf.V4.required_dirs(args)[0].rstrip("/") == loc.inputs
    assert lf.results_root("reprobe", COMMIT) == loc.results == pf.wave_locations(commit=COMMIT).results
    assert lf.out_uri("reprobe", COMMIT, "pool", 3, 96).startswith(loc.results + "/pool/shard-")
    # and the literal shapes, so a drift of BOTH sides together still shows in review
    assert loc.cache.endswith("/eval/v4-reprobe/verdict-cache") and loc.results.endswith("/eval/reprobe-" + "d" * 12)


def test_constants_are_one_definition_not_three():
    lf = _launcher()
    assert lf.DIGEST_RE is pf.DIGEST_RE is pf.wave_spec.DIGEST_RE
    assert lf.SERVICE_ACCOUNT == pf.SERVICE_ACCOUNT and lf.PROJECT_NUMBER == pf.PROJECT_NUMBER
    assert lf.BUCKET == pf.BUCKET and lf.PROJECT == pf.PROJECT


def test_the_source_allow_list_is_the_stagers_and_holds_no_fusion_360():
    stage = sys.modules["v4_stage"]
    assert pf.ALLOWED_SOURCES == stage.ALLOWED_SOURCES
    assert "f360" not in pf.ALLOWED_SOURCES and "f360" not in stage.ALLOWED_SOURCES


# ------------------------------------------------------------------ the staged corpus is complete

def _inputs(*shas: str) -> list[str]:
    return [f"{pf.INPUTS}/{h}/x.step" for h in shas]


def test_a_half_staged_corpus_is_refused():
    assert "inputs_sources" in _failed(_run(FakeGcloud(listing=_inputs(SHA_A))))
    assert "inputs_sources" not in _failed(_run(FakeGcloud(listing=_inputs(SHA_A, SHA_B))))


def test_a_staged_dir_with_no_files_does_not_count_as_staged():
    listing = [f"{pf.INPUTS}/{SHA_A}/x.step", f"{pf.INPUTS}/{SHA_B}/:"]
    assert "inputs_sources" in _failed(_run(FakeGcloud(listing=listing)))


def test_a_held_out_entry_staged_under_a_neutral_name_is_refused_by_its_sha():
    listing = _inputs(SHA_A, SHA_B, SHA_F360)           # no "f360" anywhere in the path
    report = _run(FakeGcloud(listing=listing))
    assert "inputs_sources" in _failed(report)
    assert "neutral name" in next(c.detail for c in report.checks if c.name == "inputs_sources")


def test_an_object_outside_the_cleared_pool_is_refused():
    assert "inputs_sources" in _failed(_run(FakeGcloud(listing=_inputs(SHA_A, SHA_B, "9" * 64))))


def test_the_default_path_compares_against_the_committed_pool():
    """No `expected` override and no --staged-source: the pool manifest decides (the default `main` path)."""
    wanted, barred = pf.expected_inputs(pf.DEFAULT_POOL, None)
    assert len(wanted) == 10887 and barred          # the F360-excluded pool; F360 is in the manifest
    full = _inputs(*sorted(wanted))
    ok = pf.run_preflight(runner=FakeGcloud(listing=full), image=IMAGE, commit=COMMIT, service_account=SA)
    assert "inputs_sources" not in _failed(ok), [c for c in ok.checks if not c.ok]
    short = pf.run_preflight(runner=FakeGcloud(listing=full[:-1]), image=IMAGE, commit=COMMIT, service_account=SA)
    assert "inputs_sources" in _failed(short)
    leaked = pf.run_preflight(runner=FakeGcloud(listing=full + _inputs(sorted(barred)[0])), image=IMAGE,
                              commit=COMMIT, service_account=SA)
    assert "inputs_sources" in _failed(leaked)


def test_an_unreadable_pool_manifest_is_a_refusal(tmp_path):
    report = pf.run_preflight(runner=FakeGcloud(), image=IMAGE, commit=COMMIT, service_account=SA,
                              pool_path=tmp_path / "missing.json")
    assert "inputs_sources" in _failed(report)


# ------------------------------------------------------------------ a green run says what it did not prove

def test_every_iam_only_pass_carries_its_caveat():
    report = _run(FakeGcloud())
    by = {c.name: c for c in report.checks}
    for name in ("inputs_read", "cache_write", "results_write", "image_pullable"):
        assert "deny policies" in by[name].caveat and "retention" in by[name].caveat, name
    assert "LAUNCHER" in by["inputs_read"].caveat
    assert "path-name regex" in by["inputs_sources"].caveat
    assert "built from --commit" in by["image_pinned"].caveat
    assert all(not c.caveat for c in report.checks if not c.ok)


def test_the_provenance_stamp_says_the_image_commit_link_is_unverified():
    stamp = pf.provenance_stamp(commit=COMMIT, image=IMAGE)
    assert stamp["image_commit_link"].startswith("unverified")


# ------------------------------------------------------------------ the launcher cannot proceed without it

class _Failing:
    def __init__(self, ok: bool = False):
        self.ok = ok
        self.kw: dict = {}

    def __call__(self, **kw):
        self.kw = kw
        return pf.Report([pf.Check("inputs_read", self.ok, "boom" if not self.ok else "fine",
                                   "IAM only" if self.ok else "")])


@pytest.fixture
def launcher_env(monkeypatch):
    import subprocess
    lf = _launcher()
    monkeypatch.setattr(lf.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(lf, "_gcloud_json", lambda cmd: {"image_summary": {"tags": [COMMIT[:12]]}, "bindings": [
        {"role": "roles/storage.objectAdmin", "members": [f"serviceAccount:{lf.SERVICE_ACCOUNT}"]}],
        "quotas": []})
    return lf


def test_live_checks_runs_the_preflight_with_the_launchers_own_arguments(launcher_env, monkeypatch):
    lf = launcher_env
    fake = _Failing(ok=True)
    monkeypatch.setattr(pf, "run_preflight", fake)
    args = _v4_args(lf)
    args.machine_type = "c2d-standard-8"
    problems, warnings = lf.live_checks(args, 8)
    assert not problems and any("IAM only" in w for w in warnings)
    assert fake.kw["image"] == IMAGE and fake.kw["commit"] == COMMIT
    assert fake.kw["locations"] == pf.locations_for_prefix(args.prefix, COMMIT)
    assert fake.kw["pool_path"] == Path(args.pool) and fake.kw["staged_sources"] == args.sources


def test_a_failing_preflight_is_a_launch_problem(launcher_env, monkeypatch):
    lf = launcher_env
    monkeypatch.setattr(pf, "run_preflight", _Failing(ok=False))
    args = _v4_args(lf)
    args.machine_type = "c2d-standard-8"
    problems, _ = lf.live_checks(args, 8)
    assert any("preflight inputs_read: boom" in p for p in problems)


def test_the_v3_profile_does_not_run_the_v4_preflight(launcher_env, monkeypatch):
    lf = launcher_env
    fake = _Failing(ok=False)
    monkeypatch.setattr(pf, "run_preflight", fake)
    args = _v4_args(lf)
    args.profile, args.machine_type = "v3", "c2d-standard-8"
    args.prefix = lf.PREFIX
    lf.live_checks(args, 8)
    assert fake.kw == {}


def test_an_image_the_registry_will_not_describe_is_a_refusal_not_a_warning(launcher_env, monkeypatch):
    lf = launcher_env
    monkeypatch.setattr(pf, "run_preflight", _Failing(ok=True))
    real = lf._gcloud_json

    def describe_fails(cmd):
        if "images" in cmd:
            raise RuntimeError("PERMISSION_DENIED")
        return real(cmd)

    monkeypatch.setattr(lf, "_gcloud_json", describe_fails)
    args = _v4_args(lf)
    args.machine_type = "c2d-standard-8"
    problems, warnings = lf.live_checks(args, 8)
    assert any("cannot describe the image" in p for p in problems)
    assert not any("cannot describe the image" in w for w in warnings)


def test_a_sha_directory_that_happens_to_contain_f360_is_not_a_licence_hit():
    lucky = "0" * 10 + "f360" + "a" * 50
    assert len(lucky) == 64
    exp = ({lucky, SHA_B}, {SHA_F360})
    report = _run(FakeGcloud(listing=_inputs(lucky, SHA_B)), expected=exp)
    assert "inputs_sources" not in _failed(report)
    # ...while the same text in a NAME still refuses
    assert "inputs_sources" in _failed(_run(FakeGcloud(listing=[f"{pf.INPUTS}/{lucky}/f360_part.step"]
                                                       + _inputs(SHA_B)), expected=exp))


# ------------------------------------------------------------------ a wave refuses without a SHARED cache

@pytest.mark.parametrize("cache", ["", "   ", "/tmp/cache", "memory://", "gs://", "gs://dev-crsv3-x", "gs://dev-crsv3-x/"],
                         ids=["blank", "spaces", "directory", "memory", "bare-scheme", "bucket-only", "bucket-slash"])
def test_a_wave_without_a_shared_cache_uri_is_refused(cache):
    base = pf.wave_locations(commit=COMMIT)
    loc = pf.WaveLocations(bucket=base.bucket, inputs=base.inputs, cache=cache, results=base.results)
    assert "cache_shared" in _failed(_run(FakeGcloud(), locations=loc))


def test_the_derived_cache_location_passes_cache_shared_and_the_check_is_always_run():
    report = _run(FakeGcloud())
    assert "cache_shared" not in _failed(report)
    assert "cache_shared" in {c.name for c in report.checks} and "cache_shared" in pf.CHECKS


def test_the_inputs_are_listed_flat_not_recursively():
    """`gcloud storage ls --recursive` walks the prefix one sub-prefix at a time: over the staged 10,887
    <sha256>/ directories it ran past 600 s (the runner's limit is 120 s), so a fully staged corpus FAILED
    inputs_read. The flat object glob listed the same 10,887 objects in 4.2 s (lane D, 2026-11-23)."""
    fake = FakeGcloud()
    _run(fake)
    listings = [c for c in fake.calls if c[1:3] == ["storage", "ls"]]
    assert listings == [["gcloud", "storage", "ls", f"{pf.INPUTS}/**"]]


def test_an_abc_name_whose_hex_id_contains_f360_is_not_a_licence_hit():
    """The real cleared pool holds abc/0059_00593670_f36073a853d1bbbdb978a01e_step_000.step: `f360` inside ABC's
    hex object id. A bare substring match refused the fully staged corpus. Only `f360` standing alone (not inside
    a hex run) is a name hit; a real F360 file is caught by its sha256 against the manifest in any case."""
    name = "0059_00593670_f36073a853d1bbbdb978a01e_step_000.step"
    exp = ({SHA_A, SHA_B}, {SHA_F360})
    report = _run(FakeGcloud(listing=[f"{pf.INPUTS}/{SHA_A}/{name}"] + _inputs(SHA_B)), expected=exp)
    assert "inputs_sources" not in _failed(report)
    for bad in ("f360_part.step", "x.f360.step", "parts-F360/a.step", "fusion360.step"):
        rep = _run(FakeGcloud(listing=[f"{pf.INPUTS}/{SHA_A}/{bad}"] + _inputs(SHA_B)), expected=exp)
        assert "inputs_sources" in _failed(rep), bad


# ------------------------------------------------------------------ image provenance: the label, not the clock

def test_the_image_must_carry_the_commit_it_was_built_from_as_a_label():
    report = _run(FakeGcloud())
    check = next(c for c in report.checks if c.name == "image_commit_label")
    assert check.ok and IMAGE_COMMIT in check.detail


@pytest.mark.parametrize("revision,why", [(None, "no org.opencontainers.image.revision label"),
                                          ("f" * 40, "was built from"), ("dirty", "was built from")])
def test_an_unlabelled_or_mislabelled_image_is_refused(revision, why):
    report = _run(FakeGcloud(image_revision=revision))
    assert "image_commit_label" in _failed(report) and not report.ok
    assert why in next(c.detail for c in report.checks if c.name == "image_commit_label")


def test_an_abbreviated_or_missing_image_commit_is_refused_not_prefix_matched():
    assert "image_commit_label" in _failed(_run(FakeGcloud(), image_commit=IMAGE_COMMIT[:12]))
    assert "image_commit_label" in _failed(_run(FakeGcloud(), image_commit=None))


def test_an_unreadable_registry_config_is_a_refusal():
    assert "image_commit_label" in _failed(_run(FakeGcloud(inspect_rc=1)))


def test_the_stamp_says_verified_only_with_the_label_it_checked():
    plain = pf.provenance_stamp(commit=COMMIT, image=IMAGE)
    assert plain["image_commit_link"] == pf.IMAGE_LINK_UNVERIFIED
    stamp = pf.provenance_stamp(commit=COMMIT, image=IMAGE, image_commit=IMAGE_COMMIT, image_revision=IMAGE_COMMIT)
    assert stamp["image_commit"] == IMAGE_COMMIT and stamp["image_commit_link"].startswith("verified")
    with pytest.raises(ValueError, match="built from"):
        pf.provenance_stamp(commit=COMMIT, image=IMAGE, image_commit=IMAGE_COMMIT, image_revision="f" * 40)


def test_image_revision_reads_the_config_label_of_a_multi_platform_index_too():
    single = json.dumps({"config": {"Labels": {pf.REVISION_LABEL: IMAGE_COMMIT}}})
    multi = json.dumps({"linux/amd64": {"config": {"Labels": {pf.REVISION_LABEL: IMAGE_COMMIT}}}})
    for body in (single, multi):
        assert pf.image_revision(lambda argv, b=body: pf.Result(0, b, ""), IMAGE) == IMAGE_COMMIT
    assert pf.image_revision(lambda argv: pf.Result(0, json.dumps({"config": {}}), ""), IMAGE) is None
