# Responsibility: Build one wheel and the runtime images from it - api, pipeline, and the off-box mesh runner.
# Owns: the stage order, which keeps the native mesh toolchain below the application layers.
# Boundaries: self-contained - no private registry and no prebuilt base; versions come from requirements/.

# meshpipeline - multi-stage, multi-target build. One wheel, three runtime images.
#
#   base      shared: python 3.11 + the pinned pip deps (requirements/runtime.txt). Heavy pip deps
#             (pyvista/vtk/gmsh/cadquery-ocp) install here as wheels; their SYSTEM libraries
#             are added only in the target that actually runs them (measured: every
#             entrypoint's module-level import closure is light - the heavy deps are lazy).
#
#   wheel     builds the ONE `meshpipeline` distribution. Source lives ONLY here.
#
#   runtime-base  installs that wheel into site-packages (+ ui/, alembic/, entrypoints) and
#             verifies it resolves from site-packages with its package data. The three runtime
#             targets build on this, so production runs the SAME artefact the tests install -
#             no source tree, no PYTHONPATH.
#
#   api       light API / maintenance target. Serves FastAPI + drives the Celery beat /
#             maintenance tasks. No render stack, no mesh toolchains.
#             build:  make dev-build            (supplies APP_VERSION from the product authority)
#
#   pipeline  the Celery pipeline worker. Runs the LangGraph pipeline incl. the reviewer's
#             offscreen PyVista/EGL render and the planner's OCC geometry analysis. Adds the
#             mesa/EGL/ffmpeg render stack. Meshing itself runs OFF-BOX, so NO OpenFOAM/vmtk.
#             build:  make test-container-smoke  (supplies APP_VERSION from the product authority)
#
#   mesh-toolchain  the STABLE native floor for the mesh runner: OpenFOAM 2412 (apt, pinned to
#             an exact package version), vmtk (conda, isolated), and the gmsh runtime libs.
#             Slow, ~2 GB, and the only layer that needs the ESI package repository - which is
#             served by SourceForge mirrors and is the least reliable thing in this build.
#             The `mesh` target builds it automatically - it is not a separate artefact and
#             needs no registry.
#             build (optional, to prewarm the cache):  make mesh-toolchain
#
#   mesh      the off-box mesh runner (Cloud Run mesh job). Runs OpenFOAM (cfMesh/snappy),
#             vmtk, and OCC tessellation. No db/redis/LLM.
#             build:  make mesh-image      (supplies APP_VERSION from the product authority)
#
# SELF-CONTAINED. This file is everything needed to build every image: no private registry, no
# prebuilt internal base, no digest to substitute, no credentials. A fresh clone builds the
# mesh image with one command.
#
# LAYER ORDER IS LOAD-BEARING. `mesh-toolchain` sits BELOW the application layers, so a change
# to ui/ or the wheel does not invalidate OpenFOAM and vmtk. Inverted, every mesh build
# re-downloads ~67 MB of OpenFOAM packages from whichever SourceForge mirror is chosen that
# minute, and an unreachable mirror fails the build.
# Docker's cache keeps that layer across commits. A COMPLETELY fresh build still downloads
# OpenFOAM from ESI/SourceForge, and an upstream outage can still fail it - that is the
# accepted cost of a script-only build with no prebuilt base.

# Base pinned by DIGEST (not the mutable :22.04 tag) so every org builds the SAME base regardless of
# when the tag is repushed. To bump: `docker pull ubuntu:22.04` then paste its digest here. The apt
# package sources below (deadsnakes PPA, ESI OpenFOAM repo) and the conda/pip installs remain
# network-dependent and version-pinned where they support it - see docs/deployment/overview.md "Build sources".
FROM ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982 AS requirements
# THE DOCUMENTATION/DEPENDENCY BOUNDARY, and nothing else.
#
# `requirements/runtime.txt` is hand-edited and heavily commented - the comments are its
# documentation and they change far more often than the pins do. Everything downstream needs only
# the pins, so this stage reduces the file to them: comments stripped, blank lines dropped,
# trailing whitespace removed.
#
# pip ignores comments and blank lines, so the reduced file installs exactly the same
# distributions - a property the requirements-equivalence check proves rather than assumes.
#
# No network, no apt, no pip: only the already-pulled base digest and coreutils.
COPY requirements/runtime.txt requirements/constraints.txt /in/
RUN mkdir -p /pins \
    && sed -e 's/#.*$//' -e 's/[[:space:]]*$//' -e '/^$/d' /in/runtime.txt > /pins/runtime.txt \
    && sed -e 's/#.*$//' -e 's/[[:space:]]*$//' -e '/^$/d' /in/constraints.txt \
         > /pins/constraints.txt \
    && test -s /pins/runtime.txt && test -s /pins/constraints.txt


FROM ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982 AS base
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# THE UBUNTU ARCHIVE MIRROR, chosen for availability rather than left to DNS. `archive.ubuntu.com`
# is a round-robin over six addresses, and apt resolves the host once and then retries the address
# it already picked - the same failure shape recorded for dl.openfoam.com in
# docs/reference/build-inputs.md, one layer further down. On 2026-09-11 the hosted runners resolved
# to 91.189.91.82, which was refusing connections; apt retried that one address three times
# (`Ign:` `Ign:` `Ign:` then `Err: Connection failed [IP: 91.189.91.82 80]`) and every apt-get in
# every Ubuntu stage ended at exit 100. CI and the release build both stopped, and `Acquire::Retries`
# could not have helped: the dead address is chosen before the first retry. Only a different host
# recovers, so the host is a build input here and not an accident of resolution.
#
# The default is Canonical's Azure archive, which is network-local to the GitHub-hosted runners
# this project builds on - so it is the reachable host AND the fast one - and which carries the
# security suite too, so no stage is left pointing at a host this does not cover. It is a MIRROR,
# not another source: apt verifies every index and package against the Ubuntu signing keys already
# in the image, so this decides which host may ANSWER and never what it may serve. Override
# UBUNTU_MIRROR / UBUNTU_SECURITY_MIRROR to build against a different archive.
#
# This runs in its own layer, before the first apt-get, because every later Ubuntu stage derives
# FROM base and inherits the rewritten file - one rewrite covers all seven apt-get sites. Keep it
# free of the words `curl` and `wget`: test_every_direct_download_is_content_verified scans any RUN
# block mentioning either for URLs and would read the mirror as an unverified download.
#
# The grep asserts the rewrite actually landed - a silently unmatched sed would leave the build
# pointing at the host this exists to avoid, and it would only be noticed during the next outage.
# It is anchored to `//` because the default mirror CONTAINS the string it is checking for:
# `azure.archive.ubuntu.com` ends in `archive.ubuntu.com`, so an unanchored pattern matches the
# rewritten line and fails the build every time.
ARG UBUNTU_MIRROR=http://azure.archive.ubuntu.com/ubuntu
ARG UBUNTU_SECURITY_MIRROR=http://azure.archive.ubuntu.com/ubuntu
RUN sed -i -e "s|http://archive\.ubuntu\.com/ubuntu|${UBUNTU_MIRROR}|g" \
           -e "s|http://security\.ubuntu\.com/ubuntu|${UBUNTU_SECURITY_MIRROR}|g" \
           /etc/apt/sources.list \
    && ! grep -qE '^deb .*//(archive|security)\.ubuntu\.com' /etc/apt/sources.list \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
         > /etc/apt/apt.conf.d/99-hexera-acquire

# deadsnakes FIRST: 22.04's own python3.11 is 3.11.0rc1; the PPA provides current stable
# 3.11.x (same cp311 ABI, so every pinned wheel is unchanged).
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common gnupg ca-certificates curl \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && printf 'Package: python3.11*\nPin: release o=LP-PPA-deadsnakes\nPin-Priority: 1001\n' \
        > /etc/apt/preferences.d/deadsnakes \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3.11-venv python3-pip \
    build-essential curl gnupg ca-certificates software-properties-common \
    # libpq runtime - psycopg[binary] so AsyncPostgresSaver can connect (else silent MemorySaver).
    libpq5 \
    # libGL - cadquery-ocp / vtkmodules load bundled shared libs at import (planner geometry +
    # OCC tessellation). Small; needed by both the pipeline and mesh targets, so it lives here.
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

# The build toolchain is pinned like everything else. `--upgrade pip setuptools wheel` installed
# whatever those projects had released that day, so two builds of the same commit could resolve
# the application's dependencies with different resolvers and different wheel-building behaviour.
# These are the versions the last validated images contain; this changes nothing about what runs.
ARG PIP_VERSION=26.1.2
ARG SETUPTOOLS_VERSION=83.0.0
ARG WHEEL_VERSION=0.47.0
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && python3 -m pip install --upgrade \
         "pip==${PIP_VERSION}" "setuptools==${SETUPTOOLS_VERSION}" "wheel==${WHEEL_VERSION}"

WORKDIR /srv

# pinned deps first (cached layer). vtk/pyvista/pillow are pinned EXACTLY in requirements/runtime.txt.
#
# The file arrives via `--from=requirements` rather than a direct COPY, and that indirection is the
# point. `mesh-toolchain` derives from THIS stage, so anything that invalidates a layer here
# invalidates the OpenFOAM install below it - and a direct COPY is keyed on the file's BYTES, so
# editing a comment rebuilt the entire native toolchain and re-fetched OpenFOAM from the network.
# The `requirements` stage strips comments and blank lines, so this layer is keyed on the PINS
# alone: a documentation-only edit produces an identical file, the cache hits here, and the native
# floor below is never touched. Changing a pin still invalidates it, which is correct.
COPY --from=requirements /pins/runtime.txt ./requirements/runtime.txt
COPY --from=requirements /pins/constraints.txt ./requirements/constraints.txt
RUN pip install --no-cache-dir -c requirements/constraints.txt -r requirements/runtime.txt


FROM base AS wheel
# Build the ONE distribution, once. Every runtime target installs THIS artefact, so what runs
# in production is the same wheel the tests install - not a source tree on PYTHONPATH that can
# resolve differently (and that hid a bare find_spec("engines...") until it crashed a
# container). The source lives only in this stage; it is never copied into a runtime image.
COPY pyproject.toml ./
COPY src/ ./src/
# `build` is pinned in requirements/dev.txt (the one toolchain source of truth), not inline.
# This stage is discarded - no build tooling reaches a runtime image.
COPY requirements/dev.txt ./requirements/dev.txt
COPY --from=requirements /pins/constraints.txt ./requirements/constraints.txt
RUN pip install --no-cache-dir -c requirements/constraints.txt -r requirements/dev.txt \
    && python -m build --wheel --outdir /dist \
    && ls /dist/*.whl


FROM base AS mesh-toolchain
# THE STABLE NATIVE FLOOR. Everything here is slow to build, large, and changes only when a
# toolchain version does - so it sits below the application layers and nothing above it may be
# allowed to invalidate it. Contains no application code and no wheel. Built automatically by
# the `mesh` target; `make mesh-toolchain` builds it alone when you want to prewarm or inspect
# it, but nothing requires that.
#
# Every artefact is pinned to an exact version. Unpinned, `openfoam2412` silently tracked
# whatever build ESI had published most recently (2412.250814-1, then 2412.260127-1) and
# `micromamba/latest` tracked whatever micro.mamba.pm served that day - so two builds of the
# same commit could ship different native binaries. The pinned versions below are exactly what
# the last validated mesh image contained; this changes nothing about what runs.

# OpenFOAM v2412 - ESI's official Debian repository (dl.openfoam.com, mirrored by SourceForge).
# The repository is GPG-signed and apt verifies each package's SHA-256 against the signed
# index, so the pin is the version; the checksums are recorded in
# docs/reference/third-party.md for offline audit.
ARG OPENFOAM_SERIES=2412
ARG OPENFOAM_PKG_VERSION=2412.260127-1
# The repository installer is DOWNLOADED, VERIFIED, then run - never piped into a shell. Piping
# executes whatever the endpoint served, with no opportunity to check it; the three steps are
# separated so a changed script fails before it runs as root.
#
# The signing key is verified by its COMPLETE fingerprint, not by the fact that a key arrived.
# Both values were reviewed against the official endpoint and confirmed stable across repeated
# downloads. A mismatch here is a signal to review, never to update these values.
#
# The two packages are DOWNLOADED BY DIGEST rather than resolved through apt, because
# dl.openfoam.com redirects every request to a SourceForge mirror chosen per request and a dead
# mirror times out instead of failing over. `Acquire::Retries` does not help: apt retries the
# already-resolved mirror URL, so a clean-room build hit the same dead mirror six times and
# `make mesh-image` aborted with no remedy in the manual. curl re-requests the redirector on each
# retry and is handed a different mirror. Verifying each archive against a reviewed SHA-256 makes
# the mirror irrelevant to correctness: whichever one answers must serve these exact bytes. The
# repository is still added and key-verified, so apt resolves the dependency graph from a signed
# authority; retries remain for those dependency fetches.
#
# The verified archives are installed with `dpkg -i`, not by handing their paths to `apt-get
# install`. Passing local files to apt does NOT stop it re-downloading the same packages from the
# repository - a verification build proved it fetched both from a mirror and unpacked those copies
# instead - which would have left the whole mirror problem in place behind a checksum that no
# longer described what was installed. dpkg installs exactly these bytes; the follow-up
# `apt-get -f install` then resolves the remaining dependencies from the pinned Ubuntu archive.
#
# The mirror redirect above is not merely slow or occasionally down: SourceForge sometimes answers
# with HTTP 200 and a small HTML "choose a mirror" page instead of the archive, for either file.
# `curl -f` only rejects HTTP error status codes, so that page is saved as if it were the .deb and
# only the checksum step below catches it. A single download-then-verify attempt therefore fails
# the build on a transient mirror hiccup that a second attempt (very likely against a different
# mirror) would clear. Each download is wrapped in an until-loop that re-downloads and re-verifies
# up to 5 times, sleeping between attempts, and only gives up - naming the file and the expected
# digest - once every attempt has failed. Do not collapse this back to a single curl + sha256sum:
# that is what silently broke before.
#
# The `&&`/`||` chain below is one flat list, and `&&`/`||` are left-associative with EQUAL
# precedence: `A && B || true && C` parses as `((A && B) || true) && C`, not "`|| true` applies to
# B alone". A bare `dpkg -i ... || true` at the end of this list - intended only to tolerate the
# dependency errors `dpkg -i` is expected to report - would therefore swallow the exit status of
# EVERY command before it, including both `sha256sum -c` checksum gates. That is exactly the bug
# that shipped: a mismatched checksum printed FAILED, `|| true` absorbed it, and the build ran on
# to fail three steps later with a misleading "bashrc not found" instead of at the checksum. The
# fix is to scope `|| true` to the `dpkg -i` command alone with an explicit `{ ...; }` group, and to
# isolate the whole install chain in its own group terminated by `|| exit 1` BEFORE the final
# `test -f ... || (echo ... && exit 1)` - otherwise that trailing `||` has the same shape and
# reports "bashrc not found" for a failure that happened much earlier (e.g. a missing package).
ARG OPENFOAM_REPO_SCRIPT_SHA256=f7fa288327e936b5a85e3e4a0b29bf039c06d214916f39400b830b63a3310b5b
ARG OPENFOAM_PUBKEY_FPR=DC93C096174122E256DA24063386DD74948D208F
ARG OPENFOAM_DEB_BASE=https://dl.openfoam.com/repos/deb/dists/jammy/main/pool/2412_260127
ARG OPENFOAM_COMMON_DEB_SHA256=956359cdbfd0e3a75ce1dd05e522ddfa43a124c6a087722169f275a5d40d238f
ARG OPENFOAM_DEB_SHA256=12fab3754b9ae5e2fb19edadf31e4b5e3995c85ac8f8c867bb6064bc2b7993cd
RUN { curl -fsSL https://dl.openfoam.com/add-debian-repo.sh -o /tmp/add-debian-repo.sh \
      && echo "${OPENFOAM_REPO_SCRIPT_SHA256}  /tmp/add-debian-repo.sh" | sha256sum -c - \
      && curl -fsSL https://dl.openfoam.com/pubkey.gpg -o /tmp/openfoam-pubkey.gpg \
      && gpg --show-keys --with-colons /tmp/openfoam-pubkey.gpg \
           | awk -F: '/^fpr:/{print $10; exit}' | grep -qx "${OPENFOAM_PUBKEY_FPR}" \
      && bash /tmp/add-debian-repo.sh \
      && rm -f /tmp/add-debian-repo.sh /tmp/openfoam-pubkey.gpg \
      && apt-get -o Acquire::Retries=5 update \
      && { n=0; until curl -fsSL --retry 5 --retry-all-errors --retry-delay 2 -o /tmp/openfoam-common.deb \
               "${OPENFOAM_DEB_BASE}/binary-all/openfoam${OPENFOAM_SERIES}-common_${OPENFOAM_PKG_VERSION}_all.deb" \
             && echo "${OPENFOAM_COMMON_DEB_SHA256}  /tmp/openfoam-common.deb" | sha256sum -c -; do \
               n=$((n + 1)); \
               if [ "${n}" -ge 5 ]; then \
                 echo "ERROR: /tmp/openfoam-common.deb did not verify against sha256 ${OPENFOAM_COMMON_DEB_SHA256} after 5 attempts (SourceForge may be serving a mirror-selection page instead of the package)" >&2; \
                 exit 1; \
               fi; \
               echo "WARN: openfoam-common.deb attempt ${n} failed checksum verification; retrying in 5s..." >&2; \
               sleep 5; \
             done; } \
      && { n=0; until curl -fsSL --retry 5 --retry-all-errors --retry-delay 2 -o /tmp/openfoam.deb \
               "${OPENFOAM_DEB_BASE}/binary-amd64/openfoam${OPENFOAM_SERIES}_${OPENFOAM_PKG_VERSION}_amd64.deb" \
             && echo "${OPENFOAM_DEB_SHA256}  /tmp/openfoam.deb" | sha256sum -c -; do \
               n=$((n + 1)); \
               if [ "${n}" -ge 5 ]; then \
                 echo "ERROR: /tmp/openfoam.deb did not verify against sha256 ${OPENFOAM_DEB_SHA256} after 5 attempts (SourceForge may be serving a mirror-selection page instead of the package)" >&2; \
                 exit 1; \
               fi; \
               echo "WARN: openfoam.deb attempt ${n} failed checksum verification; retrying in 5s..." >&2; \
               sleep 5; \
             done; } \
      && { dpkg -i /tmp/openfoam-common.deb /tmp/openfoam.deb 2>/dev/null || true; } \
      && apt-get -o Acquire::Retries=5 -f install -y --no-install-recommends \
      && dpkg -s "openfoam${OPENFOAM_SERIES}" >/dev/null \
      && rm -f /tmp/openfoam-common.deb /tmp/openfoam.deb \
      && rm -rf /var/lib/apt/lists/*; \
    } || exit 1 \
    && test -f "/usr/lib/openfoam/openfoam${OPENFOAM_SERIES}/etc/bashrc" \
    || (echo "ERROR: OpenFOAM bashrc not found at expected path" && exit 1)

# vmtk (ISOLATED): conda-forge vmtk 1.5.0 is py39-only and ships its own vtk-base, which cannot
# coexist with the app's pinned vtk - so it lives in a dedicated micromamba env and is reached
# ONLY as a subprocess via the `vmtk` shim (engines/vmtk/vmtk_runner.py, VMTK_BIN).
# micromamba is fetched from the GitHub RELEASE ASSET for a specific build and verified against a
# reviewed SHA-256 before it is unpacked or run.
#
# The endpoint matters as much as the checksum. A version-only redirector such as
# `micro.mamba.pm/api/micromamba/linux-64/<version>` is not an immutable identity: upstream can
# republish the same version against a newer build, the bytes change, and the pinned checksum
# stops matching. Address the release asset itself, so the identity cannot move under the pin.
#
# `2.8.1-1` is a distinct, immutable RELEASE identity - the build, not just the version - and its
# asset URL cannot be re-pointed. The checksum below was verified against that asset: downloaded
# twice byte-identically, 59 archive entries with no absolute or traversing paths, and the
# extracted binary reports 2.8.1. See docs/reference/build-inputs.md.
#
# A future mismatch here is a signal to review, never to update this value to whatever arrived.
ARG MICROMAMBA_RELEASE=2.8.1-1
ARG MICROMAMBA_VERSION=2.8.1
ARG MICROMAMBA_SHA256=8528263837623551a44464a372e5bd6b0b856479a83d2a77490a19dd98da3b06
ARG VMTK_VERSION=1.5.0
ARG VMTK_PYTHON=3.9
RUN curl -fsSL \
      "https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_RELEASE}/micromamba-linux-64.tar.bz2" \
      -o /tmp/micromamba.tar.bz2 \
    && echo "${MICROMAMBA_SHA256}  /tmp/micromamba.tar.bz2" | sha256sum -c - \
    && tar -xj -C /usr/local -f /tmp/micromamba.tar.bz2 bin/micromamba \
    && rm -f /tmp/micromamba.tar.bz2 \
    && /usr/local/bin/micromamba --version | grep -qx "${MICROMAMBA_VERSION}" \
    && /usr/local/bin/micromamba create -y -p /opt/vmtk-env -c conda-forge \
         "python=${VMTK_PYTHON}" "vmtk=${VMTK_VERSION}" \
    && /usr/local/bin/micromamba clean --all --yes \
    && printf '#!/bin/sh\nexport PATH=/opt/vmtk-env/bin:$PATH\nexec /opt/vmtk-env/bin/vmtk "$@"\n' \
      > /usr/local/bin/vmtk \
    && chmod +x /usr/local/bin/vmtk \
    && test -x /opt/vmtk-env/bin/vmtk \
    || (echo "ERROR: vmtk not installed into /opt/vmtk-env" && exit 1)

# gmsh runtime libs. `gmsh` is a declared mesh engine (structural/FEA), so the OFF-BOX mesh
# runner must be able to run it - but the shipped gmsh Python wheel links libgmsh.so against the
# FLTK/GUI stack and dlopens it even when meshing headless. `ldd libgmsh.so` on this image's wheel
# reports exactly: libGLU.so.1, libXcursor.so.1, libXft.so.2, libXinerama.so.1, libXrender.so.1,
# libfontconfig.so.1. The set below is the SAME proven closure the `pipeline` stage installs for
# gmsh (libXrender/libfontconfig arrive transitively via libxft2/libxcursor1); libGL itself is
# already in `base`. Without these, `import gmsh` fails with `OSError: libGLU.so.1` and the mesh
# runner cannot serve the gmsh engine - the defect this line fixes. A packaging capability smoke
# (tests/native/test_mesh_image_capability_smoke) fails the build/release if gmsh cannot initialise
# here. This is a lean runtime set - NOT a desktop environment.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglu1-mesa libegl1 libxft2 libxcursor1 libxinerama1 libxrandr2 libxi6 \
    && rm -rf /var/lib/apt/lists/*


FROM base AS runtime-base
# The application layers for the api and pipeline targets. The `mesh` target applies the SAME
# sequence on top of `mesh-toolchain` instead - Docker has no way to share a block between two
# different bases, and stacking mesh on this stage would put the native toolchain above the
# application again. Keep the two in step.
#
# Image metadata. APP_VERSION is passed at build time from meshpipeline.__version__ (the ONE version
# authority - see the deploy/docker build path) and never restated here as a second literal. It is
# transported and displayed, not checked: an unsupplied value simply labels the image with nothing.
# Inherited by the api/pipeline targets.
ARG APP_VERSION
LABEL org.opencontainers.image.title="Hexera Platform" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="LLM multi-agent pipeline that generates solver-ready simulation meshes" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"

# The installed distribution + the things that are NOT part of it: the frontend, the migration
# scripts, and the entrypoints.
COPY --from=wheel /dist/*.whl /tmp/
COPY deploy/verify_install.py /tmp/verify_install.py
ARG APP_VERSION
RUN pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -f /tmp/*.whl \
    && python /tmp/verify_install.py \
    && rm -f /tmp/verify_install.py


COPY ui/ ./ui/
COPY docs/reference/third-party.md ./third-party.md
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY deploy/docker/entrypoint.sh .
COPY deploy/docker/worker_entrypoint.sh .
RUN chmod +x entrypoint.sh worker_entrypoint.sh

# NO PYTHONPATH: `meshpipeline` resolves from site-packages. A checkout path here would let the
# image import a source tree that the wheel does not actually ship.
ENV PYTHONUNBUFFERED=1

# The git tree this image was built from. The integration runner compares it against the working
# tree before it lets the image touch a database: an image built from an older tree runs older
# code, and a suite that passes against it has proven nothing about the checkout under test.
# `unknown` is the honest default for an image built outside the supported runner, which treats
# it as stale rather than trusting it.
ARG MESH_SOURCE_TREE=unknown
ENV MESH_SOURCE_TREE=${MESH_SOURCE_TREE}

RUN useradd -m -u 1000 USER_A \
    && mkdir -p /srv/workspaces /srv/data /data/beat \
    && chown -R USER_A:USER_A /srv /data


FROM runtime-base AS api
# Light: FastAPI + maintenance. No render stack, no mesh toolchains.
USER USER_A
# entrypoint runs the advisory-locked migration wrapper (python -m meshpipeline.runtime.migrate)
# then execs the CMD. The worker services override ENTRYPOINT with worker_entrypoint.sh in
# docker-compose.yml, so they do NOT migrate - only this API entrypoint does.
ENTRYPOINT ["/srv/entrypoint.sh"]
CMD ["uvicorn", "meshpipeline.runtime.api_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]


FROM runtime-base AS pipeline
# The Celery pipeline worker: the reviewer renders offscreen with PyVista/EGL and imageio
# writes an MP4; gmsh's Python wheel loads GUI shared libs even headless. Meshing is off-box,
# so NO OpenFOAM/vmtk here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # PyVista headless rendering - EGL (GPU) + OSMesa (software fallback), fully offscreen
    libgl1-mesa-dri libglu1-mesa libegl1 libegl-mesa0 libxt6 libxrender1 libxext6 \
    # gmsh GUI shared libs (required even headless by the Python wheel)
    libxft2 libxcursor1 libxinerama1 libxrandr2 libxi6 \
    # FFmpeg for imageio MP4 output
    ffmpeg \
    # Xvfb - defensive fallback X server (DISPLAY is NOT exported; EGL is used); see worker_entrypoint.sh
    xvfb \
    && rm -rf /var/lib/apt/lists/*
USER USER_A
ENTRYPOINT ["/srv/worker_entrypoint.sh"]
CMD ["celery", "-A", "meshpipeline.runtime.celery_worker", "worker", \
     "--queues", "simulation_jobs", "--concurrency", "1", "--loglevel", "info"]


FROM mesh-toolchain AS mesh
# The off-box mesh runner: OpenFOAM (cfMesh/snappy) + vmtk + OCC tessellation. No db/redis/LLM.
#
# Stacked directly on `mesh-toolchain`, which this same file defines and this same build
# produces - so the native floor is below the application and a UI or wheel change never
# invalidates it. The instructions below are deliberately the same sequence as `runtime-base`;
# keep the two in step.
# Transported and displayed, not checked: an unsupplied value labels the image with nothing.
ARG APP_VERSION
LABEL org.opencontainers.image.title="Hexera Platform" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="LLM multi-agent pipeline that generates solver-ready simulation meshes" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"

COPY --from=wheel /dist/*.whl /tmp/
COPY deploy/verify_install.py /tmp/verify_install.py
ARG APP_VERSION
RUN pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -f /tmp/*.whl \
    && python /tmp/verify_install.py \
    && rm -f /tmp/verify_install.py


COPY ui/ ./ui/
COPY docs/reference/third-party.md ./third-party.md
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY deploy/docker/entrypoint.sh .
COPY deploy/docker/worker_entrypoint.sh .
RUN chmod +x entrypoint.sh worker_entrypoint.sh

# NO PYTHONPATH: `meshpipeline` resolves from site-packages. A checkout path here would let the
# image import a source tree that the wheel does not actually ship.
ENV PYTHONUNBUFFERED=1

# The git tree this image was built from. The integration runner compares it against the working
# tree before it lets the image touch a database: an image built from an older tree runs older
# code, and a suite that passes against it has proven nothing about the checkout under test.
# `unknown` is the honest default for an image built outside the supported runner, which treats
# it as stale rather than trusting it.
ARG MESH_SOURCE_TREE=unknown
ENV MESH_SOURCE_TREE=${MESH_SOURCE_TREE}

RUN useradd -m -u 1000 USER_A \
    && mkdir -p /srv/workspaces /srv/data /data/beat \
    && chown -R USER_A:USER_A /srv /data

USER USER_A
# The mesh job's command is IMMUTABLE and carries no --job-id (the DB is authoritative);
# runtime/mesh_runner reads INPUT_URI/OUTPUT_URI/ENGINE from the environment.
CMD ["python", "-m", "meshpipeline.runtime.mesh_runner"]


FROM pipeline AS validation
# Responsibility: Run the repository's own test contract against the SAME artefact production runs.
# Owns: the developer/CI tooling the tests shell out to, and nothing the application itself needs.
# Boundaries: it derives FROM pipeline and is never a base for one, so no test tooling can reach a
# released image; the repository is mounted at run time, never copied, so this layer cannot go stale.

# Why a target and not a host virtualenv: the pinned wheel set is cp311, and a host interpreter of
# any other version resolves a DIFFERENT dependency set - evidence gathered there is evidence about
# somebody's laptop. This stage is the pipeline image plus the dev toolchain, so the code under test
# is the installed wheel the api/pipeline targets ship.
USER root

# The dev toolchain from the ONE authority. `pipeline` already carries requirements/runtime.txt
# (pytest and pytest-asyncio live there); this adds ruff, mypy, build and pytest-randomly under the
# same constraints file, so no version can differ between a test run here and one in CI.
COPY requirements/dev.txt ./requirements/dev.txt
COPY --from=requirements /pins/constraints.txt ./requirements/constraints.txt
RUN pip install --no-cache-dir -c requirements/constraints.txt -r requirements/dev.txt

# What the suite SHELLS OUT to. Without these, tests that assert on `make help`, on
# `docker compose config` or on a git-clean tree do not fail - they skip, quietly, and a skipped
# contract reads exactly like a proven one in a summary line. `git` additionally makes the
# repository safe to inspect from inside the container.
#
# Both downloads below are PINNED BY CONTENT, like every other direct fetch in this file. The key
# additionally has its fingerprint checked - that says what the key claims to be; the checksum says
# the bytes are the reviewed ones, and neither substitutes for the other.
ARG DOCKER_APT_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
ARG DOCKER_APT_ASC_SHA256=1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570
RUN apt-get update && apt-get install -y --no-install-recommends \
        make git curl ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.asc \
    && echo "${DOCKER_APT_ASC_SHA256}  /tmp/docker.asc" | sha256sum -c - \
    && gpg --batch --with-colons --fingerprint --show-keys /tmp/docker.asc \
         | awk -F: '$1=="fpr"{print $10; exit}' | grep -qx "${DOCKER_APT_FINGERPRINT}" \
    && gpg --batch --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu jammy stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin \
    && rm -f /tmp/docker.asc && rm -rf /var/lib/apt/lists/*

# The repository is mounted read-only, so nothing may try to write beside it. Bytecode and pytest's
# cache are the two that do by default; both are turned off here rather than in every invocation.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTEST_ADDOPTS="-p no:cacheprovider"

# BACK TO THE PIPELINE IMAGE'S USER. Only the layers above needed root. Leaving the image on root
# is not a neutral difference: the integration tier runs as this user and writes into a bind-mounted
# scratch root, and as root it produced files the host could not delete afterwards. The validation
# runner passes an explicit --user anyway, so nothing here depends on root at run time.
USER USER_A

# No ENTRYPOINT and no application CMD: this image runs whatever the caller asks of it.
WORKDIR /repo
ENTRYPOINT []
CMD ["python", "-m", "pytest", "--version"]


FROM validation AS validation-browser
# Responsibility: Add the real browser the UI tier drives, and nothing else.
# Boundaries: a separate target because it is 134MB that only one tier needs.

# WHY IT IS NOT IN `validation`. Every layer above the MESH_SOURCE_TREE stamp is rebuilt whenever
# the source changes, and the container integration tier passes that stamp on every run - so a
# browser installed there re-downloaded 134MB on each commit to serve a tier that never starts it.
# Here it is above a stamp nobody varies, so it is fetched once.
USER root
# THE BROWSER. tests/ui drives the shipped page in a real Chrome and treats an absent one as a
# hard failure, not a skip - "a release must not pass with its browser validation quietly absent"
# (tests/ui/_chrome.py). Google's .deb is what that hint names for Ubuntu, because the `chromium`
# package here is a snap wrapper that cannot run without snapd, which no container has. The
# VERSIONED pool URL is used rather than `..._current_amd64.deb`, because "current" cannot be
# checksummed: its bytes change under the same name.
ARG CHROME_VERSION=151.0.7922.137
ARG CHROME_DEB_SHA256=e6dabf044cf9cd0279cfe86efa431682c18bfc06d06339ce055aaa87ae871727
RUN curl -fsSLo /tmp/chrome.deb \
      "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}-1_amd64.deb" \
    && echo "${CHROME_DEB_SHA256}  /tmp/chrome.deb" | sha256sum -c - \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/* \
    && google-chrome --version
USER USER_A


# ---------------------------------------------------------------------------
# THE CONSOLE. The Next.js browser front door, and the only Node image this repository builds.
# It shares no layer with the stages above: those are a Python distribution and a native mesh
# toolchain, and stacking a Node runtime on either would carry gigabytes this workload never runs.
FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS console-build
WORKDIR /build
ENV CI=1
# The workspace pins its package manager in package.json; corepack honours that pin.
RUN corepack enable
# The manifests first, so a dependency-only change is the only thing that re-resolves the store.
COPY pnpm-workspace.yaml pnpm-lock.yaml package.json tsconfig.base.json ./
COPY packages/ ./packages/
COPY apps/console/package.json ./apps/console/
RUN pnpm install --frozen-lockfile --filter @hexera/console...
COPY apps/console/ ./apps/console/
RUN pnpm --filter @hexera/console build

FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS console
ARG APP_VERSION
LABEL org.opencontainers.image.title="Hexera Console" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="The Hexera browser console" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
WORKDIR /srv
# HOSTNAME is load-bearing: Next's standalone server binds localhost without it, so on Cloud Run
# the container would start, answer nothing, and the revision would never become ready.
ENV NODE_ENV=production \
    PORT=8080 \
    HOSTNAME=0.0.0.0
# The standalone tree carries its own trimmed node_modules; static and public are not traced into
# it and are copied beside the server that serves them.
COPY --from=console-build /build/apps/console/.next/standalone/ ./
COPY --from=console-build /build/apps/console/.next/static/ ./apps/console/.next/static/
COPY --from=console-build /build/apps/console/public/ ./apps/console/public/
# node:24-slim already ships a "node" user at uid 1000 (see /etc/passwd in the base image);
# creating a new "console" user at the same uid fails with "UID 1000 is not unique", so the
# existing account is reused here rather than invented.
RUN chown -R node:node /srv
USER node
EXPOSE 8080
CMD ["node", "apps/console/server.js"]


# ---------------------------------------------------------------------------
# THE ADMIN CONSOLE. The same shape as the console stages above and pinned to the same Node base,
# deliberately: two Node images in one Dockerfile would be two things to keep current.
FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS admin-build
WORKDIR /build
ENV CI=1
RUN corepack enable
COPY pnpm-workspace.yaml pnpm-lock.yaml package.json tsconfig.base.json ./
COPY packages/ ./packages/
COPY apps/admin-console/package.json ./apps/admin-console/
RUN pnpm install --frozen-lockfile --filter @hexera/admin-console...
COPY apps/admin-console/ ./apps/admin-console/
RUN pnpm --filter @hexera/admin-console build
# The outreach sender, bundled as one file. It is a Cloud Run JOB rather than a route, so Next does
# not build it; esbuild resolves its imports into the same tree the console uses, so the job and the
# pages cannot drift apart on the engine they run.
RUN pnpm --filter @hexera/admin-console build:worker
# The one-time importer, bundled the same way. In the image rather than run from a laptop so it
# resolves the same engine, the same schema and the same connection the console does.
RUN pnpm --filter @hexera/admin-console build:import

FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS admin
ARG APP_VERSION
LABEL org.opencontainers.image.title="Hexera Admin Console" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="The Hexera operations console" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary"
WORKDIR /srv
# HOSTNAME is load-bearing: Next's standalone server binds localhost without it, so on Cloud Run
# the container would start, answer nothing, and the revision would never become ready.
ENV NODE_ENV=production \
    PORT=8080 \
    HOSTNAME=0.0.0.0
COPY --from=admin-build /build/apps/admin-console/.next/standalone/ ./
COPY --from=admin-build /build/apps/admin-console/.next/static/ ./apps/admin-console/.next/static/
COPY --from=admin-build /build/apps/admin-console/public/ ./apps/admin-console/public/
COPY --from=admin-build /build/apps/admin-console/outreach-worker.js ./apps/admin-console/outreach-worker.js
COPY --from=admin-build /build/apps/admin-console/import-outreach.js ./apps/admin-console/import-outreach.js
# node:24-slim already ships a `node` user at uid 1000; creating another at that uid fails.
RUN chown -R node:node /srv
USER node
EXPOSE 8080
CMD ["node", "apps/admin-console/server.js"]
