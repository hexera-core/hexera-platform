#!/usr/bin/env python3
# Responsibility: Where a CRS v4 wave lives in GCS, which image it runs, and the proof - from the
#                 launching host, before anything is created - that the fleet can actually use both.
# Owns: the bucket/prefix convention (`wave_locations`), the digest pin (`require_pinned_image`,
#       `resolve_to_digest`, `provenance_stamp`, `metadata_items`) and the permissions preflight
#       (`run_preflight`, CLI `main`).
# Boundaries: READ-ONLY. It issues gcloud `get-iam-policy`, `describe` and `ls` only - never a write,
#             a probe upload or a create. Every gcloud call goes through an injected `Runner`, so the
#             unit tier never reaches the network. It does not launch VMs (8b), stage inputs (8c) or
#             collect (8e). The fleet runs as the project's default compute service account.
"""The wave's location convention, its image pin and its permissions preflight.

    preflight.py --image <ref@sha256:...> --commit <40-hex> [--staged-source abc_fail ...]
    preflight.py pin <ref:tag>        # print the digest-pinned reference for a tag

Locations (same bucket as the v3 eval, so the service account's existing grants apply):

    gs://dev-crsv3-transfer-<project-number>/eval/<name>/inputs/            staged corpus (8c)
    gs://dev-crsv3-transfer-<project-number>/eval/<name>/cache/             verdict cache  (--cache-uri)
    gs://dev-crsv3-transfer-<project-number>/eval/<name>/results/<c12>/     per-wave results

`<name>` defaults to v4-reprobe. The cache prefix does NOT carry the commit: the verdict cache key
already holds the STEP sha256, the panel config hash and the verdict code version, so one prefix is
shared by every wave and a hit is valid exactly when those three agree. Results do carry the commit,
as v3's `eval/<arm>-<commit12>` does.

The preflight proves each of these and REFUSES (exit 1) when any one cannot be proven; an
unverifiable permission is a refusal, never a warning, because a wave runs on self-deleting spot VMs
and a failure found after launch leaves nothing to diagnose:

    locations        cache is not nested in inputs/results; every bucket is a crsv3 bucket
    identity         the service account is the expected one, exists and is enabled
    image_pinned     the image is `...@sha256:<64 hex>`, not a floating tag
    image_exists     the registry holds that exact digest
    image_pullable   the service account holds a registry read role on the repository
    inputs_read      readable (and non-empty) input prefix
    inputs_sources   no Fusion 360 data staged (licence hold) and no source outside the allow-list
    cache_write      the service account can read AND write the cache prefix
    results_write    the service account can write the results prefix

Permissions are proven from the IAM policies, as v3's launch_fleet does: the launching host cannot
act as the service account, and a probe upload would be a mutation. Limits (state them to the
operator): only unconditional direct grants count (conditions and group membership are not
resolved, so they refuse), and IAM deny policies are not evaluated.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

PROJECT = "hexera-dev"
PROJECT_NUMBER = "224734058693"
#: The v3 eval's transfer bucket; the compute service account already has objectAdmin on it.
BUCKET = f"gs://dev-crsv3-transfer-{PROJECT_NUMBER}"
#: The identity the fleet's VMs run as (v3_eval/launch_fleet.py SERVICE_ACCOUNT).
SERVICE_ACCOUNT = f"{PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
WAVE_NAME = "v4-reprobe"
BUCKET_PREFIX = "gs://dev-crsv3-"
DIGEST_RE = re.compile(r"^[a-z0-9.-]+-docker\.pkg\.dev/[^@\s]+@(sha256:[0-9a-f]{64})$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Sources the benchmark may measure. Mirrors task 8's reprobe.ALLOWED_SOURCES (not on this branch
#: yet): Fusion 360 ("f360") is on licence hold - non-commercial, redistribution barred - and must
#: not reach the eval bucket. Keep the two in step; add "f360" in neither until the owner rules.
ALLOWED_SOURCES: frozenset[str] = frozenset({"abc_fail", "abc_pass", "occt", "nist", "inject"})
_HELD_OUT_PATH = re.compile(r"f360|fusion[\s_-]*360", re.IGNORECASE)

#: Roles that read objects (storage.objects.get + list). legacyBucketWriter is deliberately absent:
#: it creates and deletes but cannot get.
READ_ROLES = frozenset({
    "roles/storage.objectViewer", "roles/storage.objectUser", "roles/storage.objectAdmin",
    "roles/storage.admin", "roles/storage.legacyBucketOwner", "roles/storage.legacyObjectReader",
    "roles/storage.legacyObjectOwner", "roles/editor", "roles/owner", "roles/viewer",
})
#: Roles that create AND replace/delete objects (publish re-copies files over rsync'd ones).
#: objectCreator (create-only) is deliberately absent.
WRITE_ROLES = frozenset({
    "roles/storage.objectAdmin", "roles/storage.objectUser", "roles/storage.admin",
    "roles/storage.legacyBucketWriter", "roles/storage.legacyBucketOwner", "roles/editor", "roles/owner",
})
REGISTRY_READ_ROLES = frozenset({
    "roles/artifactregistry.reader", "roles/artifactregistry.writer", "roles/artifactregistry.repoAdmin",
    "roles/artifactregistry.admin", "roles/editor", "roles/owner",
})

CHECKS = ("locations", "identity", "image_pinned", "image_exists", "image_pullable",
          "inputs_read", "inputs_sources", "cache_write", "results_write")


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
    bucket: str
    inputs: str
    cache: str
    results: str

    def results_root(self, commit: str) -> str:
        return f"{self.results}/{commit[:12]}"


def wave_locations(bucket: str = BUCKET, name: str = WAVE_NAME) -> WaveLocations:
    if not bucket.startswith(BUCKET_PREFIX) or "/" in bucket[len("gs://"):] or bucket != bucket.strip():
        raise ValueError(f"bucket must be a crsv3-owned bucket, gs://dev-crsv3-<name> with no path: {bucket!r}")
    if not _NAME_RE.match(name) or name in (".", ".."):
        raise ValueError(f"wave name must be a single path segment of [A-Za-z0-9._-]: {name!r}")
    root = f"{bucket}/eval/{name}"
    return WaveLocations(bucket=bucket, inputs=f"{root}/inputs", cache=f"{root}/cache", results=f"{root}/results")


_DEFAULT = wave_locations()
INPUTS, CACHE_URI, RESULTS = _DEFAULT.inputs, _DEFAULT.cache, _DEFAULT.results


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


def provenance_stamp(*, commit: str, image: str) -> dict[str, str]:
    """The `commit`/`image` pair every shard's PROVENANCE must carry. `image` is the digest-pinned
    reference, so the comparability gate (which compares `image`) and collect's blank refusal are
    satisfied by a real pin rather than a tag."""
    commit = (commit or "").strip()
    if not COMMIT_RE.match(commit):
        raise ValueError(f"commit must be a full 40-hex sha, not {commit!r}")
    match = DIGEST_RE.match(require_pinned_image(image))
    assert match is not None
    return {"commit": commit, "image": image, "image_digest": match.group(1)}


def metadata_items(*, commit: str, image: str, locations: WaveLocations | None = None) -> list[str]:
    """`key=value` instance-metadata items for the fleet launcher: the pin and the cache location."""
    stamp = provenance_stamp(commit=commit, image=image)
    loc = locations or _DEFAULT
    return [f"v4-commit={stamp['commit']}", f"v4-image={stamp['image']}",
            f"v4-image-digest={stamp['image_digest']}", f"v4-cache-uri={loc.cache}",
            f"v4-inputs-uri={loc.inputs}", f"v4-results-uri={loc.results_root(commit)}"]


# --------------------------------------------------------------------------- preflight

@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


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

    def storage_role(self, uri: str, roles: frozenset[str]) -> str | None:
        return _holds([self.bucket_policy(_bucket_of(uri)), self.project_policy()], self.member, roles)


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
    role = p.storage_role(loc.inputs, READ_ROLES)
    if not role:
        raise ValueError(f"{p.sa} has no unconditional read role on {_bucket_of(loc.inputs)} or the project")
    done = p.run(["gcloud", "storage", "ls", "--recursive", loc.inputs + "/"])
    if done.rc != 0:
        raise RuntimeError(f"cannot list {loc.inputs}: {done.err.strip()[:200]}")
    names = [line.strip() for line in done.out.splitlines() if line.strip()]
    if not names:
        raise ValueError(f"{loc.inputs} is empty: nothing staged")
    return f"{role}; {len(names)} listed", names


def _check_sources(names: list[str], staged: Iterable[str] | None, loc: WaveLocations) -> str:
    held = [n for n in names if _HELD_OUT_PATH.search(n[len(loc.inputs):] if n.startswith(loc.inputs) else n)]
    if held:
        raise ValueError(f"Fusion 360 data staged under {loc.inputs} (licence hold): {held[:3]}")
    bad = sorted(set(staged or ()) - ALLOWED_SOURCES)
    if bad:
        raise ValueError(f"sources {bad} are not in the allow-list {sorted(ALLOWED_SOURCES)}")
    return f"{len(names)} objects, no held-out source"


def _check_write(p: _Preflight, uri: str, *, read_back: bool) -> str:
    wrote = p.storage_role(uri, WRITE_ROLES)
    if not wrote:
        raise ValueError(f"{p.sa} has no unconditional create+delete role on {_bucket_of(uri)} or the project")
    if read_back and not p.storage_role(uri, READ_ROLES):
        raise ValueError(f"{p.sa} can write {uri} but has no read role to read it back")
    return f"{wrote} on {_bucket_of(uri)}"


def run_preflight(*, image: str, commit: str, service_account: str = SERVICE_ACCOUNT,
                  runner: Runner | None = None, locations: WaveLocations | None = None,
                  staged_sources: Iterable[str] | None = None) -> Report:
    """Run every check. A check that raises (including a runner timeout) FAILS; none is skipped."""
    p = _Preflight(runner or gcloud_runner(), service_account)
    loc = locations or _DEFAULT
    report = Report()
    state: dict[str, list[str]] = {}

    def step(name: str, fn: Callable[[], str]) -> None:
        try:
            report.checks.append(Check(name, True, fn()))
        except Exception as exc:  # noqa: BLE001 - fail closed: anything unprovable is a refusal
            report.checks.append(Check(name, False, f"{type(exc).__name__}: {exc}"))

    def inputs_read() -> str:
        detail, state["names"] = _check_inputs_read(p, loc)
        return detail

    pinned_ok = True
    try:
        require_pinned_image(image)
        provenance_stamp(commit=commit, image=image)
    except ValueError as exc:
        pinned_ok = False
        report.checks.append(Check("image_pinned", False, str(exc)))
    if pinned_ok:
        report.checks.append(Check("image_pinned", True, image))

    step("locations", lambda: _check_locations(loc))
    step("identity", lambda: _check_identity(p, SERVICE_ACCOUNT))
    if pinned_ok:
        step("image_exists", lambda: _check_image_exists(p, image))
        step("image_pullable", lambda: _check_image_pullable(p, image))
    else:
        for name in ("image_exists", "image_pullable"):
            report.checks.append(Check(name, False, "skipped: the image is not pinned by digest"))
    step("inputs_read", inputs_read)
    if "names" in state:
        step("inputs_sources", lambda: _check_sources(state["names"], staged_sources, loc))
    else:
        report.checks.append(Check("inputs_sources", False, "skipped: the inputs could not be listed"))
    step("cache_write", lambda: _check_write(p, loc.cache, read_back=True))
    step("results_write", lambda: _check_write(p, loc.results, read_back=False))
    report.checks.sort(key=lambda c: CHECKS.index(c.name))
    return report


def _print(report: Report) -> None:
    for c in report.checks:
        print(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    print("preflight passed" if report.ok else "REFUSED: preflight failed; launch nothing")


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
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--name", default=WAVE_NAME)
    parser.add_argument("--service-account", default=SERVICE_ACCOUNT)
    parser.add_argument("--staged-source", action="append", default=[], dest="staged_sources")
    args = parser.parse_args(argv)
    try:
        loc = wave_locations(args.bucket, args.name)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 1
    report = run_preflight(image=args.image, commit=args.commit, service_account=args.service_account,
                           runner=gcloud_runner(), locations=loc,
                           staged_sources=args.staged_sources or None)
    _print(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
