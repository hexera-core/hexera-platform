#!/usr/bin/env bash
# Responsibility: Run a native tier inside the mesh image, against the installed distribution.
# Boundaries: the tier is a required argument - smoke and all differ by orders of magnitude in runtime.

# Native mesh-toolchain test tier - runs INSIDE the `mesh` Docker image, the off-box execution
# image. Builds that image (so the gmsh runtime-lib fix and OpenFOAM/vmtk install are exercised),
# then runs the native tests THROUGH THE INSTALLED DISTRIBUTION (production provenance - no source
# on PYTHONPATH), reporting per-engine.
#
# The tier is a required argument - there is no default, because the two differ by orders of
# magnitude in runtime and guessing wrong wastes an image build:
#
#   run_tier.sh smoke   → the bounded packaging capability smoke (-m native_smoke): every engine
#                         executable/module present, importable, versionable; gmsh initialises in
#                         the mesh image; the dispatch registry resolves all five. Fast.
#   run_tier.sh all     → the full native tier: capability smoke + minimal five-engine native
#                         execution + controlled native failure cases. Minutes, not seconds.
#
# This proves REAL native tooling + execution locally. It does NOT prove hosted GCS/Neon/Upstash,
# Cloud Run, or production-scale mesh quality.
set -euo pipefail

# The build context and the bind-mounted suite below are repo-relative, so run from the repo
# root whatever the caller's CWD is (`make test-native-smoke` and a direct `bash` both work).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

MESH_IMAGE="${MESH_IMAGE:-meshpipeline-mesh:native}"

usage() { echo "usage: run_tier.sh {smoke|all|benchmark}" >&2; exit 2; }

# Resolve the tier BEFORE any docker work. Building the mesh image costs minutes, so a typo'd,
# missing, or over-supplied selector must fail here - having paid for an image only to be told the
# usage is wrong is the kind of feedback nobody reads twice. Exactly one argument, because
# `run_tier.sh smoke all` reads like both tiers and silently ran only the first. Nothing above this
# point touches docker, the filesystem, or the network, so a rejected invocation leaves the machine
# exactly as it found it.
#
# The native-terminal matrix needs live services and is a tier of its own
# (make test-native-terminal); it is EXCLUDED from these service-free tiers.
[ "$#" -eq 1 ] || usage
MODE="$1"
# Only the benchmark tier gets a writable mount, and only for its evidence file: the point of that
# tier is numbers somebody reads afterwards, and a container that keeps them to itself leaves a
# green line as the entire result. The other tiers stay read-only throughout.
EVIDENCE=()
case "$MODE" in
  smoke) SELECT=(-m native_smoke); TARGETS=(/srv/tests/native); TIMEOUT=600 ;;
  all)   SELECT=(-m "not native_terminal"); TARGETS=(/srv/tests/native); TIMEOUT=3600 ;;
  benchmark)
    SELECT=(-m native)
    TARGETS=(/srv/tests/native/test_native_engine_execution.py
             /srv/tests/native/test_native_engine_benchmark.py)
    TIMEOUT=3600
    EVIDENCE_DIR="${BENCHMARK_EVIDENCE_DIR:-$PWD/.benchmark-evidence}"
    # World-writable because the image runs as uid 1000 and this directory is created by whoever
    # ran make. On a host whose caller is not uid 1000, an owner-only directory means the engines
    # run for minutes and the write at the end raises PermissionError - the one failure that costs
    # the whole tier and reports nothing. Docker Desktop maps ownership and hides this; Linux does
    # not. Same reason validate.sh chmods its own evidence directories.
    mkdir -p "$EVIDENCE_DIR" && chmod 777 "$EVIDENCE_DIR"
    EVIDENCE=(-v "$EVIDENCE_DIR:/srv/evidence" -e BENCHMARK_EVIDENCE_DIR=/srv/evidence)
    ;;
  *) usage ;;
esac

# Self-contained: the `mesh` target builds the pinned native floor (`mesh-toolchain`) itself.
# That floor sits below the application layers, so Docker reuses it across commits; only a
# completely cold cache re-downloads OpenFOAM.
# The image is built by `make mesh-image`, never here: a runner that builds its own image can
# turn a stale image current in the middle of its own preflight, and the result would describe
# whatever it just built rather than what was under test.
bash tests/native/assert_mesh_image_current.sh "$MESH_IMAGE"

echo "── running native tier ($MODE) inside $MESH_IMAGE ──"
# mount the suite + pyproject (marker registry) read-only; run as the image's unprivileged user.
# testpaths is overridden to the native dir; the positional path is authoritative regardless.
docker run --rm \
  -e OMPI_ALLOW_RUN_AS_ROOT=1 -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$PWD/devtools:/srv/devtools:ro" \
  -v "$PWD/pyproject.toml:/srv/pyproject.toml:ro" \
  "${EVIDENCE[@]+"${EVIDENCE[@]}"}" \
  --entrypoint python "$MESH_IMAGE" \
  -m pytest -q -rs -p no:cacheprovider -c /srv/pyproject.toml \
  -o testpaths=/srv/tests/native -o cache_dir=/tmp/pytest_cache \
  "${SELECT[@]}" "${TARGETS[@]}"

echo "── native tier ($MODE): OK ──"
