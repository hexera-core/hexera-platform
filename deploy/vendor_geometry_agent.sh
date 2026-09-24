#!/usr/bin/env bash
# Responsibility: Build the geometry-agent distribution from an agent checkout and vendor the wheel this
#                 repository's images install, recording which agent commit it came from.
# Owns: vendor/wheels/ and vendor/wheels/PROVENANCE.json - the wheel and its provenance, nothing else.
# Boundaries: it writes into this repository only. The agent checkout is READ; it is never modified.
#
# WHY A VENDORED WHEEL AND NOT SOMETHING ELSE. The image build context is this repository (Dockerfile:
# `context: .`), and the agent's source lives in a SEPARATE repository that the context cannot reach. The
# three ways out, and why this is the one:
#
#   a path install (`pip install ../geometry_agent`)  a COPY cannot leave the build context, so this cannot
#           be expressed in the Dockerfile at all. It works only outside Docker, which is where the defect
#           came from: the developer venv had the package on PYTHONPATH and the image never did.
#   `pip install git+ssh://...`  needs a credential inside the build and a network reachable from it. The
#           Dockerfile's own boundary is "self-contained - no private registry and no prebuilt base", and
#           `make release-validate` builds the deployable images from a checkout with no registry login.
#   a vendored SOURCE tree  copies 106 modules of another repository into this one, where they would be
#           edited in place and diverge with nothing to compare against.
#   a vendored BUILT WHEEL, this script  one artefact, one recorded commit, no network and no credential in
#           the build, and the same `pip install --no-deps` line the meshpipeline wheel already uses. It can
#           go stale, which is exactly what happened to the abandoned dist/hexera-geometry-agent.tar.gz - so
#           staleness is what PROVENANCE.json and tests/unit/deploy/test_geometry_agent_distribution.py are
#           for: the wheel's own version carries the agent commit, and the test refuses a wheel that is
#           missing a module this repository imports.
#
# TWO THINGS THIS SCRIPT ADDS TO THE AGENT'S OWN PYPROJECT, both of them recorded in PROVENANCE.json:
#
#   1. package-data. The agent's pyproject declares none, so a wheel built from it as-is ships .py files
#      only and leaves behind agent/thresholds.json (the rule parameters in force),
#      agent/identity_tests.json (read with .read_text(), so its absence is a FileNotFoundError the first
#      time a plan is built) and learn/rules.json. This belongs in the agent's own pyproject; until it is
#      there, it is applied here rather than shipping a package that cannot read its own tables.
#   2. a local version segment, `+g<short sha>`. The agent's version is a static 0.1.0, so two wheels built
#      six months apart are indistinguishable by name or metadata. With the segment,
#      `importlib.metadata.version("hexera-geometry-agent")` inside the image names the commit it was built
#      from, and that is what makes a stale wheel visible from inside a running container.
#
# Usage:  bash deploy/vendor_geometry_agent.sh [path/to/geometry_agent]
#         GEOMETRY_AGENT_REPO=... bash deploy/vendor_geometry_agent.sh
#         ALLOW_DIRTY_AGENT=1 ...   builds from a dirty checkout and records it as dirty (never for a release)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT="${1:-${GEOMETRY_AGENT_REPO:-}}"
PY="${PYTHON:-python}"

die() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

[ -n "${AGENT}" ] || die "no agent checkout given.
       bash deploy/vendor_geometry_agent.sh /path/to/geometry_agent
       or set GEOMETRY_AGENT_REPO."
AGENT="$(cd "${AGENT}" && pwd)" || die "cannot enter ${AGENT}"
[ -f "${AGENT}/pyproject.toml" ] || die "${AGENT} has no pyproject.toml - that is not the agent repository"
grep -q '^name = "hexera-geometry-agent"' "${AGENT}/pyproject.toml" \
  || die "${AGENT}/pyproject.toml does not declare hexera-geometry-agent"

# The recorded commit is the whole point of the provenance file, so a checkout that cannot name one, or
# whose working tree does not match the one it names, is refused rather than recorded as if it did.
SHA="$(git -C "${AGENT}" rev-parse HEAD 2>/dev/null)" || die "${AGENT} is not a git checkout - the wheel would have no provenance"
DIRTY=false
if [ -n "$(git -C "${AGENT}" status --porcelain 2>/dev/null)" ]; then
  DIRTY=true
  [ "${ALLOW_DIRTY_AGENT:-0}" = "1" ] || die "the agent checkout at ${AGENT} has uncommitted changes, so '${SHA}'
       would not describe the wheel. Commit them, or set ALLOW_DIRTY_AGENT=1 to record it as dirty."
fi
SHORT="$(git -C "${AGENT}" rev-parse --short=10 HEAD)"
BRANCH="$(git -C "${AGENT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached)"

BUILD="$(mktemp -d)"
trap 'rm -rf "${BUILD}"' EXIT
cp -R "${AGENT}/src" "${BUILD}/src"
cp "${AGENT}/pyproject.toml" "${BUILD}/pyproject.toml"
# A host's compiled cache and a previous in-tree build's metadata are not build inputs.
find "${BUILD}/src" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "${BUILD}"/src/*.egg-info

# The version the wheel is BUILT as: the agent's own, plus the commit. Rewritten in the copy, never in the
# agent checkout.
BASE_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "${BUILD}/pyproject.toml" | head -1)"
[ -n "${BASE_VERSION}" ] || die "could not read the agent's version from its pyproject.toml"
LOCAL="g${SHORT}"
[ "${DIRTY}" = "true" ] && LOCAL="${LOCAL}.dirty"
VERSION="${BASE_VERSION}+${LOCAL}"
"${PY}" - "${BUILD}/pyproject.toml" "${BASE_VERSION}" "${VERSION}" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
needle = f'version = "{old}"'
assert text.count(needle) == 1, f"expected one {needle!r} in {path}, found {text.count(needle)}"
open(path, "w", encoding="utf-8").write(text.replace(needle, f'version = "{new}"'))
PYEOF

# The package data the agent's pyproject does not declare. Named file by file rather than as a blanket
# glob, so a NEW data file the agent starts reading is a build that leaves it out and a test that says so,
# not a silent inclusion nobody reviewed.
DATA_GLOBS='"agent/thresholds.json", "agent/identity_tests.json", "learn/rules.json"'
cat >> "${BUILD}/pyproject.toml" <<EOF

# Added by the platform's deploy/vendor_geometry_agent.sh - see that script's header. Belongs upstream.
[tool.setuptools.package-data]
geometry_agent = [${DATA_GLOBS}]
EOF

OUT="${ROOT}/vendor/wheels"
mkdir -p "${OUT}"
rm -f "${OUT}"/hexera_geometry_agent-*.whl
printf 'building hexera-geometry-agent %s from %s (%s)\n' "${VERSION}" "${AGENT}" "${SHA}"
"${PY}" -m pip wheel --no-deps --no-cache-dir --wheel-dir "${OUT}" "${BUILD}" >/dev/null \
  || die "the wheel build failed - rerun without the output redirect to see pip's own error"

WHEEL="$(ls "${OUT}"/hexera_geometry_agent-*.whl)"
[ -f "${WHEEL}" ] || die "pip reported success but no wheel landed in ${OUT}"

"${PY}" - "${WHEEL}" "${SHA}" "${SHORT}" "${BRANCH}" "${DIRTY}" "${VERSION}" "${DATA_GLOBS}" "${OUT}/PROVENANCE.json" <<'PYEOF'
import hashlib, json, sys, zipfile
from datetime import UTC, datetime
from pathlib import Path

wheel, sha, short, branch, dirty, version, data_globs, out = sys.argv[1:9]
wheel = Path(wheel)
blob = wheel.read_bytes()
names = zipfile.ZipFile(wheel).namelist()
modules = sorted({n.split("/")[1] for n in names
                  if n.startswith("geometry_agent/") and len(n.split("/")) > 2} |
                 {n.split("/")[1].removesuffix(".py") for n in names
                  if n.startswith("geometry_agent/") and n.endswith(".py") and len(n.split("/")) == 2})
data = sorted(n for n in names if n.startswith("geometry_agent/") and not n.endswith(".py"))
doc = {
    "_responsibility": "Which agent commit the vendored wheel was built from, and what it carries.",
    "_boundaries": "Written by deploy/vendor_geometry_agent.sh; never hand-edited. "
                   "tests/unit/deploy/test_geometry_agent_distribution.py checks the wheel against it.",
    "wheel": wheel.name,
    "sha256": hashlib.sha256(blob).hexdigest(),
    "bytes": len(blob),
    "version": version,
    "agent_commit": sha,
    "agent_commit_short": short,
    "agent_branch": branch,
    "agent_checkout_dirty": dirty == "true",
    "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "package_data_added_by_this_script": [g.strip().strip('"') for g in data_globs.split(",")],
    "top_level_modules": modules,
    "data_files": data,
}
Path(out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
print(f"wrote {wheel.name} ({len(blob)} bytes, {len(names)} entries)")
print(f"  version  {version}")
print(f"  commit   {sha}{' DIRTY' if dirty == 'true' else ''}")
print(f"  modules  {', '.join(modules)}")
print(f"  data     {', '.join(data) or 'NONE - the package cannot read its own tables'}")
PYEOF

printf '\nvendored into %s\n' "${OUT#"${ROOT}/"}"
printf 'The image installs it from the Dockerfile wheel stage. Commit both the wheel and PROVENANCE.json.\n'
