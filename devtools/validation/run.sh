#!/usr/bin/env bash
# Responsibility: Run any tier of this repository's test contract in the one environment it is pinned for.
# Owns: the validation image's identity, the read-only checkout mount, the per-tier skip policy and the result record.
# Boundaries: it decides WHERE a tier runs, never what it asserts; the tiers live in tests/ and the Makefile.

# WHY THIS EXISTS. The suite is written to run on a developer host prepared by `make setup`, and
# `make check` picks up .venv automatically. That works until the host has no cp311 interpreter -
# and then `python3 -m pytest` either is not installed at all or resolves a DIFFERENT pinned wheel
# set than the one the product is validated against. Both were true here: the host carried 3.13 and
# no pytest, so a whole audit reported "environmental failure" for thirteen modules that had simply
# never been given a place to run.
#
# The image this uses is the pipeline target plus requirements/dev.txt - the SAME artefact
# production runs, with the toolchain the tests shell out to. The checkout is mounted read-only, so
# a tier cannot mutate the tree it is judging.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="$PWD"

IMAGE="${VALIDATION_IMAGE:-meshpipeline-validation:local}"
# The UI tier is the only one that starts a browser, and the browser is 134MB. It gets its own
# target so the other tiers - and the container integration tier, which rebuilds on every
# source change - do not carry it.
TARGET=validation
TIER="${1:-}"
shift || true

usage() {
  cat >&2 <<'USAGE'
usage: devtools/validation/run.sh <tier> [extra pytest args]

  collect       every tier's tests are importable - fails on ONE collection error
  unit          the hermetic unit tier (needs the docker socket: some tests shell out to it)
  ui            the shipped page in a real headless Chrome
  integration   real Postgres/Redis/MinIO, via tests/integration/run_disposable.sh on the host
  native        the five-engine mesh-image tier, via tests/native/run_tier.sh on the host
  release       Gate C, via devtools/release/validate.sh on the host

  RESULTS_DIR=<path>   where the junit xml and the summary json are written
USAGE
  exit 2
}
[ -n "$TIER" ] || usage

RESULTS_DIR="${RESULTS_DIR:-$REPO/output/validation}"
mkdir -p "$RESULTS_DIR"

# The image is rebuilt only when it does not match this checkout. The stamp is the same working-tree
# digest every other image carries, so "is it current" is one question with one answer everywhere.
DIGEST="$(bash tests/integration/source_digest.sh | tail -1)"
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' src/meshpipeline/__init__.py | head -1)"
# `shift` has already consumed the tier name, so $TIER is the only thing to ask. The `-browser`
# suffix is added only when it is not already there: a caller that names the browser image
# explicitly would otherwise get `...-browser-browser`, and build a second image under it.
if [ "$TIER" = "ui" ]; then
  TARGET=validation-browser
  case "${IMAGE%:*}" in *-browser) ;; *) IMAGE="${IMAGE%:*}-browser:${IMAGE##*:}" ;; esac
fi
stamp="$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.meshpipeline.source"}}' 2>/dev/null || true)"
if [ "$stamp" != "$DIGEST" ]; then
  echo "  building $IMAGE from this checkout ($DIGEST)"
  docker build --target "$TARGET" --build-arg APP_VERSION="$VERSION" \
    --label "org.meshpipeline.source=$DIGEST" -t "$IMAGE" . >/dev/null
fi

# One place that knows how to start a container from it. `--rm` on every path: this runner may not
# leave a container behind, and a tier that fails must not leave one either.
run_in_image() {
  local -a docker_args=()
  if [ "${NEEDS_DOCKER:-0}" = "1" ]; then
    docker_args+=(-v /var/run/docker.sock:/var/run/docker.sock)
  fi
  # AS THE CHECKOUT'S OWN UID, not root. Running root looked harmless and silently changed two
  # answers: git refuses a tree it does not own ("dubious ownership"), which failed every test that
  # asks git what is tracked, and root reads a file with its permission bits cleared, which turned
  # the unreadable-credential contract into a skip. The image's own user is uid 1000, so on an
  # ordinary single-user checkout this is the same identity that owns the files.
  # /src is READ-ONLY and stays that way; in_container.sh copies it to /work and works there, so a
  # tier that mutates a tracked file (three of them do, by design) cannot reach the real checkout.
  docker run --rm --network "${RUN_NETWORK:-bridge}" \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$REPO:/src:ro" -v "$RESULTS_DIR:/out" -w /src \
    -e DEEPSEEK_API_KEY=x -e DEEPINFRA_API_KEY=x -e POSTGRES_PASSWORD=x \
    `# compose refuses to render without it, by design - and the parity test SKIPS when the` \
    `# render fails, so omitting it silently retires the contract on any host, not just this one` \
    -e APP_VERSION="$VERSION" \
    -e PYTEST_ADDOPTS="-p no:cacheprovider ${PYTEST_ADDOPTS:-}" \
    "${docker_args[@]}" "$IMAGE" bash /src/devtools/validation/in_container.sh "$@"
}

# THE EVIDENCE. A summary line is not proof a tier ran: "0 passed" and "no tests ran" both read as
# green to a human skimming. The junit xml carries per-test outcomes, and the check below refuses a
# run that collected nothing or skipped something the tier's policy does not allow.
assert_ran() {
  local xml="$1" tier="$2" allow="${3:-}"
  # THE RECORD MUST BE THIS RUN'S. A junit file left by a previous invocation reads exactly like a
  # fresh one, and when a permissions fault stopped pytest from writing its own, this function
  # happily re-reported the earlier run's numbers as the new run's result. So the file is removed
  # before the tier starts (see `fresh_record`) and its absence here is a hard failure.
  [ -f "$xml" ] || {
    echo "  FATAL: $tier produced no result record at $xml - the tier did not run to completion" >&2
    return 1
  }
  # The decision itself lives in a tracked file, not a heredoc here, so the refusal this runner
  # depends on can be tested directly rather than only by arranging a real empty run.
  python3 "$REPO/devtools/validation/assert_record.py" "$xml" "$tier" "$allow"
}


# Removes any earlier record and proves this run can write a new one. A tier that cannot write its
# evidence must fail BEFORE it spends ten minutes producing results nobody can read.
fresh_record() {
  local xml="$1"
  rm -f "$xml"
  : > "$xml" || { echo "  FATAL: cannot write $xml" >&2; exit 1; }
  rm -f "$xml"
}

case "$TIER" in
  collect)
    # Importability of EVERY tier, including the ones whose services are absent here: a collection
    # error is a broken contract regardless of whether the tier could then run.
    run_in_image -m pytest --collect-only -q tests "$@" | tail -3
    ;;
  unit)
    # The socket: tests/unit/deploy and tests/unit/dev shell out to `docker compose config` and
    # `docker info`. Without it they SKIP - which is how a compose-parity regression reaches a
    # release looking green. Read-only in effect: the only tier member that creates anything is
    # test_gate_c_cleanup_contract, which removes what it creates and is asserted to.
    fresh_record "$RESULTS_DIR/unit.xml"
    NEEDS_DOCKER=1 run_in_image -m pytest -q tests/unit -m "not external_fixture" \
      --junitxml=/out/unit.xml "$@" || true
    assert_ran "$RESULTS_DIR/unit.xml" unit
    ;;
  ui)
    fresh_record "$RESULTS_DIR/ui.xml"
    NEEDS_DOCKER=0 run_in_image -m pytest -q tests/ui --junitxml=/out/ui.xml "$@" || true
    assert_ran "$RESULTS_DIR/ui.xml" ui
    ;;
  integration|native|release)
    # These three drive Docker itself - they provision databases, buckets and images on the host
    # daemon - so they run from the host's own authority rather than from inside a container that
    # would have to be given the socket to re-enter it.
    case "$TIER" in
      integration) bash tests/integration/run_disposable.sh "$@" ;;
      native)      bash tests/native/run_tier.sh "${1:-smoke}" ;;
      release)     bash devtools/release/validate.sh ;;
    esac
    ;;
  *) usage ;;
esac
