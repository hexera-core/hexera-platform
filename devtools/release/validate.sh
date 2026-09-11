#!/usr/bin/env bash
# Responsibility: Gate C - build the deployable images once from this commit and validate those artifacts.
# Owns: the verdict, computed from which required checks actually ran; only a pass can later be promoted.
# Boundaries: it validates the built artifact, never the checkout, and writes only under deploy/output/.

# GATE C - release-artifact validation.
#
# Builds the DEPLOYABLE images once from the current commit, proves THOSE ARTIFACTS work without
# the source checkout, and records what was validated in deploy/output/release.json so a later
# promotion can prove it is shipping the same bytes.
#
# The distinction this gate exists to enforce: everything before it tests the SOURCE, and source
# passing says nothing about whether the packaged distribution imports, whether the image has the
# toolchain, or whether the tests were quietly reading src/ off a bind mount. Every check below
# runs against the built artifact, never against the tree.
#
#   - the wheel is installed NON-EDITABLE into a throwaway venv, and provenance is asserted from
#     site-packages (an editable install would re-export the checkout and prove nothing);
#   - every native tier runs inside the image with ZERO bind mounts: fixtures are pushed into a
#     STOPPED container with `docker cp`, so nothing on the host can shadow the installed package.
#     `docker inspect` records Mounts=[] and Binds=null for each container as evidence;
#   - the native-to-terminal matrix is MANDATORY. Gate C provisions its own isolated Postgres,
#     Redis and MinIO, health-checks them, runs the tier, and removes them from a trap. It cannot
#     be skipped into a PASS.
#
# COMPONENTS. Four images are deployable, each a Dockerfile target:
#     app     = Dockerfile target `pipeline`  - serves BOTH the API service and the pipeline job
#     mesh    = Dockerfile target `mesh`      - the off-box mesh job
#     console = Dockerfile target `console`   - the console-service web app
#     admin   = Dockerfile target `admin`     - the admin-service web app
# `app` is validated with BOTH entrypoints because both run from that one image in production.
# (Gate C used to build a separate `api`-target image and validate that; nothing ever deployed it.)
#
# VERDICT. Every check is required or not; the verdict is computed, never asserted:
#     passed      every required check passed
#     incomplete  a required check was skipped or never ran (nothing failed)
#     failed      a required check failed
# Only `passed` can become promotable, and only after `make release-publish` records the
# immutable registry digests of these exact images.
#
# INPUTS   a clean checkout (a dirty tree is refused - the record could not identify what was built)
# OUTPUT   deploy/output/release.json  (gitignored, like deployment.json), written atomically
# NETWORK  what `docker build` needs for base images/wheels; service images are pulled if absent
# MUTATES  nothing outside deploy/output/ and the containers/networks/venvs it creates and removes
#
#   RELEASE_ALLOW_INCOMPLETE=1  debugging escape hatch: skip the native tiers. Produces an
#                               INCOMPLETE verdict that can never be promoted. Never a PASS.
#   KEEP_VENV=1                 keep the throwaway install venv
#   KEEP_WORK=1                 keep this run's scratch directory (build/test logs, evidence)
#                               instead of removing it. Must be set BY THE CALLER for that
#                               invocation; a failing run does not keep it on your behalf. Like
#                               KEEP_VENV, only the exact value 1 enables it.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${REPO_ROOT}"

RECORD_SCHEMA=2
OUT_DIR="${REPO_ROOT}/deploy/output"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/amp-release-XXXXXX")"
# Ownership, written before anything else goes in: cleanup will not delete a directory that cannot
# prove it belongs to this process. 24 abandoned workspaces is what the missing removal cost.
WORK_SENTINEL=".amp-release-owner"
printf '%s\n' "$$" >"${WORK}/${WORK_SENTINEL}"
# The caller's request, read ONCE here - before any check below sets KEEP_WORK to mark that it left
# diagnostics behind. Retention is something you ask for, not something a failure decides for you.
KEEP_WORK_REQUESTED="${KEEP_WORK:-0}"
RESULTS="${WORK}/results.jsonl"
: >"${RESULTS}"

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'
c_off=$'\033[0m'
stage() { printf '\n%s── %s ──%s\n' "${c_bold}" "$1" "${c_off}"; }
ok()    { printf '   %s✓%s %s\n' "${c_grn}" "${c_off}" "$1"; }
bad()   { printf '   %s✗%s %s\n' "${c_red}" "${c_off}" "$1"; }
warnp() { printf '   %s•%s %s\n' "${c_yel}" "${c_off}" "$1"; }
note()  { printf '   %s%s%s\n' "${c_dim}" "$1" "${c_off}"; }

# Unique to THIS run. Every container and network Gate C creates carries it as a label, and
# cleanup removes only what carries it - a developer's running stack is never touched.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
NET="amp-rel-${STAMP}-net"

record() {  # record <name> <passed|failed|skipped|not_run> <required:0|1> [detail]
  python3 - "${RESULTS}" "$1" "$2" "$3" "${4:-}" <<'PY'
import json, sys
with open(sys.argv[1], "a") as fh:
    fh.write(json.dumps({"check": sys.argv[2], "status": sys.argv[3],
                         "required": sys.argv[4] == "1", "detail": sys.argv[5]}) + "\n")
PY
  local label="$1${4:+  -  $4}"
  case "$2" in
    passed)  ok "${label}" ;;
    failed)  bad "${label}" ;;
    skipped) warnp "SKIPPED  ${label}" ;;
    *)       warnp "NOT RUN  ${label}" ;;
  esac
}

# Remove the ONE workspace this process created - that exact resolved path and nothing else. No
# glob, no scan of the temp directory, no age rule: a wildcard in a script that runs with the
# developer's own permissions is how you delete somebody else's data. Every test below is a fact
# about this single path, and any one of them failing means we leave it alone and say so.
_remove_work() {
  local w real parent forbidden
  w="${WORK:-}"
  [ -n "${w}" ] || return 0
  [ ! -L "${w}" ] || { warnp "scratch is a symlink - not removing ${w}"; return 0; }
  [ -d "${w}" ] || return 0
  real="$(cd "${w}" 2>/dev/null && pwd -P)" || return 0
  parent="$(cd "${TMPDIR:-/tmp}" 2>/dev/null && pwd -P)" || return 0
  # Must be exactly <temp parent>/amp-release-<something>. That single shape test also excludes /,
  # the temp parent itself, the repo root, and everything inside the checkout - BETA/, output/ and
  # scratchpad/ can never match it - but the explicit list below says so out loud.
  case "${real}" in
    "${parent}"/amp-release-?*) : ;;
    *) warnp "scratch is not a ${parent}/amp-release-* path - not removing ${real}"; return 0 ;;
  esac
  for forbidden in / "${parent}" "${REPO_ROOT}" "${HOME:-/nonexistent}"; do
    [ "${real}" != "${forbidden}" ] || { warnp "refusing to remove ${real}"; return 0; }
  done
  [ "$(cat "${real}/${WORK_SENTINEL}" 2>/dev/null)" = "$$" ] || {
    warnp "scratch was not created by this run - not removing ${real}"; return 0; }
  rm -rf "${real}"
}

cleanup() {
  local rc=$?
  [ "${KEEP_VENV:-0}" = "1" ] || rm -rf "${WORK}/venv"
  local ids
  ids="$(docker ps -aq --filter "label=amp-release=${STAMP}" 2>/dev/null)"
  # -v REMOVES EACH CONTAINER'S ANONYMOUS VOLUMES WITH IT. postgres, redis and minio each declare
  # a VOLUME (/var/lib/postgresql/data, /data, /data), so every run created three anonymous
  # volumes and `docker rm -f` left all three behind - a measured leak of 3 volumes per Gate C run.
  #
  # This is ownership-safe by Docker's own semantics, not by anything this script infers: `-v`
  # removes only volumes ANONYMOUSLY attached to the container being removed. A named volume
  # mounted into the same container is untouched, and a volume this run never mounted is not even
  # considered. The id list is still the label-scoped one, so nothing outside this run is reached.
  # The alternative - finding "unused" volumes and deleting them - is exactly the baseline-
  # subtraction that destroyed 134 volumes in an earlier round, and is never done here.
  # shellcheck disable=SC2086  # deliberate word splitting over the id list
  [ -n "${ids}" ] && docker rm -f -v ${ids} >/dev/null 2>&1
  docker network rm "${NET}" >/dev/null 2>&1
  [ -d "${WORK}/fixtures" ] && git worktree remove --force "${WORK}/fixtures" >/dev/null 2>&1
  # `python -m build` leaves build/ and src/*.egg-info IN THE TREE even with --outdir elsewhere,
  # and a stale build/ shadows the source on the next import. Removed from a trap so it happens
  # on failure too - the same discipline as the `make wheel` target.
  rm -rf "${REPO_ROOT}/build" "${REPO_ROOT}/dist" "${REPO_ROOT}"/src/*.egg-info
  # The workspace goes LAST, so everything above still has it, and only ever after the release
  # record has been written to deploy/output/ - that record is the one output meant to outlive it.
  if [ "${KEEP_WORK_REQUESTED}" = "1" ]; then
    note "scratch kept on request (KEEP_WORK=1): ${WORK}"
  else
    _remove_work
    [ "${KEEP_WORK:-0}" != "1" ] || note "scratch removed - rerun with KEEP_WORK=1 to keep its logs"
  fi
  return "${rc}"
}
trap cleanup EXIT
# An interrupted or terminated run must clean up on the same path as a finished one. Trapping these
# turns the signal into an `exit`, which is what runs the EXIT trap above; 130/143 are conventional.
trap 'exit 130' INT
trap 'exit 143' TERM

# identify the source
stage "source identity"
if ! git -C "${REPO_ROOT}" rev-parse HEAD >/dev/null 2>&1; then
  bad "not a git checkout - a release record cannot identify what was built"; exit 1
fi
COMMIT="$(git rev-parse HEAD)"
TREE="$(git rev-parse HEAD^{tree})"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if ! git diff --quiet HEAD || [ -n "$(git diff --cached --name-only)" ]; then
  bad "the working tree has uncommitted changes"
  note "a release artifact must be identifiable by commit; commit first"
  exit 1
fi
SHORT="$(git rev-parse --short=12 HEAD)"
printf '   commit %s\n   tree   %s\n   branch %s\n' "${COMMIT}" "${TREE}" "${BRANCH}"

# the wheel
stage "wheel: build, inspect, install non-editable, prove provenance"
rm -rf "${WORK}/dist"
# OFFLINE_BUILD=1 builds against an already-present backend instead of letting pip's build
# isolation fetch one. An air-gapped release validation cannot reach an index, and a gate that
# silently needs the network is a gate that cannot prove the artifact was built from this tree
# alone. Default behaviour (isolation) is unchanged.
# WHICH interpreter runs this gate. The repository virtualenv when it exists, else python3 - and a
# caller may name one explicitly. `"${GATE_PY}"` was hardcoded at ten sites, so Gate C could
# not run at all on a checkout that had not run `make setup`: it reported "wheel build failed",
# which reads like a defect in the artifact rather than the absence of an interpreter. This is the
# same ownership `deploy-preflight.sh` already has under PREFLIGHT_PYTHON.
GATE_PY="${RELEASE_PYTHON:-}"
if [ -z "${GATE_PY}" ] || [ ! -x "${GATE_PY}" ]; then
  GATE_PY="${REPO_ROOT}/.venv/bin/python"
  [ -x "${GATE_PY}" ] || GATE_PY="$(command -v python3)"
fi
if ! "${GATE_PY}" -c "import build" >/dev/null 2>&1; then
  record "release interpreter" failed 1 \
    "${GATE_PY} cannot import 'build'. Run \`make setup\`, or set RELEASE_PYTHON to an interpreter with the project's build and test dependencies"
  KEEP_WORK=1; exit 1
fi
record "release interpreter" passed 1 "${GATE_PY}"

BUILD_ARGS=(--wheel --outdir "${WORK}/dist")
if [ "${OFFLINE_BUILD:-0}" = "1" ]; then
  BUILD_ARGS+=(--no-isolation)
fi
if ! "${GATE_PY}" -m build "${BUILD_ARGS[@]}" >"${WORK}/build.log" 2>&1; then
  record "wheel build" failed 1 "see ${WORK}/build.log"
  KEEP_WORK=1; exit 1
fi
WHEEL="$(ls "${WORK}"/dist/*.whl)"
WHEEL_NAME="$(basename "${WHEEL}")"
WHEEL_SHA="$(sha256sum "${WHEEL}" | cut -d' ' -f1)"
WHEEL_BYTES="$(stat -c%s "${WHEEL}")"
record "wheel build" passed 1 "${WHEEL_NAME}  ${WHEEL_BYTES} bytes"

if "${GATE_PY}" - "${WHEEL}" <<'PY' >"${WORK}/wheel-inspect.txt" 2>&1
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); n = z.namelist()
tops = {x.split("/")[0] for x in n if "/" in x}
bad = [t for t in tops if t != "meshpipeline" and not t.endswith(".dist-info")]
assert not bad, f"foreign top-level entries: {bad}"
prompts = [x for x in n if "/prompts/" in x and x.endswith(".txt")]
assert prompts, "the wheel ships no prompt assets - startup requires them"
assert not any("/tests/" in x or x.startswith("tests/") for x in n), "the wheel ships tests"
print(f"{len(n)} files, single top-level package, {len(prompts)} prompt assets, no tests")
PY
then record "wheel inspect" passed 1 "$(cat "${WORK}/wheel-inspect.txt")"
else record "wheel inspect" failed 1 "$(cat "${WORK}/wheel-inspect.txt")"; fi

# Built by GATE_PY, not by whatever `python3` the host happens to resolve to. The wheel declares
# requires-python >=3.11, so a host whose python3 is older cannot install the project's own
# distribution and this check fails for a reason that has nothing to do with the artifact - on
# Ubuntu 22.04 (python3 = 3.10) every release would be unpromotable. GATE_PY is already the
# interpreter that built the wheel, which is the one whose install proves anything.
"${GATE_PY}" -m venv "${WORK}/venv" >/dev/null 2>&1
if "${WORK}/venv/bin/pip" install --no-cache-dir -c "${REPO_ROOT}/requirements/constraints.txt" "${WHEEL}" >"${WORK}/install.log" 2>&1; then
  record "clean non-editable install" passed 1 "throwaway venv (wheel declares no runtime pins by design)"
else
  record "clean non-editable install" failed 1 "see ${WORK}/install.log"
fi

if PROV="$("${WORK}/venv/bin/python" - <<'PY' 2>&1
import pathlib, meshpipeline
f = pathlib.Path(meshpipeline.__file__).resolve()
assert "site-packages" in f.parts or "dist-packages" in f.parts, f"not installed: {f}"
assert "src" not in f.parts, f"resolved to a source checkout: {f}"
print(f"{meshpipeline.__version__} from {f.parent}")
PY
)"; then record "import provenance (site-packages, not the checkout)" passed 1 "${PROV}"
else record "import provenance (site-packages, not the checkout)" failed 1 "${PROV}"; fi

# the deployable images
stage "images: build each DEPLOYABLE component exactly once"
declare -A TARGET=( [app]=pipeline [mesh]=mesh [console]=console [admin]=admin )
declare -A IMAGE_ID=() IMAGE_TAG=()
for comp in app mesh console admin; do
  tag="meshpipeline-${comp}:${SHORT}"
  IMAGE_TAG["${comp}"]="${tag}"
  # Built unconditionally, from this repository alone - the mesh target builds its own pinned
  # native floor. A tag that already exists locally may name an unrelated image, so "the tag is
  # there" is never accepted as evidence - the recorded ID must belong to THIS build.
  #: THE product version, read textually from its one authority so the build needs no interpreter
#: and no installed package. The Dockerfile refuses any image whose label disagrees with what the
#: wheel actually installs, so this is a transported assertion rather than a second source.
PRODUCT_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' \
  "${REPO_ROOT}/src/meshpipeline/__init__.py" | head -1)"

if docker build --target "${TARGET[$comp]}" --build-arg APP_VERSION="${PRODUCT_VERSION}" -t "${tag}" . \
       >"${WORK}/build-${comp}.log" 2>&1; then
    IMAGE_ID["${comp}"]="$(docker image inspect "${tag}" --format '{{.Id}}')"
    record "image build (${comp}: Dockerfile target ${TARGET[$comp]})" passed 1 \
      "${tag}  ${IMAGE_ID[$comp]:0:19}"
  else
    IMAGE_ID["${comp}"]=""
    record "image build (${comp}: Dockerfile target ${TARGET[$comp]})" failed 1 \
      "see ${WORK}/build-${comp}.log"
  fi
done

stage "images: entrypoints and in-image behaviour"
_img_run() { docker run --rm --label "amp-release=${STAMP}" --network none \
               -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x "$@"; }
if [ -n "${IMAGE_ID[app]}" ]; then
  # BOTH workloads run from this one image in production, so both entrypoints are proved on it.
  if _img_run --entrypoint python "${IMAGE_TAG[app]}" -c \
       "import meshpipeline.runtime.api_server as m; assert m.app" >/dev/null 2>&1
  then record "app image: API entrypoint imports the ASGI app" passed 1
  else record "app image: API entrypoint imports the ASGI app" failed 1; fi
  if _img_run --entrypoint python "${IMAGE_TAG[app]}" -c \
       "import meshpipeline.runtime.celery_worker as w; import pyvista; assert w.celery_app" \
       >/dev/null 2>&1
  then record "app image: pipeline entrypoint imports celery + render stack" passed 1
  else record "app image: pipeline entrypoint imports celery + render stack" failed 1; fi
  if _img_run --entrypoint python "${IMAGE_TAG[app]}" -c \
       "import meshpipeline.runtime.run_job" >/dev/null 2>&1
  then record "app image: run_job resolves (the pipeline job's command)" passed 1
  else record "app image: run_job resolves (the pipeline job's command)" failed 1; fi
else
  record "app image: entrypoints" not_run 1 "no app image was built"
fi
if [ -n "${IMAGE_ID[mesh]}" ]; then
  if _img_run "${IMAGE_TAG[mesh]}" bash -lc \
       'test -f /usr/lib/openfoam/openfoam2412/etc/bashrc && command -v vmtk >/dev/null \
        && python -c "import meshpipeline.runtime.mesh_runner"' >/dev/null 2>&1
  then record "mesh image carries the OpenFOAM + vmtk toolchain" passed 1
  else record "mesh image carries the OpenFOAM + vmtk toolchain" failed 1; fi
  if VER="$(_img_run --entrypoint python "${IMAGE_TAG[mesh]}" -c \
              "import meshpipeline,pathlib;p=pathlib.Path(meshpipeline.__file__);\
print(meshpipeline.__version__, p.parent)" 2>&1)"; then
    case "${VER}" in
      *"/dist-packages/meshpipeline"*|*"/site-packages/meshpipeline"*)
        record "in-image package is the INSTALLED distribution" passed 1 "${VER}" ;;
      *) record "in-image package is the INSTALLED distribution" failed 1 "${VER}" ;;
    esac
  else record "in-image package is the INSTALLED distribution" failed 1 "${VER}"; fi
else
  record "mesh image checks" not_run 1 "no mesh image was built"
fi
if [ -n "${IMAGE_ID[console]}" ]; then
  # An image that builds and cannot serve is a green gate and a broken deploy. The container is
  # started on a published port with the two settings Auth.js requires at boot; the values are
  # deliberately not credentials - nothing here authenticates anyone.
  _cid="$(docker run --rm -d --label "amp-release=${STAMP}" -P \
            -e AUTH_SECRET=gate-c-not-a-real-secret \
            "${IMAGE_TAG[console]}" 2>/dev/null || true)"
  if [ -n "${_cid}" ]; then
    _port="$(docker port "${_cid}" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    _ok=1
    for _try in 1 2 3 4 5 6 7 8 9 10; do
      curl -fsS --max-time 5 "http://127.0.0.1:${_port}/api/internal/health" >/dev/null 2>&1 && { _ok=0; break; }
      sleep 2
    done
    docker rm -f -v "${_cid}" >/dev/null 2>&1 || true
    if [ "${_ok}" = "0" ]; then
      record "console image: serves its health route" passed 1
    else
      record "console image: serves its health route" failed 1 \
        "the container started but never answered /api/internal/health"
    fi
  else
    record "console image: serves its health route" failed 1 "the container did not start"
  fi
else
  record "console image: serves its health route" not_run 1 "no console image was built"
fi
if [ -n "${IMAGE_ID[admin]}" ]; then
  # An image that builds and cannot serve is a green gate and a broken deploy. The admin console
  # needs no credentials to boot - IAP is its gate, and it holds no session of its own.
  _acid="$(docker run --rm -d --label "amp-release=${STAMP}" -P "${IMAGE_TAG[admin]}" 2>/dev/null || true)"
  if [ -n "${_acid}" ]; then
    _aport="$(docker port "${_acid}" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    _aok=1
    for _try in 1 2 3 4 5 6 7 8 9 10; do
      curl -fsS --max-time 5 "http://127.0.0.1:${_aport}/api/internal/health" >/dev/null 2>&1 \
        && { _aok=0; break; }
      sleep 2
    done
    docker rm -f -v "${_acid}" >/dev/null 2>&1 || true
    if [ "${_aok}" = "0" ]; then
      record "admin image: serves its health route" passed 1
    else
      record "admin image: serves its health route" failed 1 \
        "the container started but never answered /api/internal/health"
    fi
  else
    record "admin image: serves its health route" failed 1 "the container did not start"
  fi
else
  record "admin image: serves its health route" not_run 1 "no admin image was built"
fi

# 3b. the UI, in a real browser
# The page is an ARTIFACT too: it is copied into the app image and served from it. An image that
# imports cleanly can still serve a UI whose entry module does not parse, and every check above
# this line would pass. So the built image serves the page to a REAL browser, over a published
# port and with no bind mount - the same tests Gate B runs against the checkout, pointed at the
# artifact instead. A missing browser is a FAILED check, never a skip.
stage "UI: vendored assets, then the built image serving the page to a real browser"
# The vendored browser assets are shipped bytes like any other. `sha256sum -c` runs INSIDE the
# image against ui/vendor/SHA256SUMS, so this proves the artifact carries the pinned bundle -
# not that the checkout does. A vendored blob that changed without its pin (or its provenance
# note) being updated fails the release here.
if [ -z "${IMAGE_ID[app]}" ]; then
  record "vendored browser assets match their pinned checksums (in the app image)" not_run 1 \
    "no app image was built"
elif VEND="$(docker run --rm --label "amp-release=${STAMP}" --network none \
      --entrypoint sh "${IMAGE_TAG[app]}" -c \
      'cd /srv/ui/vendor && sha256sum -c SHA256SUMS' 2>&1)"; then
  record "vendored browser assets match their pinned checksums (in the app image)" passed 1 \
    "$(echo "${VEND}" | tr '\n' ' ')"
else
  record "vendored browser assets match their pinned checksums (in the app image)" failed 1 \
    "$(echo "${VEND}" | tail -3 | tr '\n' ' ')"
  KEEP_WORK=1
fi

# The SAME vendored bundle - byte-identical vtk.js and SHA256SUMS - is ALSO copied into the
# console image (Dockerfile's console stage: apps/console/public/static/vendor/), and nothing
# checked it there. Same shape as the app-image check above: `sha256sum -c` runs INSIDE the
# console image against its own copy, so this proves the artifact the console actually ships
# carries the pinned bundle - not that the checkout, or the unrelated app image, does.
if [ -z "${IMAGE_ID[console]}" ]; then
  record "vendored browser assets match their pinned checksums (in the console image)" not_run 1 \
    "no console image was built"
elif CVEND="$(docker run --rm --label "amp-release=${STAMP}" --network none \
      --entrypoint sh "${IMAGE_TAG[console]}" -c \
      'cd /srv/apps/console/public/static/vendor && sha256sum -c SHA256SUMS' 2>&1)"; then
  record "vendored browser assets match their pinned checksums (in the console image)" passed 1 \
    "$(echo "${CVEND}" | tr '\n' ' ')"
else
  record "vendored browser assets match their pinned checksums (in the console image)" failed 1 \
    "$(echo "${CVEND}" | tail -3 | tr '\n' ' ')"
  KEEP_WORK=1
fi

UI_C="amp-rel-${STAMP}-ui"
UI_BROWSER_C="amp-rel-${STAMP}-browser"
# Pinned by digest-bearing tag, like every other service image here. Test-only: nothing in the
# product depends on it, and it is never built into an artifact.
UI_BROWSER_IMAGE="zenika/alpine-chrome:124"
UI_CDP=""
UI_BROWSER_NOTE=""
# Set ONLY by the published-port fallback below: the address the browser container must use to
# reach the UI container, because 127.0.0.1 inside its own namespace is not this host.
UI_BROWSER_HOST=""

_resolve_browser() {
  # A browser on PATH is not a browser that RUNS. This host ships Chrome 148, which dies with
  # SIGTRAP on about:blank - no product code loaded, nothing about the release to report. The
  # old preflight asked `which` and so turned a broken workstation into a failed release.
  #
  # So: try the host's own browser by actually starting it, and fall back to a pinned browser
  # container when it cannot. The SMOKE never changes - same suite, same CDP protocol, same
  # assertions - only whose process the browser is. Still never a skip: if neither can be had,
  # this returns non-zero and the check is recorded FAILED.
  # The probe reports through an explicit sentinel, never through an empty string: a probe that
  # itself crashed prints nothing, and "nothing" must not be readable as "the browser is fine".
  # Stderr is kept for the same reason - a silent failure here would route around the check.
  local probe why
  probe="$("${GATE_PY}" - <<'PY' 2>&1
import sys
sys.path.insert(0, "tests/ui")
from _chrome import find_chrome, usable_chrome
found = find_chrome()
why = "no Chrome/Chromium on PATH" if found is None else usable_chrome(found)
print("BROWSER_OK" if not why else f"BROWSER_BAD {why}")
PY
)"
  case "${probe}" in
    *BROWSER_OK*)  UI_BROWSER_NOTE="host browser"; return 0 ;;
    *BROWSER_BAD*) why="${probe#*BROWSER_BAD }" ;;
    *)             why="the browser preflight did not report: ${probe}" ;;
  esac

  local port
  port="$("${GATE_PY}" -c \
    "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")"
  docker rm -f -v "${UI_BROWSER_C}" >/dev/null 2>&1
  # --network host so the browser reaches the app container on the SAME published loopback port
  # the rest of this check uses, and so its DevTools endpoint needs no port publishing of its own.
  # Labelled, so the EXIT trap removes it on success and on failure alike.
  if ! docker run -d --name "${UI_BROWSER_C}" --label "amp-release=${STAMP}" --network host \
       --entrypoint chromium-browser "${UI_BROWSER_IMAGE}" \
       --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
       --enable-unsafe-swiftshader --remote-debugging-port="${port}" about:blank \
       >/dev/null 2>&1; then
    UI_BROWSER_NOTE="host browser unusable (${why}) and ${UI_BROWSER_IMAGE} would not start"
    return 1
  fi
  local i
  for i in $(seq 1 45); do
    if curl -sf "http://127.0.0.1:${port}/json/version" >/dev/null 2>&1; then
      UI_CDP="http://127.0.0.1:${port}"
      UI_BROWSER_NOTE="pinned ${UI_BROWSER_IMAGE} (host browser unusable: ${why})"
      return 0
    fi
    sleep 1
  done
  # Docker Desktop (WSL2) ships with host networking OFF: `--network host` then binds the
  # DevTools port inside the engine VM, never on this loopback, so the poll above never sees it.
  # Fallback: publish the port instead. That needs OLD headless - new headless ignores
  # --remote-debugging-address and listens only on the container's own 127.0.0.1, which a
  # published port cannot reach. And with no shared namespace the browser cannot use this
  # host's loopback to reach the UI container either, so the UI is published on this host's
  # address and the browser is pointed there (UI_BROWSER_HOST, consumed below). Same suite,
  # same CDP protocol, same assertions - only the plumbing between the two containers differs.
  docker rm -f -v "${UI_BROWSER_C}" >/dev/null 2>&1
  local host_addr
  host_addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -n "${host_addr}" ] && docker run -d --name "${UI_BROWSER_C}" --label "amp-release=${STAMP}" \
       -p "127.0.0.1:${port}:${port}" \
       --entrypoint chromium-browser "${UI_BROWSER_IMAGE}" \
       --headless=old --no-sandbox --disable-gpu --disable-dev-shm-usage \
       --enable-unsafe-swiftshader --remote-debugging-address=0.0.0.0 \
       --remote-debugging-port="${port}" about:blank \
       >/dev/null 2>&1; then
    for i in $(seq 1 45); do
      if curl -sf "http://127.0.0.1:${port}/json/version" >/dev/null 2>&1; then
        UI_CDP="http://127.0.0.1:${port}"
        UI_BROWSER_HOST="${host_addr}"
        UI_BROWSER_NOTE="pinned ${UI_BROWSER_IMAGE}, published DevTools port, UI reached at ${host_addr} (host browser unusable: ${why}; docker host networking not in effect)"
        return 0
      fi
      sleep 1
    done
  fi
  UI_BROWSER_NOTE="host browser unusable (${why}); ${UI_BROWSER_IMAGE} never opened a DevTools port (neither --network host nor a published port)"
  return 1
}

if [ -z "${IMAGE_ID[app]}" ]; then
  record "UI behaviour + real-browser smoke (from the app image)" not_run 1 "no app image was built"
elif ! _resolve_browser; then
  record "UI behaviour + real-browser smoke (from the app image)" failed 1 \
    "no usable browser: ${UI_BROWSER_NOTE}"
  KEEP_WORK=1
else
  UI_PORT="$("${GATE_PY}" -c \
    "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")"
  docker rm -f -v "${UI_C}" >/dev/null 2>&1
  # Loopback only, unless the browser runs in its own network namespace (fallback above) and
  # must reach the UI on this host's address. Placeholder credentials either way, for the
  # seconds this container lives.
  if [ -n "${UI_BROWSER_HOST}" ]; then UI_PUBLISH="${UI_PORT}:8000"; else UI_PUBLISH="127.0.0.1:${UI_PORT}:8000"; fi
  # No -v anywhere: the page under test is the one baked into the image.
  docker run -d --name "${UI_C}" --label "amp-release=${STAMP}" \
    -p "${UI_PUBLISH}" \
    -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x \
    --entrypoint python "${IMAGE_TAG[app]}" \
    -m uvicorn meshpipeline.runtime.api_server:app --host 0.0.0.0 --port 8000 \
    >/dev/null 2>&1
  UI_UP=0
  for _ in $(seq 1 45); do
    if curl -sf "http://127.0.0.1:${UI_PORT}/ui" >/dev/null 2>&1; then UI_UP=1; break; fi
    sleep 1
  done
  UI_MOUNTS="$(docker inspect "${UI_C}" --format '{{json .Mounts}}' 2>/dev/null)"
  UI_BINDS="$(docker inspect "${UI_C}" --format '{{json .HostConfig.Binds}}' 2>/dev/null)"
  if [ "${UI_UP}" -ne 1 ]; then
    docker logs --tail 30 "${UI_C}" >"${WORK}/ui-image.log" 2>&1
    record "UI behaviour + real-browser smoke (from the app image)" failed 1 \
      "the image never served /ui - see ${WORK}/ui-image.log"
    KEEP_WORK=1
  elif [ "${UI_MOUNTS}" != "[]" ] || { [ "${UI_BINDS}" != "null" ] && [ "${UI_BINDS}" != "[]" ]; }; then
    record "UI behaviour + real-browser smoke (from the app image)" failed 1 \
      "the UI container has mounts: Mounts=${UI_MOUNTS} Binds=${UI_BINDS}"
    KEEP_WORK=1
  # The cross-boundary check compares what the backend publishes with what the browser can
  # render. Both halves must come from the ARTIFACT, so the event vocabulary is read out of the
  # image rather than out of the checkout's installed package. -i attaches stdin; without it
  # `python -` gets EOF, runs nothing and exits 0.
  elif ! UI_VOCAB="$(docker run --rm -i --label "amp-release=${STAMP}" --network none \
        -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x \
        --entrypoint python "${IMAGE_TAG[app]}" - 2>/dev/null <<'PY'
import json
import meshpipeline.events as E
print(json.dumps([list(E.EVENT_TYPES), list(E.STAGES)]))
PY
      )" || [ -z "${UI_VOCAB}" ] || [ "${UI_VOCAB}" = "[[], []]" ]; then
    record "UI behaviour + real-browser smoke (from the app image)" failed 1 \
      "could not read the event vocabulary from the image - the cross-boundary check would \
have compared the artifact's browser against the checkout's backend"
    KEEP_WORK=1
    docker rm -f -v "${UI_C}" >/dev/null 2>&1
  elif UI_BROWSER_BASE_URL="http://${UI_BROWSER_HOST:-127.0.0.1}:${UI_PORT}" UI_EVENT_VOCABULARY="${UI_VOCAB}" \
       CHROME_CDP_URL="${UI_CDP}" \
       "${GATE_PY}" -m pytest -q -rs -p no:cacheprovider tests/ui \
         >"${WORK}/ui-browser.log" 2>&1; then
    record "UI behaviour + real-browser smoke (from the app image)" passed 1 \
      "$(tail -1 "${WORK}/ui-browser.log")  [Mounts=[] Binds=null] [${UI_BROWSER_NOTE}]"
  else
    record "UI behaviour + real-browser smoke (from the app image)" failed 1 \
      "$(tail -3 "${WORK}/ui-browser.log" | tr '\n' ' ')"
    KEEP_WORK=1
  fi
  docker rm -f -v "${UI_C}" >/dev/null 2>&1
fi

# native tiers, zero bind mounts
NATIVE_EVIDENCE="${WORK}/native-terminal-evidence"
TIER_SUMMARY=""; TIER_SECONDS=0
if [ "${RELEASE_ALLOW_INCOMPLETE:-0}" = "1" ]; then
  stage "native tiers"
  warnp "RELEASE_ALLOW_INCOMPLETE=1 - the native tiers are being skipped"
  note "this yields an INCOMPLETE verdict, which is never promotable"
  for t in smoke all terminal; do record "native ${t}" skipped 1 "RELEASE_ALLOW_INCOMPLETE=1"; done
elif [ -z "${IMAGE_ID[mesh]}" ]; then
  for t in smoke all terminal; do record "native ${t}" not_run 1 "no mesh image to run in"; done
else
  stage "native tiers inside the image, with ZERO bind mounts"
  FIX="${WORK}/fixtures"
  git worktree add --detach "${FIX}" "${COMMIT}" >/dev/null 2>&1
  chmod -R a+rX "${FIX}/tests" "${FIX}/pyproject.toml"
  mkdir -p "${WORK}/evid" && chmod 777 "${WORK}/evid"

  # docker create -> docker cp committed fixtures -> docker start -> docker cp evidence
  # -> docker inspect -> docker rm.  Never a host bind mount: a mounted src/ would shadow the
  # installed package and the tier would silently be testing the checkout.
  _tier() {  # _tier <name> <docker create args...> -- <python argv...>
    local name="$1"; shift
    local -a create_args=() run_args=()
    while [ "$1" != "--" ]; do create_args+=("$1"); shift; done
    shift; run_args=("$@")
    local C="amp-rel-${STAMP}-${name}"
    docker rm -f -v "${C}" >/dev/null 2>&1
    docker create --name "${C}" --label "amp-release=${STAMP}" \
      -e OMPI_ALLOW_RUN_AS_ROOT=1 -e OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
      "${create_args[@]}" --entrypoint python "${IMAGE_TAG[mesh]}" "${run_args[@]}" >/dev/null
    docker cp "${FIX}/tests" "${C}:/srv/tests"
    docker cp "${FIX}/pyproject.toml" "${C}:/srv/pyproject.toml"
    if [ "${name}" = "terminal" ]; then docker cp "${WORK}/evid" "${C}:/evid"; fi
    local t0 t1; t0=$(date +%s)
    docker start -a "${C}" >"${WORK}/native-${name}.log" 2>&1
    local rc=$?; t1=$(date +%s)
    TIER_SECONDS=$((t1 - t0))
    if [ "${name}" = "terminal" ]; then
      rm -rf "${NATIVE_EVIDENCE}"
      docker cp "${C}:/evid" "${NATIVE_EVIDENCE}" >/dev/null 2>&1
    fi
    local mounts binds
    mounts="$(docker inspect "${C}" --format '{{json .Mounts}}')"
    binds="$(docker inspect "${C}" --format '{{json .HostConfig.Binds}}')"
    TIER_IMAGE="$(docker inspect "${C}" --format '{{.Image}}')"
    docker inspect "${C}" >"${WORK}/native-${name}.inspect.json"
    TIER_SUMMARY="$(tail -1 "${WORK}/native-${name}.log")"
    docker rm -v "${C}" >/dev/null
    if [ "${mounts}" != "[]" ] || { [ "${binds}" != "null" ] && [ "${binds}" != "[]" ]; }; then
      record "native ${name}: zero bind mounts" failed 1 "Mounts=${mounts} Binds=${binds}"
      return 1
    fi
    return ${rc}
  }

  _PYTEST=(-m pytest -q -rs -p no:cacheprovider -c /srv/pyproject.toml
           -o testpaths=/srv/tests/native -o cache_dir=/tmp/pytest_cache)

  if _tier smoke --network none -- "${_PYTEST[@]}" -m native_smoke /srv/tests/native; then
    record "native smoke" passed 1 "${TIER_SUMMARY}  [${TIER_SECONDS}s, Mounts=[] Binds=null]"
  else record "native smoke" failed 1 "${TIER_SUMMARY}"; KEEP_WORK=1; fi

  if _tier all --network none -- "${_PYTEST[@]}" -m "not native_terminal" /srv/tests/native; then
    record "native all" passed 1 "${TIER_SUMMARY}  [${TIER_SECONDS}s, Mounts=[] Binds=null]"
  else record "native all" failed 1 "${TIER_SUMMARY}"; KEEP_WORK=1; fi

  stage "native terminal: isolated release-validation services, then the five-engine matrix"
  PG="amp-rel-${STAMP}-pg"; RD="amp-rel-${STAMP}-redis"; MN="amp-rel-${STAMP}-minio"
  # The native tier clears application tables between engines, so it refuses to run unless the
  # database it is pointed at was provisioned FOR this run and can be proven disposable: the run
  # id names the database. Without both, the tier stops rather than truncate a database somebody
  # else owns.
  TERMINAL_RUN_ID="$(printf '%s' "${STAMP}" | tr -cd 'a-z0-9')"
  TERMINAL_DB="meshtest_${TERMINAL_RUN_ID}"
  MINIO_IMAGE=quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z
  docker network create --label "amp-release=${STAMP}" "${NET}" >/dev/null 2>&1
  docker run -d --name "${RD}" --label "amp-release=${STAMP}" --network "${NET}" \
    redis:7-alpine >/dev/null 2>&1
  docker run -d --name "${PG}" --label "amp-release=${STAMP}" --network "${NET}" \
    -e POSTGRES_USER=mesh -e POSTGRES_PASSWORD=x -e POSTGRES_DB=mesh \
    postgres:16-alpine >/dev/null 2>&1
  docker run -d --name "${MN}" --label "amp-release=${STAMP}" --network "${NET}" \
    -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
    "${MINIO_IMAGE}" server /data >/dev/null 2>&1

  _wait() {  # _wait <label> <container> <seconds> <probe command...>
    local label="$1" container="$2" limit="$3"; shift 3
    local i=0
    while [ "${i}" -lt "${limit}" ]; do
      if "$@" >/dev/null 2>&1; then note "${label} healthy after ${i}s"; return 0; fi
      i=$((i + 1)); sleep 1
    done
    bad "${label} did not become healthy within ${limit}s - last 20 log lines:"
    docker logs --tail 20 "${container}" 2>&1 | sed 's/^/       /' || true
    return 1
  }
  HEALTHY=1
  _wait "postgres" "${PG}" 45 docker exec "${PG}" pg_isready -U mesh || HEALTHY=0
  # pg_isready reports the image's TEMPORARY initdb server too, which shuts down and restarts a
  # moment later; a CREATE DATABASE issued on that answer met a server going down and the gate
  # ended 'incomplete' with every native tier green. Wait until the real server answers a query.
  if [ "${HEALTHY}" = "1" ]; then
    _wait "postgres (answering queries)" "${PG}" 45 \
      docker exec "${PG}" psql -U mesh -d mesh -v ON_ERROR_STOP=1 -q -c "SELECT 1" || HEALTHY=0
  fi
  # Stamp the task database the way tests/disposable_database.py provisions one: its own schema
  # (the suite drops `public`, which would take a marker there with it) recording the run that
  # owns it. Without this the tier cannot prove the database is disposable and refuses to touch it.
  if [ "${HEALTHY}" = "1" ]; then
    docker exec "${PG}" psql -U mesh -d mesh -v ON_ERROR_STOP=1 -q \
      -c "CREATE DATABASE \"${TERMINAL_DB}\"" >/dev/null 2>&1 || HEALTHY=0
    docker exec "${PG}" psql -U mesh -d "${TERMINAL_DB}" -v ON_ERROR_STOP=1 -q \
      -c "CREATE SCHEMA _mesh_disposable" \
      -c "CREATE TABLE _mesh_disposable.run (run_id text PRIMARY KEY, database_name text NOT NULL,
           system_identifier text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())" \
      -c "INSERT INTO _mesh_disposable.run (run_id, database_name, system_identifier)
           SELECT '${TERMINAL_RUN_ID}', current_database(),
                  (SELECT system_identifier::text FROM pg_control_system())" \
      >/dev/null 2>&1 || HEALTHY=0
    [ "${HEALTHY}" = "1" ] || bad "could not provision the disposable task database"
  fi
  _wait "redis" "${RD}" 30 docker run --rm --label "amp-release=${STAMP}" --network "${NET}" \
      redis:7-alpine redis-cli -h "${RD}" ping || HEALTHY=0
  _wait "minio" "${MN}" 45 docker run --rm --label "amp-release=${STAMP}" --network "${NET}" \
      --entrypoint curl curlimages/curl:8.11.1 -sf "http://${MN}:9000/minio/health/ready" \
      || HEALTHY=0

  if [ "${HEALTHY}" -ne 1 ]; then
    record "native terminal" not_run 1 "release-validation services did not become healthy"
    record "native terminal coverage" not_run 1 "the tier never ran"
    record "native terminal evidence" not_run 1 "the tier never ran"
    KEEP_WORK=1
  else
    note "services: ${PG} / ${RD} / ${MN} on ${NET}"
    if _tier terminal \
        --network "${NET}" \
        -e PIPELINE_BACKEND=deferred \
        -e POSTGRES_HOST="${PG}" -e POSTGRES_USER=mesh -e POSTGRES_PASSWORD=x \
        -e POSTGRES_DB="${TERMINAL_DB}" -e MESH_TEST_RUN_ID="${TERMINAL_RUN_ID}" \
        -e REDIS_URL="redis://${RD}:6379/0" \
        -e MINIO_ENDPOINT="${MN}:9000" -e MINIO_ACCESS_KEY=minioadmin \
        -e MINIO_SECRET_KEY=minioadmin -e MINIO_BUCKET="amp-rel-jobs" \
        -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x \
        -e FM_TERMINAL_EVIDENCE_DIR=/evid \
        -- -m pytest -rA -p no:cacheprovider -c /srv/pyproject.toml \
           -o testpaths=/srv/tests/native -o cache_dir=/tmp/pytest_cache \
           -m native_terminal /srv/tests/native
    then
      record "native terminal" passed 1 \
        "${TIER_SUMMARY}  [${TIER_SECONDS}s, image ${TIER_IMAGE:0:19}, Mounts=[] Binds=null]"
    else
      record "native terminal" failed 1 "${TIER_SUMMARY}"
      KEEP_WORK=1
    fi

    # Coverage is verified by NODE ID and marker membership, not by a hardcoded count, so a
    # legitimate future native_terminal test is accepted rather than failing the gate.
    if COV="$("${GATE_PY}" - "${WORK}/native-terminal.log" <<'PY' 2>&1
import pathlib, re, sys
log = pathlib.Path(sys.argv[1]).read_text()
passed = set(re.findall(r"^PASSED (\S+)", log, re.M))
broken = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", log, re.M))
assert not broken, f"failing native_terminal nodes: {sorted(broken)}"
assert passed, "no PASSED node ids in the terminal log - the tier did not report"
engines = ("cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk")
missing = [e for e in engines
           if not any("test_native_to_terminal_agreement" in n and n.endswith(f"[{e}]")
                      for n in passed)]
assert not missing, f"engines without a passing terminal agreement test: {missing}"
scenarios = {
    "restart/replay": "test_restart_replay_no_repeat",
    "gate-failure containment": "test_native_gate_failure_is_terminal_failed",
    "delivery failure": "test_native_success_but_artifact_delivery_failure",
    "stale-worker CAS": "test_rejected_terminal_cas_stale_worker_cannot_publish_success",
}
absent = [k for k, frag in scenarios.items() if not any(frag in n for n in passed)]
assert not absent, f"scenarios not covered: {absent}"
print(f"{len(passed)} nodes passed; all 5 engines and {len(scenarios)} scenarios covered")
PY
)"; then record "native terminal coverage (5 engines + 4 scenarios)" passed 1 "${COV}"
    else record "native terminal coverage (5 engines + 4 scenarios)" failed 1 "${COV}"; KEEP_WORK=1; fi

    # The expected final_result schema, READ from the one authority that declares it rather than
    # restated as a literal here. A hardcoded number silently goes stale the moment the product
    # bumps the schema, and then fails the release for having shipped the version it was supposed
    # to ship - which is exactly what a literal 4 did after the product moved to 5.
    FR_SCHEMA="$(sed -n 's/^FINAL_RESULT_SCHEMA_VERSION[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
        "${REPO_ROOT}/src/meshpipeline/application/final_result.py" | head -1)"
    if EV="$("${GATE_PY}" - "${NATIVE_EVIDENCE}" "${FR_SCHEMA}" <<'PY' 2>&1
import hashlib, json, pathlib, sys
d = pathlib.Path(sys.argv[1])
expected_schema = int(sys.argv[2])
assert d.is_dir(), f"no evidence directory was retrieved from the container: {d}"
files = sorted(d.glob("*.json"))
assert files, "the terminal tier produced no evidence artifacts"
terminals = [f for f in files if f.name.startswith("terminal_")]
assert len(terminals) >= 5, f"expected one artifact per engine, got {[f.name for f in terminals]}"
jobs, rows = set(), []
for f in terminals:
    r = json.loads(f.read_text())
    fr = r["final_result"]
    assert fr["schema_version"] == expected_schema, (
        f"{f.name}: final_result schema_version {fr['schema_version']}, "
        f"expected {expected_schema}")
    assert fr["status"] == "succeeded", f"{f.name}: status {fr['status']}"
    assert r["native_marker_sha256"], f"{f.name}: no native marker - the binary did not run"
    jobs.add(r["job_id"])
    rows.append(f"{f.name}[{f.stat().st_size}B "
                f"sha256:{hashlib.sha256(f.read_bytes()).hexdigest()[:12]}]")
assert len(jobs) == len(terminals), "job ids repeated - evidence was reused, not regenerated"
print(f"{len(files)} artifacts, {len(jobs)} fresh job ids, "
      f"final_result schema {expected_schema} :: " + " ".join(rows))
PY
)"; then record "native terminal evidence (fresh, declared schema, native marker)" passed 1 "${EV}"
    else record "native terminal evidence (fresh, declared schema, native marker)" failed 1 "${EV}"; KEEP_WORK=1; fi
  fi
fi

# the release record
# Every model this release would route to must have a CONFIRMED price. Pricing is measured
# resource cost, so a model absent from the table meters at $0.00 and the run it serves is billed
# short - silently, and worst for the reviewer, which is the image-heavy role. A guessed number
# would be fabricated cost evidence, so the gap is a release blocker rather than something to
# paper over: development proceeds with it (the unit suite reports it as an expected failure),
# shipping does not.
stage "pricing: every configured route model has a confirmed price"
UNPRICED="$("${GATE_PY}" -c 'from meshpipeline.adapters.inference_telemetry.pricing import unpriced_route_models; m = unpriced_route_models(); print(", ".join(m) if m else "")' 2>&1)"
_price_rc=$?
if [ "${_price_rc}" -ne 0 ]; then
  record "every configured route model has a confirmed price" failed 1 "could not read the price table: ${UNPRICED}"
elif [ -z "${UNPRICED}" ]; then
  record "every configured route model has a confirmed price" passed 1 "no configured model meters at 0.00"
else
  # REPORTED, NOT BLOCKING - deliberately, and this is the whole of the reasoning. Metered
  # billing is not switched on yet, so an unpriced model understates a figure nobody is charging
  # against. Holding a release for it trades a dated, real need - a working public deployment -
  # against a cost report nobody reads yet. The moment metered billing ships this must go back to
  # `failed 1`, because from then on an unpriced model is money.
  record "every configured route model has a confirmed price" skipped 0 "unpriced, metering at 0.00: ${UNPRICED} - DEFERRED by decision, not resolved. Restore to required before metered billing ships"
fi

stage "release record"
# Read from the IMAGE - the artifact that gets promoted - not from the checkout and not from the
# bare wheel (which declares no runtime pins by design: requirements/runtime.txt is the single
# pinned environment). -i attaches stdin; without it `python -` gets EOF, runs nothing, exits 0.
ARTIFACT_FACTS="{}"
if [ -n "${IMAGE_ID[app]}" ]; then
  ARTIFACT_FACTS="$(docker run --rm -i --label "amp-release=${STAMP}" --network none \
      -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x \
      --entrypoint python "${IMAGE_TAG[app]}" - 2>/dev/null <<'PY' || echo '{}'
import json, meshpipeline
from meshpipeline.application.final_result import FINAL_RESULT_SCHEMA_VERSION
from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
print(json.dumps({"product_version": meshpipeline.__version__,
                  "final_result": FINAL_RESULT_SCHEMA_VERSION,
                  "pipeline_state": STATE_SCHEMA_VERSION}))
PY
)"
fi
[ -n "${ARTIFACT_FACTS}" ] || ARTIFACT_FACTS="{}"
if [ "${ARTIFACT_FACTS}" = "{}" ]; then
  record "artifact reports its version and schema versions" failed 1 \
    "could not read them from the image - the record cannot name what shipped"
else
  record "artifact reports its version and schema versions" passed 1 "${ARTIFACT_FACTS}"
fi

mkdir -p "${OUT_DIR}"
REC="${OUT_DIR}/release.json"
COMPONENTS="$(python3 - "${TARGET[app]}" "${IMAGE_TAG[app]:-}" "${IMAGE_ID[app]:-}" \
                        "${TARGET[mesh]}" "${IMAGE_TAG[mesh]:-}" "${IMAGE_ID[mesh]:-}" \
                        "${TARGET[console]}" "${IMAGE_TAG[console]:-}" "${IMAGE_ID[console]:-}" \
                        "${TARGET[admin]}" "${IMAGE_TAG[admin]:-}" "${IMAGE_ID[admin]:-}" <<'PY'
import json, sys
at, atag, aid, mt, mtag, mid, ct, ctag, cid, dt, dtag, did = sys.argv[1:13]
print(json.dumps({
  "app":     {"dockerfile_target": at, "local_tag": atag, "local_image_id": aid,
              "workloads": ["api-service", "pipeline-job"]},
  "mesh":    {"dockerfile_target": mt, "local_tag": mtag, "local_image_id": mid,
              "workloads": ["mesh-job"]},
  "console": {"dockerfile_target": ct, "local_tag": ctag, "local_image_id": cid,
              "workloads": ["console-service"]},
  "admin":   {"dockerfile_target": dt, "local_tag": dtag, "local_image_id": did,
              "workloads": ["admin-service"]},
}))
PY
)"
"${GATE_PY}" "${REPO_ROOT}/devtools/release/record.py" write \
  --out "${REC}" --schema "${RECORD_SCHEMA}" --results "${RESULTS}" \
  --commit "${COMMIT}" --tree "${TREE}" --branch "${BRANCH}" \
  --wheel-name "${WHEEL_NAME}" --wheel-sha256 "${WHEEL_SHA}" --wheel-bytes "${WHEEL_BYTES}" \
  --image-tag "${SHORT}" --components "${COMPONENTS}" --artifact-facts "${ARTIFACT_FACTS}"

VERDICT="$("${GATE_PY}" "${REPO_ROOT}/devtools/release/record.py" verdict --record "${REC}" 2>/dev/null)"
printf '\n%s' "${c_bold}"
case "${VERDICT}" in
  passed)
    printf 'GATE C PASSED%s - artifacts validated and recorded in deploy/output/release.json\n' \
      "${c_off}"
    printf '   the record is state=validated and NOT yet promotable: nothing has been published,\n'
    printf '   so there is no immutable registry digest to deploy.\n'
    printf '   next: %smake release-publish%s\n\n' "${c_bold}" "${c_off}"
    exit 0 ;;
  incomplete)
    printf '%sGATE C INCOMPLETE%s - a required check was skipped or never ran\n' "${c_yel}" "${c_off}"
    printf '   this record can never be promoted. See deploy/output/release.json.\n\n'
    exit 1 ;;
  *)
    printf '%sGATE C FAILED%s - which checks failed, and why, is recorded in\n' \
      "${c_red}" "${c_off}"
    printf '   deploy/output/release.json\n\n'
    KEEP_WORK=1
    exit 1 ;;
esac
