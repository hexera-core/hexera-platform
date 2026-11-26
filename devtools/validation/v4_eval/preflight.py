#!/usr/bin/env python3
# Responsibility: Where a CRS v4 wave lives in GCS, which image it runs, and the proof - from the
#                 launching host, before anything is created - that the fleet can actually use both.
# Owns: the permission model (role -> storage permission table), the bucket/prefix convention
#       (`wave_locations`, derived from wave_spec), the digest pin (`require_pinned_image`,
#       `resolve_to_digest`, `provenance_stamp`, `metadata_items`) and the permissions preflight
#       (`run_preflight`, CLI `main`).
# Boundaries: READ-ONLY. It issues gcloud `get-iam-policy`, `describe` and `ls` only - never a write,
#             a probe upload or a create. Every gcloud call goes through an injected `Runner`, so the
#             unit tier never reaches the network. It does not launch VMs (8b), stage inputs (8c) or
#             collect (8e). The fleet runs as the project's default compute service account.
"""The wave's location convention, its image pin and its permissions preflight.

    preflight.py --image <ref@sha256:...> --commit <40-hex> [--staged-source abc_fail ...]
    preflight.py pin <ref:tag>        # print the digest-pinned reference for a tag

Locations are NOT restated here: they come from wave_spec.py, which v3_eval/launch_fleet.py (the
authority on what a wave writes) also imports:

    gs://dev-crsv3-transfer-<project-number>/eval/<name>/inputs/            staged corpus (8c)
    gs://dev-crsv3-transfer-<project-number>/eval/<name>/verdict-cache/     verdict cache  (--cache-uri)
    gs://dev-crsv3-transfer-<project-number>/eval/reprobe-<c12>/            per-commit results root

`<name>` defaults to v4-reprobe. The cache prefix does NOT carry the commit: the verdict cache key
already holds the STEP sha256, the panel config hash and the verdict code version, so one prefix is
shared by every wave. Results carry the commit, as v3's `eval/<arm>-<commit12>` does.

The preflight proves each of these and REFUSES (exit 1) when any one cannot be proven; an
unverifiable permission is a refusal, never a warning, because a wave runs on self-deleting spot VMs
and a failure found after launch leaves nothing to diagnose:

    locations        cache is not nested in inputs/results; every bucket is a crsv3 bucket
    identity         the service account is the expected one, exists and is enabled
    image_pinned     the image is `...@sha256:<64 hex>`, not a floating tag
    image_exists     the registry holds that exact digest
    image_pullable   the service account holds a registry read role on the repository
    inputs_read      the union of the service account's roles supplies storage.objects.get AND .list
                     on the input bucket, and the prefix lists non-empty
    inputs_sources   the staged corpus is COMPLETE (every cleared pool entry's sha256 directory is
                     present), holds nothing outside the cleared pool, and no Fusion 360 data
    cache_shared     the cache URI is a shared gs://<bucket>/<prefix>, never blank, a directory or memory
    cache_write      the roles supply get + list + create + delete on the cache prefix
    results_write    the roles supply get + list + create + delete on the results prefix

Permissions are modelled, not guessed from role names: a role is only a bundle of permissions, and a
role that holds `list` but not `get` (legacyBucketOwner) or `get` but not `list`
(legacyObjectReader/Owner) cannot read a corpus. The union over the account's unconditional grants
must cover the permissions the wave needs.

What a green run does NOT prove (each passing check prints it): it proves IAM GRANTS. IAM deny
policies, org policy, VPC-SC, uniform bucket-level access, retention and object lock are not
evaluated; the `storage ls` in inputs_read runs as the LAUNCHER, not as the service account; nothing is
written. Only unconditional direct grants count (conditions and group membership are not resolved, so
they refuse). The image digest is pinned but not verified to have been built from --commit.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


#: One definition each, shared with the launcher (wave_spec) and the stager (stage.ALLOWED_SOURCES).
wave_spec: Any = _load("v4_wave_spec", HERE / "wave_spec.py")
_stage: Any = _load("v4_stage", HERE / "stage.py")
_pool: Any = _load("v4_pool_manifest", HERE / "pool.py")

PROJECT: str = wave_spec.PROJECT
PROJECT_NUMBER: str = wave_spec.PROJECT_NUMBER
BUCKET: str = wave_spec.BUCKET
SERVICE_ACCOUNT: str = wave_spec.SERVICE_ACCOUNT
DIGEST_RE: re.Pattern[str] = wave_spec.DIGEST_RE
WAVE_NAME = "v4-reprobe"
BUCKET_PREFIX = "gs://dev-crsv3-"
DEFAULT_POOL = HERE.parent / "corpora" / "v4" / "pool.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Sources the benchmark may measure: stage.py's allow-list, imported so the two cannot diverge.
#: Fusion 360 ("f360") is on licence hold and is absent from both until the owner rules.
ALLOWED_SOURCES: frozenset[str] = _stage.ALLOWED_SOURCES
#: `f360` standing alone, not inside a hex run (ABC ids are hex: abc/0059_00593670_f36073a8... is cleared ABC)
_HELD_OUT_PATH = re.compile(r"(?<![0-9a-f])f360(?![0-9a-f])|fusion[\s_-]*360", re.IGNORECASE)
_SHA_DIR = re.compile(r"^[0-9a-f]{64}/")

GET, LIST = "storage.objects.get", "storage.objects.list"
CREATE, DELETE = "storage.objects.create", "storage.objects.delete"
#: What the wave needs of the bucket. Reading the corpus and hitting the cache need get AND list.
#: Writing needs create AND delete (publish re-copies over rsync'd files; a rerun removes a FAILED
#: marker) and, because the cache is read back and rsync lists its destination, get + list as well.
READ_PERMISSIONS = frozenset({GET, LIST})
WRITE_PERMISSIONS = frozenset({GET, LIST, CREATE, DELETE})

_ALL = frozenset({GET, LIST, CREATE, DELETE, "storage.objects.update"})
#: Role -> the object permissions it carries (only the four this wave cares about, plus update).
#: Source: `gcloud iam roles describe <role>` for the three legacy roles (as run by the reviewer of
#: task 8d: legacyBucketOwner has objects.list but NOT objects.get; legacyObjectReader and
#: legacyObjectOwner have objects.get but NOT objects.list), and Google's published predefined-role
#: reference for the rest. NOT re-run in this change (no network allowed): re-verify with
#: `gcloud iam roles describe roles/storage.<role> --format='value(includedPermissions)'`.
#: A role missing from this table contributes NOTHING (custom roles are not resolved: refusal).
#: legacyBucketWriter has list+create+delete but not get; objectCreator is create-only.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "roles/storage.objectViewer": frozenset({GET, LIST}),
    "roles/storage.objectCreator": frozenset({CREATE}),
    "roles/storage.objectUser": frozenset({GET, LIST, CREATE, DELETE, "storage.objects.update"}),
    "roles/storage.objectAdmin": _ALL,
    "roles/storage.admin": _ALL,
    "roles/storage.legacyBucketReader": frozenset({LIST}),
    "roles/storage.legacyBucketWriter": frozenset({LIST, CREATE, DELETE}),
    "roles/storage.legacyBucketOwner": frozenset({LIST, CREATE, DELETE, "storage.objects.update"}),
    "roles/storage.legacyObjectReader": frozenset({GET}),
    "roles/storage.legacyObjectOwner": frozenset({GET, "storage.objects.update"}),
    "roles/viewer": frozenset({GET, LIST}),
    "roles/editor": _ALL,
    "roles/owner": _ALL,
}
REGISTRY_READ_ROLES = frozenset({
    "roles/artifactregistry.reader", "roles/artifactregistry.writer", "roles/artifactregistry.repoAdmin",
    "roles/artifactregistry.admin", "roles/editor", "roles/owner",
})

CHECKS = ("locations", "cache_shared", "identity", "image_pinned", "image_exists", "image_pullable",
          "image_commit_label", "inputs_read", "inputs_sources", "cache_write", "results_write")


# --------------------------------------------------------------------------- runner seam

@dataclass(frozen=True)
class Result:
    rc: int
    out: str = ""
    err: str = ""


#: Runs one command and returns its result. The default shells out to gcloud; tests pass a fake.
Runner = Callable[[Sequence[str]], Result]


def _subprocess_runner(argv: Sequence[str]) -> Result:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(124, "", f"{type(exc).__name__}: {exc}")
    return Result(done.returncode, done.stdout, done.stderr)


def gcloud_runner() -> Runner:
    return _subprocess_runner


# --------------------------------------------------------------------------- locations

@dataclass(frozen=True)
class WaveLocations:
    """The three prefixes a wave touches. `cache` and `results` are what the launcher passes the VMs."""
    bucket: str
    inputs: str
    cache: str
    results: str            # the per-commit results ROOT, <bucket>/eval/reprobe-<c12>


_PLACEHOLDER_COMMIT = "0" * 40


def locations_for_prefix(prefix: str, commit: str, bucket: str = BUCKET) -> WaveLocations:
    """The launcher's locations for `--prefix`: inputs under it, the cache from wave_spec, results at
    wave_spec.results_root. Nothing is restated here; drift fails tests/unit/dev/test_v4_preflight.py."""
    return WaveLocations(bucket=bucket, inputs=f"{prefix}/inputs", cache=wave_spec.verdict_cache_uri(prefix),
                         results=wave_spec.results_root(wave_spec.V4_ARM, commit, bucket))


def wave_locations(bucket: str = BUCKET, name: str = WAVE_NAME, commit: str = _PLACEHOLDER_COMMIT) -> WaveLocations:
    if not bucket.startswith(BUCKET_PREFIX) or "/" in bucket[len("gs://"):] or bucket != bucket.strip():
        raise ValueError(f"bucket must be a crsv3-owned bucket, gs://dev-crsv3-<name> with no path: {bucket!r}")
    if not _NAME_RE.match(name) or name in (".", ".."):
        raise ValueError(f"wave name must be a single path segment of [A-Za-z0-9._-]: {name!r}")
    return locations_for_prefix(f"{bucket}/eval/{name}", commit, bucket)


_DEFAULT = wave_locations()
INPUTS, CACHE_URI = _DEFAULT.inputs, _DEFAULT.cache


def _bucket_of(uri: str) -> str:
    return "gs://" + uri[len("gs://"):].split("/", 1)[0]


def _within(inner: str, outer: str) -> bool:
    return inner.rstrip("/") == outer.rstrip("/") or inner.startswith(outer.rstrip("/") + "/")


# --------------------------------------------------------------------------- image pin

def require_pinned_image(image: str) -> str:
    """The reference unchanged if it carries a full sha256 digest; ValueError otherwise (a tag moves)."""
    if not DIGEST_RE.match(image or ""):
        raise ValueError(
            f"image must be pinned by digest (<region>-docker.pkg.dev/...@sha256:<64 lowercase hex>), "
            f"not {image!r}; `preflight.py pin <ref:tag>` resolves a tag")
    return image


def resolve_to_digest(ref: str, runner: Runner) -> str:
    """Ask the registry for the digest behind `ref` (a tag) and return the pinned reference."""
    done = runner(["gcloud", "artifacts", "docker", "images", "describe", ref, "--format=json"])
    if done.rc != 0:
        raise ValueError(f"cannot describe {ref}: {done.err.strip()[:300]}")
    try:
        digest = (json.loads(done.out or "{}").get("image_summary") or {}).get("digest") or ""
    except json.JSONDecodeError as exc:
        raise ValueError(f"unreadable describe output for {ref}: {exc}") from exc
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"the registry returned no usable digest for {ref}: {digest!r}")
    repo = ref.split("@", 1)[0]
    last = repo.rsplit("/", 1)[-1]
    if ":" in last:
        repo = repo[: len(repo) - len(last) + last.index(":")]
    return require_pinned_image(f"{repo}@{digest}")


#: The stamp of an image whose label was not checked. `commit` is the code archive the VMs run; `image` is the
#: toolchain, pinned by digest; without the label check nothing says which commit built it.
IMAGE_LINK_UNVERIFIED = "unverified: the digest is pinned, not checked against an image label for this commit"
#: The OCI label `make mesh-image` (and image_build.py) stamps with the 40-hex commit of a CLEAN tree ("dirty" else).
#: The registry config of a pinned digest cannot change, so a label read from it is a fact about that digest.
REVISION_LABEL = "org.opencontainers.image.revision"


def image_revision(runner: Runner, image: str) -> str | None:
    """The REVISION_LABEL of `image`'s config, read from the registry (no pull), or None when it has none. A config
    the registry will not give raises: unknown is not 'unlabelled'. A multi-platform index is read per platform, and
    its platforms must agree."""
    done = runner(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Image}}"])
    if done.rc != 0:
        raise RuntimeError(f"cannot read the image config of {image}: {done.err.strip()[:200]}")
    try:
        body = json.loads(done.out or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"unreadable image config for {image}: {exc}") from exc
    configs = [body] if "config" in body else [v for v in body.values() if isinstance(v, dict)]
    found = {((c.get("config") or {}).get("Labels") or {}).get(REVISION_LABEL) for c in configs}
    if len(found) != 1:
        raise ValueError(f"the platforms of {image} disagree on {REVISION_LABEL}: {sorted(map(str, found))}")
    return found.pop()


def check_image_commit(image_commit: str | None, revision: str | None) -> str:
    """The launch's `--image-commit` against the label actually on the image. Full 40-hex on both sides: a prefix
    would match more than one commit, and "built 2 minutes after" is not a link."""
    if not image_commit or not COMMIT_RE.match(image_commit):
        raise ValueError(f"--image-commit must be the full 40-hex commit the image was built from, not {image_commit!r}")
    if revision is None:
        raise ValueError(f"the image has no {REVISION_LABEL} label: rebuild it with image_build.py (or make "
                         "mesh-image from a clean tree), which stamps the commit it was built from")
    if revision != image_commit:
        raise ValueError(f"the image was built from {revision!r} ({REVISION_LABEL}), not --image-commit {image_commit}")
    return f"verified: {REVISION_LABEL}={revision}"


def provenance_stamp(*, commit: str, image: str, image_commit: str | None = None,
                     image_revision: str | None = None) -> dict[str, str]:
    """The `commit`/`image` pair every shard's PROVENANCE must carry. `image` is the digest-pinned
    reference, so the comparability gate (which compares `image`) and collect's blank refusal are
    satisfied by a real pin rather than a tag. With `image_commit` and the `image_revision` label read from the
    registry, the link is checked (`check_image_commit`, which raises on a mismatch) and the stamp says verified."""
    commit = (commit or "").strip()
    if not COMMIT_RE.match(commit):
        raise ValueError(f"commit must be a full 40-hex sha, not {commit!r}")
    match = DIGEST_RE.match(require_pinned_image(image))
    assert match is not None
    stamp = {"commit": commit, "image": image, "image_digest": match.group(1),
             "image_commit_link": IMAGE_LINK_UNVERIFIED}
    if image_commit is not None:
        stamp["image_commit"] = image_commit
        stamp["image_commit_link"] = check_image_commit(image_commit, image_revision)
    return stamp


def metadata_items(*, commit: str, image: str, locations: WaveLocations | None = None) -> list[str]:
    """`key=value` instance-metadata items for the fleet launcher: the pin and the cache location."""
    stamp = provenance_stamp(commit=commit, image=image)
    loc = locations or wave_locations(commit=commit)
    return [f"v4-commit={stamp['commit']}", f"v4-image={stamp['image']}",
            f"v4-image-digest={stamp['image_digest']}", f"v4-cache-uri={loc.cache}",
            f"v4-inputs-uri={loc.inputs}", f"v4-results-uri={loc.results}"]


# --------------------------------------------------------------------------- preflight

@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    caveat: str = ""   # printed under a PASSING check: what the pass does not prove


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def _json(done: Result, what: str) -> dict:
    if done.rc != 0:
        raise RuntimeError(f"cannot read {what}: {done.err.strip()[:200] or 'rc=' + str(done.rc)}")
    try:
        data = json.loads(done.out or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unreadable {what}: {exc}") from exc
    return data if isinstance(data, dict) else {}


IAM_ONLY = ("IAM grants only: deny policies, org policy, VPC-SC, uniform bucket-level access, "
            "retention and object lock are not evaluated")
CAVEATS = {
    "image_pinned": "the digest is not verified to have been built from --commit, or to be the mesh image",
    "image_pullable": IAM_ONLY + "; registry reachability from the VM is not tested",
    "inputs_read": IAM_ONLY + "; the `storage ls` ran as the LAUNCHER, not as the service account",
    "inputs_sources": ("completeness and licence checks run on object paths and pool-manifest sha256 "
                       "directories, not on file content; Fusion 360 is spotted by a path-name regex "
                       "and by sha, so F360 bytes staged under a manifest sha are not seen"),
    "cache_write": IAM_ONLY + "; nothing was written or read as the service account",
    "results_write": IAM_ONLY + "; nothing was written as the service account",
}


def _supplied(policies: Iterable[dict], member: str, needed: frozenset[str]) -> tuple[list[str], frozenset[str]]:
    """(unconditional roles granted to `member` that carry any needed permission, permissions still
    missing after the UNION of every role the member holds). A role absent from ROLE_PERMISSIONS
    supplies nothing."""
    have: set[str] = set()
    roles: list[str] = []
    for policy in policies:
        for b in policy.get("bindings", []):
            perms = ROLE_PERMISSIONS.get(b.get("role", ""), frozenset())
            if member in b.get("members", []) and not b.get("condition") and perms & needed:
                if b["role"] not in roles:
                    roles.append(str(b["role"]))
                have |= perms
    return roles, needed - have


def _holds(policies: Iterable[dict], member: str, roles: frozenset[str]) -> str | None:
    """A role from `roles` granted to `member` by an unconditional binding in any policy, else None."""
    for policy in policies:
        for b in policy.get("bindings", []):
            if b.get("role") in roles and member in b.get("members", []) and not b.get("condition"):
                return str(b["role"])
    return None


class _Preflight:
    def __init__(self, runner: Runner, sa: str) -> None:
        self.run, self.sa, self.member = runner, sa, f"serviceAccount:{sa}"
        self._cache: dict[str, dict] = {}

    def policy(self, kind: str, target: str, argv: list[str]) -> dict:
        key = f"{kind}:{target}"
        if key not in self._cache:
            self._cache[key] = _json(self.run(argv + ["--format=json"]), f"the {kind} IAM policy of {target}")
        return self._cache[key]

    def bucket_policy(self, bucket: str) -> dict:
        return self.policy("bucket", bucket, ["gcloud", "storage", "buckets", "get-iam-policy", bucket])

    def project_policy(self) -> dict:
        return self.policy("project", PROJECT, ["gcloud", "projects", "get-iam-policy", PROJECT])

    def storage_permissions(self, uri: str, needed: frozenset[str]) -> str:
        """The roles that together supply every permission in `needed` on `uri`'s bucket; ValueError
        names the missing permissions otherwise."""
        roles, missing = _supplied([self.bucket_policy(_bucket_of(uri)), self.project_policy()],
                                   self.member, needed)
        if missing:
            raise ValueError(f"{self.sa} lacks {sorted(missing)} on {_bucket_of(uri)} (unconditional roles "
                             f"held that count: {roles or 'none'}); a role name is not a permission")
        return ", ".join(roles)


def _check_locations(loc: WaveLocations) -> str:
    for uri in (loc.inputs, loc.cache, loc.results):
        if not uri.startswith(BUCKET_PREFIX):
            raise ValueError(f"{uri} is not under a crsv3 bucket ({BUCKET_PREFIX}...)")
    pairs = [("cache", loc.cache, "inputs", loc.inputs), ("inputs", loc.inputs, "cache", loc.cache),
             ("cache", loc.cache, "results", loc.results), ("results", loc.results, "cache", loc.cache),
             ("inputs", loc.inputs, "results", loc.results), ("results", loc.results, "inputs", loc.inputs)]
    for a, ua, b, ub in pairs:
        if _within(ua, ub):
            raise ValueError(f"the {a} prefix {ua} is inside the {b} prefix {ub}")
    return f"inputs={loc.inputs} cache={loc.cache} results={loc.results}"


def _check_cache_shared(loc: WaveLocations) -> str:
    problem = wave_spec.shared_cache_problem(loc.cache)
    if problem:
        raise ValueError(problem)
    return f"cache={loc.cache} is a shared object-store location"


def _check_identity(p: _Preflight, expected: str) -> str:
    if p.sa != expected:
        raise ValueError(f"service account {p.sa!r} is not the expected {expected!r}")
    data = _json(p.run(["gcloud", "iam", "service-accounts", "describe", p.sa,
                        f"--project={PROJECT}", "--format=json"]), f"service account {p.sa}")
    if data.get("email") != p.sa:
        raise ValueError(f"gcloud reports {data.get('email')!r} for {p.sa}")
    if data.get("disabled"):
        raise ValueError(f"{p.sa} is disabled")
    return f"{p.sa} exists and is enabled"


def _check_image_exists(p: _Preflight, image: str) -> str:
    pinned = DIGEST_RE.match(require_pinned_image(image))
    assert pinned is not None
    done = p.run(["gcloud", "artifacts", "docker", "images", "describe", image, "--format=json"])
    if done.rc != 0:
        raise RuntimeError(f"registry has no {image}: {done.err.strip()[:200]}")
    got = (json.loads(done.out or "{}").get("image_summary") or {}).get("digest")
    if got != pinned.group(1):
        raise ValueError(f"registry reports digest {got!r}, the pin is {pinned.group(1)}")
    return f"{pinned.group(1)} exists"


def _check_image_pullable(p: _Preflight, image: str) -> str:
    match = re.match(r"^([a-z0-9-]+)-docker\.pkg\.dev/([^/]+)/([^/]+)/", image)
    if not match:
        raise ValueError(f"cannot find the registry repository in {image!r}")
    location, project, repo = match.groups()
    policy = p.policy("repository", f"{project}/{repo}", [
        "gcloud", "artifacts", "repositories", "get-iam-policy", repo,
        f"--location={location}", f"--project={project}"])
    role = _holds([policy, p.project_policy()], p.member, REGISTRY_READ_ROLES)
    if not role:
        raise ValueError(f"{p.sa} has no unconditional registry read role on {project}/{repo} or the project")
    return f"{role} on {project}/{repo}"


def _check_inputs_read(p: _Preflight, loc: WaveLocations) -> tuple[str, list[str]]:
    role = p.storage_permissions(loc.inputs, READ_PERMISSIONS)
    # the flat object glob, not --recursive: recursive walks sub-prefix by sub-prefix and took >600 s on the
    # staged 10,887 <sha256>/ directories (runner limit 120 s); the glob listed them in 4.2 s
    done = p.run(["gcloud", "storage", "ls", loc.inputs + "/**"])
    if done.rc != 0:
        raise RuntimeError(f"cannot list {loc.inputs}: {done.err.strip()[:200]}")
    names = [line.strip() for line in done.out.splitlines() if line.strip()]
    if not names:
        raise ValueError(f"{loc.inputs} is empty: nothing staged")
    return f"{role}; {len(names)} listed", names


def expected_inputs(pool_path: Path, sources: Iterable[str] | None) -> tuple[set[str], set[str]]:
    """(sha256 of every pool entry the wave stages, sha256 of every entry it must NOT stage)."""
    wanted = set(sources) & ALLOWED_SOURCES if sources else set(ALLOWED_SOURCES)
    entries = _pool.read_pool(pool_path)
    keep = {e.sha256 for e in entries if e.source in wanted}
    return keep, {e.sha256 for e in entries} - keep


def _check_sources(names: list[str], staged: Iterable[str] | None, loc: WaveLocations,
                   expected: tuple[set[str], set[str]]) -> str:
    keep, refused = expected
    if not keep:
        raise ValueError("the pool manifest has no entry in the allowed sources: nothing to compare against")
    rel = [n[len(loc.inputs):].lstrip("/") if n.startswith(loc.inputs) else n for n in names]
    # The sha256 directory is hex, and 64 random hex digits contain "f360" often enough to matter (it
    # does, in the real pool); only what follows a sha directory is a NAME. The sha itself is checked
    # against the manifest below.
    held = [n for n in rel if _HELD_OUT_PATH.search(_SHA_DIR.sub("", n, count=1))]
    if held:
        raise ValueError(f"Fusion 360 data staged under {loc.inputs} (licence hold): {held[:3]}")
    bad = sorted(set(staged or ()) - ALLOWED_SOURCES)
    if bad:
        raise ValueError(f"sources {bad} are not in the allow-list {sorted(ALLOWED_SOURCES)}")
    objects = [r for r in rel if r and not r.endswith(("/", ":")) and "/" in r]
    got = {r.split("/", 1)[0] for r in objects}
    barred = sorted(got & refused)
    if barred:
        raise ValueError(f"{len(barred)} staged sha256 dir(s) belong to pool entries outside the allowed "
                         f"sources (e.g. {barred[:2]}): a held-out source staged under a neutral name")
    unexpected = sorted(got - keep)
    if unexpected:
        raise ValueError(f"{len(unexpected)} staged top-level entr(ies) are not in the cleared pool "
                         f"(e.g. {unexpected[:3]})")
    missing = sorted(keep - got)
    if missing:
        raise ValueError(f"the staged corpus is incomplete: {len(got)} of {len(keep)} expected sha256 "
                         f"directories present, {len(missing)} missing (e.g. {missing[:2]})")
    return f"{len(keep)} of {len(keep)} expected files staged ({len(objects)} objects), no held-out source"


def _check_write(p: _Preflight, uri: str) -> str:
    return p.storage_permissions(uri, WRITE_PERMISSIONS) + f" on {_bucket_of(uri)}"


def run_preflight(*, image: str, commit: str, image_commit: str | None = None, service_account: str = SERVICE_ACCOUNT,
                  runner: Runner | None = None, locations: WaveLocations | None = None,
                  staged_sources: Iterable[str] | None = None, pool_path: Path | None = None,
                  expected: tuple[set[str], set[str]] | None = None) -> Report:
    """Run every check. A check that raises (including a runner timeout) FAILS; none is skipped.

    `pool_path` (default: the committed v4 pool) says what the staged corpus must hold; `expected`
    overrides it with (wanted sha256s, barred sha256s) for tests."""
    p = _Preflight(runner or gcloud_runner(), service_account)
    loc = locations or wave_locations(commit=commit)
    report = Report()
    state: dict[str, list[str]] = {}

    def step(name: str, fn: Callable[[], str]) -> None:
        try:
            report.checks.append(Check(name, True, fn(), CAVEATS.get(name, "")))
        except Exception as exc:  # noqa: BLE001 - fail closed: anything unprovable is a refusal
            report.checks.append(Check(name, False, f"{type(exc).__name__}: {exc}"))

    def inputs_read() -> str:
        detail, state["names"] = _check_inputs_read(p, loc)
        return detail

    def inputs_sources() -> str:
        exp = expected if expected is not None else expected_inputs(pool_path or DEFAULT_POOL, staged_sources)
        return _check_sources(state["names"], staged_sources, loc, exp)

    pinned_ok = True
    try:
        require_pinned_image(image)
        provenance_stamp(commit=commit, image=image)
    except ValueError as exc:
        pinned_ok = False
        report.checks.append(Check("image_pinned", False, str(exc)))
    if pinned_ok:
        report.checks.append(Check("image_pinned", True, image, CAVEATS["image_pinned"]))

    step("locations", lambda: _check_locations(loc))
    step("cache_shared", lambda: _check_cache_shared(loc))
    step("identity", lambda: _check_identity(p, SERVICE_ACCOUNT))
    if pinned_ok:
        step("image_exists", lambda: _check_image_exists(p, image))
        step("image_pullable", lambda: _check_image_pullable(p, image))
        step("image_commit_label", lambda: check_image_commit(image_commit, image_revision(p.run, image)))
    else:
        for name in ("image_exists", "image_pullable", "image_commit_label"):
            report.checks.append(Check(name, False, "skipped: the image is not pinned by digest"))
    step("inputs_read", inputs_read)
    if "names" in state:
        step("inputs_sources", inputs_sources)
    else:
        report.checks.append(Check("inputs_sources", False, "skipped: the inputs could not be listed"))
    step("cache_write", lambda: _check_write(p, loc.cache))
    step("results_write", lambda: _check_write(p, loc.results))
    report.checks.sort(key=lambda c: CHECKS.index(c.name))
    return report


def _print(report: Report) -> None:
    for c in report.checks:
        print(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        if c.ok and c.caveat:
            print(f"         note: {c.caveat}")
    print("preflight passed (see the notes: it proves IAM grants and staged names, not runtime access)"
          if report.ok else "REFUSED: preflight failed; launch nothing")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["pin"]:
        if len(argv) != 2:
            print("usage: preflight.py pin <ref:tag>")
            return 2
        try:
            print(resolve_to_digest(argv[1], gcloud_runner()))
        except ValueError as exc:
            print(f"REFUSED: {exc}")
            return 1
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", required=True, help="<ref>@sha256:<64 hex>")
    parser.add_argument("--commit", required=True, help="the 40-hex code commit")
    parser.add_argument("--image-commit", default=None,
                        help="the 40-hex commit the image was built from; must equal its org.opencontainers.image.revision")
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--name", default=WAVE_NAME)
    parser.add_argument("--service-account", default=SERVICE_ACCOUNT)
    parser.add_argument("--staged-source", action="append", default=[], dest="staged_sources")
    parser.add_argument("--pool", default=str(DEFAULT_POOL), help="the pool manifest the staged corpus is compared to")
    args = parser.parse_args(argv)
    try:
        loc = wave_locations(args.bucket, args.name, args.commit)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 1
    report = run_preflight(image=args.image, commit=args.commit, image_commit=args.image_commit,
                           service_account=args.service_account,
                           runner=gcloud_runner(), locations=loc,
                           staged_sources=args.staged_sources or None, pool_path=Path(args.pool))
    _print(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
