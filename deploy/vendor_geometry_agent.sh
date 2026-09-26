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
# WHAT THIS SCRIPT ADDS TO THE AGENT'S OWN PYPROJECT, recorded in PROVENANCE.json either way:
#
#   1. package-data, ONLY if the agent has not declared it. A wheel built from an agent that declares none
#      ships .py files only and leaves behind agent/thresholds.json (the rule parameters in force),
#      agent/identity_tests.json (read with .read_text(), so its absence is a FileNotFoundError the first
#      time a plan is built) and learn/rules.json. The agent now declares these three itself, which is where
#      the declaration belongs, so the script adds nothing and records `package_data_declared_by:
#      agent_pyproject`. Against an older agent checkout it still adds them and says so. It cannot do both:
#      a second [tool.setuptools.package-data] table is a duplicate TOML key and fails the build.
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

# The package data the agent must ship. DERIVED FROM THE AGENT'S OWN SOURCE, not from a list kept here.
#
# WHY THE LIST IS GONE (2026-09-26). It used to be three names in this script and the same three in
# tests/unit/deploy/test_geometry_agent_distribution.py, with a subset test between them - two checks, one
# hand-kept list, and therefore one blind spot. By then the agent's pyproject declared FOUR files
# (learn/engine_cell_correction.json was the fourth, the fitted cell-forecast correction that
# hexera.correction_info reads); neither list had heard of it, so nothing checked it, and PROVENANCE.json
# recorded package_data as those three while its own data_files listed four. With the table absent the forecast
# reverts to raw in silence, because correction_info finds no row and falls through to a table that does not
# exist inside the container either.
#
# WHAT IS DERIVED, AND FROM WHAT. Every table the agent reads out of its own installed tree is resolved against
# `__file__` (`Path(__file__).with_name("x.json")`, `.parent / "x"`, `.parents[1] / "learn" / "x"`), because that
# is the only shape that works from inside a wheel - src/geometry_agent/learn/calibration.py carries the
# measurement behind that sentence. So the required set is read off the source, and then:
#
#   the agent declares a table covering all of them   nothing is added, and PROVENANCE.json records the AGENT'S
#                                                     OWN declaration rather than a copy kept here
#   the agent declares a table that misses one        the build is REFUSED. Appending a second
#                                                     [tool.setuptools.package-data] is a duplicate TOML key
#   the agent declares no table at all                this script adds one holding exactly what the source
#                                                     reads, and says so (an older agent checkout)
DATA_INFO="$("${PY}" - "${BUILD}/pyproject.toml" "${BUILD}/src" <<'PYEOF'
import fnmatch, posixpath, re, sys, tomllib
from pathlib import Path

pyproject, src = Path(sys.argv[1]), Path(sys.argv[2])

FILE_ROOTED = re.compile(
    r"Path\(\s*__file__\s*\)((?:\s*\.\s*resolve\(\s*\)|\s*\.\s*parent\b|\s*\.\s*parents\s*\[\s*\d+\s*\])*)"
    r"(?:\s*\.\s*with_name\(\s*[\"']([^\"']+)[\"']\s*\)|((?:\s*/\s*[\"'][^\"']+[\"'])+))")
SEGMENT = re.compile(r"/\s*[\"']([^\"']+)[\"']")
UPWARDS = re.compile(r"\.\s*parents\s*\[\s*(\d+)\s*\]|\.\s*(parent)\b")
PKG = "geometry_agent/"


def data_reads(root):
    """{path inside the package: [modules that read it]} for every non-.py file resolved against __file__."""
    out = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if not rel.startswith(PKG):
            continue
        for m in FILE_ROOTED.finditer(path.read_text(encoding="utf-8")):
            base = rel
            for up in UPWARDS.finditer(m.group(1) or ""):
                # `Path(__file__).parent` is the module's directory; `parents[k]` is k+1 levels up from the
                # file itself, which is why these two count differently.
                for _ in range(int(up.group(1)) + 1 if up.group(1) else 1):
                    base = posixpath.dirname(base)
            if m.group(2):
                base, segments = posixpath.dirname(base), [m.group(2)]
            else:
                segments = SEGMENT.findall(m.group(3) or "")
            if not segments or segments[-1].endswith(".py") or "." not in segments[-1]:
                continue
            target = posixpath.normpath(posixpath.join(base, *segments))
            if target.startswith(PKG):
                out.setdefault(target[len(PKG):], []).append(rel)
    return out


reads = data_reads(src)
if not reads:
    sys.exit("FAIL: no data file read was found anywhere in the agent's source. Either the agent stopped "
             "reading its own tables or this derivation is broken; it must not silently declare nothing.")
absent = sorted(k for k in reads if not (src / PKG / k).exists())
if absent:
    sys.exit(f"FAIL: the agent's source reads {', '.join(absent)} and the checkout does not contain "
             f"{'them' if len(absent) > 1 else 'it'}, so no wheel can carry "
             f"{'them' if len(absent) > 1 else 'it'}.")
for name, who in sorted(reads.items()):
    print(f"  reads {name}  <- {', '.join(who)}", file=sys.stderr)

table = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("setuptools", {})
declared = table.get("package-data", {})
have = list(declared.get("geometry_agent", []))
uncovered = sorted(k for k in reads if not any(fnmatch.fnmatch(k, g) for g in have))
if declared and uncovered:
    # A table exists but is narrower than what the package reads. Appending would be a duplicate key, so the
    # build is refused rather than shipping a package that cannot read its own tables.
    sys.exit(f"FAIL: the agent's pyproject declares [tool.setuptools.package-data] without "
             f"{', '.join(uncovered)}, which {', '.join(sorted({m for k in uncovered for m in reads[k]}))} "
             f"reads. Add them there; this script cannot append a second table.")
owner, globs = ("agent_pyproject", sorted(have)) if declared else ("this_script", sorted(reads))
print(owner)
print(", ".join(f'"{g}"' for g in globs))
PYEOF
)" || die "could not derive the agent's package-data declaration"
DATA_OWNER="$(printf '%s\n' "${DATA_INFO}" | sed -n 1p)"
DATA_GLOBS="$(printf '%s\n' "${DATA_INFO}" | sed -n 2p)"
[ -n "${DATA_GLOBS}" ] || die "the package-data derivation produced no file list"
if [ "${DATA_OWNER}" = "this_script" ]; then
  cat >> "${BUILD}/pyproject.toml" <<EOF

# Added by the platform's deploy/vendor_geometry_agent.sh - see that script's header. Belongs upstream.
[tool.setuptools.package-data]
geometry_agent = [${DATA_GLOBS}]
EOF
fi
printf 'package-data declared by: %s\n' "${DATA_OWNER}"
printf 'package-data recorded:    %s\n' "${DATA_GLOBS}"

OUT="${ROOT}/vendor/wheels"
mkdir -p "${OUT}"
rm -f "${OUT}"/hexera_geometry_agent-*.whl
printf 'building hexera-geometry-agent %s from %s (%s)\n' "${VERSION}" "${AGENT}" "${SHA}"
"${PY}" -m pip wheel --no-deps --no-cache-dir --wheel-dir "${OUT}" "${BUILD}" >/dev/null \
  || die "the wheel build failed - rerun without the output redirect to see pip's own error"

WHEEL="$(ls "${OUT}"/hexera_geometry_agent-*.whl)"
[ -f "${WHEEL}" ] || die "pip reported success but no wheel landed in ${OUT}"

"${PY}" - "${WHEEL}" "${SHA}" "${SHORT}" "${BRANCH}" "${DIRTY}" "${VERSION}" "${DATA_GLOBS}" "${OUT}/PROVENANCE.json" "${DATA_OWNER}" <<'PYEOF'
import fnmatch, hashlib, json, sys, zipfile
from datetime import UTC, datetime
from pathlib import Path

wheel, sha, short, branch, dirty, version, data_globs, out, data_owner = sys.argv[1:10]
wheel = Path(wheel)
blob = wheel.read_bytes()
names = zipfile.ZipFile(wheel).namelist()
modules = sorted({n.split("/")[1] for n in names
                  if n.startswith("geometry_agent/") and len(n.split("/")) > 2} |
                 {n.split("/")[1].removesuffix(".py") for n in names
                  if n.startswith("geometry_agent/") and n.endswith(".py") and len(n.split("/")) == 2})
data = sorted(n for n in names if n.startswith("geometry_agent/") and not n.endswith(".py"))
package_data = [g.strip().strip('"') for g in data_globs.split(",") if g.strip()]
# THE LAST PLACE THIS CAN BE CAUGHT BEFORE THE WHEEL IS THE ONE THE IMAGE INSTALLS. Everything above reasons
# about the agent's source and its declaration; this reads the built zip. A file that is declared and does not
# land - a path setuptools resolved differently, a file outside the package directory, an exclude - would
# otherwise be recorded in `package_data` and absent from `data_files`, which is the shape the
# provenance was in on 2026-09-26 (three declared, four shipped) with nothing looking at it.
absent = sorted(g for g in package_data
                if not any(fnmatch.fnmatch(n, f"geometry_agent/{g}") for n in names))
if absent:
    sys.exit(f"FAIL: the wheel declares {', '.join(absent)} as package data and does not contain "
             f"{'them' if len(absent) > 1 else 'it'}. The installed package could not read its own table; "
             f"the wheel is left in place unrecorded so nothing installs it by accident.")
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
    "package_data": package_data,
    "package_data_declared_by": data_owner,
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
