# Responsibility: Prove the installed wheels resolve from site-packages with their package data, and that
#                 the one path where a missing runtime dependency hides actually RUNS.
# Boundaries: the checking code is stdlib-only, so it can report in an image where nothing else installed.
#             The last section deliberately is not: it imports the geometry agent and trimesh and measures a
#             two-region part, because importing a package proves less than running the thing it is imported
#             for, and that difference is the defect this file exists to catch.
from __future__ import annotations

import pathlib
import site
import sys

import meshpipeline

pkg = pathlib.Path(meshpipeline.__file__).resolve().parent

# 1. It resolves from an installed location. (Debian/Ubuntu's system python uses
#    dist-packages; a venv uses site-packages - accept either, reject a checkout.)
install_roots = [pathlib.Path(p).resolve() for p in (site.getsitepackages() + [site.getusersitepackages()])]
if not any(root in pkg.parents for root in install_roots):
    sys.exit(f"FAIL: meshpipeline resolves from {pkg}, which is not an install root "
             f"({[str(r) for r in install_roots]}) - the image is importing a source tree")

# 2. No checkout path is needed (or present) to import it.
if "/srv/src" in str(pkg) or "PYTHONPATH" in [k for k in ("PYTHONPATH",) if __import__("os").environ.get(k)]:
    sys.exit(f"FAIL: the image still leans on a checkout path (pkg={pkg}, "
             f"PYTHONPATH={__import__('os').environ.get('PYTHONPATH')!r})")

# 3. The package DATA came with the wheel - the app cannot load its prompts without it, and a
#    wheel that ships only .py fails at first use, not at build.
#    The prompt list is DERIVED from the installed settings/policy.py's REQUIRED_PROMPTS -
#    the app's own declaration of what it refuses to start without - parsed textually so this
#    stays stdlib-only and import-safe at build time. A hardcoded copy would fail the build on
#    files nothing needs, the first time a prompt is retired.
import re

policy_src = (pkg / "settings" / "policy.py").read_text(encoding="utf-8")
block = re.search(r"REQUIRED_PROMPTS[^=]*=\s*\[(.*?)\]", policy_src, re.S)
if not block:
    sys.exit("FAIL: could not locate REQUIRED_PROMPTS in the installed settings/policy.py - "
             "the wheel is broken or the declaration moved; update deploy/verify_install.py")
prompt_files = re.findall(r'\(\s*"[^"]+"\s*,\s*"([^"]+)"\s*\)', block.group(1))
if not prompt_files:
    sys.exit("FAIL: REQUIRED_PROMPTS parsed empty - the declaration changed shape; "
             "update deploy/verify_install.py")

# Prompts are the only PACKAGE DATA the wheel must carry. Review presentation is ordinary
# package CODE (render/review_palette.py), so it needs no package-data assertion here; if it
# ever fails to ship, importing it fails loudly on its own.
for rel in [f"prompts/{f}" for f in prompt_files]:
    if not (pkg / rel).is_file():
        sys.exit(f"FAIL: the installed wheel is missing package data: {rel}")

print(f"OK: meshpipeline installed at {pkg} (version {getattr(meshpipeline, '__version__', 'n/a')}), "
      f"package data present, no checkout path needed")


# 4. THE GEOMETRY AGENT - the Surveyor's measuring and looking half - is installed too.
#
#    This is the check the image never had. Nothing installed the package: not requirements/runtime.txt,
#    not pyproject.toml, not a Dockerfile line, not a deploy script. And nothing failed either, because
#    every entry point that imports it wraps the import in try/except and treats absence as an outcome to
#    record - so a deployment produced no document, no survey and no plan, said why in a log nobody read,
#    and handed the builder the byte-for-byte pre-Surveyor message. The tests that would have caught it
#    are pytest.importorskip, so they skipped themselves in exactly the image where the package was
#    missing.
#
#    So it is asserted HERE, in the build, where absence is a failed image rather than a quiet
#    degradation in production. The same three questions as above: does it resolve, does it resolve from
#    an install root rather than a checkout, and did its package DATA come with it.
import importlib.metadata as _md  # noqa: E402

try:
    import geometry_agent
except Exception as exc:  # noqa: BLE001 - any import failure is the same verdict: no Surveyor in this image
    sys.exit(f"FAIL: the geometry agent is not installed in this image ({type(exc).__name__}: {exc}).\n"
             f"      Without it every measurement, look, survey and plan records only what it could not\n"
             f"      do, and the builder gets the pre-Surveyor message. The Dockerfile's `wheel` stage\n"
             f"      copies vendor/wheels/hexera_geometry_agent-*.whl into /dist for this; build it with\n"
             f"      `bash deploy/vendor_geometry_agent.sh /path/to/geometry_agent` and commit it.")

gpkg = pathlib.Path(geometry_agent.__file__).resolve().parent
if not any(root in gpkg.parents for root in install_roots):
    sys.exit(f"FAIL: geometry_agent resolves from {gpkg}, which is not an install root "
             f"({[str(r) for r in install_roots]}) - the image is importing a source tree")

#: Every subpackage src/meshpipeline imports from the agent. A wheel missing one of these imports fine
#: and fails at the first real job, which is what the abandoned dist/hexera-geometry-agent.tar.gz would
#: have done: it carried agent, facts, render, testing and evaluate, and none of ask, chain, contract,
#: learn, reconcile or vision. Derived from the imports, not guessed - the same list is checked against
#: the source tree by tests/unit/deploy/test_geometry_agent_distribution.py, which is what keeps it true.
GEOMETRY_SUBPACKAGES = ("agent", "ask", "chain", "contract", "evaluate", "facts", "learn", "reconcile",
                        "vision")
# Either shape counts: `evaluate` is a single module (evaluate.py), the rest are packages.
missing = [name for name in GEOMETRY_SUBPACKAGES
           if not ((gpkg / name / "__init__.py").is_file() or (gpkg / f"{name}.py").is_file())]
if missing:
    sys.exit(f"FAIL: the installed geometry agent is missing subpackages this application imports: "
             f"{missing}. The vendored wheel is stale - rebuild it from an agent checkout that has them.")

#: The data files the package reads through Path(__file__), which a wheel that declares no package-data
#: leaves behind. `identity_tests.json` is read with .read_text(), so its absence is a FileNotFoundError
#: the first time a plan is built - at a customer's upload, not at build.
GEOMETRY_DATA = ("agent/thresholds.json", "agent/identity_tests.json", "learn/rules.json")
missing_data = [rel for rel in GEOMETRY_DATA if not (gpkg / rel).is_file()]
if missing_data:
    sys.exit(f"FAIL: the installed geometry agent is missing package data: {missing_data}. The wheel was "
             f"built without the package-data declaration deploy/vendor_geometry_agent.sh adds.")

try:
    gversion = _md.version("hexera-geometry-agent")
except Exception:  # noqa: BLE001 - an unnamed version is a weaker report, not a failed image
    gversion = "n/a"
print(f"OK: geometry_agent installed at {gpkg} (version {gversion}), "
      f"{len(GEOMETRY_SUBPACKAGES)} subpackages and {len(GEOMETRY_DATA)} data files present")


# 5. THE PATH A MULTI-REGION PART TAKES, RUN. Everything above asks whether a file is there. That is
#    strictly weaker than asking whether the code works, and the gap between the two is where this
#    image's last real defect lived: the geometry agent imports fine with no `rtree` and no
#    `embreex` in the environment and then measures NOTHING. Measured in the built `api` image on
#    the real parts in tests/fixtures/geometry: with neither library, one region and two regions
#    both come back `measurement_failed` with `No module named 'rtree'` in the reason, because
#    `facts.features.detect_features` casts rays on every part and `facts.regions.detect_regions`
#    casts more on a part with two or more. At a customer's upload, silently: the platform never
#    fails an upload over a measurement.
#
#    A MULTI-REGION PART IS WHAT THIS RUNS, because it is the stricter of the two: it exercises
#    both ray paths, and it is the one whose absence a one-region check would miss if the reach in
#    `facts.chords` ever goes away.
#
#    WHY NEITHER REQUIREMENTS FILE COULD HAVE TOLD US. `rtree` is not a Requires-Dist of the
#    geometry-agent wheel and the agent never writes `import rtree`. trimesh writes it, on the
#    agent's behalf, at the bottom of the nesting test:
#      detect_regions -> _encloses -> container.ray.intersects_any
#        -> trimesh.ray.ray_triangle.intersects_id -> mesh.triangles_tree
#        -> trimesh.triangles.bounds_tree -> trimesh.util.bounds_tree -> `import rtree`
#    So the only check that can see it is one that RUNS the call. This is that check.
#
#    IT RUNS ON BOTH RAY BACKENDS, deliberately. With `embreex` installed trimesh picks the embree
#    intersector and never reaches for an r-tree at all, so a run on the default backend alone would
#    pass in an image with no rtree and leave the fallback to fail at a customer. The second run
#    hides embreex from the import system for the length of the call, which is the environment
#    trimesh falls back to whenever embree cannot load, and measures that path too.
import importlib as _importlib  # noqa: E402

_GEOMETRY_RAY_FALLBACK = "embreex"


class _Hide:
    """Refuse one top-level module, so a fallback path can be measured rather than assumed."""

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == self.name:
            raise ImportError(f"hidden by deploy/verify_install.py: {name}")
        return None


def _two_region_part(trimesh):
    """A box with a smaller box inside it: two surface regions, one nested in the other. Built here
    rather than shipped, because a fixture file is a thing that can go missing from a wheel."""
    return trimesh.util.concatenate([trimesh.creation.box(extents=(0.2, 0.2, 0.2)),
                                     trimesh.creation.box(extents=(0.05, 0.05, 0.05))])


def _measure_the_nesting(trimesh, detect_regions) -> str:
    """Run the nesting test on a two-region part and return the backend it went through. Raises."""
    part = _two_region_part(trimesh)
    backend = type(part.ray).__module__
    regions = detect_regions(part)
    if len(regions) != 2:
        raise AssertionError(f"the measurement found {len(regions)} regions in a part built with two")
    inner = [r for r in regions if r.nested_in]
    outer = [r for r in regions if not r.nested_in]
    if len(inner) != 1 or len(outer) != 1 or inner[0].nested_in != outer[0].id:
        raise AssertionError(f"the nesting test placed {[(r.id, r.nested_in) for r in regions]}, not one "
                             f"region inside the other")
    return backend


try:
    import trimesh as _trimesh
    from geometry_agent.facts.regions import detect_regions as _detect_regions
except Exception as exc:  # noqa: BLE001
    sys.exit(f"FAIL: the geometry agent's measurement cannot even be imported in this image "
             f"({type(exc).__name__}: {exc}). requirements/runtime.txt has to carry what the agent's "
             f"wheel imports, and the wheel is installed with --no-deps.")

try:
    _default_backend = _measure_the_nesting(_trimesh, _detect_regions)
except Exception as exc:  # noqa: BLE001
    sys.exit(f"FAIL: measuring a part with two regions does not work in this image "
             f"({type(exc).__name__}: {exc}).\n"
             f"      This is the nesting test in geometry_agent.facts.regions.detect_regions, which every\n"
             f"      multi-region upload runs: a CHT assembly, a body inside a body, a fluid volume with a\n"
             f"      sealed bubble. A single-region part measures fine without it, so the absence shows up\n"
             f"      at a customer's upload unless it shows up here. If this is an ImportError, pin what it\n"
             f"      names in requirements/runtime.txt: trimesh reaches for rtree and embreex on the\n"
             f"      agent's behalf and neither is declared by the agent's wheel.")

sys.meta_path.insert(0, _Hide(_GEOMETRY_RAY_FALLBACK))
try:
    for _name in [m for m in list(sys.modules) if m.split(".")[0] in (_GEOMETRY_RAY_FALLBACK, "trimesh")]:
        del sys.modules[_name]
    _fallback = _importlib.import_module("trimesh")
    _fallback_backend = _measure_the_nesting(_fallback, _detect_regions)
except Exception as exc:  # noqa: BLE001
    sys.exit(f"FAIL: with {_GEOMETRY_RAY_FALLBACK} hidden, measuring a part with two regions does not work "
             f"({type(exc).__name__}: {exc}).\n"
             f"      That is the backend trimesh falls back to whenever embree cannot load in this image,\n"
             f"      and a fallback that raises is not a fallback. It needs rtree; pin it in\n"
             f"      requirements/runtime.txt.")
finally:
    sys.meta_path.pop(0)

print(f"OK: a two-region part measures and its nesting is found, on the backend this image will use "
      f"({_default_backend}) and on the one it falls back to ({_fallback_backend})")
