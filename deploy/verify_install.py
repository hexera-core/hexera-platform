# Responsibility: Prove the installed wheels resolve from site-packages with their package data.
# Boundaries: stdlib-only and import-safe: it runs inside the image build before anything else is trusted.
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
