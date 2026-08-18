#!/usr/bin/env bash
# Responsibility: Reproduce the environment the suite is written for - a writable checkout with an editable venv.
# Owns: the task workspace, the validation-owned virtualenv, and the editable install of the copied package.
# Boundaries: it prepares and hands off; it chooses no tests and asserts nothing about them.

# WHY A COPY AND A VENV. Three separate contracts in tests/unit pin this shape, and approximating
# it does not fail loudly - it fails as somebody else's bug:
#
#   tests/unit/hygiene/test_package_provenance   the imported package must BE this checkout, so an
#                                                installed wheel silently tests the wrong source
#   tests/unit/security/..._read_boundary        probes write to sys.prefix and its PARENT, which
#                                                on a system interpreter is /usr and /
#   tests/unit/hygiene/test_mypy_baseline_ratchet  and the render-isolation mutation control edit a
#   tests/unit/infra/test_render_dependency_isolation  tracked file and restore it
#
# A read-only mount of the real checkout satisfies none of them, and mounting it writable would let
# a test that dies mid-way leave the developer's tree edited. So the checkout is COPIED, the copy is
# what gets mutated, and the copy dies with the container.
#
# WHY THE VENV IS NOT UNDER THE COPY, AND WHY .venv IS NOT COPIED. `make setup` builds the host
# .venv with the host's interpreter - it prefers python3.12, and this image ships 3.11. The copy
# used to bring that directory along and then create a venv OVER it, which does not repair
# `bin/python -> python3.12`: the symlink stayed dangling, and the tier died before collecting a
# single test. So a repository-local virtualenv is treated as what it is - generated host state,
# never source input - and the environment this runs in is created here, outside the copied tree,
# from the interpreter the image supplies.
set -euo pipefail

# Both default to what run.sh mounts and are overridable ONLY so this boundary can be exercised
# directly - the host-virtualenv matrix in tests/unit/dev drives this script with a fixture checkout
# instead of arranging seven containers. run.sh never sets them.
SRC="${VALIDATION_SRC:-/src}"
# Under /tmp because the container runs as the checkout's uid, and / is root-owned: a workspace at
# /work would need the image to pre-create and chown it, which is one more thing to keep in step.
TASK="${VALIDATION_TASK:-/tmp/validation}"
WORK="$TASK/work"      # the copied working tree - what the tests see as their checkout
VENV="$TASK/venv"      # this run's interpreter home - a sibling of the copy, never inside it

die() { printf '\nVALIDATION BOOTSTRAP FAILED: %s\n' "$1" >&2; exit 1; }

# This script's only recursive delete is of TASK, so TASK is checked structurally before anything
# is removed - an empty, relative or surprising value must stop the run, not delete something else.
# A caller cannot point it at the checkout, a home directory, or the root of a filesystem.
case "$TASK" in
  /tmp/?*) ;;
  *) die "the task workspace must be an absolute path inside /tmp, and not /tmp itself (got '${TASK}')" ;;
esac
case "$TASK" in
  */..* | *//*) die "the task workspace path is not canonical (got '${TASK}')" ;;
esac
SRC_REAL="$(cd "$SRC" 2>/dev/null && pwd -P)" || die "the source checkout ${SRC} does not exist"
[ "$TASK" != "$SRC_REAL" ] || die "the task workspace may not be the checkout itself"
case "$SRC_REAL" in
  "$TASK"/*) die "the task workspace may not contain the checkout" ;;
esac
[ "$TASK" != "${HOME:-/nonexistent}" ] || die "the task workspace may not be the home directory"

cleanup() { rm -rf "$TASK"; }
# Success, test failure, bootstrap failure and interruption all land here, so no run leaves a
# workspace or an environment behind for the next one to inherit.
trap cleanup EXIT INT TERM

rm -rf "$TASK"
mkdir -p "$WORK" || die "could not create the task workspace at ${WORK}"

# tar rather than `cp -a`, only because it can leave a directory out. `.git` COMES WITH IT - the
# hygiene tier asks git what is tracked, and a copy without it fails every one of those tests for a
# reason that has nothing to do with the code. Everything else in the working tree comes too,
# tracked or not: validation judges the tree as it stands, not as it was committed.
#
# What is left out is exactly the supported project virtualenv directories (.gitignore lines 43-45),
# anchored to the repository root so devtools/env/ - which is tracked source - is unaffected.
tar -C "$SRC" -cf - --anchored \
      --exclude=./.venv --exclude=./venv --exclude=./env . \
  | tar -C "$WORK" -xpf - \
  || die "could not copy the checkout into ${WORK}"
cd "$WORK"

[ ! -e "$WORK/.venv" ] || die "a host virtualenv reached the copy at ${WORK}/.venv - it would \
shadow the environment this run creates, which is the failure this boundary exists to prevent"

# --system-site-packages: the pinned wheel set is already installed in the image from
# requirements/runtime.txt + dev.txt. The venv exists to give sys.prefix a writable home with a
# writable parent - which a system interpreter, rooted at /usr, does not have - not to resolve
# dependencies a second time. `python` is the image's own interpreter, and deliberately not a
# version named here: the image decides which Python its wheels were pinned for.
python -m venv --system-site-packages "$VENV" \
  || die "could not create the validation virtualenv at ${VENV} using $(command -v python). No test
    has run. This is the image's own interpreter, so a failure here is the image, not the checkout."
[ -x "$VENV/bin/python" ] \
  || die "${VENV}/bin/python is missing or not executable after creating the virtualenv"

"$VENV/bin/pip" install --quiet --no-deps -e "$WORK" \
  || die "could not install the copied checkout into ${VENV}. No test has run."

# Not `exec`: the trap above has to survive the tier so the workspace is removed however it ends.
# The tier's own exit status is this script's, so a test failure stays a test failure and is never
# reported as a bootstrap failure.
set +e
"$VENV/bin/python" "$@"
status=$?
set -e
exit "$status"
