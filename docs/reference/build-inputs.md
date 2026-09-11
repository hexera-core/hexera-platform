# Build inputs and reproducibility

Every artifact Hexera fetches to build, test or deploy is identified immutably and verified
cryptographically. This document says what is locked, where each lock's authority lives, and how
an intentional upgrade is made.

## The contract

An input satisfies the contract when it has **both**:

1. **Immutable identity**, an exact version and build, a full commit SHA, an immutable release
   asset path, or an OCI manifest digest. Something that cannot be re-pointed at different bytes.
2. **Content verification**, a SHA-256, an OCI digest, or signed repository metadata anchored to
   a key verified by complete fingerprint.

A readable tag may sit beside the immutable authority, but the digest or checksum is what controls
resolution.

**Reproducibility is not availability.** An immutable artifact is reproducible *while it remains
published*. Where an input depends on third-party retention, that is stated below rather than
implied away.

## What is locked: and where

| Input | Identity authority | Content authority | Where |
|---|---|---|---|
| Base OS image | `ubuntu:22.04@sha256:0e0a0fc6…` | OCI digest | `Dockerfile` |
| PostgreSQL | `postgres:16-alpine@sha256:57c72fd2…` | OCI digest | `docker-compose.yml` |
| Redis | `redis:7-alpine@sha256:6ab0b6e7…` | OCI digest | `docker-compose.yml`, `.github/workflows/ci.yml` |
| MinIO | `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:a1ea29fa…` | OCI digest | `docker-compose.yml` |
| MinIO client | `quay.io/minio/mc@sha256:a7fe349e…` | OCI digest | `docker-compose.yml` |
| SearXNG | `searxng/searxng@sha256:3bc6ae0e…` | OCI digest | `docker-compose.yml` |
| curl (CI) | `curlimages/curl:8.11.1@sha256:c1fe1679…` | OCI digest | `.github/workflows/ci.yml` |
| CI actions | full 40-character commit SHAs | Git object identity | `.github/workflows/ci.yml` |
| micromamba | release `2.8.1-1` asset URL | `ARG MICROMAMBA_SHA256` + post-install version check | `Dockerfile` |
| OpenFOAM repository installer | official endpoint | `ARG OPENFOAM_REPO_SCRIPT_SHA256` | `Dockerfile` |
| OpenFOAM signing key | official endpoint | complete fingerprint `DC93C096…948D208F` | `Dockerfile` |
| OpenFOAM packages | `openfoam2412=2412.260127-1` | `ARG OPENFOAM_DEB_SHA256`, `ARG OPENFOAM_COMMON_DEB_SHA256` | `Dockerfile` |
| Python runtime deps | exact `==` pins | pip resolution against those pins | `requirements/runtime.txt` |
| Python build toolchain | pip/setuptools/wheel exact pins | same | `Dockerfile` |
| Application wheel | built from the committed tree | Gate C provenance | `Dockerfile` |

Ecosystems the repository does not use, Node/npm, git submodules, cloned dependencies, have no
locks because they have no acquisitions. The reproducibility guard derives this from the tree, so
adding one without a lock fails rather than passing unnoticed.

## Platform

All locks target **linux/amd64**. The digests above are the manifests selected for that platform.

## Making an intentional upgrade

1. Decide the new identity, a release/build, not a floating version.
2. Fetch the artifact and record its bytes: size, SHA-256, and a repeated download to confirm the
   bytes are stable.
3. Inspect it. For an archive, list its entries and confirm no absolute or traversing paths.
4. Confirm the artifact identifies itself as expected once installed.
5. Update the identity **and** the content authority together, in one commit that says why.
6. Rebuild the affected image and compare the installed inventory against the previous one, so the
   change is the intended one and nothing else moved.

## When upstream bytes change

A checksum or digest mismatch is **a signal to review, never a value to update**.

Updating a checksum so it matches whatever arrived removes the only evidence that something
changed. It converts a detection into a silent acceptance, which is precisely the failure the
checksum exists to prevent, and it is indistinguishable from accepting a compromised artifact.

The correct response is to determine *why* the bytes differ:

- **Upstream republished the same version against a new build.** This happened with micromamba
  2.8.1. The repair is to move to the separately identified build (`2.8.1-1`) whose URL cannot be
  re-pointed, not to re-checksum the mutable endpoint.
- **The artifact was genuinely re-released.** Follow the upgrade procedure above.
- **Nothing upstream explains it.** Stop and investigate. Do not build.

A build that fails on a mismatch is working.

## Artifact availability

| Input | Preservation |
|---|---|
| Container images | third-party registry retention (Docker Hub) |
| micromamba `2.8.1-1` | GitHub release asset retention |
| OpenFOAM packages | ESI repository retention; the version is pinned but the repository is not snapshotted |
| Python distributions | PyPI retention |
| CI actions | GitHub repository retention |

None of these is mirrored into this repository. Every one is immutably *identified*, so a rebuild
either reproduces the same bytes or fails loudly, but if an upstream artifact is withdrawn, that
rebuild cannot be performed until the input is re-sourced deliberately. That is a known and
accepted limitation, recorded here rather than described as full independence.

The OpenFOAM repository is the weakest link: ESI publishes no immutable snapshot suite, so the
repository can in principle change what it serves for a version. Both packages are therefore
fetched as exact versioned archives and checked against reviewed SHA-256 values before install, on
top of the signed index and the pinned version, so substitution is not merely detectable, it fails
the build.

That acquisition also decides *which* mirror may answer. `dl.openfoam.com` redirects each request
to a SourceForge mirror chosen per request, and a mirror that is down times out rather than failing
over; `Acquire::Retries` cannot recover, because apt retries the mirror URL it already resolved.
The archives are fetched with `curl --retry`, which re-requests the redirector and lands on a
different mirror, then installed with `dpkg -i`. Handing the local paths to `apt-get install`
instead is not equivalent: apt re-downloads the same packages from the repository and installs
those copies, which would put the mirror back in the path behind a checksum that no longer
described what was installed.
